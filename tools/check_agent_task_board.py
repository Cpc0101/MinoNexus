#!/usr/bin/env python3
"""Disposable task-board admission and read-model integration smoke."""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    with tempfile.TemporaryDirectory(prefix='mino-board-contract-') as root:
        os.environ['MINO_NEXUS_DATA_DIR'] = root
        os.environ['MINO_NEXUS_DEV_DATA_DIR'] = root
        import mino_nexus.startup
        from mino_nexus.core.database import DB_PATH, ensure_db, session_scope
        assert DB_PATH.resolve().parent == Path(root).resolve()
        ensure_db()
        from mino_nexus.assistant import agent_tasks as tasks
        from mino_nexus.assistant.agent_task_runtime import tracked_mcp
        from mino_nexus.contract.agent_workflow import transition
        from mino_nexus.models.agent_workflow import AgentWorkflow
        from mino_nexus.api.rMcp import _task_schema
        calls = []

        @tracked_mcp
        def execute(uid, name, args):
            calls.append((uid, name, args))
            return {'content': [{'type': 'text', 'text': '{"ok":true}'}]}

        def rejected(args, reason):
            previous = len(calls)
            result = execute('fixture-owner', 'debug_case_execute', args)
            assert result['isError']
            assert json.loads(result['content'][0]['text'])['reason_code'] == reason
            assert len(calls) == previous, 'admission must precede side effects'

        with patch('mino_nexus.assistant.assistant_reports.configuration', return_value={'effective_url': 'https://fixture.invalid', 'source': 'fixture'}):
            rejected({}, 'agent_task_binding_required')
            task = tasks.create('fixture-owner', 'business', 'Fixture scope', scope='Requirement A', environment='pre')
            rejected({'agent_task_id': 'business'}, 'agent_task_startup_required')
            rejected({'agent_task_id': 'other-owner-task'}, 'agent_task_binding_invalid')
            try:
                tasks.mutate('fixture-owner', 'business', 'event', {'event_id': 'bad', 'summary': 'no URL', 'startup_delivered': True})
            except ValueError:
                pass
            else:
                raise AssertionError('Missing announcement URL accepted')
            tasks.mutate('fixture-owner', 'business', 'event', {'event_id': 'announce', 'summary': 'Published ' + task['board_url'], 'startup_delivered': True, 'phase': 'case_design', 'next_action': 'Publish source cases'})
            state, _ = transition(None, 'bootstrap', 'draft', {'scope': 'Requirement A', 'project_id': 'fixture-project'}, tasks.now())
            with session_scope() as db:
                db.add(AgentWorkflow(owner_user_id='fixture-owner', workflow_id='draft', state=state, revision=0, updated_at=tasks.now()))
                from mino_nexus.models.agent_task import AgentTask
                row = db.get(AgentTask, ('fixture-owner', 'business'))
                row.state = {**row.state, 'links': [{'kind': 'workflow', 'id': 'draft'}]}
            rejected({'agent_task_id': 'business'}, 'case_source_not_locked')
            assert tasks.get('fixture-owner', 'business')['presentation']['phase'] == 'case_design'
            locked, _ = transition(None, 'init', 'draft', {'scope': 'Requirement A', 'source_read_id': 'fixture-read', 'source': {'kind': 'fixture', 'reference': 'offline'}, 'expected_units': ['A']}, tasks.now())
            with session_scope() as db:
                row = db.get(AgentWorkflow, ('fixture-owner', 'draft'))
                row.state = locked
            accepted = execute('fixture-owner', 'debug_case_execute', {'agent_task_id': 'business'})
            assert not accepted.get('isError') and calls[-1][2] == {}
            tasks.create('other-owner', 'foreign', 'Other scope')
            rejected({'agent_task_id': 'foreign'}, 'agent_task_binding_invalid')
            tasks.create('fixture-owner', 'finished', 'Finished task')
            tasks.mutate('fixture-owner', 'finished', 'event', {'event_id': 'done', 'summary': 'Stopped before testing', 'status': 'cancelled'})
            rejected({'agent_task_id': 'finished'}, 'agent_task_binding_invalid')
            tasks.mutate('fixture-owner', 'business', 'question' , {'event_id': 'question', 'question_id': 'otp', 'question': 'Provide OTP configuration', 'missing': 'OTP', 'evidence': ['fixture has no OTP']})
            before = tasks.get('fixture-owner', 'business')['current_action']
            tasks.tool_begin('fixture-owner', 'business', 'read', 'resource.env.get', 'technical title')
            tasks.tool_end('fixture-owner', 'business', 'read', 'resource.env.get', True, 1)
            current = tasks.get('fixture-owner', 'business')
            assert current['status'] == 'waiting_user' and current['current_action'] == before
            tasks.mutate('fixture-owner', 'business', 'answer', {'event_id': 'answer', 'question_id': 'otp', 'answer': 'Fixture answer'})
            tasks.tool_begin('fixture-owner', 'business', 'read2', 'resource.env.get', 'technical title')
            tasks.tool_end('fixture-owner', 'business', 'read2', 'resource.env.get', True, 1)
            assert tasks.get('fixture-owner', 'business')['status'] == 'waiting_agent'
            result = execute('fixture-owner', 'resource_env_get', {'agent_task_id': 'business'})
            receipt = json.loads(result['content'][0]['text'])
            assert receipt['agent_task_id'] == 'business' and receipt['board_url'] == task['board_url']
            execute('fixture-owner', 'resource_env_get', {})
            assert tasks.listing('fixture-owner')['total'] == 2
            assert tasks.listing('fixture-owner', view_kind='calls')['total'] == 0
            assert tasks.listing('other-owner')['total'] == 1
            assert 'agent_task_id' in _task_schema({'id': 'debug.case.execute'})['required']
            for i in range(105):
                tasks.mutate('fixture-owner', 'business', 'event', {'event_id': f'e{i}', 'summary': f'Business progress {i}'})
            page = tasks.events('fixture-owner', 'business')
            assert page['has_more'] and page['items'][0]['summary'] == 'Business progress 104'
            incremental = tasks.events('fixture-owner', 'business', after_seq=page['high_watermark'])
            assert incremental['items'] == []
            tasks.create('fixture-owner', 'lifecycle', 'Message lifecycle fixture')
            tasks.mutate('fixture-owner', 'lifecycle', 'question', {'event_id': 'ask', 'question_id': 'real', 'question': 'Which endpoint is in scope?', 'missing': 'endpoint'})
            tasks.mutate('fixture-owner', 'lifecycle', 'answer', {'event_id': 'answer', 'question_id': 'real', 'answer': 'fixture endpoint'})
            tasks.mutate('fixture-owner', 'lifecycle', 'event', {'event_id': 'end', 'summary': 'Cancelled fixture', 'status': 'cancelled'})
            ended = tasks.get('fixture-owner', 'lifecycle')
            assert ended['status'] == 'cancelled' and not ended['resume_required']
            assert ended['questions'][0]['status'] == 'answered' and not ended['questions'][0]['acknowledged_at']
            assert not ended['questions'][0]['actionable'] and ended['questions'][0]['history_reason'] == 'task_ended'
            assert '历史' in ended['presentation']['next_action']
            with session_scope() as db:
                row = db.get(AgentTask, ('fixture-owner', 'business'))
                legacy = {'question_id': 'legacy-session', 'summary': '请补充本次的所需信息。', 'missing': 'session_id', 'evidence': [], 'status': 'open', 'answer': '', 'answered_at': '', 'acknowledged_at': ''}
                row.state = {**row.state, 'status': 'waiting_user', 'questions': [legacy]}
            legacy_view = tasks.get('fixture-owner', 'business')
            assert legacy_view['status'] == 'paused' and not legacy_view['questions'][0]['actionable']
            assert legacy_view['questions'][0]['status'] == 'open'  # projection never fabricates acknowledgement
            assert next(item for item in tasks.listing('fixture-owner')['items'] if item['task_id'] == 'business')['pending_questions'] == 0
        print('PASS: task binding and announcement gates precede side effects; source drafting gate; owned business projection; question state; persistent link receipts; diagnostic filtering; latest/incremental event pagination')
        print('No device/UI execution or client-display claim.')


if __name__ == '__main__':
    main()
