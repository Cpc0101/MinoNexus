"""Local authoring sessions using the existing Scout admission and record ledger."""
from __future__ import annotations

import time
from typing import Any

from mino_nexus.core import protocol as P
from mino_nexus.core.async_util import run_sync
from mino_nexus.platform.identity import require_user_id
from mino_nexus.resources.node_registry import get_registry
from mino_nexus.resources.scout_transport_owner import (
    normalize_incoming, target_owner, wire_owner,
)

_CONTROL_NODE_RECONNECT_WAIT_SEC = 10.0
_CONTROL_NODE_POLL_SEC = 0.25


def _owned_by_node_account(node: Any, owner: str, sn: str) -> bool:
    """节点账号一致即可；实体手机不归账号，插在该节点上就放行。"""
    if node is None:
        return False
    if str(getattr(node, "owner_user_id", "") or "") == owner:
        return True
    from mino_nexus.resources.access_scope import is_physical_device

    return is_physical_device(str(sn or ""))


class DebugCallError(ValueError):
    """A Scout debug call failed without conflating transport and business reasons."""

    def __init__(self, reason_code: str, message: str = "", *, transport: bool = False):
        self.reason_code = str(reason_code or "debug_node_unavailable")
        self.message = str(message or self.reason_code)
        self.transport = bool(transport)
        super().__init__(self.message)


def _call(node, capability, params, *, run_id="", sn="", platform="", timeout=15.0):
    try:
        response = run_sync(node.send(
            P.MsgType.EXECUTE,
            P.Execute(
                run_id=run_id, step_idx=-1, sn=sn, platform=platform,
                capability_id=capability, params=params, timeout_sec=timeout,
            ),
            timeout=timeout,
        ))
    except Exception as exc:
        raise DebugCallError("debug_transport_timeout", str(exc), transport=True) from exc
    if response is None or not isinstance(response, P.Result):
        raise DebugCallError("debug_transport_timeout", transport=True)
    if response.status != P.EventStatus.PASS:
        data = response.data if isinstance(response.data, dict) else {}
        reason_code = str(
            data.get("reason_code") or response.error or response.summary
            or "debug_node_unavailable"
        ).strip()
        message = str(data.get("message") or response.summary or response.error or reason_code).strip()
        raise DebugCallError(reason_code or "debug_node_unavailable", message)
    if not isinstance(response.data, dict):
        raise DebugCallError("debug_transport_timeout", transport=True)
    return dict(response.data)


def _scope(record: dict[str, Any], node: Any = None) -> dict[str, str]:
    scope = {key: str(record.get(key) or "") for key in (
        "task_id", "attempt_id", "session_id",
    )}
    scope["owner_user_id"] = wire_owner(record, node) if node is not None else str(record.get("owner_user_id") or "")
    return scope


def _terminal_debug_error(record: dict[str, Any]) -> DebugCallError | None:
    """Map a terminal debug record to the error a later control call should show."""
    from mino_nexus.resources import scout_record_store

    if record.get("status") not in scout_record_store.TERMINAL:
        return None
    summary = record.get("summary") if isinstance(record.get("summary"), dict) else {}
    if summary.get("reason_code") == "scout_restarted":
        return DebugCallError(
            "debug_session_expired",
            "Scout restarted; the debug session must be started again",
        )
    if summary.get("state") == "interrupted":
        return DebugCallError(
            "debug_session_interrupted",
            "The debug worker was interrupted",
        )
    return DebugCallError("debug_session_closed", "The debug session is closed")


def _wait_control_node(record: dict[str, Any], owner_user_id: str) -> tuple[Any, dict[str, Any]]:
    """Wait briefly through a heartbeat gap, but never mask a terminal session."""
    from mino_nexus.resources import scout_record_store

    session_id = str(record.get("session_id") or "")
    deadline = time.monotonic() + _CONTROL_NODE_RECONNECT_WAIT_SEC
    current = record
    while True:
        terminal = _terminal_debug_error(current)
        if terminal is not None:
            raise terminal
        node = get_registry().get_node(str(current.get("node_id") or ""))
        if (node is not None and node.alive and node.send
                and _owned_by_node_account(node, owner_user_id, str(current.get("sn") or ""))):
            return node, current
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(_CONTROL_NODE_POLL_SEC, remaining))
        latest = scout_record_store.get(session_id)
        if not latest:
            raise DebugCallError("debug_session_not_found", "The debug session no longer exists")
        current = latest
    terminal = _terminal_debug_error(current)
    if terminal is not None:
        raise terminal
    node = get_registry().get_node(str(current.get("node_id") or ""))
    detail = "debug_node_unavailable"
    if node is not None:
        detail += f": node={node.node_id}, last_seen_ago_sec={round(time.time() - node.last_seen, 1)}"
    raise DebugCallError("debug_node_unavailable", detail)


