"""SQLite-backed resource admission; UI checks are informative, not locks."""
from __future__ import annotations

from mino_nexus.models.device_reservation import DeviceReservation


class ResourceBusy(ValueError):
    def __init__(self, sn: str, run_id: str):
        super().__init__("设备或共享桌面繁忙，请等待当前任务结束")
        self.sn, self.busy_task_id = sn, run_id


def resource_group(row: dict, *, headless=True) -> str:
    """Return the one admission group for a target.

    ``headless`` is retained in the public call shape for compatibility, but it
    never changes grouping. Web run/debug/headed/headless share one SN pool.
    """
    sn, platform = str(row.get("sn") or ""), str(row.get("platform") or row.get("type") or "")
    node = str(row.get("node_id") or row.get("current_scout_id") or "")
    if platform in ("mac", "win"):
        from mino_nexus.core.platform_contract import native_sn
        if sn != native_sn(platform, node):
            raise ValueError("native_identity_conflict")
        # Device identities change; host desktop contention never splits.
        return f"desktop:{node}:console"
    if platform in {"desktop", "mac", "win"}:
        return f"desktop:{node}:console"
    return f"web:{sn}" if platform == "web" else f"device:{sn}"


def _recorded_target(db, payload: dict, sn: str) -> dict:
    from mino_nexus.models.device import Device
    row = dict((payload.get("device_targets") or {}).get(sn) or {})
    stored = db.get(Device, sn) if sn else None
    row.setdefault("sn", sn)
    if not row.get("platform"):
        row["platform"] = (payload.get("platforms_by_sn") or {}).get(sn) or (stored.platform if stored else payload.get("platform"))
    if not row.get("node_id"):
        row["node_id"] = stored.node_id if stored else ""
    if not row["node_id"] and row.get("platform") in {"desktop", "mac", "win"} and str(sn).startswith(("desktop:", "mac:", "win:")):
        row["node_id"] = str(sn).split(":")[1]
    return row


def _web_pool_context(sn: str, legacy_ids: set[str]) -> tuple[dict, int]:
    """Combine Nexus reservations with lanes claimed outside Nexus.

    Scout reports both aggregate lane counts and the run ID on each claimed
    lane. Union the two ID sets so a formal reservation that has not reached
    Scout still counts, while the same run observed by both sides counts once.
    """
    from mino_nexus.core.slots import web_lane_snapshot
    from mino_nexus.resources.node_registry import get_registry

    node, _ = get_registry().resolve(sn)
    workloads = list(node.device_workload or []) if node else []
    capacity = next((item for item in workloads
                     if item.sn == sn and item.lanes_total is not None), None)
    pool = web_lane_snapshot(capacity, fallback_active=len(legacy_ids))
    if not pool["reported"]:
        # Without a heartbeat, Nexus reservations are the only trustworthy
        # evidence. fallback_active already equals this count, so subtracting
        # the overlap again would turn one run into two.
        return pool, len(legacy_ids)
    scout_ids = {str(item.run_id) for item in workloads
                 if item.sn == sn and item.run_id}
    # ``active`` covers unnamed/transient lane holders. The union covers Nexus
    # reservations made between Scout heartbeats. Taking the larger value avoids
    # double-counting a run that appears in both snapshots.
    committed = max(int(pool["active"]), len(legacy_ids | scout_ids))
    return pool, committed


def busy_for(row: dict, *, headless=True) -> str:
    from mino_nexus.core.database import session_scope
    group = resource_group(row, headless=headless)
    from mino_nexus.models.run import AppRegressionRun
    with session_scope() as db:
        if group.startswith("web:"):
            reservations = db.query(DeviceReservation).filter(DeviceReservation.resource_key.like(group + ":lane:%")).all()
        else:
            found = db.get(DeviceReservation, group)
            reservations = [found] if found else []
        owners = {r.run_id for r in reservations}
        # Historical tasks without reservation rows remain visible as busy.
        for run in db.query(AppRegressionRun).filter(AppRegressionRun.status.in_(("running", "queued"))):
            payload = run.payload if isinstance(run.payload, dict) else {}
            for sn in payload.get("sns") or [payload.get("sn") or run.sn]:
                target = _recorded_target(db, payload, sn)
                if resource_group(target, headless=payload.get("playwright_headless", True)) == group:
                    owners.add(run.run_id)
                    break
        if group.startswith("web:"):
            pool, committed = _web_pool_context(group.removeprefix("web:"), owners)
            if pool["free"] == 0 or committed >= pool["usable"]:
                return next(iter(owners), "") or f"web-lane-full:{group.removeprefix('web:')}"
            return ""
        return next(iter(owners), "") if owners else ""


