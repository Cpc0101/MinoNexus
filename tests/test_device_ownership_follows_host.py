"""实体手机不归账号所有：插在哪台 Scout 上，谁都能看到并使用。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from mino_nexus.core import protocol as P
from mino_nexus.platform import identity
from mino_nexus.resources import (
    access_scope,
    device_admission,
    device_store,
    node_forward,
    node_registry,
    node_store,
    ui_devices,
)


PHONE = P.DeviceManifest(sn="R5CT30PHONE", platform="android", channels={"adb": "connected"})
IPHONE = P.DeviceManifest(sn="00008030-IPHONE", platform="ios", channels={"ios_wda": "disconnected"})
WEB_SLOT = P.DeviceManifest(sn="web-nodeb", platform="web", channels={"playwright": "online"})


def _patch_store(monkeypatch, devices: dict[str, dict]) -> None:
    def fake_get_device(sn: str):
        row = devices.get(sn)
        return dict(row) if row else None

    def fake_remember(sn: str, **kwargs):
        row = dict(devices.get(sn) or {"sn": sn})
        row.update({k: v for k, v in kwargs.items() if v not in ("", None)})
        # 实体设备的 owner 强制为空，且清扫历史脏值。
        if kwargs.get("platform") in {"android", "ios", "adb", "wda"}:
            row["owner_user_id"] = ""
            row["owner_name"] = ""
        if kwargs.get("scout_id"):
            row["current_scout_id"] = kwargs["scout_id"]
        devices[sn] = row
        return dict(row)

    monkeypatch.setattr(node_registry, "get_device", fake_get_device)
    monkeypatch.setattr(node_registry, "remember_device", fake_remember)
    monkeypatch.setattr(node_registry, "stored_node", lambda nid: {})


def _fake_registry(live):
    return SimpleNamespace(
        resolve=lambda sn: (live, ""),
        get_node=lambda node_id: live if live is not None and live.node_id == node_id else None,
    )


def test_physical_device_moves_with_host_and_drops_account_owner(monkeypatch):
    devices = {
        PHONE.sn: {
            "sn": PHONE.sn,
            "platform": "android",
            "owner_user_id": "ownera",
            "owner_name": "Alice",
            "current_scout_id": "nodea",
        }
    }
    _patch_store(monkeypatch, devices)
    reg = node_registry.NodeRegistry()
    node_a = node_registry.NodeSession(node_id="nodea", owner_user_id="ownera")
    node_a.devices[PHONE.sn] = P.DeviceManifest(sn=PHONE.sn, platform="android", channels={"adb": "disconnected"})
    node_b = node_registry.NodeSession(node_id="nodeb", owner_user_id="ownerb")
    node_b.devices[PHONE.sn] = PHONE
    reg._nodes.update({node_a.node_id: node_a, node_b.node_id: node_b})
    reg._owner[PHONE.sn] = "nodea"

    reg._place_device(PHONE.sn, "nodeb", device=PHONE, session=node_b)

    assert PHONE.sn in node_b.devices
    rows = {row["sn"]: row for row in reg.devices()}
    assert rows[PHONE.sn]["node_id"] == "nodeb"
    # 实体设备不写账号归属，历史脏值被清空。
    assert devices[PHONE.sn]["owner_user_id"] == ""
    assert devices[PHONE.sn]["owner_name"] == ""
    # 旧节点上的残留立刻让位，避免同一台手机挂在两个节点。
    assert PHONE.sn not in node_a.devices


def test_offline_iphone_stays_on_current_host_without_account_owner(monkeypatch):
    """WDA 没起来 / iPhone 离线时，也要在当前插着的节点下出现。"""
    devices = {
        IPHONE.sn: {
            "sn": IPHONE.sn,
            "platform": "ios",
            "owner_user_id": "ownera",
            "current_scout_id": "nodea",
        }
    }
    _patch_store(monkeypatch, devices)
    reg = node_registry.NodeRegistry()
    node_b = node_registry.NodeSession(node_id="nodeb", owner_user_id="ownerb")
    node_b.devices[IPHONE.sn] = IPHONE
    reg._nodes[node_b.node_id] = node_b

    reg._place_device(IPHONE.sn, "nodeb", device=IPHONE, session=node_b)

    assert reg._owner[IPHONE.sn] == "nodeb"
    assert devices[IPHONE.sn]["owner_user_id"] == ""
    assert devices[IPHONE.sn]["current_scout_id"] == "nodeb"


def test_late_offline_report_from_old_node_does_not_steal_back(monkeypatch):
    devices = {PHONE.sn: {"sn": PHONE.sn, "platform": "android", "owner_user_id": "", "current_scout_id": "nodea"}}
    _patch_store(monkeypatch, devices)
    reg = node_registry.NodeRegistry()
    node_a = node_registry.NodeSession(node_id="nodea", owner_user_id="ownera")
    node_b = node_registry.NodeSession(node_id="nodeb", owner_user_id="ownerb")
    node_b.devices[PHONE.sn] = PHONE
    reg._nodes.update({node_a.node_id: node_a, node_b.node_id: node_b})
    reg._owner[PHONE.sn] = "nodeb"

    offline = P.DeviceManifest(sn=PHONE.sn, platform="android", channels={"adb": "disconnected"})
    reg._place_device(PHONE.sn, "nodea", device=offline, session=node_a)

    assert reg._owner[PHONE.sn] == "nodeb"
    assert PHONE.sn not in node_a.devices


def test_offline_takeover_when_old_host_already_disconnected(monkeypatch):
    devices = {PHONE.sn: {"sn": PHONE.sn, "platform": "android", "owner_user_id": "", "current_scout_id": "nodea"}}
    _patch_store(monkeypatch, devices)
    reg = node_registry.NodeRegistry()
    node_a = node_registry.NodeSession(node_id="nodea", owner_user_id="ownera")
    node_a.devices[PHONE.sn] = P.DeviceManifest(
        sn=PHONE.sn, platform="android", channels={"adb": "disconnected"},
    )
    node_b = node_registry.NodeSession(node_id="nodeb", owner_user_id="ownerb")
    offline = P.DeviceManifest(sn=PHONE.sn, platform="android", channels={"adb": "disconnected"})
    node_b.devices[PHONE.sn] = offline
    reg._nodes.update({node_a.node_id: node_a, node_b.node_id: node_b})
    reg._owner[PHONE.sn] = "nodea"

    reg._place_device(PHONE.sn, "nodeb", device=offline, session=node_b)

    assert reg._owner[PHONE.sn] == "nodeb"
    assert PHONE.sn not in node_a.devices


def test_device_store_clears_owner_for_physical_device(monkeypatch):
    root = {
        "devices": {
            PHONE.sn: {
                "sn": PHONE.sn,
                "platform": "android",
                "owner_user_id": "ownera",
                "owner_name": "Alice",
                "current_scout_id": "nodea",
            }
        }
    }
    monkeypatch.setattr(device_store, "_root", lambda: root)
    monkeypatch.setattr(device_store, "_save", lambda value: None)

    row = device_store.remember(
        PHONE.sn,
        platform="android",
        scout_id="nodeb",
        owner_user_id="ownerb",
        owner_name="Bob",
    )

    assert row["owner_user_id"] == ""
    assert row["owner_name"] == ""
    assert row["current_scout_id"] == "nodeb"
    # 公开视图同样不得泄露归属。
    assert device_store.public_device(root["devices"][PHONE.sn])["owner_user_id"] == ""


def test_device_store_keeps_owner_for_web_slot(monkeypatch):
    root = {"devices": {}}
    monkeypatch.setattr(device_store, "_root", lambda: root)
    monkeypatch.setattr(device_store, "_save", lambda value: None)

    row = device_store.remember(WEB_SLOT.sn, platform="web", scout_id="nodeb",
                                owner_user_id="ownerb", owner_name="Bob")

    assert row["owner_user_id"] == "ownerb"


def test_access_scope_physical_device_has_no_owner(monkeypatch):
    live = SimpleNamespace(node_id="nodeb", owner_user_id="ownerb", devices={})
    monkeypatch.setattr(device_store, "get_device", lambda sn: {"sn": sn, "platform": "android", "owner_user_id": "ownera"})
    monkeypatch.setattr(node_registry, "get_registry", lambda: _fake_registry(live))
    monkeypatch.setattr(node_store, "get_node", lambda nid: {"owner_user_id": "ownerb"})

    assert access_scope.device_owner_id(PHONE.sn) == ""
    assert access_scope.is_physical_device(PHONE.sn) is True

    monkeypatch.setattr(access_scope, "current_user_id", lambda: "someone-else")
    assert access_scope.require_owned_device(PHONE.sn) == "someone-else"


def test_access_scope_web_slot_still_rejects_owner_mismatch(monkeypatch):
    live = SimpleNamespace(node_id="nodeb", owner_user_id="ownerb", devices={})
    monkeypatch.setattr(device_store, "get_device", lambda sn: {"sn": sn, "platform": "web", "owner_user_id": "ownera"})
    monkeypatch.setattr(node_registry, "get_registry", lambda: _fake_registry(live))
    monkeypatch.setattr(node_store, "get_node", lambda nid: {"owner_user_id": "ownerb"})

    assert access_scope.device_owner_id(WEB_SLOT.sn) == ""

    monkeypatch.setattr(access_scope, "current_user_id", lambda: "ownera")
    with pytest.raises(Exception):
        access_scope.require_owned_device(WEB_SLOT.sn)


class _UiRegistry:
    def __init__(self, rows):
        self._rows = rows

    def devices(self):
        return [dict(row) for row in self._rows]

    def nodes(self):
        return []


def _patch_ui(monkeypatch, owner_for=("owner-a", "")):
    uid, owner = owner_for
    monkeypatch.setattr(identity, "current_user_id", lambda: uid)
    monkeypatch.setattr(access_scope, "device_owner_id", lambda sn, node_id="": owner)
    monkeypatch.setattr(ui_devices, "stored_nodes", lambda: {})
    monkeypatch.setattr(ui_devices, "stored_devices", lambda: {})
    monkeypatch.setattr(ui_devices, "get_device", lambda sn: {"sn": sn})
    monkeypatch.setattr(ui_devices, "lock_password_public", lambda sn: {})
    monkeypatch.setattr(ui_devices, "_isolation_index", lambda: {})
    monkeypatch.setattr(device_admission, "busy_for", lambda row, headless=True: "")


def test_ui_devices_shows_physical_phone_regardless_of_account(monkeypatch):
    _patch_ui(monkeypatch)
    rows = ui_devices.ui_devices(registry=_UiRegistry([{
        "sn": PHONE.sn, "node_id": "node-b", "platform": "android",
        "node_alive": True, "status": "online", "owner_user_id": "",
        "channels": {"adb": "connected"},
    }]))

    row = next(r for r in rows if r["sn"] == PHONE.sn)
    assert row["owner_user_id"] == ""
    assert row["owner_name"] == ""
    assert row["online"] is True
    assert row["executable"] is True


def test_ui_devices_still_hides_other_owners_web_slot(monkeypatch):
    _patch_ui(monkeypatch)
    rows = ui_devices.ui_devices(registry=_UiRegistry([{
        "sn": WEB_SLOT.sn, "node_id": "node-b", "platform": "web",
        "node_alive": True, "status": "online", "owner_user_id": "owner-b",
        "channels": {"playwright": "online"},
    }]))

    assert rows == []


def test_evaluate_does_not_block_unowned_physical_device(monkeypatch):
    from mino_nexus.resources.device_targets import evaluate

    monkeypatch.setattr(device_admission, "busy_for", lambda row, headless=True: "")
    monkeypatch.setattr(identity, "current_user_id", lambda: "someone")

    phone = evaluate({"sn": PHONE.sn, "platform": "android", "status": "online",
                      "node_alive": True, "owner_user_id": ""})
    assert phone["executable"] is True
    assert phone["blocking_code"] == ""

    other_owner = evaluate({"sn": PHONE.sn, "platform": "ios", "status": "online",
                            "node_alive": True, "owner_user_id": "owner-b"})
    assert other_owner["executable"] is True

    web = evaluate({"sn": WEB_SLOT.sn, "platform": "web", "status": "online",
                    "node_alive": True, "owner_user_id": ""})
    assert web["executable"] is False
    assert web["blocking_code"] == "unclaimed"


def test_forward_owner_allows_physical_device_but_not_web_slot(monkeypatch):
    node = SimpleNamespace(node_id="node-b", devices={PHONE.sn: PHONE, WEB_SLOT.sn: WEB_SLOT})
    monkeypatch.setattr(access_scope, "node_owner_id", lambda nid: "owner-b")

    node_forward.require_owner(node, "owner-a", sn=PHONE.sn)

    with pytest.raises(node_forward.ForwardError) as exc:
        node_forward.require_owner(node, "owner-a", sn=WEB_SLOT.sn)
    assert exc.value.status == 404


def test_visible_devices_includes_other_accounts_phone(monkeypatch):
    from mino_nexus.assistant import assistant_tools
    from mino_nexus.platform import auth_store
    from mino_nexus.resources import node_store as ns

    rows = [
        {"sn": PHONE.sn, "node_id": "node-b", "platform": "android", "status": "online"},
        {"sn": WEB_SLOT.sn, "node_id": "node-b", "platform": "web", "status": "online"},
    ]
    monkeypatch.setattr(ui_devices, "ui_devices", lambda: rows)
    monkeypatch.setattr(auth_store, "api_session_for", lambda uid: {})
    monkeypatch.setattr(ns, "node_visible", lambda snapshot, sess: False)
    monkeypatch.setattr(ns, "get_node", lambda nid: {})
    monkeypatch.setattr(node_registry, "get_registry", lambda: SimpleNamespace(get_node=lambda nid: None))

    visible = assistant_tools._visible_devices("user-a")
    assert [row["sn"] for row in visible] == [PHONE.sn]


def test_observers_broadcast_physical_events_to_every_logged_in_user(monkeypatch):
    from mino_nexus.api.ws import observers
    from mino_nexus.platform import auth_store

    monkeypatch.setattr(access_scope, "is_physical_device", lambda sn: sn == PHONE.sn)
    monkeypatch.setattr(access_scope, "device_owner_id", lambda sn, node_id="": "owner-a")
    monkeypatch.setattr(access_scope, "node_owner_id", lambda nid: "owner-a")
    monkeypatch.setattr(auth_store, "session_of", lambda token: {"user_id": token})

    assert observers._event_owner({"sn": PHONE.sn}) == "*"
    assert observers._event_owner({"sn": WEB_SLOT.sn}) == "owner-a"

    class _WS:
        def __init__(self):
            self.sent = []
            self.closed = False

        async def send_text(self, text):
            self.sent.append(text)

        async def close(self, code=1000, reason=""):
            self.closed = True

    ws_a, ws_b = _WS(), _WS()
    monkeypatch.setattr(observers, "_CLIENTS", {ws_a: ("owner-a", "owner-a"), ws_b: ("user-b", "user-b")})

    asyncio.run(observers._broadcast({"sn": PHONE.sn, "status": "online"}))
    assert len(ws_a.sent) == 1 and len(ws_b.sent) == 1

    ws_a.sent.clear()
    ws_b.sent.clear()
    asyncio.run(observers._broadcast({"sn": WEB_SLOT.sn, "status": "online"}))
    assert len(ws_a.sent) == 1 and ws_b.sent == []


def test_debug_control_node_allows_physical_but_not_other_web_node(monkeypatch):
    from mino_nexus.loop import debug_cases

    monkeypatch.setattr(access_scope, "is_physical_device", lambda sn: sn == PHONE.sn)
    other = SimpleNamespace(owner_user_id="owner-b")
    assert debug_cases._owned_by_node_account(other, "owner-a", PHONE.sn) is True
    assert debug_cases._owned_by_node_account(other, "owner-a", WEB_SLOT.sn) is False


# --- Scout 线路 owner：Scout 只认节点认证账号，Nexus 记录仍属于使用者 ---

def _physical_row(owner="user-b", platform="android", sn=PHONE.sn):
    return {
        "owner_user_id": owner, "sn": sn, "platform": platform, "node_id": "node-a",
        "task_id": "task-1", "attempt_id": "attempt-1", "session_id": "session-1",
        "payload": {"run_id": "run-1", "platform": platform, "script_path": "tests/a.py"},
        "summary": {}, "context": {"device_hint": {}},
    }


def test_wire_owner_follows_node_account_for_physical_only():
    from mino_nexus.resources import scout_transport_owner as wire

    phone = _physical_row()
    node = SimpleNamespace(owner_user_id="owner-a")
    assert wire.wire_owner(phone, node) == "owner-a"
    assert phone["owner_user_id"] == "user-b"

    web = _physical_row(platform="web", sn=WEB_SLOT.sn)
    assert wire.wire_owner(web, node) == "user-b"

    # 平台可能只在载荷、摘要或顶层出现。
    assert wire.is_physical_record({"payload": {"platform": "ios"}}) is True
    assert wire.is_physical_record({"summary": {"platform": "android"}}) is True
    assert wire.is_physical_record({"platform": "wda"}) is True
    assert wire.is_physical_record({"platform": "web"}) is False

    # 节点 owner 缺失时不动本地 owner，避免把记录送进空账号。
    assert wire.wire_owner(phone, SimpleNamespace(owner_user_id="")) == "user-b"
    assert wire.target_owner(PHONE.sn, "android", "user-b", node) == "owner-a"
    assert wire.target_owner(WEB_SLOT.sn, "web", "user-b", node) == "user-b"


def test_normalize_incoming_maps_only_node_account_to_local_user():
    from mino_nexus.resources import scout_transport_owner as wire

    phone = _physical_row()
    node = SimpleNamespace(owner_user_id="owner-a")
    incoming = {"session_id": "session-1", "owner_user_id": "owner-a", "summary_version": 2}
    assert wire.normalize_incoming(phone, incoming, node)["owner_user_id"] == "user-b"
    # 已经是本地 owner 的载荷保持不变。
    local = {"session_id": "session-1", "owner_user_id": "user-b"}
    assert wire.normalize_incoming(phone, local, node) is local
    # Web 槽不跨账号。
    web = _physical_row(platform="web", sn=WEB_SLOT.sn)
    assert wire.normalize_incoming(web, incoming, node) is incoming


def test_scout_record_request_uses_node_owner_without_touching_local_record(monkeypatch):
    from mino_nexus.loop import scout_record_tasks

    sent = []

    def send(msg_type, request, timeout=0):
        sent.append(request)
        return "result"

    node = SimpleNamespace(owner_user_id="owner-a", send=send)
    monkeypatch.setattr(scout_record_tasks, "run_sync", lambda value: value)

    row = _physical_row()
    params = {"task_id": "task-1", "attempt_id": "attempt-1", "session_id": "session-1",
              "owner_user_id": "user-b"}
    scout_record_tasks._request(row, node, "record.status", params)

    assert sent[-1].params["owner_user_id"] == "owner-a"
    assert params["owner_user_id"] == "user-b"
    assert row["owner_user_id"] == "user-b"

    web = _physical_row(platform="web", sn=WEB_SLOT.sn)
    scout_record_tasks._request(web, node, "record.status", params)
    assert sent[-1].params["owner_user_id"] == "user-b"


def test_record_sync_maps_scout_owner_back_to_local_user(monkeypatch):
    from mino_nexus.loop import scout_record_tasks

    row = _physical_row()
    captured = {}

    monkeypatch.setattr(scout_record_tasks.records, "for_node", lambda sid, nid: dict(row))
    monkeypatch.setattr(scout_record_tasks.records, "receive",
                        lambda node_id, incoming: captured.update(incoming) or dict(row))
    monkeypatch.setattr(scout_record_tasks, "project", lambda value: None)
    monkeypatch.setattr(access_scope, "node_owner_id", lambda nid: "owner-a")

    scout_record_tasks.receive("node-a", {
        "session_id": "session-1", "owner_user_id": "owner-a", "summary_version": 1,
        "status": "running", "sealed": False,
    })
    assert captured["owner_user_id"] == "user-b"


def test_debug_scope_and_status_mapping_for_physical_device(monkeypatch):
    from mino_nexus.loop import debug_cases
    from mino_nexus.resources import scout_record_store

    row = _physical_row()
    node = SimpleNamespace(owner_user_id="owner-a")
    assert debug_cases._scope(row, node)["owner_user_id"] == "owner-a"
    assert debug_cases._scope(row)["owner_user_id"] == "user-b"
    web = _physical_row(platform="web", sn=WEB_SLOT.sn)
    assert debug_cases._scope(web, node)["owner_user_id"] == "user-b"

    captured = {}
    monkeypatch.setattr(access_scope, "node_owner_id", lambda nid: "owner-a")
    monkeypatch.setattr(scout_record_store, "receive",
                        lambda node_id, data: captured.update(data) or dict(row))
    debug_cases._persist_status("node-a", {
        "session_id": "session-1", "owner_user_id": "owner-a", "summary_version": 3,
    }, row)
    assert captured["owner_user_id"] == "user-b"


def test_debug_control_uses_node_owner_on_wire_and_maps_result_back(monkeypatch):
    from mino_nexus.loop import debug_cases
    from mino_nexus.resources import scout_record_store

    row = _physical_row()
    row["payload"]["execution_kind"] = "debug"
    node = SimpleNamespace(node_id="node-a", owner_user_id="owner-a", alive=True)
    calls = []

    def call(target, capability, params, **kwargs):
        calls.append((capability, dict(params)))
        return {"owner_user_id": "owner-a", "state": "running",
                "task_id": "task-1", "session_id": "session-1"}

    monkeypatch.setattr(scout_record_store, "get", lambda sid, owner=None: dict(row))
    monkeypatch.setattr(debug_cases, "require_user_id", lambda: "user-b")
    monkeypatch.setattr(debug_cases, "_wait_control_node", lambda record, owner: (node, record))
    monkeypatch.setattr(debug_cases, "_call", call)

    data = debug_cases.control("session-1", "execute", {"script_path": "tests/a.py"})

    assert calls[0][0] == "debug.execute"
    assert calls[0][1]["owner_user_id"] == "owner-a"
    assert data["owner_user_id"] == "user-b"
    assert data["task_id"] == "task-1"


def test_script_dispatch_legacy_path_sends_node_owner(monkeypatch):
    """旧 Scout（没有记录 API）也必须显式带节点账号，否则空 owner 会被拒。"""
    from mino_nexus.casework import run_store, run_write_safety, script_workspace
    from mino_nexus.contract import ownership, step_pointer
    from mino_nexus.loop import script_dispatch
    from mino_nexus.records.eventlog import writer as writer_module
    from mino_nexus.resources import repo_apps
    from mino_nexus.runtime import run_context as run_context_module

    sent: dict[str, object] = {}
    node = SimpleNamespace(
        node_id="node-a", owner_user_id="owner-a", alive=True,
        record_api_version=0, devices={}, runtime_capabilities=[],
    )

    def send(msg_type, request, timeout=0):
        sent["capability"] = request.capability_id
        sent["platform"] = request.platform
        sent["params"] = dict(request.params)
        return SimpleNamespace(
            status=P.EventStatus.PASS,
            data={"status": "pass", "summary": "ok"},
            error="", summary="",
        )

    node.send = send

    class _Registry:
        def resolve(self, sn):
            return node, ""

        def get_node(self, node_id):
            return node

    class _Writer:
        def close(self, **kwargs):
            pass

    monkeypatch.setattr(node_registry, "get_registry", lambda: _Registry())
    monkeypatch.setattr(run_store, "get", lambda run_id: {
        "owner_user_id": "user-b", "platform": "android", "revision": "a" * 40,
        "device_targets": {}, "execution_kind": "case",
    })
    monkeypatch.setattr(run_write_safety, "protected", lambda run_id, sn="": False)
    monkeypatch.setattr(script_workspace, "public_url", lambda url: url)
    monkeypatch.setattr(ownership, "current_user_id", lambda: "user-b")
    monkeypatch.setattr(step_pointer, "compute_case_wall_budget_sec", lambda case: 30.0)
    monkeypatch.setattr(repo_apps, "key_for_app", lambda project_id, app_id: "")
    monkeypatch.setattr(writer_module, "open_session", lambda **kwargs: _Writer())
    monkeypatch.setattr(run_context_module, "build_run_context", lambda sn, **kwargs: SimpleNamespace(
        session_id="", case_id="", case_seq=0, scout_run_id="", project_id="",
        env_profile="test", env_surface="",
    ))
    monkeypatch.setattr(run_context_module, "bind_case_project", lambda ctx, case: None)
    from mino_nexus.resources.accounts import lease as lease_module

    monkeypatch.setattr(lease_module, "restore_leases_for_run", lambda ctx, run_id: 0)
    monkeypatch.setattr(script_dispatch, "run_sync", lambda value: value)

    outcome = script_dispatch.dispatch_script_case(
        run_id="run-1",
        case={"case_id": "c1", "name": "phone", "platform": "android"},
        sn=PHONE.sn, app_id="app-1", platform="android", project_id="p1",
        repo={"url": "https://example.com/repo", "branch": "main"},
    )

    assert sent["capability"] == P.SCRIPT_RUN_CAPABILITY
    assert sent["platform"] == "android"
    assert sent["params"]["owner_user_id"] == "owner-a"
    assert outcome["status"] == "pass"