def _persist_status(node_id: str, data: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.resources import scout_record_store
    from mino_nexus.resources.access_scope import node_owner_id

    version = data.get("summary_version")
    if (isinstance(version, int) and not isinstance(version, bool) and version > 0
            and data.get("session_id") == fallback.get("session_id")):
        return scout_record_store.receive(node_id, normalize_incoming(fallback, data, node_owner_id(node_id)))
    return fallback


def _stopping(task_id: str, session_id: str, blocking_code: str, *, stop_confirmed: bool = False,
              lease_released: bool = False) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "session_id": session_id,
        "state": "stopping",
        "stop_confirmed": stop_confirmed,
        "lease_released": lease_released,
        "blocking_code": blocking_code,
    }


def _leases_released(task_id: str, session_id: str, owner_user_id: str) -> bool:
    """Confirm every resource holding is gone instead of trusting finish() alone."""
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.device_reservation import DeviceReservation
    from mino_nexus.models.execution_resource import ExecutionResource
    from mino_nexus.models.pool_account import PoolAccount
    from mino_nexus.models.resource_ops import DeviceResourceLease
    from mino_nexus.models.run import AppRegressionRun

    aliases = {str(value) for value in (task_id, session_id) if str(value or "")}
    with session_scope() as db:
        run = db.get(AppRegressionRun, task_id)
        if (run is None or run.owner_user_id != owner_user_id or not run.finished_at
                or str(run.status or "").lower() in {"queued", "running"}):
            return False
        resources = db.query(ExecutionResource).filter(ExecutionResource.run_id == task_id,
                                                       ExecutionResource.owner_user_id == owner_user_id).all()
        if any(str(resource.state or "") != "released" for resource in resources):
            return False
        accounts = db.query(PoolAccount).filter(PoolAccount.project_id == str(run.payload.get("project_id") or "")).all()
        if any(str((account.lease or {}).get("run_id") or "") in aliases for account in accounts):
            return False
        if db.query(DeviceResourceLease).filter(DeviceResourceLease.run_id.in_(aliases)).first():
            return False
        if db.query(DeviceReservation).filter(DeviceReservation.run_id.in_(aliases)).first():
            return False
    return True


def _close_state(record: dict[str, Any], owner_user_id: str) -> dict[str, Any]:
    """Project an already-terminal record and report the remaining close gates."""
    from mino_nexus.casework import run_store
    from mino_nexus.loop.scout_record_tasks import project
    from mino_nexus.resources import scout_record_store

    if record.get("status") in scout_record_store.TERMINAL:
        project(record)
        record = scout_record_store.get(record["session_id"]) or record
    task_id = str(record["task_id"])
    session_id = str(record["session_id"])
    stop_confirmed = scout_record_store.stop_evidence_confirmed(record)
    doc = run_store.get(task_id) or {}
    finished = bool(doc.get("finished_at")) and not run_store.task_is_live(doc)
    lease_released = finished and _leases_released(task_id, session_id, owner_user_id)
    if stop_confirmed and finished and lease_released:
        return {
            "task_id": task_id,
            "session_id": session_id,
            "state": "closed",
            "stop_confirmed": True,
            "lease_released": True,
        }
    if (not stop_confirmed and (record.get("payload") or {}).get("platform") == "web"
            and record.get("cancel_requested") is True):
        blocking = "stop_evidence_required"
    elif finished:
        blocking = "lease_release_pending"
    else:
        blocking = "run_finalize_pending"
    return _stopping(task_id, session_id, blocking, stop_confirmed=stop_confirmed,
                     lease_released=lease_released)


