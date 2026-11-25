#!/usr/bin/env python3
"""Isolated requirement-target read/write contract smoke, not device QA."""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    with tempfile.TemporaryDirectory(prefix='mino-requirements-') as root:
        os.environ['MINO_NEXUS_DATA_DIR'] = root
        os.environ['MINO_NEXUS_DEV_DATA_DIR'] = root
        import mino_nexus.startup
        from mino_nexus.core.database import ensure_db, engine, session_scope
        ensure_db()
        assert Path(engine.url.database).resolve().parent == Path(root).resolve()
        from mino_nexus.models.project import Project, App
        from mino_nexus.models.run import AppRegressionRun
        from mino_nexus.assistant import agent_tasks as tasks, requirement_board as requirements
        from mino_nexus.core.nexus_time import timestamp
        from mino_nexus.casework.run_store import to_task_json
        with session_scope() as db:
            db.add(Project(id='PRJ', name='Fixture', created_by='owner'))
            db.add(App(id='APP', project_id='PRJ', name='Fixture Web', platforms='web,ios', created_by='owner'))
            db.add(App(id='OTHER', project_id='OTHER_PROJECT', name='Other', platforms='web'))
        config = {'effective_url': 'https://fixture.invalid', 'source': 'fixture'}
        source = [{'id': 'REQ', 'app_ids': ['APP'], 'title': 'Requirement'}]
        with patch('mino_nexus.assistant.assistant_reports.configuration', return_value=config), patch.object(requirements, 'list_project_requirements', return_value=source):
            for tid in ('a', 'b'):
                tasks.create('owner', tid, 'Fixture task', 'PRJ')
                requirements.bind('owner', tid, 'REQ', 'APP', 'web', 'pre')
            for arguments in [('stranger', 'a', 'REQ', 'APP', 'web', 'pre'), ('owner', 'a', 'MISSING', 'APP', 'web', 'pre'), ('owner', 'a', 'REQ', 'OTHER', 'web', 'pre'), ('owner', 'a', 'REQ', 'APP', 'android', 'pre')]:
                try:
                    requirements.bind(*arguments)
                except (ValueError, KeyError):
                    pass
                else:
                    raise AssertionError('Invalid target accepted')
            projected = requirements.board('owner', 'PRJ')['requirements'][0]
            assert len(projected['target_nodes']) == 2 and len(projected['target_nodes'][0]['tasks']) == 2
            assert projected['target_nodes'][1]['platform'] == 'ios' and not projected['target_nodes'][1]['tasks']
            assert tasks.get('owner', 'a')['presentation']['environment'] == 'Fixture Web · web · pre'
            requirements.bind('owner', 'b', 'REQ', 'APP', 'ios', 'test')
            assert len(requirements.board('owner', 'PRJ')['requirements'][0]['target_nodes']) == 2
            tasks.mutate('owner', 'a', 'question', {'event_id': 'q', 'question_id': 'q', 'question': 'Fixture question'})
            tasks.mutate('owner', 'a', 'answer', {'event_id': 'answer', 'question_id': 'q', 'answer': 'Fixture answer'})
            assert tasks.get('owner', 'a')['resume_required']
            tasks.mutate('owner', 'a', 'acknowledge', {'event_id': 'ack', 'question_id': 'q'})
            assert not tasks.get('owner', 'a')['resume_required']
            with session_scope() as db:
                db.add(AppRegressionRun(run_id='run', app_id='APP', owner_user_id='owner', payload={'app_id': 'APP', 'run_id': 'run', 'requirement_id': 'REQ', 'platform': 'web', 'env_profile': 'pre', 'status': 'done', 'cases': []}))
            entries = requirements.board('owner', 'PRJ')['requirements'][0]['target_nodes']
            assert sum(len(n['tasks']) for n in entries) == 3
            assert all(not n['tasks'] for n in requirements.board('stranger', 'PRJ')['requirements'][0]['target_nodes'])
        with patch.dict(os.environ, {'MINO_NEXUS_MODE': 'production'}):
            assert timestamp('2026-11-25T09:29:05') == '2026-11-25T09:29:05Z'
            assert timestamp('2026-11-25T17:29:05+08:00') == '2026-11-25T09:29:05Z'
            task = to_task_json({'app_id': 'APP', 'started_at': '2026-11-25T09:29:05', 'cases': []})
            assert task['started_at'].endswith('Z') and task['app_name'] == 'Fixture Web'
        print('PASS: owned target binding; multiple tasks; move without duplication; invalid targets rejected; answer/ack distinction; requirement execution grouping; production legacy time and application identity')


if __name__ == '__main__':
    main()
