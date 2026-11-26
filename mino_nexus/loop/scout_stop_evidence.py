"""Refresh canonical stop facts and audit sealed writes without replaying actions."""
import logging
from time import monotonic

from mino_nexus.resources import scout_record_store as records
from mino_nexus.loop.scout_record_proxy import _request

_LOG = logging.getLogger(__name__)


def fetch_stop_evidence(row, *, review_unknown=False):
    current = records.receive(row['node_id'], _request(row, 'record.status', timeout=5))
    proof = _request(current, 'record.resource_status', timeout=5)
    if not isinstance(proof, dict):
        raise ValueError('stop_evidence_required')
    if review_unknown or records.execution_outcome_unknown(current) or records.resource_isolated(current) or current['summary'].get('execution_stopped') is not True or current['summary'].get('browser_closed') is not True:
        proof['retirement_write_audit'] = audit_writes(current, review_unknown=review_unknown)
    return current, proof


def audit_writes(row, *, review_unknown=False):
    summary = row.get('summary') or {}
    watermark = summary.get('last_event_seq')
    if summary.get('sealed') is not True or type(watermark) is not int or watermark < 1:
        raise ValueError('recovery_write_evidence_required')
    cursor, active, seen, writes, unknown, local_waits, deadline = 0, set(), set(), set(), set(), set(), monotonic() + 20
    while cursor < watermark:
        if monotonic() >= deadline:
            raise ValueError('recovery_write_evidence_timeout')
        page = _request(row, 'record.events', {'after_seq': cursor, 'limit': 200, 'max_bytes': 524288}, timeout=5)
        if not isinstance(page, dict) or page.get('sealed') is not True or type(page.get('high_watermark')) is not int or page['high_watermark'] != watermark:
            raise ValueError('recovery_write_evidence_changed')
        items = page.get('items')
        if not isinstance(items, list) or not items or len(items) > 200:
            raise ValueError('recovery_write_evidence_incomplete')
        for event in items:
            if not isinstance(event, dict) or type(event.get('seq')) is not int or event['seq'] != cursor + 1 or event['seq'] > watermark:
                raise ValueError('recovery_write_evidence_incomplete')
            if not isinstance(event.get('type'), str) or not event['type']:
                _invalid_event(row, event, 'invalid_event_type')
            cursor += 1
            data = event.get('payload')
            if not isinstance(data, dict):
                raise ValueError('recovery_write_evidence_required')
            if event.get('type') in ('tool/call', 'tool/result') and data.get('detail_in_artifact'):
                _invalid_event(row, event, 'artifact_payload_required')
            if event.get('type') == 'tool/call':
                if data.get('effect_class') not in ('read', 'write') or type(data.get('action_seq')) is not int or data['action_seq'] < 1:
                    raise ValueError('recovery_write_evidence_required')
                if data['action_seq'] in seen:
                    raise ValueError('recovery_write_evidence_required')
                seen.add(data['action_seq'])
                if data['effect_class'] == 'write':
                    active.add(data['action_seq'])
                    writes.add(data['action_seq'])
            elif event.get('type') == 'tool/result':
                capability, seq = data.get('capability_id'), data.get('action_seq')
                # Auxiliary results (local wait, OTP box, page observation) borrow the
                # previous device action sequence. They settle nothing about that action.
                if capability in _AUXILIARY_CAPABILITIES:
                    if type(seq) is not int or seq < 1 or not _auxiliary_receipt(data):
                        _invalid_event(row, event, 'invalid_auxiliary_result')
                    if seq not in seen:
                        seen.add(seq)
                        local_waits.add(seq)
                    continue
                # Scout's checkout receipt is the only result with no device action behind it.
                if capability == 'checkout':
                    if not _scout_checkout_receipt(data):
                        _invalid_event(row, event, 'invalid_internal_result')
                    continue
                if type(seq) is not int or seq < 1:
                    _invalid_event(row, event, 'invalid_action_seq')
                if data['action_seq'] not in seen:
                    # Scout's local wait reports a result directly without a device call.
                    if not _local_wait_receipt(data):
                        _invalid_event(row, event, 'result_without_call')
                    seen.add(data['action_seq'])
                    local_waits.add(data['action_seq'])
                if data['action_seq'] in local_waits and not _local_wait_receipt(data):
                    _invalid_event(row, event, 'local_wait_result_changed')
                if data['action_seq'] not in writes:
                    continue
                if data.get('status') not in ('pass', 'fail', 'blocked', 'declined', 'skipped', 'cancelled'):
                    raise ValueError('recovery_write_evidence_required')
                if _unknown_write(data):
                    if not review_unknown:
                        raise ValueError('recovery_write_outcome_unknown')
                    unknown.add(data['action_seq'])
                active.discard(data['action_seq'])
        if type(page.get('next_seq')) is not int or page['next_seq'] != cursor or type(page.get('has_more')) is not bool or page['has_more'] != (cursor < watermark):
            raise ValueError('recovery_write_evidence_incomplete')
    if monotonic() >= deadline:
        raise ValueError('recovery_write_evidence_timeout')
    if active and not review_unknown:
        raise ValueError('recovery_write_outcome_unknown')
    audit = {'contract': 'sealed_write_audit.v1', 'summary_version': row['summary_version'],
             'last_event_seq': watermark, 'open_write_actions': len(active)}
    if review_unknown:
        from mino_nexus.platform.identity import require_user_id
        audit.update(contract='retired_write_review.v1', reviewed_by=require_user_id(),
                     historical_outcome_preserved=True, unknown_write_actions=len(unknown | active))
    return audit


