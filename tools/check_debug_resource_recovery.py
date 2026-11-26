"""Disposable recovery contracts; never operates devices or the real database."""
import os
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def reject(fn, reason):
    try:
        fn()
    except ValueError as exc:
        assert str(exc) == reason, str(exc)
    else:
        raise AssertionError(reason)


def main():
    with tempfile.TemporaryDirectory(prefix='mino-debug-recovery-') as root:
        os.environ['MINO_NEXUS_DATA_DIR'] = root
        os.environ['MINO_NEXUS_DEV_DATA_DIR'] = root
        import mino_nexus.startup
        from mino_nexus.core.database import DB_PATH, ensure_db, session_scope
        assert DB_PATH.resolve().parent == Path(root).resolve()
        ensure_db()
        from mino_nexus.platform.identity import execution_user
        from mino_nexus.resources import scout_record_store as records
        from mino_nexus.loop import scout_stop_evidence as evidence
        from mino_nexus.loop.scout_resource_resolution import reconcile_task_resources
        from mino_nexus.models.run import AppRegressionRun
        from mino_nexus.models.scout_record import ScoutRecord
        from mino_nexus.models.execution_resource import ExecutionResource
        from mino_nexus.models.resource_ops import DeviceResourceLease
        from mino_nexus.models.pool_account import PoolAccount
        from mino_nexus.models.device_reservation import DeviceReservation
        from mino_nexus.records.write_state import protected
        identity = dict(task_id='task', run_id='task', session_id='attempt', attempt_id='attempt',
                        owner_user_id='owner', node_id='node', sn='web-node', case_id='case', platform='web', script_path='case.py')
        summary = {**identity, 'status': 'blocked', 'state': 'interrupted', 'sealed': True,
                   'summary_version': 2, 'execution_started': 'unknown', 'execution_stopped': False,
                   'browser_closed': False, 'resource_isolated': True, 'scout_process_generation': 'old',
                   'reason_code': 'scout_restarted', 'last_event_seq': 2}
        page = {'sealed': True, 'high_watermark': 2, 'next_seq': 2, 'has_more': False,
                'items': [{'seq': 1, 'type': 'tool/call', 'payload': {'effect_class': 'write', 'action_seq': 1}},
                          {'seq': 2, 'type': 'tool/result', 'payload': {'action_seq': 1, 'status': 'pass', 'data': {}}}]}
        proof = {**summary, 'resource_stop': {'contract': 'script_resource_stop.v1', 'stop_confirmed': True,
                 'worker_active': False, 'browser_closed': True, 'mode': 'retired_generation',
                 'scout_process_generation': 'new', 'process_started_at': 100,
                 'reason_code': 'web_lane_stopped'}}
        with execution_user('owner'):
            with session_scope() as db:
                db.add(AppRegressionRun(run_id='task', owner_user_id='owner', status='done', finished_at='2026-11-26T00:00:00Z',
                    payload={'run_id': 'task', 'task_id': 'task', 'owner_user_id': 'owner', 'status': 'done',
                             'finished_at': '2026-11-26T00:00:00Z', 'project_id': 'project', 'reason_code': 'scout_restarted',
                             'cases': [{'case_id': 'case', 'status': 'blocked', 'reason_code': 'scout_restarted'}]}))
                db.add(ScoutRecord(**{k: identity[k] for k in ('task_id','session_id','attempt_id','owner_user_id','node_id','sn','case_id')},
                    status='blocked', summary_version=2, cancel_requested=False, payload={**identity, 'execution_kind': 'debug'},
                    summary={**summary, 'sealed': False, 'summary_version': 2},
                    context={'project_id': 'project', 'resource_request': {'attempt_id': 'attempt', 'resource_epochs': {'device:web-node:task': 1}}}, updated_at='2026-11-26T00:00:00Z'))
                db.add(ExecutionResource(resource_key='device:web-node:task', project_id='project', owner_user_id='owner',
                    run_id='task', execution_unit_id='unit', attempt_id='attempt', sn='web-node', epoch=1, expires_at=0, state='isolated'))
                db.add(PoolAccount(id='account', project_id='project', lease={'run_id': 'attempt'}))
                db.add(DeviceReservation(resource_key='reserved', run_id='task'))
                db.add(DeviceResourceLease(sn='other-node', package_id='https://example.invalid', run_id='other-task', project_id='project'))
                db.add(DeviceResourceLease(sn='web-node', package_id='https://example.invalid', run_id='task', project_id='project'))
            records.request_cancel('task')
            row = records.get('attempt')
            assert row['cancel_requested'] and records.web_stop_recovery_candidate(row)
            assert protected('task')
            def request(row, capability, params=None, **kwargs):
                return deepcopy({'record.status': summary, 'record.resource_status': proof, 'record.events': page}[capability])
            with patch.object(evidence, '_request', side_effect=request):
                from mino_nexus.loop import debug_cases
                node = SimpleNamespace(node_id='node', owner_user_id='owner', alive=True, send=True)
                with patch.object(debug_cases, 'get_registry', return_value=SimpleNamespace(get_node=lambda _: node)), \
                     patch.object(debug_cases, '_owned_by_node_account', return_value=True), \
                     patch.object(debug_cases, '_scope', return_value=identity), \
                     patch.object(debug_cases, '_call', return_value=summary) as control, \
                     patch.object(debug_cases, '_close_state', side_effect=[{'state': 'stopping'}, {'state': 'closed'}]):
                    assert debug_cases._close(row)['state'] == 'closed'
                    assert control.call_args.args[1] == 'record.status'  # terminal-unsealed must not return early
                current, verified = evidence.fetch_stop_evidence(row)
                assert current['summary']['sealed']
                assert not records.web_stop_proof_valid(current, proof)
                assert records.web_stop_proof_valid(current, verified)
                for key in ['owner_user_id', 'attempt_id', 'sn']:
                    wrong = deepcopy(verified); wrong[key] = 'foreign'
                    assert not records.web_stop_proof_valid(current, wrong)
                wrong = deepcopy(verified); wrong['summary_version'] = 1
                assert not records.web_stop_proof_valid(current, wrong)
                wrong = deepcopy(verified); wrong['resource_stop']['worker_active'] = True
                assert not records.web_stop_proof_valid(current, wrong)
                bad = deepcopy(page); bad['items'] = bad['items'][:1]; bad['next_seq'] = 1; bad['has_more'] = True
                with patch.object(evidence, '_request', return_value=bad):
                    reject(lambda: evidence.audit_writes(current), 'recovery_write_evidence_incomplete')
                bad = deepcopy(page); bad['items'][1]['payload']['data'] = {'execution': {'execution_started': 'unknown'}}
                with patch.object(evidence, '_request', return_value=bad):
                    reject(lambda: evidence.audit_writes(current), 'recovery_write_outcome_unknown')
                bad = deepcopy(page); bad['items'][1]['payload']['data'] = {'reason_code': 'write_outcome_unknown'}
                with patch.object(evidence, '_request', return_value=bad):
                    reject(lambda: evidence.audit_writes(current), 'recovery_write_outcome_unknown')
                saved = records.save_resource_stop('attempt', verified)
                assert records.web_stop_recovery_candidate(saved)  # lost commit is recoverable
                result = reconcile_task_resources('task')
                assert not protected('task')
                with session_scope() as db:
                    doc = db.get(AppRegressionRun, "task").payload
                    assert doc["reason_code"] == "scout_restarted" and doc["cases"][0]["status"] == "blocked"
                    assert db.get(PoolAccount, "account").lease == {}
                    assert db.query(DeviceReservation).count() == 0
                    assert db.query(DeviceResourceLease).count() == 1
                    assert db.query(DeviceResourceLease).first().run_id == "other-task"
                    resource = db.get(ExecutionResource, 'device:web-node:task')
                    assert resource.state == 'released' and resource.epoch == 2
                assert records.stop_evidence_confirmed(records.get('attempt'))
                assert reconcile_task_resources('task') == result
            # A late unknown receipt must not be hidden by an earlier pass for the same write.
            late_row = deepcopy(current); late_row['summary']['last_event_seq'] = 3
            late_page = deepcopy(page); late_page.update(high_watermark=3, next_seq=3)
            late_page['items'].append({'seq': 3, 'type': 'tool/result', 'payload': {
                'action_seq': 1, 'status': 'blocked', 'data': {'execution': {'write_outcome': 'unknown'}}}})
            with patch.object(evidence, '_request', return_value=late_page):
                reject(lambda: evidence.audit_writes(late_row), 'recovery_write_outcome_unknown')
                assert evidence.audit_writes(late_row, review_unknown=True)['unknown_write_actions'] == 1
            # Byte-limited pages and local waits reproduce the original Scout ledger.
            wait_payload = {'capability_id': 'wait', 'executor_used': 'core', 'status': 'pass',
                            'data': {}, 'elapsed_ms': 600, 'error': '', 'reason_code': '', 'action_seq': 93}
            wait_row = deepcopy(current); wait_row['summary']['last_event_seq'] = 3
            wait_page = deepcopy(page); wait_page.update(high_watermark=3, next_seq=3)
            wait_page['items'].append({'seq': 3, 'type': 'tool/result', 'payload': wait_payload})
            def paged_request(row, capability, params=None, **kwargs):
                assert params['max_bytes'] == 524288
                cursor = params['after_seq']
                items = wait_page['items'][cursor:cursor + 1]
                return {**wait_page, 'items': deepcopy(items), 'next_seq': cursor + 1, 'has_more': cursor + 1 < 3}
            with patch.object(evidence, '_request', side_effect=paged_request):
                assert evidence.audit_writes(wait_row)['open_write_actions'] == 0
            for field, value in [('capability_id', 'tap'), ('executor_used', 'web'), ('data', {'resource_isolated': True}),
                                 ('status', 'blocked'), ('elapsed_ms', True), ('error', 'unknown')]:
                invalid = deepcopy(wait_page); invalid['items'][2]['payload'][field] = value
                with patch.object(evidence, '_request', return_value=invalid):
                    reject(lambda: evidence.audit_writes(wait_row, review_unknown=True), 'recovery_write_evidence_required')
            changed_wait_row = deepcopy(wait_row); changed_wait_row['summary']['last_event_seq'] = 4
            changed_wait = deepcopy(wait_page); changed_wait.update(high_watermark=4, next_seq=4)
            changed_wait['items'].append({'seq': 4, 'type': 'tool/result', 'payload': {**wait_payload, 'capability_id': 'tap'}})
            with patch.object(evidence, '_request', return_value=changed_wait):
                reject(lambda: evidence.audit_writes(changed_wait_row, review_unknown=True), 'recovery_write_evidence_required')
            for index in (0, 1):
                invalid = deepcopy(page); invalid['items'][index]['payload']['detail_in_artifact'] = True
                with patch.object(evidence, '_request', return_value=invalid):
                    reject(lambda: evidence.audit_writes(current, review_unknown=True), 'recovery_write_evidence_required')
            malformed = deepcopy(page); malformed['items'][1]['payload']['action_seq'] = []
            with patch.object(evidence, '_request', return_value=malformed):
                reject(lambda: evidence.audit_writes(current, review_unknown=True), 'recovery_write_evidence_required')
            malformed = deepcopy(page); malformed['items'][0]['seq'] = True
            with patch.object(evidence, '_request', return_value=malformed):
                reject(lambda: evidence.audit_writes(current), 'recovery_write_evidence_incomplete')
            wrong = deepcopy(verified); wrong['resource_stop']['process_started_at'] = float('nan')
            assert not records.web_stop_proof_valid(current, wrong)
            wrong = deepcopy(verified); wrong['resource_stop']['mode'] = []
            assert not records.web_stop_proof_valid(current, wrong)

            review_identity = {**identity, 'task_id': 'review-task', 'run_id': 'review-task',
                               'session_id': 'review-attempt', 'attempt_id': 'review-attempt'}
            review_summary = {**summary, **review_identity}
            review_proof = {**proof, **review_identity}
            review_page = deepcopy(page)
            review_page['items'][1]['payload'].update(status='blocked', reason_code='write_outcome_unknown')
            with session_scope() as db:
                db.add(AppRegressionRun(run_id='review-task', owner_user_id='owner', status='cancelled',
                    finished_at='2026-11-26T00:00:00Z', payload={'run_id': 'review-task', 'task_id': 'review-task',
                    'owner_user_id': 'owner', 'status': 'cancelled', 'finished_at': '2026-11-26T00:00:00Z',
                    'project_id': 'project', 'reason_code': 'write_outcome_unknown', 'write_safety': {
                    'scout:review-attempt': {'state': 'isolated', 'attempt_id': 'review-attempt',
                    'sn': 'web-node', 'stop_confirmed': False, 'reason_code': 'write_outcome_unknown'}},
                    'cases': [{'case_id': 'case', 'status': 'blocked', 'reason_code': 'write_outcome_unknown'}]}))
                db.add(ScoutRecord(**{k: review_identity[k] for k in ('task_id','session_id','attempt_id','owner_user_id','node_id','sn','case_id')},
                    status='blocked', summary_version=2, cancel_requested=True, payload={**review_identity, 'execution_kind': 'debug'},
                    summary=review_summary, context={'project_id': 'project', 'resource_request': {
                    'attempt_id': 'review-attempt', 'resource_epochs': {'device:web-node:review-task': 1}}}, updated_at='2026-11-26T00:00:00Z'))
                db.add(ExecutionResource(resource_key='device:web-node:review-task', project_id='project', owner_user_id='owner',
                    run_id='review-task', execution_unit_id='unit', attempt_id='review-attempt', sn='web-node', epoch=1, expires_at=0, state='isolated'))
                db.add(PoolAccount(id='review-account', project_id='project', lease={'run_id': 'review-attempt'}))
                db.add(DeviceResourceLease(sn='web-node', package_id='https://example.invalid', run_id='review-task', project_id='project'))
            def review_request(row, capability, params=None, **kwargs):
                # The synchronous Scout proxy must run off the HTTP event-loop thread.
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    pass
                else:
                    raise AssertionError('synchronous Scout proxy called on an active event loop')
                from mino_nexus.platform.identity import require_user_id
                assert require_user_id() == 'owner'
                return deepcopy({'record.status': review_summary, 'record.resource_status': review_proof, 'record.events': review_page}[capability])
            from mino_nexus.casework.run_write_safety import confirm_baseline, recover_stopped
            from mino_nexus.casework.run_safety_status import status as safety_status
            import asyncio
            with patch.object(evidence, '_request', side_effect=review_request):
                assert safety_status('review-task')['baseline_review_available']
                assert protected('review-task')
                reject(lambda: reconcile_task_resources('review-task'), 'recovery_write_outcome_unknown')
                reject(lambda: asyncio.run(recover_stopped('review-task')), 'recovery_write_outcome_unknown')
                row, reviewed_proof = evidence.fetch_stop_evidence(records.get('review-attempt'), review_unknown=True)
                assert reviewed_proof['retirement_write_audit']['unknown_write_actions'] == 1
                assert records.web_stop_proof_valid(row, reviewed_proof)
                wrong = deepcopy(reviewed_proof); wrong['retirement_write_audit']['reviewed_by'] = 'other'
                assert not records.web_stop_proof_valid(row, wrong)
                wrong = deepcopy(reviewed_proof); wrong['resource_stop']['mode'] = 'same_process'
                assert not records.web_stop_proof_valid(row, wrong)
                review_proof['resource_stop']['worker_active'] = True
                reject(lambda: asyncio.run(confirm_baseline('review-task')), 'stop_evidence_required')
                review_proof['resource_stop']['worker_active'] = False
                with session_scope() as db:
                    db.get(ExecutionResource, 'device:web-node:review-task').epoch = 2
                reject(lambda: asyncio.run(confirm_baseline('review-task')), 'resource_resolution_conflict')
                with session_scope() as db:
                    assert db.get(PoolAccount, 'review-account').lease == {'run_id': 'review-attempt'}
                    assert db.get(ExecutionResource, 'device:web-node:review-task').state == 'isolated'
                    db.get(ExecutionResource, 'device:web-node:review-task').epoch = 1
                from mino_nexus.contract.ownership import bind_user, reset_user
                token = bind_user('other')
                try:
                    reject(lambda: asyncio.run(confirm_baseline('review-task')), 'task_not_found')
                finally:
                    reset_user(token)
                result = asyncio.run(confirm_baseline('review-task'))
                assert result['isolation_exit']['committed'] and not result['isolation_exit']['resource_reset_verified']
                assert result['resource_resolution']['verification_kind'] == 'retired_web_lane_review'
                assert result['resource_resolution']['automatic_replay_allowed'] is False
                assert not protected('review-task')
                assert asyncio.run(confirm_baseline('review-task')) == result
                with session_scope() as db:
                    run = db.get(AppRegressionRun, 'review-task')
                    assert run.status == 'cancelled' and run.payload['reason_code'] == 'write_outcome_unknown'
                    assert run.payload['cases'][0]['status'] == 'blocked'
                    assert db.get(PoolAccount, 'review-account').lease == {}
                    resource = db.get(ExecutionResource, 'device:web-node:review-task')
                    assert resource.state == 'released' and resource.epoch == 2
                    assert db.query(DeviceResourceLease).count() == 1
                    assert db.query(DeviceResourceLease).first().run_id == 'other-task'
            from mino_nexus.casework.isolation_exit import commit_baseline_release
            physical_doc = {'run_id': 'physical-task', 'task_id': 'physical-task', 'owner_user_id': 'owner',
                            'status': 'cancelled', 'platform': 'android', 'write_safety': {
                            'action': {'state': 'isolated', 'sn': 'phone', 'action_ref': {'run_id': 'physical-task', 'step_idx': 1}}}}
            with session_scope() as db:
                db.add(AppRegressionRun(run_id='physical-task', owner_user_id='owner', status='cancelled',
                    finished_at='2026-11-26T00:00:00Z', payload=physical_doc))
            reject(lambda: asyncio.run(confirm_baseline('physical-task')), 'stop_evidence_required')
            outcome = commit_baseline_release('physical-task', actor='owner', sns={'phone'}, observations=[],
                                               expected_actions=physical_doc['write_safety'])
            assert not outcome['committed'] and outcome['blocked_reason'] == 'stop_evidence_required'
            outcome = commit_baseline_release('physical-task', actor='owner', sns={'phone'}, observations=[], expected_actions={})
            assert not outcome['committed'] and outcome['blocked_reason'] == 'resource_resolution_conflict'
            print('PASS: explicit retired-generation review preserves unknown outcomes; zero-action isolation; live-worker rejection; epoch conflict rollback; cross-owner rejection; idempotent baseline; late unknown receipt; malformed evidence')
            changed = {**summary, 'summary_version': 3, 'execution_started': 'yes'}
            reject(lambda: records.receive('node', changed), 'record_terminal_conflict')
        print('PASS: refresh unsealed legacy terminal; exact retired-generation proof; sealed write audit; unknown writes stay fenced; atomic release; immutable original verdict; idempotent recovery')
        print('Offline contracts only; no device or production-task recovery claim.')


if __name__ == '__main__':
    main()
