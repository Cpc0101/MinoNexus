"""Shared Web lane capacity and version-string helpers.

A Web SN has exactly one lane pool. Formal runs, debug sessions, scripts, v5,
headed and headless executions are labels on a lane claim, never separate pools.
"""
from __future__ import annotations

import os
from typing import Any


DEFAULT_WEB_PLAYWRIGHT_PARALLEL_LANES = 8
MIN_WEB_PLAYWRIGHT_PARALLEL_LANES = 1
MAX_WEB_PLAYWRIGHT_PARALLEL_LANES = 64


def configured_web_lanes() -> int:
    """Return configured capacity for the one Web pool on each Scout node."""
    raw = str(os.environ.get("MINO_WEB_PLAYWRIGHT_LANES", "") or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_WEB_PLAYWRIGHT_PARALLEL_LANES
    except ValueError:
        value = DEFAULT_WEB_PLAYWRIGHT_PARALLEL_LANES
    return max(MIN_WEB_PLAYWRIGHT_PARALLEL_LANES,
               min(MAX_WEB_PLAYWRIGHT_PARALLEL_LANES, value))


# Compatibility export. New code should prefer configured_web_lanes() so tests and
# isolated smoke runs can set the value before building a node.
WEB_PLAYWRIGHT_PARALLEL_LANES = configured_web_lanes()


def _workload_value(workload: Any, name: str) -> Any:
    if isinstance(workload, dict):
        return workload.get(name)
    return getattr(workload, name, None)


def web_lane_snapshot(workload: Any | None = None, *, fallback_active: int = 0) -> dict[str, Any]:
    """Normalize one heartbeat capacity row into the shared Web pool snapshot.

    Scout's heartbeat is authoritative when it contains a valid
    ``lanes_total/lanes_free/lanes_quarantined`` tuple. A quarantine consumes
    capacity but is not an active claim. With no valid heartbeat, Nexus falls
    back to local configuration and the supplied Nexus run count.
    """
    total = _workload_value(workload, "lanes_total")
    free = _workload_value(workload, "lanes_free")
    quarantined = _workload_value(workload, "lanes_quarantined")
    quarantined = 0 if quarantined is None else quarantined
    valid = (
        type(total) is int and total > 0
        and type(free) is int and 0 <= free <= total
        and type(quarantined) is int and 0 <= quarantined <= total - free
    )
    if valid:
        usable = total - quarantined
        return {
            "reported": True,
            "total": total,
            "usable": usable,
            "free": free,
            "active": usable - free,
            "quarantined": quarantined,
            "full": free == 0,
        }

    total = configured_web_lanes()
    active = max(0, min(total, int(fallback_active or 0)))
    free = max(0, total - active)
    return {
        "reported": False,
        "total": total,
        "usable": total,
        "free": free,
        "active": active,
        "quarantined": 0,
        "full": free == 0,
    }


def web_lane_capacity(workload: Any | None = None, *, fallback_active: int = 0) -> int:
    """Return usable lane count from Scout heartbeat, falling back to config."""
    return int(web_lane_snapshot(workload, fallback_active=fallback_active)["usable"])


def version_string_looks_invalid(ver: str) -> bool:
    v = str(ver or "").strip().lower()
    if not v:
        return True
    if "强停" in ver or "force-stop" in v or "force_stop" in v:
        return True
    if v.startswith("fail") or "error" in v[:24]:
        return True
    return False
