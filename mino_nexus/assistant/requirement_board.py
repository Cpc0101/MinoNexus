"""Explicit requirement target bindings and owned progress projections."""
from copy import deepcopy
from mino_nexus.core.database import session_scope
from sqlalchemy import text as sql
from mino_nexus.models.agent_task import AgentTask
from mino_nexus.models.run import AppRegressionRun
from mino_nexus.contract.agent_workflow import text
from mino_nexus.platform.identity import execution_user
from mino_nexus.resources import project_store
from mino_nexus.casework.case_import import list_project_requirements
from mino_nexus.assistant import agent_tasks


def bind(owner, task_id, requirement_id, app_id, platform, environment):
    values = {k: text(v, k, 128) for k, v in dict(requirement_id=requirement_id, app_id=app_id,
                                               platform=platform, environment=environment).items()}
    with execution_user(owner), session_scope() as db:
        db.execute(sql("BEGIN IMMEDIATE"))
        row = agent_tasks._row(db, owner, task_id)
        if row.state['source'] == 'tool_call':
            raise ValueError('Independent calls cannot be requirement tasks')
        app = project_store.require_app(app_id)
        if not row.project_id or app['project_id'] != row.project_id:
            raise ValueError('Task and target must belong to the same project')
        req = next((r for r in list_project_requirements(row.project_id) if r['id'] == requirement_id), None)
        if not req or (req['app_ids'] and app_id not in req['app_ids']):
            raise ValueError('Requirement not found for target application')
        platforms = str(app.get('platforms') or '').replace(',', ' ').split()
        if platform not in platforms:
            raise ValueError('Platform not configured for target application')
        state = deepcopy(row.state)
        state['target'] = values
        if row.state.get('target') != values:
            row.state = state
            row.revision += 1
        return agent_tasks.view(row, db)


def board(owner, project_id):
    from mino_nexus.casework.run_store import to_task_json
    with execution_user(owner):
        requirements = list_project_requirements(project_id)
        apps = {a['id']: a for a in project_store.list_apps(project_id)}
        with session_scope() as db:
            agents = db.query(AgentTask).filter_by(owner_user_id=owner, project_id=project_id).all()
            runs = db.query(AppRegressionRun).filter(AppRegressionRun.owner_user_id == owner, AppRegressionRun.app_id.in_(list(apps)), AppRegressionRun.payload['requirement_id'].as_string().in_([r['id'] for r in requirements])).all()
            nodes = {}
            for req in requirements:
                nodes[req['id']] = {}
            def node(target):
                rid, aid = target.get('requirement_id'), target.get('app_id')
                req = next((r for r in requirements if r['id'] == rid), None)
                if not req or aid not in apps or (req['app_ids'] and aid not in req['app_ids']):
                    return None
                key = (aid, target.get('platform') or '', target.get('environment') or '')
                return nodes[rid].setdefault(key, {**target, 'app_name': apps[aid]['name'], 'tasks': []})
            for row in agents:
                target = row.state.get('target') or {}
                entry = node(target)
                if entry is not None:
                    task = agent_tasks.view(row, db)
                    entry['tasks'].append({'kind': 'agent', 'task_id': row.task_id, 'title': task['title'],
                        'status': task['status'], 'phase_label': task['presentation']['phase_label'],
                        'updated_at': task['updated_at'], 'board_url': task['board_url']})
            for run in runs:
                doc = run.payload or {}
                entry = node({'requirement_id': doc.get('requirement_id'), 'app_id': doc.get('app_id'),
                              'platform': doc.get('platform'), 'environment': doc.get('env_profile')})
                if entry is not None:
                    task = to_task_json(doc, include_cases=False)
                    entry['tasks'].append({'kind': 'execution', 'task_id': task['task_id'], 'title': task.get('title') or task['task_id'],
                        'status': task['result_status'], 'phase_label': '编写与调试' if task['execution_kind'] == 'debug' else '正式测试',
                        'updated_at': task.get('finished_at') or task.get('started_at'), 'board_url': task['report_url']})
            for req in requirements:
                req['target_nodes'] = list(nodes[req['id']].values())
                covered = {(n['app_id'], n.get('platform') or '') for n in req['target_nodes']}
                for aid in req['app_ids'] or apps:
                    if aid in apps:
                        for platform in str(apps[aid].get('platforms') or '').split(','):
                            if (aid, platform.strip()) in covered:
                                continue
                            req['target_nodes'].append({'app_id': aid, 'app_name': apps[aid]['name'], 'platform': platform.strip(), 'environment': '', 'tasks': []})
            return {'requirements': requirements}
