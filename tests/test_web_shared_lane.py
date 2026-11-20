from __future__ import annotations

from types import SimpleNamespace

from mino_nexus.core.slots import (
    DEFAULT_WEB_PLAYWRIGHT_PARALLEL_LANES,
    MAX_WEB_PLAYWRIGHT_PARALLEL_LANES,
    configured_web_lanes,
    web_lane_snapshot,
)
from mino_nexus.core.platform_contract import native_sn
from mino_nexus.resources.device_admission import resource_group


def test_configured_web_lanes_defaults_and_clamps(monkeypatch):
    monkeypatch.delenv("MINO_WEB_PLAYWRIGHT_LANES", raising=False)
    assert configured_web_lanes() == DEFAULT_WEB_PLAYWRIGHT_PARALLEL_LANES == 8

    monkeypatch.setenv("MINO_WEB_PLAYWRIGHT_LANES", "0")
    assert configured_web_lanes() == 1

    monkeypatch.setenv("MINO_WEB_PLAYWRIGHT_LANES", "999")
    assert configured_web_lanes() == MAX_WEB_PLAYWRIGHT_PARALLEL_LANES == 64

    monkeypatch.setenv("MINO_WEB_PLAYWRIGHT_LANES", "invalid")
    assert configured_web_lanes() == DEFAULT_WEB_PLAYWRIGHT_PARALLEL_LANES


def test_web_lane_snapshot_uses_scout_shared_pool_heartbeat():
    heartbeat = SimpleNamespace(lanes_total=8, lanes_free=5, lanes_quarantined=1)
    assert web_lane_snapshot(heartbeat, fallback_active=3) == {
        "reported": True,
        "total": 8,
        "usable": 7,
        "free": 5,
        "active": 2,
        "quarantined": 1,
        "full": False,
    }

    full = SimpleNamespace(lanes_total=8, lanes_free=0, lanes_quarantined=2)
    assert web_lane_snapshot(full) == {
        "reported": True,
        "total": 8,
        "usable": 6,
        "free": 0,
        "active": 6,
        "quarantined": 2,
        "full": True,
    }


def test_web_lane_snapshot_falls_back_when_heartbeat_is_missing():
    assert web_lane_snapshot(None, fallback_active=3) == {
        "reported": False,
        "total": 8,
        "usable": 8,
        "free": 5,
        "active": 3,
        "quarantined": 0,
        "full": False,
    }


def test_web_labels_do_not_create_separate_admission_groups():
    row = {"sn": "webe22615fbbb917111", "platform": "web", "node_id": "node-1"}
    assert resource_group(row, headless=True) == "web:webe22615fbbb917111"
    assert resource_group(row, headless=False) == "web:webe22615fbbb917111"


def test_non_web_devices_remain_single_occupancy_groups():
    assert resource_group({"sn": "android-1", "platform": "android"}) == "device:android-1"
    assert resource_group({"sn": native_sn("mac", "node-1"), "platform": "mac", "node_id": "node-1"}) == "desktop:node-1:console"
