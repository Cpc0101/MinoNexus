"""Local authoring sessions using the existing Scout admission and record ledger."""
from __future__ import annotations

from mino_nexus.core import protocol as P
from mino_nexus.core.async_util import run_sync
from mino_nexus.platform.identity import require_user_id
from mino_nexus.resources.node_registry import get_registry


def _call(node, capability, params, *, run_id="", sn="", platform=""):
    response = run_sync(node.send(P.MsgType.EXECUTE, P.Execute(
        run_id=run_id, step_idx=-1, sn=sn, platform=platform, capability_id=capability,
        params=params, timeout_sec=15), timeout=15))
    if response is None or response.status != P.EventStatus.PASS:
        raise ValueError(str(getattr(response, "error", "") or getattr(response, "summary", "") or "debug_node_unavailable"))
    return dict(response.data or {})


def start(app, args):
    from mino_nexus.loop.case_runner import run_cases
    from mino_nexus.resources import repo_apps, project_store
    from mino_nexus.resources.project_env import resolve_run_env_profile
    owner = require_user_id()
    for key in ("sn", "node_id", "app_key", "workspace", "client_request_id"):
        if not isinstance(args.get(key), str) or not args[key].strip():
            raise ValueError("debug_argument_missing:" + key)
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.run_admission import RunAdmission
    from mino_nexus.casework import run_store
    with session_scope() as database:
        receipt = database.query(RunAdmission).filter_by(owner_user_id=owner, request_id=args["client_request_id"]).one_or_none()
        original = run_store.get(receipt.run_id) if receipt else None
    if original:
        if original.get("execution_kind") != "debug" or original.get("debug_start_request") != args:
            raise ValueError("debug_request_conflict")
        return {**run_store.to_task_json(original), "session_id": original["cases"][0]["report_run_id"], "execution_kind": "debug", "workspace": original["workspace"]}
    project_id = str(app.get("project_id") or "")
    if args.get("project_id") and args["project_id"] != project_id:
        raise ValueError("project_scope_invalid")
    node, _ = get_registry().resolve(args["sn"])
    if node is None or node.node_id != args["node_id"] or node.owner_user_id != owner or not node.send or node.record_api_version != 1:
        raise ValueError("debug_local_node_mismatch")
    key = repo_apps.key_for_app(project_id, str(app["id"]))
    if key and key != args["app_key"]:
        raise ValueError("debug_app_key_mismatch")
    env = project_store.project_env(project_id)
    profile = resolve_run_env_profile(env, requested=args.get("env_profile", ""))
    from mino_nexus.resources.device_targets import select
    _, targets = select(app, [args["sn"]], surface=args.get("env_surface", ""), profile=profile, headless=False, allow_queue=False)
    platform = targets[args["sn"]]["platform"]
    prepared = _call(node, "debug.prepare", {"owner_user_id": owner, "workspace": args["workspace"], "app_key": args["app_key"], "platform": platform, "env_profile": profile})
    if prepared.get("debug_api_version") != 1:
        raise ValueError("debug_api_required")
    result = run_cases(app, sn=args["sn"], platform=platform, env_profile=profile,
        env_surface=args.get("env_surface", ""), plugin_user_id=owner, client_request_id=args["client_request_id"],
        playwright_headless=False, debug={**{key: args[key] for key in ("workspace", "node_id", "app_key")}, "request": args})
    from mino_nexus.casework import run_store
    task_id = result.get("task_id") or result.get("run_id")
    doc = run_store.get(task_id)
    session = doc["cases"][0]["report_run_id"]
    return {**result, "session_id": session, "execution_kind": "debug", "workspace": args["workspace"]}


def control(session_id, operation, args):
    from mino_nexus.resources import scout_record_store
    from mino_nexus.casework import run_store
    if operation not in {"execute", "status", "cancel", "reset", "close", "checkpoint", "checkpoint_get"}:
        raise ValueError("debug_operation_invalid")
    record = scout_record_store.get(session_id)
    if not record:
        # A just-created task may not yet have reached durable Scout admission.
        doc = run_store.get(str(session_id).split("::", 1)[0])
        if not doc or doc.get("owner_user_id") != require_user_id() or doc.get("execution_kind") != "debug" or not any(c.get("report_run_id") == session_id for c in doc.get("cases", [])):
            raise ValueError("debug_session_not_found")
        if operation == "close":
            run_store.cancel_run(doc["run_id"])
            return {"task_id": doc["run_id"], "session_id": session_id, "state": "cancel_requested", "stop_confirmed": False}
        elif operation != "status":
            raise ValueError("debug_session_not_ready")
        return {"task_id": doc["run_id"], "session_id": session_id, "state": "starting" if not doc.get("finished_at") else "closed", "status": doc["status"], "cases": doc.get("cases", [])}
    if record["payload"].get("execution_kind") != "debug":
        raise ValueError("debug_session_not_found")
    if operation == "close":
        run_store.cancel_run(record["task_id"])
        scout_record_store.request_cancel(record["task_id"])
        return {"task_id": record["task_id"], "session_id": session_id, "state": "cancel_requested", "stop_confirmed": False}
    node = get_registry().get_node(record["node_id"])
    if node is None or not node.alive or not node.send or node.owner_user_id != require_user_id():
        raise ValueError("debug_node_unavailable")
    params = {**args, **{key: record[key] for key in ("task_id", "attempt_id", "session_id", "owner_user_id")}}
    data = _call(node, "debug." + operation, params, run_id=record["task_id"], sn=record["sn"], platform=record["payload"]["platform"])
    if operation == "status":
        data["formal_runs"] = run_store.debug_formal_runs(session_id)
    return {**data, "task_id": record["task_id"], "session_id": session_id}


def resolve_origin(app_id, reference, revision, env_profile):
    from mino_nexus.resources import scout_record_store
    from mino_nexus.casework import run_store
    if not isinstance(reference, dict) or set(reference) != {"session_id", "checkpoint_id"} or not all(isinstance(v, str) and v for v in reference.values()):
        raise ValueError("debug_origin_reference_invalid")
    import re
    if not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
        raise ValueError("debug_origin_fixed_commit_required")
    record = scout_record_store.get(reference["session_id"])
    parent = run_store.get(record["task_id"]) if record else None
    if not parent or parent.get("app_id") != app_id or parent.get("env_profile") != env_profile or parent.get("execution_kind") != "debug":
        raise ValueError("debug_origin_scope_mismatch")
    summary = record.get("summary", {})
    if not summary.get("sealed") or summary.get("execution_stopped") is not True or summary.get("resource_isolated"):
        raise ValueError("debug_origin_stop_required")
    receipt = control(reference["session_id"], "checkpoint_get", {"checkpoint_id": reference["checkpoint_id"]})
    if receipt.get("validation_contract") != "case_lifecycle.v1":
        raise ValueError("debug_checkpoint_validation_required")
    return P.validate_debug_origin({key: receipt[key] for key in ("session_id", "checkpoint_id", "script_path", "workspace_hash", "script_hash", "stage")})
