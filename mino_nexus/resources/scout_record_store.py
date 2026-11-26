"""Owner-scoped dispatch receipts and last acknowledged Scout summaries."""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from math import isfinite

from sqlalchemy import and_, or_, text, update

from mino_nexus.core.database import session_scope
from mino_nexus.models.scout_record import ScoutRecord
from mino_nexus.platform.identity import require_user_id

TERMINAL = frozenset({"pass", "fail", "blocked", "declined", "skipped", "cancelled"})
_WEB_STOP_MODES = frozenset({"same_process", "retired_generation"})
_TERMINAL_SCAN_CURSORS: dict[str, tuple[str, str]] = {}
_ACTIVE_HOLDINGS_SQL = """
  (EXISTS (SELECT 1 FROM execution_resources e
            WHERE e.run_id=scout_record_index.task_id AND e.state!='released')
   OR EXISTS (SELECT 1 FROM pool_accounts a
              WHERE json_extract(a.lease,'$.run_id') IN
                    (scout_record_index.task_id,scout_record_index.session_id))
   OR EXISTS (SELECT 1 FROM device_resource_leases l
              WHERE l.run_id IN (scout_record_index.task_id,scout_record_index.session_id))
   OR EXISTS (SELECT 1 FROM device_reservations v
              WHERE v.run_id=scout_record_index.task_id))
"""
_SUMMARY_FIELDS = frozenset({
    "execution_kind", "debug_state", "workspace_hash", "debug_origin_matched",
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


def view(record: ScoutRecord | dict[str, Any]) -> dict[str, Any]:
    """Return an immutable snapshot for ORM rows or already projected records."""
    return _view(record) if isinstance(record, ScoutRecord) else deepcopy(record)


def web_stop_proof_valid(record: dict[str, Any], proof: dict[str, Any] | None) -> bool:
    """Validate a fresh Web stop receipt against this exact durable attempt."""
    if not isinstance(proof, dict) or record.get("status") not in TERMINAL:
        return False
    summary = record.get("summary") or {}
    payload = record.get("payload") or {}
    stop = proof.get("resource_stop")
    if (record.get("status") != proof.get("status")
            or payload.get("platform") != "web" or proof.get("platform") != "web"
            or summary.get("platform") != "web"
            or summary.get("status") != record.get("status")
            or summary.get("sealed") is not True
            or proof.get("sealed") is not True
            or not isinstance(stop, dict)):
        return False
    for key in ("task_id", "session_id", "attempt_id", "owner_user_id", "node_id", "sn", "case_id"):
        expected = str(record.get(key) or "")
        if not expected or str(proof.get(key) or "") != expected:
            return False
    version = proof.get("summary_version")
    if (type(version) is not int or version < 1
            or version != int(record.get("summary_version") or 0)
            or type(summary.get("summary_version")) is not int
            or summary.get("summary_version") != version):
        return False
    summary_generation = summary.get("scout_process_generation")
    if (not isinstance(summary_generation, str) or not summary_generation.strip()
            or proof.get("scout_process_generation") != summary_generation):
        return False
    generation = stop.get("scout_process_generation")
    started = stop.get("process_started_at")
    mode = stop.get("mode")
    if (stop.get("contract") != "script_resource_stop.v1"
            or stop.get("stop_confirmed") is not True
            or stop.get("worker_active") is not False
            or stop.get("browser_closed") is not True
            or stop.get("reason_code") != "web_lane_stopped"
            or not isinstance(mode, str) or mode not in _WEB_STOP_MODES
            or not isinstance(generation, str) or not generation.strip()
            or type(started) not in (int, float) or not isfinite(started) or started <= 0):
        return False
    uncertain = (execution_outcome_unknown(record) or resource_isolated(record)
                 or summary.get("execution_stopped") is not True or summary.get("browser_closed") is not True)
    audit = proof.get("retirement_write_audit") or {}
    if not isinstance(audit, dict) or (audit.get("contract") is not None and not isinstance(audit["contract"], str)):
        return False
    reviewed = audit.get("contract") == "retired_write_review.v1"
    if reviewed and (mode != "retired_generation" or audit.get("reviewed_by") != record.get("owner_user_id")
                     or audit.get("historical_outcome_preserved") is not True
                     or type(audit.get("unknown_write_actions")) is not int
                     or audit["unknown_write_actions"] < 0):
        return False
    if uncertain or reviewed:
        lifetime_unknown = (execution_outcome_unknown(record) or summary.get("execution_stopped") is not True
                            or summary.get("browser_closed") is not True)
        if ((lifetime_unknown and mode != "retired_generation")
                or audit.get("contract") not in {"sealed_write_audit.v1", "retired_write_review.v1"}
                or type(audit.get("summary_version")) is not int or audit["summary_version"] != version
                or type(audit.get("last_event_seq")) is not int or audit["last_event_seq"] < 1
                or type(summary.get("last_event_seq")) is not int
                or audit["last_event_seq"] != summary["last_event_seq"]
                or type(audit.get("open_write_actions")) is not int or audit["open_write_actions"] < 0):
            return False
        if reviewed:
            if audit["unknown_write_actions"] < audit["open_write_actions"]:
                return False
        elif audit["open_write_actions"] != 0:
            return False
    if mode == "same_process" and (proof.get("execution_stopped") is not True or proof.get("browser_closed") is not True):
        return False
    if mode == "same_process":
        return generation == summary_generation
    return generation != summary_generation


def resource_isolated(record: dict[str, Any]) -> bool:
    """Isolation flags a possibly-committed business write, not a live worker."""
    summary = record.get("summary") or {}
    return summary.get("resource_isolated") is True


def execution_outcome_unknown(record: dict[str, Any]) -> bool:
    """Only unknown/interrupted execution blocks stop proof; isolation alone does not."""
    summary = record.get("summary") or {}
    return (summary.get("execution_started") == "unknown"
            or summary.get("state") == "interrupted"
            or summary.get("reason_code") == "write_outcome_unknown")


def stop_evidence_confirmed(record: dict[str, Any]) -> bool:
    """Whether execution and Web browser lifetime are known to have stopped."""
    summary = record.get("summary") or {}
    if record.get("status") not in TERMINAL or summary.get("sealed") is not True:
        return False
    if (record.get("payload") or {}).get("platform") == "web":
        proof = (record.get("context") or {}).get("resource_stop")
        if web_stop_proof_valid(record, proof):
            return True
    if execution_outcome_unknown(record):
        return False
    if summary.get("execution_started") == "no":
        return True
    if summary.get("execution_stopped") is not True:
        return False
    if (record.get("payload") or {}).get("platform") == "web":
        return False
    return True


def release_evidence_confirmed(record: dict[str, Any]) -> bool:
    """Conservative gate shared by projection, recovery, and run finalization."""
    if record.get("status") not in TERMINAL:
        return False
    summary = record.get("summary") or {}
    if summary.get("sealed") is not True:
        return False
    resolution = (record.get("context") or {}).get("resource_resolution") or {}
    if resolution.get("state") == "released":
        return True
    if execution_outcome_unknown(record) or resource_isolated(record):
        return False
    if summary.get("execution_started") == "no":
        return True
    if summary.get("execution_stopped") is not True:
        return False
    if summary.get("browser_closed") is False:
        return False
    if ((record.get("payload") or {}).get("platform") == "web"
            and record.get("cancel_requested") is True):
        return stop_evidence_confirmed(record)
    return True


def web_stop_recovery_candidate(record: dict[str, Any]) -> bool:
    """Whether a terminal Web record still needs a stop receipt fetched from Scout."""
    summary = record.get("summary") or {}
    if (record.get("status") not in TERMINAL or record.get("cancel_requested") is not True
            or (record.get("payload") or {}).get("platform") != "web"):
        return False
    unresolved_retirement = ((execution_outcome_unknown(record) or resource_isolated(record))
                             and ((record.get("context") or {}).get("resource_resolution") or {}).get("state") != "released")
    return not stop_evidence_confirmed(record) or unresolved_retirement


def save_resource_stop(session_id: str, proof: dict[str, Any]) -> dict[str, Any]:
    """Persist only a validated, version-matched Web stop receipt."""
    owner = require_user_id()
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(ScoutRecord, str(session_id))
        if row is None or row.owner_user_id != owner:
            raise PermissionError("record_not_found")
        current = _view(row)
        if not web_stop_proof_valid(current, proof):
            raise ValueError("stop_evidence_invalid")
        previous = (row.context or {}).get("resource_stop") or {}
        if int(previous.get("summary_version") or 0) > int(proof["summary_version"]):
            raise ValueError("stop_evidence_stale")
        stop = proof.get("resource_stop") or {}
        snapshot = {
            key: deepcopy(proof.get(key)) for key in (
                "task_id", "session_id", "attempt_id", "owner_user_id", "node_id", "sn",
                "case_id", "platform", "status", "summary_version", "sealed",
                "execution_stopped", "browser_closed", "scout_process_generation",
                "resource_isolated",
            )
        }
        snapshot.update(resource_stop=deepcopy(stop), retirement_write_audit=deepcopy(proof.get("retirement_write_audit")), recorded_at=_now())
        row.context = {**(row.context or {}), "resource_stop": snapshot}
        row.updated_at = _now()
        db.flush()
        return _view(row)


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


def retire_attempt(session_id: str, *, reason_code: str, evidence: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Terminate an attempt whose worker generation is gone.

    Nothing is sealed: no Scout ever reported a final result for this attempt, so
    ``sealed`` stays false and every downstream consumer that demands sealed
    evidence keeps refusing to treat it as a completed case.
    """
    with session_scope() as db:
        row = db.get(ScoutRecord, str(session_id or ""))
        if row is None:
            return None
        if row.status in TERMINAL:
            return _view(row)
        row.status = "cancelled"
        row.summary = {**(row.summary or {}), **{k: v for k, v in (evidence or {}).items() if v is not None},
                       "status": "cancelled", "state": "cancelled", "sealed": False,
                       "execution_stopped": False, "browser_closed": False,
                       "reason_code": reason_code, "worker_generation_retired": True,
                       "detail_availability": "unavailable"}
        row.summary_version = int(row.summary_version or 0) + 1
        row.cancel_requested = True
        row.updated_at = _now()
        db.flush()
        return _view(row)


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
        if version < row.summary_version or (version == row.summary_version and row.summary.get("sealed") is True):
            return _view(row)
        if row.status in TERMINAL and row.summary.get("sealed") is True and status != row.status:
            raise ValueError("record_terminal_conflict")
        if row.status in TERMINAL and row.summary.get("sealed") is True and any(key in row.summary and row.summary[key] != incoming.get(key)
                                         for key in ("state", "reason_code", "execution_started", "resource_isolated",
                                                     "execution_stopped", "browser_closed", "scout_process_generation")):
            raise ValueError("record_terminal_conflict")
        summary = {key: deepcopy(value) for key, value in incoming.items() if key in _SUMMARY_FIELDS}
        from mino_nexus.core.protocol import validate_data_summary
        summary.update(validate_data_summary(summary))
        if row.status in TERMINAL and row.summary.get("sealed") is True and any(row.summary.get(key, default) != summary[key] for key, default in (("data_outputs", {}), ("data_missing", []))):
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
        if row.status in TERMINAL and row.summary.get("sealed") is True and any(row.summary.get(key) != summary.get(key) for key in ("test_report", "assertions", "source_coverage", "source_hash", "revision", "started_at", "finished_at", "elapsed_ms", "last_event_seq", "run_purpose")):
            raise ValueError("record_terminal_conflict")
        # No arbitrary nested logs or screenshots may be hidden in a summary.
        import json
        if len(json.dumps(summary, ensure_ascii=False).encode()) > 131072:
            raise ValueError("record_summary_too_large")
        db.execute(update(ScoutRecord).where(ScoutRecord.session_id == sid, ScoutRecord.summary_version <= version)
                   .values(status=status, summary_version=version, summary=summary, updated_at=_now()))
        db.flush()
        db.expire(row)
        return _view(row)


def request_cancel(task_id: str) -> list[dict[str, Any]]:
    owner = require_user_id()
    with session_scope() as db:
        rows = db.query(ScoutRecord).filter_by(task_id=task_id, owner_user_id=owner).all()
        for row in rows:
            if row.status not in TERMINAL or ((row.payload or {}).get("execution_kind") == "debug"
                    and (row.payload or {}).get("platform") == "web" and not stop_evidence_confirmed(_view(row))):
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
        current = _view(latest)
        if not release_evidence_confirmed(current):
            return
        request = current["context"].get("resource_request") or {}
        if not request:
            return
        run = db.get(AppRegressionRun, current["task_id"])
        # Expiry alone is not an unknown write when this exact attempt has a
        # sealed, safe result. Actual pending/isolated write intents still win.
        isolated = bool(unresolved(run.payload if run else {}, current["sn"]))
        for key, epoch in (request.get("resource_epochs") or {}).items():
            row = db.get(ExecutionResource, key)
            if (row is None or row.epoch != epoch or row.attempt_id != request.get("attempt_id")
                    or row.run_id != current["task_id"] or row.owner_user_id != current["owner_user_id"]):
                continue
            row.state = "isolated" if isolated else "released"
            if not isolated:
                row.expires_at = 0


def _terminal_scan_page(
    scan_key: str,
    limit: int,
    predicate: Callable[[dict[str, Any]], bool],
) -> list[dict[str, Any]]:
    """Scan bounded pages with a keyset cursor so old invalid rows cannot starve later work."""
    page_size = max(1, limit) * 8
    cursor = _TERMINAL_SCAN_CURSORS.get(scan_key)

    def query_page(db):
        query = (db.query(ScoutRecord)
                 .filter(ScoutRecord.status.in_(TERMINAL), text(_ACTIVE_HOLDINGS_SQL)))
        if cursor:
            updated_at, session_id = cursor
            query = query.filter(or_(
                ScoutRecord.updated_at > updated_at,
                and_(ScoutRecord.updated_at == updated_at, ScoutRecord.session_id > session_id),
            ))
        return (query.order_by(ScoutRecord.updated_at, ScoutRecord.session_id)
                .distinct().limit(page_size).all())

    with session_scope() as db:
        rows = query_page(db)
        if not rows and cursor:
            _TERMINAL_SCAN_CURSORS.pop(scan_key, None)
            cursor = None
            rows = query_page(db)
        page = [_view(row) for row in rows]

    if not page:
        return []
    candidates = [row for row in page if predicate(row)]
    selected = candidates[:max(1, limit)]
    marker = selected[-1] if selected else page[-1]
    _TERMINAL_SCAN_CURSORS[scan_key] = (str(marker["updated_at"]), str(marker["session_id"]))
    return selected


def terminal_records_for_settlement(limit: int = 32) -> list[dict[str, Any]]:
    """Bounded recovery of committed terminal results whose release was interrupted."""
    return _terminal_scan_page("settlement", limit, release_evidence_confirmed)


def terminal_records_for_recovery(limit: int = 32) -> list[dict[str, Any]]:
    """Bounded scan for canceled Web attempts that need a fresh stop receipt."""
    return _terminal_scan_page("recovery", limit, web_stop_recovery_candidate)


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
