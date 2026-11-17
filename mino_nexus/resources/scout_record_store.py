"""Owner-scoped dispatch receipts and last acknowledged Scout summaries."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import update, text

from mino_nexus.core.database import session_scope
from mino_nexus.models.scout_record import ScoutRecord
from mino_nexus.platform.identity import require_user_id

TERMINAL = frozenset({"pass", "fail", "blocked", "declined", "skipped", "cancelled"})
_SUMMARY_FIELDS = frozenset({
    "task_id", "run_id", "session_id", "attempt_id", "case_id", "owner_user_id", "node_id", "sn", "platform",
    "revision", "script_path", "state", "status", "summary", "reason_code", "failure_stage", "elapsed_ms",
    "execution_started", "started_at", "finished_at", "summary_version", "last_event_seq", "sealed",
    "detail_availability", "assertions", "source_coverage", "source_hash", "resource_isolated", "test_report",
    "execution_stopped", "browser_closed", "scout_process_generation", "data_outputs", "data_missing", "run_purpose",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _view(row: ScoutRecord) -> dict[str, Any]:
    out = {key: getattr(row, key) for key in (
        "session_id", "attempt_id", "task_id", "owner_user_id", "node_id", "case_id", "sn", "status",
        "summary_version", "cancel_requested", "updated_at",
    )}
    out.update(payload=deepcopy(row.payload), context=deepcopy(row.context), summary=deepcopy(row.summary))
    return out


def register(*, node_id: str, payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    owner = require_user_id()
    sid = str(payload.get("session_id") or "")
    attempt = str(payload.get("attempt_id") or sid)
    task = str(payload.get("task_id") or payload.get("run_id") or "")
    if not all((sid, attempt, task, node_id)) or payload.get("owner_user_id") != owner:
        raise ValueError("record_identity_invalid")
    frozen = deepcopy(payload)
    frozen.update(attempt_id=attempt, task_id=task)
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        previous = db.get(ScoutRecord, sid)
        if previous:
            if previous.owner_user_id != owner or previous.node_id != node_id or previous.payload != frozen:
                raise ValueError("record_submission_conflict")
            return _view(previous)
        row = ScoutRecord(session_id=sid, attempt_id=attempt, task_id=task, owner_user_id=owner,
                          node_id=node_id, case_id=str(payload.get("case_id") or ""), sn=str(payload.get("sn") or ""),
                          status="queued", summary_version=0, cancel_requested=False,
                          payload=frozen, context=deepcopy(context), summary={}, updated_at=_now())
        db.add(row)
        db.flush()
        return _view(row)


def get(session_id: str, owner_user_id: str | None = None) -> dict[str, Any] | None:
    owner = require_user_id(owner_user_id)
    with session_scope() as db:
        row = db.get(ScoutRecord, str(session_id))
        return _view(row) if row and row.owner_user_id == owner else None


def for_node(session_id: str, node_id: str) -> dict[str, Any] | None:
    """Only for authenticated node callbacks, not an HTTP ownership bypass."""
    if not node_id:
        return None
    with session_scope() as db:
        row = db.get(ScoutRecord, str(session_id))
        return _view(row) if row and row.node_id == node_id else None


def for_task(task_id: str) -> list[dict[str, Any]]:
    owner = require_user_id()
    with session_scope() as db:
        rows = db.query(ScoutRecord).filter_by(task_id=task_id, owner_user_id=owner).all()
        return [_view(row) for row in rows]


def pending_for_node(node_id: str) -> list[dict[str, Any]]:
    """Transport recovery has an authenticated node identity but no request user."""
    with session_scope() as db:
        rows = db.query(ScoutRecord).filter(ScoutRecord.node_id == node_id,
                                          ~ScoutRecord.status.in_(TERMINAL)).limit(200).all()
        return [_view(row) for row in rows]


def receive(node_id: str, incoming: dict[str, Any]) -> dict[str, Any]:
    sid = str(incoming.get("session_id") or "")
    version = incoming.get("summary_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("record_version_invalid")
    status = str(incoming.get("status") or "")
    if status not in TERMINAL | {"queued", "running"}:
        raise ValueError("record_status_invalid")
    if status in TERMINAL and incoming.get("sealed") is not True:
        raise ValueError("record_terminal_unsealed")
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(ScoutRecord, sid)
        if row is None or row.node_id != node_id:
            raise PermissionError("record_not_found")
        for key in ("task_id", "attempt_id", "case_id", "owner_user_id", "sn"):
            if str(incoming.get(key) or "") != str(getattr(row, key)):
                raise PermissionError("record_owner_mismatch")
        if str(incoming.get("run_id") or "") != str(row.payload.get("run_id") or ""):
            raise ValueError("record_run_mismatch")
        for key in ("platform", "script_path"):
            if str(incoming.get(key) or "") != str(row.payload.get(key) or ""):
                raise PermissionError("record_owner_mismatch")
        if str(incoming.get("node_id") or "") != node_id:
            raise PermissionError("record_owner_mismatch")
        if version <= row.summary_version:
            return _view(row)
        if row.status in TERMINAL and status != row.status:
            raise ValueError("record_terminal_conflict")
        if row.status in TERMINAL and any(key in row.summary and row.summary[key] != incoming.get(key)
                                         for key in ("state", "reason_code", "execution_started", "resource_isolated",
                                                     "execution_stopped", "browser_closed", "scout_process_generation")):
            raise ValueError("record_terminal_conflict")
        summary = {key: deepcopy(value) for key, value in incoming.items() if key in _SUMMARY_FIELDS}
        from mino_nexus.core.protocol import validate_data_summary
        summary.update(validate_data_summary(summary))
        if row.status in TERMINAL and any(row.summary.get(key, default) != summary[key] for key, default in (("data_outputs", {}), ("data_missing", []))):
            raise ValueError("record_terminal_conflict")
        if "test_report" in summary:
            from mino_nexus.contract.business_report import normalize_test_report
            summary["test_report"] = normalize_test_report(summary["test_report"])
        for key in ("summary", "reason_code", "failure_stage"):
            summary[key] = str(summary.get(key) or "")[:1000 if key == "summary" else 120]
        for key in ("started_at", "finished_at"):
            value = summary.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                try:
                    summary[key] = datetime.fromtimestamp(value / 1000 if value > 1e12 else value, timezone.utc).isoformat() if value else ""
                except (ValueError, OverflowError, OSError):
                    raise ValueError("record_timestamp_invalid") from None
        if isinstance(summary.get("assertions"), dict):
            raw_counts = summary["assertions"]
            aliases = {"total": "assertions", "pass": "passed", "fail": "failed", "unknown": "inconclusive"}
            summary["assertions"] = {key: raw_counts.get(key, raw_counts.get(alias)) for key, alias in aliases.items()}
        from mino_nexus.contract.business_report import validate_report_summary
        validate_report_summary(summary, row.payload)
        if row.status in TERMINAL and any(row.summary.get(key) != summary.get(key) for key in ("test_report", "assertions", "source_coverage", "source_hash", "revision", "started_at", "finished_at", "elapsed_ms", "last_event_seq", "run_purpose")):
            raise ValueError("record_terminal_conflict")
        # No arbitrary nested logs or screenshots may be hidden in a summary.
        import json
        if len(json.dumps(summary, ensure_ascii=False).encode()) > 131072:
            raise ValueError("record_summary_too_large")
        db.execute(update(ScoutRecord).where(ScoutRecord.session_id == sid, ScoutRecord.summary_version < version)
                   .values(status=status, summary_version=version, summary=summary, updated_at=_now()))
        db.flush()
        db.expire(row)
        return _view(row)


def request_cancel(task_id: str) -> list[dict[str, Any]]:
    owner = require_user_id()
    with session_scope() as db:
        rows = db.query(ScoutRecord).filter_by(task_id=task_id, owner_user_id=owner).all()
        for row in rows:
            if row.status not in TERMINAL:
                row.cancel_requested = True
                row.updated_at = _now()
        db.flush()
        return [_view(row) for row in rows]


def reject_unstarted(session_id: str, reason_code: str, *, status: str = "blocked") -> dict[str, Any]:
    """Persist an explicit admission rejection, never a transport timeout."""
    owner = require_user_id()
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(ScoutRecord, session_id)
        if row is None or row.owner_user_id != owner:
            raise PermissionError("record_not_found")
        if row.summary_version or row.status in TERMINAL:
            return _view(row)
        row.status = status
        row.summary = {key: row.payload.get(key, "") for key in (
            "task_id", "run_id", "session_id", "attempt_id", "owner_user_id", "case_id", "sn")}
        row.summary.update(node_id=row.node_id, status=status, state="completed", sealed=True,
                           execution_started="no", summary=reason_code, reason_code=reason_code,
                           detail_availability="missing", failure_stage="admission", resource_isolated=False)
        row.updated_at = _now()
        db.flush()
        return _view(row)


def resumable_task_ids() -> list[str]:
    """Startup recovery only; each caller must bind the persisted task owner."""
    with session_scope() as db:
        from mino_nexus.models.run import AppRegressionRun
        rows = db.query(ScoutRecord.task_id).join(AppRegressionRun, AppRegressionRun.run_id == ScoutRecord.task_id).filter(
            AppRegressionRun.status.in_({"running", "queued"})).distinct().all()
        return [row[0] for row in rows]


def settle_resources(record: dict[str, Any]) -> None:
    """A sealed worker result can release its exact resource epoch after server downtime."""
    if record["status"] not in TERMINAL or not record["summary"].get("sealed"):
        return
    from mino_nexus.models.execution_resource import ExecutionResource
    from mino_nexus.models.run import AppRegressionRun
    from mino_nexus.records.write_state import unresolved
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        latest = db.get(ScoutRecord, record["session_id"])
        if latest is None or latest.owner_user_id != record["owner_user_id"] or latest.status not in TERMINAL:
            return
        request = latest.context.get("resource_request") or {}
        if not request or not latest.summary.get("sealed"):
            return
        run = db.get(AppRegressionRun, latest.task_id)
        summary = latest.summary
        resolved = latest.context.get("resource_resolution", {}).get("state") == "released"
        isolated = (not resolved and (summary.get("resource_isolated") is True
                    or summary.get("execution_started") == "unknown" or summary.get("state") == "interrupted"
                    or summary.get("reason_code") == "write_outcome_unknown"
                    or summary.get("execution_stopped") is False or summary.get("browser_closed") is False))
        # Expiry alone is not an unknown write when this exact attempt has a
        # sealed, safe result. Actual pending/isolated write intents still win.
        isolated = isolated or bool(unresolved(run.payload if run else {}, record["sn"]))
        for key, epoch in (request.get("resource_epochs") or {}).items():
            row = db.get(ExecutionResource, key)
            if (row is None or row.epoch != epoch or row.attempt_id != request.get("attempt_id")
                    or row.run_id != record["task_id"] or row.owner_user_id != record["owner_user_id"]):
                continue
            row.state = "isolated" if isolated else "released"
            if not isolated:
                row.expires_at = 0


def terminal_records_for_settlement(limit: int = 32) -> list[dict[str, Any]]:
    """Bounded recovery of committed terminal results whose release was interrupted."""
    with session_scope() as db:
        rows = (db.query(ScoutRecord).filter(ScoutRecord.status.in_(TERMINAL), text("""
          (COALESCE(json_extract(scout_record_index.summary,'$.resource_isolated'),0)!=1
            AND COALESCE(json_extract(scout_record_index.summary,'$.execution_started'),'')!='unknown'
            AND COALESCE(json_extract(scout_record_index.summary,'$.state'),'')!='interrupted'
            AND COALESCE(json_extract(scout_record_index.summary,'$.reason_code'),'')!='write_outcome_unknown'
            AND COALESCE(json_extract(scout_record_index.summary,'$.execution_stopped'),1)!=0
            AND COALESCE(json_extract(scout_record_index.summary,'$.browser_closed'),1)!=0
            OR json_extract(scout_record_index.context,'$.resource_resolution.state')='released')
          AND (EXISTS (SELECT 1 FROM execution_resources e WHERE e.run_id=scout_record_index.task_id AND e.state!='released')
            OR EXISTS (SELECT 1 FROM pool_accounts a WHERE json_extract(a.lease,'$.run_id') IN
              (scout_record_index.task_id,scout_record_index.session_id))
            OR EXISTS (SELECT 1 FROM device_resource_leases l WHERE l.run_id IN (scout_record_index.task_id,scout_record_index.session_id))
            OR EXISTS (SELECT 1 FROM device_reservations v WHERE v.run_id=scout_record_index.task_id))
        """))
                .order_by(ScoutRecord.updated_at).distinct().limit(limit).all())
        return [_view(row) for row in rows]


def save_resources(session_id: str, request: dict[str, Any]) -> None:
    owner = require_user_id()
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(ScoutRecord, session_id)
        original = (row.context or {}).get("resource_request") if row else None
        if (row is None or row.owner_user_id != owner or not original
                or original.get("attempt_id") != request.get("attempt_id")):
            raise PermissionError("resource_attempt_mismatch")
        row.context = {**row.context, "resource_request": deepcopy(request)}
