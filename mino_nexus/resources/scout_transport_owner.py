"""Scout 线路上的记录归属：实体手机跟随接入节点的认证账号。

Scout 的 `ScriptRecords` 在注册成功后就绑定到**节点认证账号**，记录 RPC 一律
要求 `owner_user_id` 等于该账号。实体手机不归账号所有，使用者可能是另一个
账号；这时 Nexus 作为可信代理，把线路上的 owner 换成本次执行所属节点的账号，
返回后立刻换回 Nexus 本地记录的 owner（使用者账号）。

Web 虚拟槽和宿主桌面不是实体设备，不经过这里的替换，仍按账号隔离。
"""
from __future__ import annotations

from typing import Any

from mino_nexus.contract.execution.device import is_physical_platform


def _text(value: Any) -> str:
    return str(value or "")


def node_owner(node: Any) -> str:
    """→ 节点认证账号。接受节点对象、节点字典，或已经是账号字符串。"""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        return _text(node.get("owner_user_id"))
    return _text(getattr(node, "owner_user_id", ""))


def record_platform(record: dict[str, Any]) -> str:
    """平台的权威来源可能在载荷、已存摘要或记录顶层。"""
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    summary = record.get("summary") if isinstance(record.get("summary"), dict) else {}
    for value in (payload.get("platform"), summary.get("platform"), record.get("platform")):
        text = _text(value).strip().lower()
        if text:
            return text
    return ""


def is_physical_record(record: dict[str, Any], node: Any = None) -> bool:
    """实体手机记录：平台已知按平台判定，平台缺失时按落盘的设备事实判定。"""
    platform = record_platform(record)
    if platform:
        return is_physical_platform(platform)
    sn = _text(record.get("sn")).strip()
    if not sn:
        return False
    from mino_nexus.resources.access_scope import is_physical_device
    try:
        return is_physical_device(sn)
    except Exception:
        return False


def wire_owner(record: dict[str, Any], node: Any) -> str:
    """Nexus 本地记录的 owner → Scout 线路上要求的 owner。"""
    local = _text(record.get("owner_user_id"))
    remote = node_owner(node)
    if remote and remote != local and is_physical_record(record, node):
        return remote
    return local


def normalize_incoming(record: dict[str, Any], incoming: Any, node: Any) -> Any:
    """Scout 返回的 owner 换回 Nexus 本地记录的 owner。"""
    if not isinstance(incoming, dict):
        return incoming
    local = _text(record.get("owner_user_id"))
    remote = node_owner(node)
    if (local and remote and local != remote and is_physical_record(record, node)
            and _text(incoming.get("owner_user_id")) == remote):
        return {**incoming, "owner_user_id": local}
    return incoming


def target_owner(sn: str, platform: str, owner: str, node: Any) -> str:
    """没有本地记录时（调试准备、旧入口）按目标设备算线路 owner。"""
    record = {"owner_user_id": _text(owner), "sn": _text(sn), "platform": _text(platform)}
    return wire_owner(record, node)
