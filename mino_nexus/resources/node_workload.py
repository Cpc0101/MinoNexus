"""合并 Scout 心跳 device_workload 与 Nexus run_store 的在途 run。"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from mino_nexus.core import protocol as P
from mino_nexus.resources.device_store import list_devices as stored_devices
from mino_nexus.resources.node_registry import NodeSession, get_registry
from mino_nexus.core.slots import web_lane_snapshot
from mino_nexus.contract.execution.device import is_web_slot
from mino_nexus.contract.runs import get, running_run_ids_for_sn


def scout_workload_busy(workload: P.DeviceWorkload | None) -> bool:
    """Whether a non-Web workload row represents an executing step."""
    if workload is None:
        return False
    if workload.run_id or workload.capability_id or workload.step_idx >= 0:
        return True
    total, free, quarantined = workload.lanes_total, workload.lanes_free, workload.lanes_quarantined
    # Missing or invalid capacity must not release an uncertain non-Web device.
    if (type(total) is not int or type(free) is not int or type(quarantined) is not int
            or total <= 0 or not 0 <= free <= total or not 0 <= quarantined <= total - free):
        return True
    return free < total or quarantined > 0


def build_node_workload(node: NodeSession) -> dict[str, Any]:
    scout_by_sn: dict[str, list[P.DeviceWorkload]] = {}
    for workload in node.device_workload or []:
        if str(workload.sn or "").strip():
            scout_by_sn.setdefault(workload.sn, []).append(workload)
    devices_out: list[dict[str, Any]] = []
    web_sn = ""
    web_pool: dict[str, Any] = web_lane_snapshot()
    for sn in sorted(node.devices):
        dev = node.devices[sn]
        reported = scout_by_sn.get(sn, [])
        scout = next((item for item in reported if item.run_id or item.capability_id or item.step_idx >= 0),
                     reported[-1] if reported else None)
        run_ids = running_run_ids_for_sn(sn)
        is_web = is_web_slot(sn, dev.platform or "web")
        pool = None
        if is_web:
            capacity = next((item for item in reported if item.lanes_total is not None), None)
            pool = web_lane_snapshot(capacity, fallback_active=len(run_ids))
            if not web_sn:
                web_sn, web_pool = sn, pool
        nexus_runs: list[dict[str, Any]] = []
        for rid in run_ids:
            doc = get(rid) or {}
            nexus_runs.append({
                "run_id": rid,
                "app_id": doc.get("app_id") or "",
                "status": doc.get("status") or "",
            })
        busy = pool["full"] if pool is not None else (
            any(scout_workload_busy(item) for item in reported) or bool(run_ids))
        devices_out.append({
            "sn": sn,
            "platform": dev.platform,
            "scout_step": asdict(scout) if scout else None,
            "nexus_run_ids": run_ids,
            "nexus_runs": nexus_runs,
            "busy": busy,
            **(
                {
                    "web_parallel_active": pool["active"],
                    "web_parallel_max": pool["usable"],
                    "web_parallel_full": pool["full"],
                    "web_parallel_free": pool["free"],
                    "web_lanes_total": pool["total"],
                    "web_lanes_quarantined": pool["quarantined"],
                    "web_lanes_reported": pool["reported"],
                }
                if pool is not None
                else {}
            ),
        })
    return {
        "node_id": node.node_id,
        "alive": node.alive,
        "busy": node.busy,
        "active_runs": list(node.active_runs or []),
        "scout_version": node.scout_version or "",
        "web_playwright_parallel": {
            "sn": web_sn,
            "active": web_pool["active"],
            "max": web_pool["usable"],
            "free": web_pool["free"],
            "quarantined": web_pool["quarantined"],
            "reported": web_pool["reported"],
            "full": bool(web_sn) and web_pool["full"],
        },
        "devices": devices_out,
    }


def _offline_device_sns(node_id: str) -> list[str]:
    nid = str(node_id or "").strip()
    sns: list[str] = []
    for row in stored_devices().values():
        sn = str(row.get("sn") or "").strip()
        if not sn:
            continue
        if str(row.get("current_scout_id") or "") == nid:
            sns.append(sn)
    return sorted(set(sns))


def build_offline_workload(node_id: str) -> dict[str, Any]:
    sns = _offline_device_sns(node_id)
    devices_out: list[dict[str, Any]] = []
    for sn in sns:
        run_ids = running_run_ids_for_sn(sn)
        nexus_runs = [
            {
                "run_id": rid,
                "app_id": (get(rid) or {}).get("app_id") or "",
                "status": (get(rid) or {}).get("status") or "",
            }
            for rid in run_ids
        ]
        devices_out.append({
            "sn": sn,
            "platform": "",
            "scout_step": None,
            "nexus_run_ids": run_ids,
            "nexus_runs": nexus_runs,
            "busy": bool(run_ids),
        })
    return {
        "node_id": node_id,
        "alive": False,
        "busy": bool(devices_out),
        "active_runs": [],
        "scout_version": "",
        "devices": devices_out,
        "source": "offline",
    }


def workload_for_node_id(node_id: str) -> dict[str, Any] | None:
    nid = str(node_id or "").strip()
    if not nid:
        return None
    node = get_registry().get_node(nid)
    if node is not None:
        return build_node_workload(node)
    offline = build_offline_workload(nid)
    if offline.get("devices"):
        return offline
    return None
