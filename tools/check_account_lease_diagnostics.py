#!/usr/bin/env python3
"""Disposable SQLite lease and debug projection smoke; no services or target UI."""
import os
import sys
import tempfile
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    with tempfile.TemporaryDirectory(prefix='nexus-lease-diagnostics-') as root:
        os.environ['MINO_NEXUS_DATA_DIR'] = root
        os.environ['MINO_NEXUS_DEV_DATA_DIR'] = root
        import mino_nexus.startup
        from mino_nexus.core.database import DB_PATH, ensure_db, session_scope
        assert DB_PATH.resolve().parent == Path(root).resolve()
        ensure_db()
        from mino_nexus.resources import pool_account_store as store, resource_claim_tx as tx
        from mino_nexus.resources.accounts import lease, app_scope
        from mino_nexus.models.pool_account import PoolAccount
        from mino_nexus.models.scout_record import ScoutRecord
        from mino_nexus.models.run import AppRegressionRun
        from mino_nexus.resources import scout_record_store as records
        from mino_nexus.platform.identity import execution_user
        from mino_nexus.loop import script_callback as callback, scout_record_tasks as tasks
        from mino_nexus.casework import run_store
        from mino_nexus.core import protocol as P

        def ctx(run='old', session='old-session'):
            return SimpleNamespace(run_id=run, session_id=session, case_id='case', sn='web-node',
                project_id='p', app_id='app', platform='web', env_profile='test', env_surface='web.app',
                target_package='https://example.invalid', account_slot='buyer', leased_accounts=[],
                leased_account_leases=[], leased_account_by_slot={}, picked_account={}, resource_lease={})

        holder = {'run_id': 'old', 'session_id': 'old-session', 'case_id': 'case', 'sn': 'web-node',
                  'slot': 'buyer', 'app_id': 'app', 'platform': 'web', 'env_surface': 'web.app',
                  'target_package': 'https://example.invalid', 'expires_at': '2099-01-01T00:00:00Z'}
        request = {**holder, 'run_id': 'new', 'session_id': 'new-session', 'slot': 'default'}
        with session_scope() as db:
            db.add(PoolAccount(id='a', project_id='p', app_ids=['app'], env='test', email='a@example.invalid',
                               password='not-readable-fixture', lease=holder))
            db.add(PoolAccount(id='b', project_id='p', app_ids=['app'], env='test', email='b@example.invalid'))
        assert not store.try_claim_account_lease('p', 'a', request, app_id='app')
        detail = tx.claim_failure_detail()
        assert detail['reason_code'] == 'resource_account_lease_held'
        assert (detail['holder_run_id'], detail['holder_session_id'], detail['holder_slot']) == ('old', 'old-session', 'buyer')
        assert (detail['request_run_id'], detail['request_session_id'], detail['request_slot']) == ('new', 'new-session', 'default')
        assert 'password' not in json.dumps(detail) and 'a@example.invalid' not in json.dumps(detail)
        assert store.get_account('p', 'a')['lease'] == holder
        assert store.try_claim_account_lease('p', 'a', holder, app_id='app')
        assert not store.try_claim_account_lease('p', 'a', {**holder, 'slot': 'other'}, app_id='app')
        assert not store.try_claim_account_lease('p', 'b', holder, app_id='app')
        assert tx.claim_failure_code() == 'account_slot_identity_mismatch'
        assert store.get_account('p', 'b')['lease'] == {}
        tx.reset_claim_failure()
        assert tx.claim_failure_detail() == {} and tx.claim_failure_code() == ''
        print('PASS: structured holder/request run/session/slot; cross-run exclusion; same-slot reuse; durable slot uniqueness; no credentials')

        def rows(*args, **kwargs):
            return store.list_accounts('p')
        with execution_user('owner'), patch.object(lease.ps, 'require_app', return_value={'id': 'app', 'project_id': 'p', 'platforms': ['web']}), \
             patch.object(lease.ps, 'project_env', return_value={'channels': [{'id': 'web.app', 'app_id': 'app', 'platform': 'web', 'kind': 'web', 'field': 'base_url'}], 'profiles': {'test': {'web.app': {'base_url': 'https://example.invalid'}}}}), \
             patch.object(lease, 'list_test_accounts', side_effect=rows), \
             patch.object(app_scope, 'validate_project_apps'), \
             patch.object(store, 'read_leased_account_credentials', return_value={'password': 'fixture', 'otp': '123456'}):
            parent = ctx()
            restored, error = lease.restore_leases_for_run(parent, 'old')
            assert not error and restored[0]['id'] == 'a'
            assert store.get_account('p', 'a')['lease']['session_id'] == 'old-session'
            again, _, error = lease.lease_scoped_script_account(parent, {'account_requirements': {
                'all': [{'facet': 'account_id', 'op': 'eq', 'value': 'a'}]}}, slot='buyer')
            assert not error and again['id'] == 'a'
            before = deepcopy(parent.leased_accounts)
            changed, _, error = lease.lease_scoped_script_account(parent, {'account_requirements': {
                'all': [{'facet': 'account_id', 'op': 'eq', 'value': 'b'}]}}, slot='buyer')
            assert changed is None and error == 'account_slot_identity_mismatch'
            assert parent.leased_accounts == before and store.get_account('p', 'b')['lease'] == {}
            assert lease.restore_leases_for_run(ctx('new', 'new-session'), 'old')[1] == 'account_restore_run_mismatch'
            wrong = ctx(); wrong.env_profile = 'prod'
            assert lease.restore_leases_for_run(wrong, 'old')[1] == 'account_slot_scope_mismatch'
            assert not wrong.leased_accounts
            wrong = ctx(session='wrong-session')
            assert lease.restore_leases_for_run(wrong, 'old')[1] == 'account_slot_scope_mismatch'
            wrong = ctx('new'); wrong.leased_account_leases = deepcopy(parent.leased_account_leases)
            assert not lease.renew_run_lease(wrong)
            with session_scope() as db:
                row = db.get(PoolAccount, 'a'); row.lease = {**row.lease, 'expires_at': '2000-01-01T00:00:00Z'}
            assert lease.restore_leases_for_run(ctx(), 'old')[1] == 'resource_account_missing'
            assert store.get_account('p', 'a')['lease']['run_id'] == 'old'
            with session_scope() as db:
                db.get(PoolAccount, 'a').lease = deepcopy(holder)
            cold = ctx()
            changed, error = lease.lease_script_account(cold, {'account_requirements': {
                'all': [{'facet': 'account_id', 'op': 'eq', 'value': 'b'}]}})
            assert changed is None and error == 'account_slot_identity_mismatch'
            assert store.get_account('p', 'b')['lease'] == {}
            new = ctx('new', 'new-session'); new.account_slot = 'default'
            with patch.object(lease, '_pick_row', return_value=None):
                refused, error = lease.lease_script_account(new, {'account_requirements': {
                    'all': [{'facet': 'account_id', 'op': 'eq', 'value': 'a'}]}})
            assert refused is None and error == 'resource_account_lease_held'
            assert tx.claim_failure_detail()['request_session_id'] == 'new-session'
            # Full wire failure uses the same typed diagnostic, without credentials.
            admission = SimpleNamespace(ctx=new, case={}, project_id='p', app_id='app', run_id='new',
                session_id='new-session', case_id='case', owner_user_id='owner', node_id='node', lock=__import__('threading').Lock())
            params = {'run_id': 'new', 'session_id': 'new-session', 'requirements': {
                'all': [{'facet': 'account_id', 'op': 'eq', 'value': 'a'}]}}
            with patch.object(callback, '_require', return_value=admission), \
                 patch('mino_nexus.resources.app_automation.package_for_app', return_value='https://example.invalid'), \
                 patch('mino_nexus.resources.project_env.resolve_surface_for_app', return_value='web.app'), \
                 patch.object(lease, '_pick_row', return_value=None):
                response = callback.handle('node', P.Execute(run_id='new', step_idx=-1, sn='web-node', capability_id='case.account', params=params))
            assert response.status == P.EventStatus.FAIL and response.error == 'resource_account_lease_held'
            assert response.data['reason_detail']['holder_session_id'] == 'old-session'
            assert response.data['reason_detail']['request_session_id'] == 'new-session'
            assert 'fixture' not in json.dumps(response.data)
            with patch.object(lease, 'restore_lease_slot_for_run', return_value=(None, 'fixture_restore_failed')), \
                 patch.object(lease, '_pick_row', side_effect=AssertionError('restore failure must not allocate')):
                assert lease.lease_script_account(ctx(), {'account_requirements': {'all': [
                    {'facet': 'account_id', 'op': 'eq', 'value': 'a'}]}})[1] == 'fixture_restore_failed'
        fake_record = {'status': 'running', 'task_id': 'old', 'session_id': 'old-session', 'case_id': 'case',
                       'sn': 'web-node', 'owner_user_id': 'owner', 'context': {'app_id': 'app', 'project_id': 'p',
                       'env_profile': 'test', 'env_surface': 'web.app'}, 'payload': {'platform': 'web'}}
        from mino_nexus.loop.script_runtime import ScriptAbort
        with patch.object(records, 'for_node', return_value=fake_record), \
             patch('mino_nexus.runtime.run_context.build_run_context', return_value=ctx()), \
             patch.object(lease, 'restore_leases_for_run', return_value=([], 'account_slot_scope_mismatch')), \
             patch.object(callback, 'register') as registered:
            try:
                tasks.restore_admission('node', 'old-session')
            except ScriptAbort as exc:
                assert exc.reason_code == 'account_slot_scope_mismatch'
            else:
                raise AssertionError('admission ignored lease restore failure')
            registered.assert_not_called()
        print('PASS: cold restore, identity-change rejection, env/run/session guards, expired isolation, typed callback error, restore failure stops allocation')

        identity = {'task_id': 'debug-run', 'run_id': 'debug-run', 'session_id': 'debug-session',
                    'attempt_id': 'debug-attempt', 'case_id': 'debug-case', 'owner_user_id': 'owner',
                    'node_id': 'node', 'sn': 'web-node', 'platform': 'web', 'script_path': 'case.py'}
        old_command = {'request_id': 'old-request', 'state': 'completed', 'started_at': '2026-11-27T00:00:00Z',
                       'timeout_sec': 300, 'elapsed_ms': 53000, 'result': {'status': 'fail'}}
        active = {'request_id': 'current-request', 'state': 'executing', 'timeout_sec': 240,
                  'elapsed_ms': 1000, 'deadline_at': '2026-11-27T00:04:00Z', 'remaining_ms': 239000, 'overdue': False}
        otp_wait = {'state': 'waiting', 'elapsed_ms': 27000, 'polls': 1, 'matched': 0}
        with execution_user('owner'):
            records.register(node_id='node', payload=identity, context={})
            current = records.receive('node', {**identity, 'status': 'running', 'summary_version': 1, 'sealed': False,
                'session_state': 'executing', 'session_age_ms': 600000, 'command': old_command, 'active_command': active, 'otp_wait': otp_wait})
            assert current['summary']['otp_wait'] == otp_wait
            assert records.get('debug-session')['summary']['otp_wait'] == otp_wait
            assert current['summary']['command'] == old_command and current['summary']['active_command'] == active
            with session_scope() as db:
                db.add(AppRegressionRun(run_id='debug-run', owner_user_id='owner', status='running', payload={
                    'run_id': 'debug-run', 'owner_user_id': 'owner', 'status': 'running', 'cases': [
                    {'case_id': 'debug-case', 'sn': 'web-node', 'session_id': 'debug-session', 'status': 'running'}]}))
            with patch('mino_nexus.loop.observe.agent_stream.emit_testing_task'), \
                 patch.object(records, 'settle_resources'), \
                 patch('mino_nexus.loop.scout_resource_resolution.settle_terminal_task'):
                assert tasks.project(current)
                projected = run_store.get('debug-run')['cases'][0]
                assert projected['command'] == old_command and projected['active_command'] == active
                idle = records.receive('node', {**identity, 'status': 'running', 'summary_version': 2, 'sealed': False,
                    'session_state': 'ready', 'session_age_ms': 601000, 'command': old_command, 'active_command': None, 'otp_wait': None})
                assert idle['summary']['otp_wait'] is None
                assert tasks.project(idle)
                projected = run_store.get('debug-run')['cases'][0]
                assert projected['active_command'] is None and projected['command'] == old_command
                assert run_store._case_brief(projected)['session_age_ms'] == 601000
        print('PASS: session/command timing stored and projected separately; old request retained; idle clears active_command; otp_wait snapshot retained/cleared without starting OTP')
        print('Offline fixtures only; no business database, device, server, commit or deployment.')


if __name__ == '__main__':
    main()
