"""Explicit Web lane retirement; original Scout verdicts remain immutable."""
from __future__ import annotations

from copy import deepcopy
import time
from datetime import datetime, timezone

from mino_nexus.core.log import SLog
from mino_nexus.platform.identity import require_user_id
from mino_nexus.resources import scout_record_store as records
from mino_nexus.casework import run_store


_RECOVERY_RETRY_AFTER_SEC = 30.0
_RECOVERY_RETRY_AT: dict[str, float] = {}


def _recovery_due(row: dict, now: float) -> bool:
    key = f"{row['session_id']}:{row.get('summary_version') or 0}"
    previous = _RECOVERY_RETRY_AT.get(key, 0.0)
    if previous and now < previous:
        return False
    _RECOVERY_RETRY_AT[key] = now + _RECOVERY_RETRY_AFTER_SEC
    if len(_RECOVERY_RETRY_AT) > 512:
        cutoff = now - 300.0
        for stale in [item for item, retry_at in _RECOVERY_RETRY_AT.items()
                      if retry_at - _RECOVERY_RETRY_AFTER_SEC < cutoff]:
            _RECOVERY_RETRY_AT.pop(stale, None)
    return True


def reconcile_task_resources(task_id: str) -> dict:
    actor = require_user_id()
    doc = run_store.get(task_id)
    if not doc or doc.get("owner_user_id") != actor:
        raise ValueError("task_not_found")
    if run_store.task_is_live(doc) or not doc.get("finished_at"):
        raise ValueError("task_still_running")
    if doc.get("resource_resolution"):
        return run_store.to_task_json(doc)
    rows = records.for_task(task_id)
    if not rows:
        raise ValueError("record_not_found")
    if any(row["payload"].get("platform") != "web" for row in rows):
        raise ValueError("resource_baseline_required")
    from mino_nexus.loop.scout_stop_evidence import fetch_stop_evidence
    proofs = []
    refreshed = []
    for row in rows:
        if not row["summary_version"] and row["summary"].get("execution_started") == "no":
            refreshed.append(row)
            continue
        row, proof = fetch_stop_evidence(row)
        refreshed.append(row)
        if row["status"] not in records.TERMINAL or row["summary"].get("sealed") is not True:
            raise ValueError("stop_evidence_required")
        if not row["summary_version"] and row["summary"].get("execution_started") == "no":
            continue  # Persisted rejection before any Scout acceptance.
        stop = proof.get("resource_stop") or {}
        if not records.web_stop_proof_valid(row, proof):
            raise ValueError("stop_evidence_required")
        refreshed[-1] = records.save_resource_stop(row["session_id"], proof)
        proofs.append({"attempt_id": row["attempt_id"], "summary_version": row["summary_version"], "resource_stop": stop,
                       "retirement_write_audit": proof.get("retirement_write_audit")})
    if not proofs:
        raise ValueError("stop_evidence_required")
    return _commit(task_id, actor, refreshed, proofs)


def _commit(task_id: str, actor: str, rows: list[dict], proofs: list[dict]) -> dict:
    from sqlalchemy import text
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.run import AppRegressionRun
    from mino_nexus.models.scout_record import ScoutRecord
    from mino_nexus.models.execution_resource import ExecutionResource
    from mino_nexus.records.write_state import unresolved

    with run_store._LOCK:
        with session_scope() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            run = db.get(AppRegressionRun, task_id)
            if run is None or run.owner_user_id != actor:
                raise ValueError("task_not_found")
            doc = deepcopy(run.payload or {})
            if doc.get("resource_resolution"):
                return run_store.to_task_json(doc)
            if run_store.task_is_live(doc) or not run.finished_at:
                raise ValueError("task_still_running")
            indexed = db.query(ScoutRecord).filter_by(task_id=task_id, owner_user_id=actor).all()
            snapshots = {row["attempt_id"]: row for row in rows}
            if len(indexed) != len(snapshots):
                raise ValueError("resource_resolution_conflict")
            for record in indexed:
                old = snapshots.get(record.attempt_id)
                if (old is None or record.summary_version != old["summary_version"]
                        or record.status not in records.TERMINAL or record.summary.get("sealed") is not True
                        or record.node_id != old["node_id"] or record.context != old["context"]):
                    raise ValueError("resource_resolution_conflict")
            attempts = set(snapshots)
            if any(action.get("attempt_id") not in attempts for action in unresolved(doc)):
                raise ValueError("resource_baseline_required")
            holdings = db.query(ExecutionResource).filter_by(run_id=task_id, owner_user_id=actor).all()
            for resource in holdings:
                if resource.state != "released" and not _owns_epoch(resource, indexed):
                    raise ValueError("resource_resolution_conflict")
            resolution = {"state": "released", "verification_kind": "web_lane_stop", "actor": actor,
                          "resolved_at": datetime.now(timezone.utc).isoformat(),
                          "business_result_unchanged": True, "proofs": deepcopy(proofs)}
            for record in indexed:
                record.context = {**record.context, "resource_resolution": resolution}
            for resource in holdings:
                if resource.state != "released":
                    resource.epoch += 1
                    resource.state, resource.expires_at = "released", 0
            _release_auxiliary(db, task_id, doc, indexed)
            for action in (doc.get("write_safety") or {}).values():
                if action.get("attempt_id") in attempts:
                    action.update(state="recovered", stop_confirmed=True, recovery=resolution)
            for case in doc.get("cases") or []:
                case.update(resource_isolated=False, resource_resolution=resolution)
            doc.update(resource_isolated=False, resource_resolution=resolution)
            run.payload = doc
            db.flush()
        run_store._LIVE.pop(task_id, None)
    return run_store.to_task_json(doc)


