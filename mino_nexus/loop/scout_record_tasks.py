"""Durable Scout admissions. Transport acknowledgement never means test success."""
from __future__ import annotations

import time
from typing import Any

from mino_nexus.core import protocol as P
from mino_nexus.core.async_util import run_sync
from mino_nexus.core.log import SLog
from mino_nexus.platform.identity import execution_user, require_user_id
from mino_nexus.resources import scout_record_store as records


def project(row: dict[str, Any]) -> bool:
    """Project only the last durable summary; evidence remains on the original node."""
    from mino_nexus.casework import run_store
    summary = row.get("summary") or {}
    if row["status"] in records.TERMINAL and not records.release_evidence_confirmed(row):
        return False
    with execution_user(row["owner_user_id"]):
        doc = run_store.get(row["task_id"])
        if not doc or not run_store.task_is_live(doc):
            if doc and doc.get("finished_at"):
                from mino_nexus.loop.scout_resource_resolution import settle_terminal_task
                settle_terminal_task(row["task_id"], row["owner_user_id"])
                return True
            return False
        fields = {key: summary[key] for key in (
            "execution_kind", "debug_state", "workspace_hash", "debug_origin_matched",
            "summary", "reason_code", "failure_stage", "elapsed_ms", "execution_started",
            "started_at", "finished_at", "sealed", "last_event_seq", "run_purpose", "revision",
            "assertions", "source_coverage", "source_hash", "resource_isolated", "test_report", "data_outputs", "data_missing",
        ) if key in summary}
        resolution = row.get("context", {}).get("resource_resolution")
        if resolution:
            fields.update(resource_isolated=False, resource_resolution=resolution)
        fields.update(session_id=row["session_id"], attempt_id=row["attempt_id"],
                      record_node_id=row["node_id"], record_source="scout",
                      detail_availability=summary.get("detail_availability", "pending"),
                      status_sync_at=row["updated_at"], summary_version=row["summary_version"],
                      status=row["status"])
        if fields.get("resource_isolated"):
            with run_store._LOCK:
                guarded = run_store.get(row["task_id"])
                guarded.setdefault("write_safety", {})["scout:" + row["attempt_id"]] = {
                    "state": "isolated", "sn": row["sn"], "reason_code": "write_outcome_unknown",
                    "stop_confirmed": bool(summary.get("sealed")), "attempt_id": row["attempt_id"],
                }
                run_store.put(guarded)
        latest = run_store.patch_case(row["task_id"], row["case_id"], target_sn=row["sn"], **fields)
        if row["status"] in records.TERMINAL:
            records.settle_resources(row)
            cases = latest.get("cases") or []
            if cases and all(case.get("status") in records.TERMINAL for case in cases):
                status = "cancelled" if latest.get("cancel_requested") else (
                    "done")
                finished = run_store.finish(row["task_id"], status=status)
                if not run_store.task_is_live(finished):
                    from mino_nexus.loop.scout_resource_resolution import settle_terminal_task
                    settle_terminal_task(row["task_id"], row["owner_user_id"])
                    from mino_nexus.loop.observe.agent_stream import emit_testing_task
                    from mino_nexus.loop.observe.persist_run_finish import persist_run_finish
                    persist_run_finish(finished)
                    emit_testing_task({"event": "task_finished", "run_id": row["task_id"],
                                       "task_id": row["task_id"], "status": finished["status"],
                                       "app_id": finished.get("app_id") or ""})
        return True


def receive(node_id: str, incoming: dict[str, Any]) -> dict[str, Any]:
    row = records.receive(node_id, incoming)
    project(row)
    return row


def restore_admission(node_id: str, session_id: str):
    from mino_nexus.loop.script_callback import ScriptAdmission, register
    from mino_nexus.runtime.run_context import build_run_context
    row = records.for_node(session_id, node_id)
    if not row or row["status"] in records.TERMINAL:
        return None
    data, payload = row["context"], row["payload"]
    with execution_user(row["owner_user_id"]):
        ctx = build_run_context(row["sn"], run_id=row["task_id"], app_id=data.get("app_id", ""),
                                provider_id=data.get("provider_id", ""), target_package=data.get("package", ""))
        ctx.session_id, ctx.case_id, ctx.scout_run_id = session_id, row["case_id"], session_id
        ctx.project_id, ctx.env_profile = data.get("project_id", ""), data.get("env_profile", "test")
        ctx.env_surface = str(data.get("env_surface") or "")
        if "env_surface" not in data:
            # Older admissions can recover the selected channel from their owner's task.
            from mino_nexus.casework import run_store
            doc = run_store.get(row["task_id"]) or {}
            target = (doc.get("device_targets") or {}).get(row["sn"]) or {}
            ctx.env_surface = str(target.get("target_surface", doc.get("env_surface")) or "")
        from mino_nexus.resources.accounts.lease import restore_leases_for_run
        restore_leases_for_run(ctx, row["task_id"])
        admission = ScriptAdmission(run_id=row["task_id"], session_id=session_id, case_id=row["case_id"],
            sn=row["sn"], node_id=node_id, owner_user_id=row["owner_user_id"], ctx=ctx, writer=None,
            project_id=data.get("project_id", ""), app_id=data.get("app_id", ""),
            provider_id=data.get("provider_id", ""), platform=payload.get("platform", ""),
            case=data.get("case") or {})
        register(admission)
        return admission


