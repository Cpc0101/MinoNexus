"""Human-readable facts from owned, explicitly linked ledgers."""
from mino_nexus.contract.agent_workflow import summary
from mino_nexus.contract.case_receipts import project as receipts
from mino_nexus.models.agent_workflow import AgentWorkflow
from mino_nexus.models.run import AppRegressionRun

LABELS = {'analysis': '理解需求', 'case_design': '补写人工用例', 'authoring': '编写与调试脚本',
          'preparation': '准备环境与数据', 'verification': '正式测试', 'report': '交付报告'}


def project(db, row):
    state = row.state
    workflows, execution = [], None
    for ref in reversed(state.get('links', [])):
        if ref['kind'] == 'workflow':
            workflow = db.get(AgentWorkflow, (row.owner_user_id, ref['id']))
            if workflow:
                facts = summary(workflow.state)
                workflows.append({'workflow_id': ref['id'], **{k: facts[k] for k in
                    ('scope', 'source_status', 'coverage', 'units', 'help_requests', 'delivery_status', 'draft_actions')},
                    'active_attempts': [a for a in facts['attempts'] if not a['outcome']]})
        elif ref['kind'] == 'execution' and execution is None:
            run = db.get(AppRegressionRun, ref['id'])
            if run and run.owner_user_id == row.owner_user_id:
                doc = run.payload or {}
                execution = {'task_id': ref['id'], 'status': doc.get('status'), **receipts(doc)}
    phase = state.get('phase', '')
    action = state.get('current_action', '')
    if workflows:
        flow = workflows[0]
        if flow['source_status'] == 'drafting':
            phase = 'case_design'
            if flow['draft_actions']:
                action = flow['draft_actions'][-1]['action']
        elif flow['active_attempts']:
            attempt = flow['active_attempts'][-1]
            phase = attempt['stage']
            action = f"{attempt['unit']}：{attempt['action']}"
    active = (execution or {}).get('active_cases', [])
    if active:
        action = '；'.join(f"{c['case_id']}：{c['step'] or '设备未上报具体步骤'}" for c in active[:3])
    workflow_questions = [q for flow in workflows for q in flow['help_requests'] if not q.get('answer')]
    actionable = [q for q in state.get('questions', []) if q.get('missing') not in {'session_id', 'client_request_id', 'attempt_id'}]
    questions = [q for q in actionable if q['status'] == 'open']
    next_action = state.get('next_action', '')
    if state['status'] in {'completed', 'failed', 'cancelled'}:
        next_action = '任务已结束；沟通记录仅作历史保留，未读取不代表仍需回答。'
    elif questions or workflow_questions:
        next_action = f"请回答下方 {len(questions) + len(workflow_questions)} 个问题；答案提交后等待 Agent 读取并核验"
    elif any(q['status'] == 'answered' for q in actionable):
        next_action = '答案已提交，等待 Agent 读取并核验'
    target = state.get('target') or {}
    environment = state.get('environment') or ''
    if target.get('app_id'):
        from mino_nexus.models.project import App
        app = db.get(App, target['app_id'])
        environment = ' · '.join([app.name if app else target['app_id'], target['platform'], target['environment']])
    return {'target': target, 'phase': phase, 'phase_label': LABELS.get(phase, '阶段尚未上报'),
            'action': action or '尚未收到业务进度上报', 'next_action': next_action or '下一步尚未上报',
            'scope': state.get('scope') or (workflows[0]['scope'] if workflows else state['title']),
            'environment': environment or '环境尚未上报',
            'workflows': workflows, 'execution': execution}