def _reservation_in_group(key: str, group: str) -> bool:
    return key == group or (group.startswith("web:") and
        (key.startswith(group + ":lane:") or key.startswith(group + ":v5-queue:")))


def admit_in_transaction(db, doc: dict) -> None:
    """Caller holds BEGIN IMMEDIATE and persists the run in the same transaction."""
    from mino_nexus.models.run import AppRegressionRun
    rid = str(doc["run_id"])
    if doc.get("status") not in ("running", "queued"):
        from mino_nexus.records.write_state import unresolved
        pending = unresolved(doc)
        from mino_nexus.models.execution_resource import ExecutionResource
        import time
        pending += [{"sn": row.sn} for row in db.query(ExecutionResource).filter_by(run_id=rid).all()
                    if row.state == "isolated" or (row.state == "held" and row.expires_at <= time.time())]
        if not pending:
            db.query(DeviceReservation).filter_by(run_id=rid).delete()
            return
        protected_groups = set()
        for action in pending:
            sn = str(action.get("sn") or "")
            if not sn:
                return  # Unknown device identity must retain all reservations.
            target = _recorded_target(db, doc, sn)
            if not target.get("platform") or not target.get("node_id"):
                return
            try:
                protected_groups.add(resource_group(target, headless=doc.get("playwright_headless", True)))
            except ValueError:
                return
        for reservation in db.query(DeviceReservation).filter_by(run_id=rid).all():
            if not any(_reservation_in_group(reservation.resource_key, group) for group in protected_groups):
                db.delete(reservation)
        return
    previous = db.get(AppRegressionRun, rid)
    if previous is not None:
        return
    from mino_nexus.resources.scout_updates import check_admission
    targets = [_recorded_target(db, doc, sn) for sn in doc.get("sns") or [doc.get("sn")] if sn]
    check_admission(db, [t["node_id"] for t in targets], task_id=rid)
    doc["allowed_node_ids"] = sorted({t["node_id"] for t in targets})
    # Existing runs made before this upgrade also participate in admission.
    live = db.query(AppRegressionRun).filter(AppRegressionRun.status.in_(("running", "queued"))).all()
    from mino_nexus.records.write_state import unresolved
    reservations = db.query(DeviceReservation).all()
    reserved = {row.run_id for row in reservations}
    def participates(run, group):
        return run.status in {"running", "queued"} or any(
            row.run_id == run.run_id and _reservation_in_group(row.resource_key, group)
            for row in reservations)
    known = {row.run_id for row in live}
    if reserved - known:
        live += db.query(AppRegressionRun).filter(AppRegressionRun.run_id.in_(reserved - known)).all()
    acquired = set()
    for sn in doc.get("sns") or [doc.get("sn")]:
        if not sn:
            continue
        row = _recorded_target(db, doc, sn)
        group = resource_group(row, headless=doc.get("playwright_headless", True))
        if group in acquired:
            continue
        acquired.add(group)
        legacy_ids: set[str] = set()
        legacy_id = ""
        for run in live:
            if not participates(run, group):
                continue
            payload = run.payload if isinstance(run.payload, dict) else {}
            for other_sn in payload.get("sns") or [payload.get("sn")]:
                old_row = _recorded_target(db, payload, other_sn)
                if resource_group(old_row, headless=payload.get("playwright_headless", True)) == group:
                    legacy_ids.add(run.run_id)
                    legacy_id = run.run_id
                    break
        if group.startswith("web:"):
            pool, committed = _web_pool_context(sn, legacy_ids)
            limit = int(pool["usable"])
            if pool["free"] == 0 or committed >= limit:
                raise ResourceBusy(sn, legacy_id)
            occupied = {r.resource_key for r in db.query(DeviceReservation).filter(DeviceReservation.resource_key.like(group + ":lane:%"))}
            free = next((f"{group}:lane:{i}" for i in range(limit) if f"{group}:lane:{i}" not in occupied), "")
            if not free:
                raise ResourceBusy(sn, "")
            group = free
        else:
            limited = 1
            if len(legacy_ids) >= limited:
                raise ResourceBusy(sn, legacy_id)
            occupied = db.get(DeviceReservation, group)
            if occupied and occupied.run_id != rid:
                raise ResourceBusy(sn, occupied.run_id)
        db.add(DeviceReservation(resource_key=group, run_id=rid))


def owns_desktop(run_id: str, node_id: str) -> bool:
    from mino_nexus.core.database import session_scope
    with session_scope() as db:
        row = db.get(DeviceReservation, f"desktop:{node_id}:console")
        return bool(row and row.run_id == run_id)
