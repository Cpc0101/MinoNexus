"""Settle runs whose Scout worker generation is demonstrably gone.

A debug run keeps its device and account leases forever when the Scout process
that owned its record dies: no terminal record is ever written, so no
projection can settle the run. Lease TTL expiry is deliberately not treated as
proof anywhere else, so expiry alone must not settle it either.

The only safe evidence is a reachable node whose current ``scout_process_generation``
differs from the generation stamped on the record. That says the process holding
the attempt is gone; it says nothing about business data, so a run that still has
unresolved write actions keeps its isolation and only the stop-proof path can
release it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

RETIRED_REASON = "worker_generation_retired"
STALE_AFTER_SEC = 120.0


def _epoch(value: Any) -> float:
    try:
        return datetime.fromisoformat(str(value or "")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def retired_records(doc: dict[str, Any] | None) -> tuple[list[dict[str, Any]], str]:
    """Return (retired non-terminal records, reason). An empty list means no proof."""
    from mino_nexus.resources import scout_record_store as records
    from mino_nexus.resources.node_registry import get_registry

    task_id = str((doc or {}).get("run_id") or "")
    if not task_id:
        return [], "task_id_missing"
    all_rows = records.for_task(task_id)
    rows = [row for row in all_rows if row.get("status") not in records.TERMINAL]
    if not all_rows:
        return [], "no_live_record"
    # A worker can finish writing its terminal record right after the run doc was
    # read; the projection then never fires and the run stays "running".
    if not rows:
        return [], "records_terminal"
    now = datetime.now(timezone.utc).timestamp()
    out: list[dict[str, Any]] = []
    for row in rows:
        node, _ = get_registry().resolve(str(row.get("sn") or ""))
        if node is None or not callable(getattr(node, "send", None)) or not getattr(node, "alive", False):
            return [], "worker_node_unreachable"
        generation = str((row.get("summary") or {}).get("scout_process_generation") or "")
        current = str(getattr(node, "scout_process_generation", "") or "")
        if not generation or not current:
            return [], "worker_generation_missing"
        if generation == current:
            return [], "worker_still_live"
        age = now - _epoch(row.get("updated_at"))
        if age < STALE_AFTER_SEC:
            return [], "worker_record_fresh"
        out.append({
            "session_id": str(row.get("session_id") or ""),
            "attempt_id": str(row.get("attempt_id") or ""),
            "case_id": str(row.get("case_id") or ""),
            "sn": str(row.get("sn") or ""),
            "node_id": str(row.get("node_id") or ""),
            "record_generation": generation,
            "node_generation": current,
            "record_status": str(row.get("status") or ""),
            "record_updated_at": str(row.get("updated_at") or ""),
            "record_age_sec": round(age, 1),
        })
    return out, RETIRED_REASON


def settle_terminal(task_id: str) -> dict[str, Any]:
    """Settle a live run whose Scout records are all terminal already."""
    from mino_nexus.casework import run_store
    from mino_nexus.casework.run_write_safety import protected
    from mino_nexus.resources import scout_record_store as records

    doc = run_store.get(task_id)
    if not doc:
        raise ValueError("task_not_found")
    if not run_store.task_is_live(doc):
        return {"task_id": task_id, "finalized": False, "reason_code": "task_already_finished"}
    rows = records.for_task(task_id)
    if not rows or any(row.get("status") not in records.TERMINAL for row in rows):
        return {"task_id": task_id, "finalized": False, "reason_code": "records_still_open"}
    # A sealed summary is Scout's own durable final word. A terminal row that was
    # never sealed (retired worker, or a record closed after its worker died) has no
    # stop proof that can ever arrive, so it must not be settled on that evidence.
    unsealed = [str(row.get("session_id") or "") for row in rows
                if (row.get("summary") or {}).get("sealed") is not True]
    if unsealed:
        return {"task_id": task_id, "finalized": False, "reason_code": "records_not_sealed",
                "unsealed_sessions": unsealed}
    statuses = [str(row.get("status") or "") for row in rows]
    status = "failed" if "failed" in statuses else ("cancelled" if "cancelled" in statuses else "done")
    isolated = bool(protected(task_id))
    finished = run_store.finish(
        task_id, status=status, reason_code="record_terminal_missed_projection",
        error="Scout 记录已终结但任务未投影；按记录终态结算", release=not isolated,
        terminal_records_confirmed=True)
    return {
        "task_id": task_id, "finalized": True, "status": str(finished.get("status") or ""),
        "reason_code": "record_terminal_missed_projection",
        "terminal_records": [{"session_id": row.get("session_id"), "status": row.get("status")} for row in rows],
        "resource_isolated": bool(protected(task_id)),
        "business_result_unchanged": True,
        "isolation_exit": {"committed": False, "blocked_reason": "baseline_recheck_required" if isolated else "",
                           "release_path": "write-safety/baseline" if isolated else ""},
    }


def finalize(task_id: str, retired: list[dict[str, Any]]) -> dict[str, Any]:
    """Mark a proven-dead run terminal and settle whatever can be settled."""
    from mino_nexus.casework import run_store
    from mino_nexus.casework.run_write_safety import protected, unresolved
    from mino_nexus.resources import scout_record_store as records

    doc = run_store.get(task_id)
    if not doc:
        raise ValueError("task_not_found")
    if not run_store.task_is_live(doc):
        return {"task_id": task_id, "finalized": False, "reason_code": "task_already_finished"}
    isolated = bool(unresolved(doc))
    for item in retired:
        records.retire_attempt(str(item["session_id"]), reason_code=RETIRED_REASON, evidence={
            "record_generation": item.get("record_generation"),
            "node_generation": item.get("node_generation"),
            "record_updated_at": item.get("record_updated_at"),
        })
    finished = run_store.finish(
        task_id, status="cancelled", reason_code=RETIRED_REASON,
        error="Scout 进程代际已退出，任务按终止结算；未结算的写动作仍保持隔离",
        retired_workers=retired, release=not isolated,
    )
    return {
        "task_id": task_id,
        "finalized": True,
        "status": str(finished.get("status") or ""),
        "reason_code": RETIRED_REASON,
        "retired_workers": retired,
        "resource_isolated": bool(protected(task_id)),
        "business_result_unchanged": True,
        "isolation_exit": {
            "committed": False,
            "blocked_reason": "baseline_recheck_required" if isolated else "",
            "release_path": "write-safety/baseline" if isolated else "",
        },
    }
