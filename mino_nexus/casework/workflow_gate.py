"""正式脚本执行必须绑定已经锁定来源的 Agent workflow。

技能层的提示可以被跳过；这里把“来源回执 → expected_units → 本次 case_ids”
做成服务端硬门禁，任何带 script_targets 的正式执行都不能绕开。
"""
from __future__ import annotations

from typing import Any


class RunScopeRejected(ValueError):
    reason_code = "formal_run_requires_workflow_binding"

    def __init__(self, message: str, *, reason_code: str = ""):
        self.reason_code = reason_code or self.reason_code
        super().__init__(message)


def require_run_scope(
    owner_user_id: str,
    workflow_id: str,
    *,
    project_id: str,
    targets: list[dict[str, Any]],
) -> dict[str, Any]:
    workflow_id = str(workflow_id or "").strip()
    if not workflow_id:
        raise RunScopeRejected(
            "正式脚本执行必须绑定 workflow_id；先 case_source_list → case_source_read → "
            "agent_workflow_init 锁定 expected_units，再用 agent_workflow_get 的 workflow_id 开跑",
            reason_code="formal_run_requires_workflow_binding",
        )
    from mino_nexus.casework.case_sources import resolve_successful_read
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.agent_workflow import AgentWorkflow

    with session_scope() as db:
        row = db.get(AgentWorkflow, (owner_user_id, workflow_id))
        if row is None:
            raise RunScopeRejected("workflow 不存在或不属于当前用户", reason_code="workflow_not_found")
        state = row.state or {}
        locked = [str(unit) for unit in state.get("expected_units") or []]
        if not locked:
            raise RunScopeRejected("workflow 没有锁定 expected_units", reason_code="workflow_scope_unlocked")
        read_id = str(state.get("source_read_id") or "")
        if not read_id:
            raise RunScopeRejected("workflow 没有成功的 case_source_read 回执", reason_code="workflow_source_unverified")
        try:
            canonical = resolve_successful_read(read_id, owner_user_id, db=db)
        except ValueError as exc:
            raise RunScopeRejected(
                f"workflow 来源回执已失效：{exc}", reason_code="workflow_source_unverified"
            ) from exc
        for source_project in (state.get("project_id"), canonical.get("project_id")):
            if source_project and str(source_project) != str(project_id or ""):
                raise RunScopeRejected("workflow 来源项目与本次执行应用不一致", reason_code="workflow_project_mismatch")
        requested = {str(cid) for target in targets for cid in target.get("case_ids") or []}
        unknown = sorted(requested - set(locked))
        if unknown:
            raise RunScopeRejected(
                "本次用例不在 workflow expected_units 内：" + ", ".join(unknown),
                reason_code="case_not_in_workflow_scope",
            )
        return {
            "workflow_id": workflow_id,
            "source_read_id": read_id,
            "expected_units": locked,
            "requested_units": sorted(requested),
        }
