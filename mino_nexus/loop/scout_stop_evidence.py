"""Refresh canonical stop facts and audit sealed writes without replaying actions."""
from time import monotonic

from mino_nexus.resources import scout_record_store as records
from mino_nexus.loop.scout_record_proxy import _request


def fetch_stop_evidence(row):
    current = records.receive(row['node_id'], _request(row, 'record.status', timeout=5))
    proof = _request(current, 'record.resource_status', timeout=5)
    if records.execution_outcome_unknown(current) or records.resource_isolated(current):
        proof['retirement_write_audit'] = audit_writes(current)
    return current, proof


def audit_writes(row):
    summary = row.get('summary') or {}
    watermark = summary.get('last_event_seq')
    if summary.get('sealed') is not True or type(watermark) is not int or watermark < 1:
        raise ValueError('recovery_write_evidence_required')
    cursor, active, seen, deadline = 0, set(), set(), monotonic() + 20
    while cursor < watermark:
        if monotonic() >= deadline:
            raise ValueError('recovery_write_evidence_timeout')
        page = _request(row, 'record.events', {'after_seq': cursor, 'limit': 200}, timeout=5)
        if page.get('sealed') is not True or page.get('high_watermark') != watermark:
            raise ValueError('recovery_write_evidence_changed')
        items = page.get('items')
        if not isinstance(items, list) or not items:
            raise ValueError('recovery_write_evidence_incomplete')
        for event in items:
            if not isinstance(event, dict) or type(event.get('seq')) is not int or event['seq'] != cursor + 1 or event['seq'] > watermark:
                raise ValueError('recovery_write_evidence_incomplete')
            cursor += 1
            data = event.get('payload') or {}
            if not isinstance(data, dict):
                raise ValueError('recovery_write_evidence_required')
            if event.get('type') == 'tool/call':
                if data.get('effect_class') not in {'read', 'write'} or type(data.get('action_seq')) is not int:
                    raise ValueError('recovery_write_evidence_required')
                if data['action_seq'] in seen:
                    raise ValueError('recovery_write_evidence_required')
                seen.add(data['action_seq'])
                if data['effect_class'] == 'write':
                    active.add(data['action_seq'])
            elif event.get('type') == 'tool/result' and data.get('action_seq') in active:
                if data.get('status') not in {'pass', 'fail', 'blocked', 'declined', 'skipped', 'cancelled'}:
                    raise ValueError('recovery_write_evidence_required')
                if _unknown_write(data):
                    raise ValueError('recovery_write_outcome_unknown')
                active.remove(data['action_seq'])
        if page.get('next_seq') != cursor or page.get('has_more') != (cursor < watermark):
            raise ValueError('recovery_write_evidence_incomplete')
    if active:
        raise ValueError('recovery_write_outcome_unknown')
    return {'contract': 'sealed_write_audit.v1', 'summary_version': row['summary_version'],
            'last_event_seq': watermark, 'open_write_actions': 0}


def _unknown_write(value):
    if isinstance(value, dict):
        if (value.get('execution_started') == 'unknown' or value.get('write_outcome') == 'unknown'
                or value.get('resource_isolated') is True
                or value.get('reason_code') in {'write_outcome_unknown', 'stop_unconfirmed', 'worker_stop_unconfirmed'}):
            return True
        return any(_unknown_write(item) for item in value.values())
    if isinstance(value, list):
        return any(_unknown_write(item) for item in value)
    return False