def _close(record: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.casework import run_store
    from mino_nexus.resources import scout_record_store

    task_id = str(record["task_id"])
    session_id = str(record["session_id"])
    owner = require_user_id()

    # Cancellation is only an intent. Resources remain fenced until Scout has
    # persisted a sealed terminal summary and any Web lane stop has been proved.
    run_store.cancel_run(task_id, release=False)
    record = scout_record_store.get(session_id) or record
    state = _close_state(record, owner)
    if state["state"] == "closed":
        return state
    record = scout_record_store.get(session_id) or record

    terminal = record.get("status") in scout_record_store.TERMINAL
    if terminal and not scout_record_store.web_stop_recovery_candidate(record):
        return state

    node = get_registry().get_node(str(record.get("node_id") or ""))
    if (node is None or not node.alive or not node.send
            or not _owned_by_node_account(node, owner, str(record.get("sn") or ""))):
        return _stopping(task_id, session_id, "stop_evidence_required")
    params = _scope(record, node)
    try:
        if not terminal:
            ack = _call(
                node, "debug.close", params, run_id=task_id, sn=str(record.get("sn") or ""),
                platform=str((record.get("payload") or {}).get("platform") or ""), timeout=15,
            )
            record = _persist_status(node.node_id, ack, record)

            deadline = time.monotonic() + 5.0
            while record.get("status") not in scout_record_store.TERMINAL:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                status = _call(
                    node, "record.status", _scope(record, node), run_id=task_id,
                    sn=str(record.get("sn") or ""),
                    platform=str((record.get("payload") or {}).get("platform") or ""),
                    timeout=max(0.5, min(2.5, remaining)),
                )
                record = _persist_status(node.node_id, status, record)
                if record.get("status") not in scout_record_store.TERMINAL:
                    time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        if record.get("status") not in scout_record_store.TERMINAL:
            return _stopping(task_id, session_id, "stop_evidence_required")

        if (not scout_record_store.stop_evidence_confirmed(record)
                and scout_record_store.web_stop_recovery_candidate(record)):
            proof = _call(
                node, "record.resource_status", _scope(record, node), run_id=task_id,
                sn=str(record.get("sn") or ""), platform="web", timeout=5,
            )
            if not scout_record_store.web_stop_proof_valid(record, proof):
                return _stopping(task_id, session_id, "stop_evidence_required")
            record = scout_record_store.save_resource_stop(session_id, proof)

        return _close_state(record, owner)
    except (DebugCallError, PermissionError, ValueError):
        return _stopping(task_id, session_id, "stop_evidence_required")


def start(app, args):
    from mino_nexus.loop.case_runner import run_cases
    from mino_nexus.resources import repo_apps, project_store
    from mino_nexus.resources.device_targets import normalize_target_sns, select
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
    env = project_store.project_env(project_id)
    profile = resolve_run_env_profile(env, requested=args.get("env_profile", ""))
    physical_sns, resolved_surface = normalize_target_sns(
        app, [args["sn"]], surface=args.get("env_surface", ""), profile=profile,
    )
    if len(physical_sns) != 1:
        raise ValueError("debug_target_invalid")
    physical_sn = physical_sns[0]
    node, _ = get_registry().resolve(physical_sn)
    if (node is None or node.node_id != args["node_id"] or not node.send
            or node.record_api_version != 1
            or not _owned_by_node_account(node, owner, physical_sn)):
        raise ValueError("debug_local_node_mismatch")
    key = repo_apps.key_for_app(project_id, str(app["id"]))
    if key and key != args["app_key"]:
        raise ValueError("debug_app_key_mismatch")
    _, targets = select(app, [physical_sn], surface=resolved_surface, profile=profile, headless=False, allow_queue=False)
    platform = targets[physical_sn]["platform"]
    prepared = _call(node, "debug.prepare", {
        "owner_user_id": target_owner(physical_sn, platform, owner, node),
        "workspace": args["workspace"], "app_key": args["app_key"],
        "platform": platform, "env_profile": profile,
    })
    if prepared.get("debug_api_version") != 1:
        raise ValueError("debug_api_required")
    result = run_cases(app, sn=physical_sn, platform=platform, env_profile=profile,
        env_surface=resolved_surface, plugin_user_id=owner, client_request_id=args["client_request_id"],
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
            run_store.cancel_run(doc["run_id"], release=False)
            return _stopping(doc["run_id"], session_id, "record_admission_pending")
        elif operation != "status":
            raise ValueError("debug_session_not_ready")
        return {"task_id": doc["run_id"], "session_id": session_id, "state": "starting" if not doc.get("finished_at") else "closed", "status": doc["status"], "cases": doc.get("cases", [])}
    if record["payload"].get("execution_kind") != "debug":
        raise ValueError("debug_session_not_found")
    if operation == "close":
        return _close(record)
    node, record = _wait_control_node(record, require_user_id())
    params = {**args, **{key: record[key] for key in ("task_id", "attempt_id", "session_id")},
              "owner_user_id": wire_owner(record, node)}
    data = _call(node, "debug." + operation, params, run_id=record["task_id"], sn=record["sn"], platform=record["payload"]["platform"])
    data = normalize_incoming(record, data, node)
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
