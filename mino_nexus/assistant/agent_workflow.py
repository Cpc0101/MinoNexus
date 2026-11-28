"""Persist Agent progress in Nexus SQL; no device or case actions."""
from datetime import datetime, timezone
import json

from sqlalchemy import text as sql
from mino_nexus.contract.agent_workflow import text, transition, unit_list, workflow_source
from mino_nexus.core.database import session_scope
from mino_nexus.models.agent_workflow import AgentWorkflow


def operate(owner_user_id: str, workflow_id: str, operation: str, args: dict) -> dict:
    owner = text(owner_user_id, "owner_user_id", 128)
    workflow_id = text(workflow_id, "workflow_id", 128)
    with session_scope() as db:
        if operation != "get":
            db.execute(sql("BEGIN IMMEDIATE"))
        row = db.get(AgentWorkflow, (owner, workflow_id))
        old = row.state if row else None
        if operation == "init":
            source_read_id = text(args.get("source_read_id", ""), "source_read_id", 128, required=False)
            if not source_read_id:
                raise ValueError("source_read_id is required")
            # Every init, including an idempotent retry or a legacy-workflow
            # upgrade, revalidates the successful receipt in the same immediate
            # transaction. Explicit compatibility fields must agree instead of
            # being silently replaced by cached workflow values.
            from mino_nexus.casework.case_sources import resolve_successful_read

            canonical = resolve_successful_read(source_read_id, owner, db=db)
            expected_units = canonical["expected_units"]
            if "expected_units" in args and args["expected_units"] is not None:
                supplied_units = unit_list(args["expected_units"], "expected_units", required=False)
                if supplied_units != expected_units:
                    raise ValueError("expected_units do not match successful source read")
            if "source" in args and args["source"] is not None:
                supplied_source = workflow_source(args["source"])
                if supplied_source != canonical["source"]:
                    raise ValueError("source does not match successful source read")
            # Use only the validated receipt snapshot. The transition still
            # compares it with any already-locked workflow identity.
            args = {**args, "source_read_id": canonical["read_id"],
                    "expected_units": expected_units, "source": canonical["source"], "project_id": canonical["project_id"]}
        if operation == "finish" and args.get("outcome") == "complete" and old:
            attempt = next((a for a in old["attempts"] if a["attempt_id"] == args.get("attempt_id")), None)
            if attempt and attempt["stage"] == "verification":
                from mino_nexus.casework import run_store
                from mino_nexus.contract.case_receipts import require_verified_unit
                from mino_nexus.platform.identity import execution_user

                task_id = text(args.get("task_id", ""), "task_id", 128, required=False)
                if not task_id:
                    raise ValueError("formal_verification_requires_task_id")
                with execution_user(owner):
                    task = run_store.get(task_id)
                    if task is None:
                        raise ValueError("formal_verification_task_missing")
                    project_id = old.get("project_id")
                    if not project_id:
                        from mino_nexus.models.case_source import CaseSource
                        source = db.get(CaseSource, (old.get("source") or {}).get("source_id", ""))
                        if source is None or source.owner_user_id != owner:
                            raise ValueError("formal_verification_source_scope_missing")
                        project_id = source.project_id
                    if task.get("project_id") != project_id:
                        raise ValueError("formal_verification_project_mismatch")
                    require_verified_unit(run_store.to_task_json(task), attempt["unit"])
                    old = {**old, "project_id": project_id}
        if old:
            from copy import deepcopy
            from mino_nexus.casework import run_store
            from mino_nexus.contract.case_receipts import project as receipt_project
            candidate = deepcopy(old)
            observed = dict(candidate.get("observed_dispositions") or {})
            explicit = {item.get("unit") for item in candidate.get("attempts") or [] if item.get("disposition")}
            task_ids = {item.get("task_id") for item in candidate.get("attempts") or [] if item.get("task_id")}
            task_ids |= set(candidate.get("task_contexts") or {})
            mapping = {"verified": "verified", "script_repair": "script_repair", "build_mismatch": "build_mismatch",
                       "precondition": "precondition_creatable", "precondition_historical": "precondition_historical",
                       "bug": "bug", "accepted_difference": "accepted_difference"}
            gap_kind = {"build_mismatch": "build_gap", "precondition_historical": "historical_gap", "accepted_difference": "accepted_difference"}
            gap_title = {"build_gap": "被测构建不符", "historical_gap": "历史样本不足", "accepted_difference": "文案可接受差异"}
            changed = False
            stamped = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            expected_units = list(candidate.get("expected_units") or candidate.get("units") or [])
            for task_id in task_ids:
                task = run_store.get(task_id) if task_id else None
                if task is None or task.get("owner_user_id") not in {None, owner} and task.get("owner_user_id") != owner:
                    continue
                for receipt in receipt_project(run_store.to_task_json(task)).get("case_receipts") or []:
                    unit, mapped = receipt.get("case_id"), mapping.get(receipt.get("disposition") or "")
                    if unit and mapped and unit not in explicit and observed.get(unit) != mapped:
                        observed[unit] = mapped
                        changed = True
                    kind = gap_kind.get(mapped or "")
                    if not unit or not kind or unit not in expected_units:
                        continue
                    if any(item.get("case_id") == unit and item.get("kind") == kind for item in candidate.get("findings") or []):
                        continue
                    diffs = [item for item in receipt.get("accepted_differences") or [] if isinstance(item, dict)]
                    session_id = str(receipt.get("session_id") or "")
                    evidence = [f"session:{session_id}/artifact:{item.get('artifact_id')}" for item in diffs if session_id and item.get("artifact_id")]
                    from mino_nexus.contract.agent_workflow import _store_finding
                    _store_finding(candidate, {
                        "finding_id": f"auto-{kind}-{unit}"[:128], "kind": kind, "strict": False, "case_id": unit,
                        "title": gap_title[kind], "steps": "见对应用例",
                        "case_text": str(receipt.get("expectation") or "需求未强制该写法"),
                        "expected": "；".join(str(item.get("expected") or "") for item in diffs) or str(receipt.get("expectation") or "见回执"),
                        "actual": "；".join(str(item.get("actual") or "") for item in diffs) or str(receipt.get("summary") or "见回执"),
                        "evidence": evidence,
                    }, stamped)
                    changed = True
            if changed:
                candidate["observed_dispositions"] = observed
                old = candidate
        if operation == "finish" and old and args.get("task_id"):
            from copy import deepcopy
            from mino_nexus.casework import run_store
            old = deepcopy(old)
            task = run_store.get(args["task_id"])
            if task is None or task.get("owner_user_id") != owner or task.get("project_id") != old.get("project_id"):
                raise ValueError("Workflow task scope mismatch")
            old.setdefault("task_contexts", {})[args["task_id"]] = {
                key: task.get(key) for key in ("app_id", "env_profile", "platform", "revision", "execution_kind", "started_at", "finished_at")}
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        state, result = transition(old, operation, workflow_id, args, now)
        stored = row.state if row else None
        synced = stored is not None and (state.get("observed_dispositions") != stored.get("observed_dispositions") or state.get("findings") != stored.get("findings"))
        if (row is None or state != row.state) and (operation != "get" or synced):
            if len(json.dumps(state, ensure_ascii=False).encode()) > 2_000_000:
                raise ValueError("Workflow record limit reached; deliver this scope and start a new workflow")
            if row:
                row.state, row.revision, row.updated_at = state, row.revision + 1, now
            else:
                row = AgentWorkflow(owner_user_id=owner, workflow_id=workflow_id, state=state, revision=1, updated_at=now)
                db.add(row)
        return {**result, "revision": row.revision, "updated_at": row.updated_at}


def list_workflows(owner_user_id: str, project_id: str, limit: int = 50, offset: int = 0) -> dict:
    from sqlalchemy import func
    from mino_nexus.contract.agent_workflow import coverage
    with session_scope() as db:
        query = db.query(AgentWorkflow).filter(AgentWorkflow.owner_user_id == owner_user_id)
        if project_id:
            query = query.filter(AgentWorkflow.state["project_id"].as_string() == project_id)
        total = query.with_entities(func.count()).scalar()
        rows = query.order_by(AgentWorkflow.updated_at.desc(), AgentWorkflow.workflow_id).offset(offset).limit(limit).all()
        return {"total": total, "items": [{"workflow_id": row.workflow_id,
                "scope": row.state["scope"], "project_id": row.state.get("project_id", ""),
                "source_status": "locked" if row.state.get("source_read_id") else "drafting",
                "delivery_status": row.state.get("delivery_status", "in_progress"),
                "coverage": coverage(row.state), "revision": row.revision,
                "updated_at": row.updated_at} for row in rows]}