def dispatch(*, run_id, case, sn, app_id, package, provider_id, platform, project_id, repo, doc, ctx, node):
    from mino_nexus.casework.script_workspace import public_url
    from mino_nexus.contract.step_pointer import compute_case_wall_budget_sec
    from mino_nexus.loop.script_dispatch import _device_hint
    from mino_nexus.loop.script_callback import ScriptAdmission, register, unregister
    from mino_nexus.casework import run_store
    if doc.get("execution_kind") == "debug" and (run_store.cancel_requested(run_id) or not run_store.task_is_live(run_store.get(run_id))):
        return {"status": "cancelled", "reason_code": "task_cancelled", "execution_started": "no", "summary": "调试会话开始前已结束", "run_purpose": "authoring"}
    sid = str(case.get("report_run_id") or run_id)
    budget = max(1.0, min(86400.0, float(compute_case_wall_budget_sec(case) or 600)))
    if case.get("attempt_deadline_at"):
        budget = max(1.0, min(budget, float(case["attempt_deadline_at"]) - time.time()))
    payload = P.dump_script_run(P.load_script_run(P.dump_script_run(P.ScriptRun(
        task_id=run_id, run_id=run_id, session_id=sid, attempt_id=sid,
        owner_user_id=require_user_id(), case_id=str(case.get("case_id") or ""), sn=sn,
        platform=platform, repo_url=public_url(repo.get("url") or ""), branch=repo.get("branch") or "",
        revision=doc.get("revision") or "", script_path=case.get("script_path") or "",
        execution_kind=doc.get("execution_kind", "case"), workspace=doc.get("workspace", ""),
        debug_origin=doc.get("debug_origin") or {},
        execution_timeout_sec=budget, inputs=case.get("inputs") or {}, source_context=doc.get("source_context") or {},
        web_capabilities=case.get("web_capabilities", [])))))
    from mino_nexus.resources.resource_claim_tx import _claim
    resource_request = dict(_claim.get() or {})
    row = records.register(node_id=node.node_id, payload=payload, context={
        "resource_request": resource_request,
        "app_id": app_id, "project_id": project_id, "package": package,
        "provider_id": provider_id, "env_profile": ctx.env_profile, "env_surface": ctx.env_surface,
        "case": {key: case[key] for key in ("case_id", "account_requirements", "account_template_id", "template_id") if key in case},
        "device_hint": {**_device_hint(node, sn, platform, ctx.env_profile, app_id, project_id),
                        **({"app_key": doc["debug_app_key"]} if doc.get("execution_kind") == "debug" else {})},
    })
    register(ScriptAdmission(run_id=run_id, session_id=sid, case_id=row["case_id"], sn=sn,
        node_id=node.node_id, owner_user_id=row["owner_user_id"], ctx=ctx, writer=None,
        project_id=project_id, app_id=app_id, provider_id=provider_id, platform=platform, case=case))
    try:
        project(row)
        return wait_attempt(row)
    finally:
        unregister(run_id, sid)


def _request(row, node, cap, params):
    payload = row["payload"]
    request = P.Execute(run_id=payload["run_id"], step_idx=-1, sn=row["sn"],
                        capability_id=cap, platform=payload.get("platform", ""), params=params,
                        timeout_sec=15, executor_order=[], device_hint=row["context"].get("device_hint") or {})
    return run_sync(node.send(P.MsgType.EXECUTE, request, timeout=15))


def wait_attempt(row: dict[str, Any]) -> dict[str, Any]:
    """Wait in a background worker; disconnects never finalize or replay a new attempt."""
    from mino_nexus.casework import run_store
    from mino_nexus.resources.node_registry import get_registry
    delay = 1.0
    while True:
        row = records.get(row["session_id"]) or row
        if row["status"] in records.TERMINAL:
            project(row)
            return {**row["summary"], "status": row["status"], "steps": []}
        if run_store.cancel_requested(row["task_id"]) and not row["cancel_requested"]:
            records.request_cancel(row["task_id"])
            row = records.get(row["session_id"]) or row
        if not refresh_resources(row) and not row["cancel_requested"]:
            records.request_cancel(row["task_id"])
            row = records.get(row["session_id"]) or row
        node = get_registry().get_node(row["node_id"])
        try:
            if node and node.alive and node.send and node.owner_user_id == row["owner_user_id"]:
                scope = {key: row[key] for key in ("task_id", "session_id", "attempt_id", "owner_user_id")}
                result = _request(row, node, "record.cancel" if row["cancel_requested"] else "record.status", scope)
                error = str(getattr(result, "error", "") or "")
                if result is not None and error == "record_not_found" and row["summary_version"] == 0:
                    if row["cancel_requested"]:
                        row = records.reject_unstarted(row["session_id"], "task_cancelled", status="cancelled")
                        continue
                    # This exact immutable receipt is safe to redeliver after a lost ACK.
                    result = _request(row, node, P.SCRIPT_SUBMIT_CAPABILITY, row["payload"])
                    if result is not None and getattr(result, "status", None) == P.EventStatus.FAIL:
                        # Admission failed before any accepted summary. Confirm absence on the next poll.
                        code = str(getattr(result, "error", "") or "script_admission_rejected")
                        if code not in {"record_submission_conflict", "record_unavailable"}:
                            row = records.reject_unstarted(row["session_id"], code)
                            continue
                data = getattr(result, "data", None)
                if isinstance(data, dict) and data.get("summary_version"):
                    row = receive(row["node_id"], data)
                    delay = 1.0
        except Exception as exc:
            SLog.w("ScoutRecords", f"record reconciliation pending session={row['session_id']}: {type(exc).__name__}")
        # No terminal inference from timeout or offline. Scout owns its execution deadline.
        time.sleep(delay)
        delay = min(10.0, delay * 1.5)


def refresh_resources(row: dict[str, Any]) -> bool:
    """Resume only the original durable resource epoch; never reacquire another one."""
    request = row["context"].get("resource_request") or {}
    if not request or row["status"] in records.TERMINAL:
        return True
    from mino_nexus.casework.execution_resources import renew
    with execution_user(row["owner_user_id"]):
        return renew(request)