def _unknown_write(value, depth=0):
    if depth > 32:
        raise ValueError('recovery_write_evidence_required')
    if isinstance(value, dict):
        if (value.get('execution_started') == 'unknown' or value.get('write_outcome') == 'unknown'
                or value.get('resource_isolated') is True
                or value.get('reason_code') in ('write_outcome_unknown', 'stop_unconfirmed', 'worker_stop_unconfirmed')):
            return True
        return any(_unknown_write(item, depth + 1) for item in value.values())
    if isinstance(value, list):
        return any(_unknown_write(item, depth + 1) for item in value)
    return False


_AUXILIARY_CAPABILITIES = ('wait', 'otp', 'control_observe')


def _auxiliary_receipt(data):
    """Read-only receipt that rides on the previous action sequence and proves no write."""
    if _local_wait_receipt(data):
        return True
    capability, executor, body = data.get('capability_id'), data.get('executor_used'), data.get('data')
    if (data.get('status') != 'pass' or data.get('error') != ''
            or data.get('reason_code') != '' or type(data.get('elapsed_ms')) is not int
            or data['elapsed_ms'] < 0 or not isinstance(body, dict)
            or _unknown_write(data) or data.get('effect_class') == 'write'
            or 'execution_control' in body or 'execution' in body):
        return False
    return (capability == 'otp' and executor == 'core') or (capability == 'control_observe' and executor == 'playwright')


def _local_wait_receipt(data):
    return (data.get('capability_id') == 'wait' and data.get('executor_used') == 'core'
            and data.get('status') == 'pass' and data.get('data') == {}
            and data.get('error') == '' and data.get('reason_code') == ''
            and type(data.get('elapsed_ms')) is int and data['elapsed_ms'] >= 0
            and not _unknown_write(data))


def _scout_checkout_receipt(data):
    return (type(data.get('action_seq')) is int and data['action_seq'] == 0
            and data.get('capability_id') == 'checkout'
            and data.get('executor_used') == 'scout'
            and data.get('status') == 'pass' and data.get('data') == {}
            and data.get('error') == '' and data.get('reason_code') == ''
            and type(data.get('elapsed_ms')) is int and data['elapsed_ms'] == 0
            and isinstance(data.get('source_hash'), str) and bool(data['source_hash'])
            and not _unknown_write(data))


def _invalid_event(row, event, detail):
    _LOG.warning('Recovery audit rejected attempt=%s seq=%s reason=%s',
                 row.get('attempt_id'), event.get('seq'), detail)
    raise ValueError('recovery_write_evidence_required')