def _owns_epoch(resource, indexed) -> bool:
    return any(resource.attempt_id == (record.context.get("resource_request") or {}).get("attempt_id")
               and resource.epoch == ((record.context.get("resource_request") or {}).get("resource_epochs") or {}).get(resource.resource_key)
               and resource.sn == record.sn and resource.owner_user_id == record.owner_user_id
               for record in indexed)


def _release_auxiliary(db, task_id, doc, indexed) -> None:
    from mino_nexus.models.pool_account import PoolAccount
    from mino_nexus.models.resource_ops import DeviceResourceLease
    from mino_nexus.models.device_reservation import DeviceReservation
    aliases = {task_id, *(record.session_id for record in indexed)}
    projects = {str(record.context.get("project_id") or "") for record in indexed} - {""}
    if doc.get("project_id"):
        projects.add(str(doc["project_id"]))
    for account in db.query(PoolAccount).filter(PoolAccount.project_id.in_(projects)).all():
        if str((account.lease or {}).get("run_id") or "") in aliases:
            account.lease, account.updated_at = {}, int(time.time())
    db.query(DeviceResourceLease).filter(DeviceResourceLease.run_id.in_(aliases),
        DeviceResourceLease.sn.in_({record.sn for record in indexed})).delete(synchronize_session=False)
    db.query(DeviceReservation).filter_by(run_id=task_id).delete(synchronize_session=False)


def settle_terminal_task(task_id: str, owner: str) -> None:
    """Atomic compensation including legacy leases left after a terminal projection."""
    from sqlalchemy import text
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.run import AppRegressionRun
    from mino_nexus.models.scout_record import ScoutRecord
    from mino_nexus.models.execution_resource import ExecutionResource
    from mino_nexus.records.write_state import unresolved
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        run = db.get(AppRegressionRun, task_id)
        if run is None or run.owner_user_id != owner or not run.finished_at:
            return
        doc = run.payload or {}
        indexed = db.query(ScoutRecord).filter_by(task_id=task_id, owner_user_id=owner).all()
        if not indexed or unresolved(doc):
            return
        for record in indexed:
            if not records.release_evidence_confirmed(records.view(record)):
                return
        holdings = db.query(ExecutionResource).filter_by(run_id=task_id, owner_user_id=owner).all()
        if any(resource.state != "released" and not _owns_epoch(resource, indexed) for resource in holdings):
            return
        for resource in holdings:
            if resource.state != "released":
                resource.state, resource.expires_at = "released", 0
        _release_auxiliary(db, task_id, doc, indexed)


def repair_terminal_resources() -> None:
    """Settle persisted proofs and recover missing Web stops from the original Scout."""
    from mino_nexus.platform.identity import execution_user
    from mino_nexus.loop.scout_record_tasks import project
    for row in records.terminal_records_for_settlement():
        with execution_user(row["owner_user_id"]):
            project(row)
            settle_terminal_task(row["task_id"], row["owner_user_id"])
    now = time.monotonic()
    for row in records.terminal_records_for_recovery():
        if not _recovery_due(row, now):
            continue
        with execution_user(row["owner_user_id"]):
            try:
                from mino_nexus.loop.scout_stop_evidence import fetch_stop_evidence
                row, proof = fetch_stop_evidence(row)
                if not records.web_stop_proof_valid(row, proof):
                    SLog.w("ScoutResources", f"web stop evidence remains unconfirmed session={row['session_id']}")
                    continue
                current = records.save_resource_stop(row["session_id"], proof)
                project(current)
                reconcile_task_resources(row["task_id"])
            except Exception as exc:
                reason = str(getattr(exc, "reason_code", "") or type(exc).__name__)
                SLog.w("ScoutResources", f"web stop recovery pending session={row['session_id']} reason={reason}")
