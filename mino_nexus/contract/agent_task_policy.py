"""MCP task-bound entry points, independent of client or token heuristics."""
PHASES = ('analysis', 'case_design', 'authoring', 'preparation', 'verification', 'report')
BOUND_TOOLS = frozenset({'debug_case', 'debug_case_execute', 'case_run',
                        'agent_workflow_bootstrap', 'agent_workflow_init', 'agent_workflow_start',
                        'agent_workflow_draft', 'agent_workflow_finish', 'agent_workflow_report', 'agent_workflow_finding',
                        'agent_workflow_context', 'agent_workflow_resolve',
                        'resource_asset_prepare', 'resource_asset_prepared'})


def requires_task(name):
    return name.replace('.', '_') in BOUND_TOOLS
