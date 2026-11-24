"""用户节点/设备权限：节点与虚拟槽按账号隔离，实体手机不归账号所有。

实体手机（Android / iOS）没有稳定的人-机绑定，也不该要求"这台手机属于谁"：
插在哪台 Scout 电脑上，就在那台电脑的账号下展示和执行；任何已登录用户都可
以看到并使用它。换电脑、换账号插上都不需要迁移归属，因为这里不再记录归属。

Web 虚拟槽（`web{node_id}`）和宿主桌面（desktop / mac / win）不是物理设备，
仍按节点认证账号隔离，并保留历史冲突保护。节点本身、插件、锁屏密码、历史
任务与执行证据仍各按自己的 owner 边界校验。
"""
from __future__ import annotations

from fastapi import HTTPException
from mino_nexus.contract.execution.device import is_physical_platform
from mino_nexus.platform.identity import current_user_id


def node_owner_id(node_id: str) -> str:
    from mino_nexus.resources.node_registry import get_registry
    from mino_nexus.resources.node_store import get_node
    live = get_registry().get_node(node_id)
    if live is not None:
        return str(live.owner_user_id or "")
    return str((get_node(node_id) or {}).get("owner_user_id") or "")


def require_owned_node(node_id: str) -> str:
    uid = current_user_id()
    if not uid:
        raise HTTPException(status_code=401, detail="请先登录")
    if node_owner_id(node_id) != uid:
        raise HTTPException(status_code=404, detail="没有这个节点")
    return uid


def _device_facts(sn: str, node_id: str = "") -> tuple[str, str, str]:
    """→ (归一前平台, 当前节点, 落盘 owner)。一次把注册表和落盘读齐。"""
    from mino_nexus.resources.node_registry import get_registry
    from mino_nexus.resources.device_store import get_device
    stored = get_device(sn) or {}
    live, _reason = get_registry().resolve(sn)
    live_device = live.devices.get(sn) if live is not None else None
    nid = str(
        node_id
        or (live.node_id if live is not None else "")
        or stored.get("current_scout_id")
        or ""
    ).strip()
    platform = str(
        (getattr(live_device, "platform", "") if live_device is not None else "")
        or stored.get("platform")
        or stored.get("type")
        or ""
    ).strip().lower()
    return platform, nid, str(stored.get("owner_user_id") or "")


def device_platform(sn: str, node_id: str = "") -> str:
    return _device_facts(sn, node_id)[0]


def is_physical_device(sn: str, node_id: str = "") -> bool:
    """实体手机是否插在某个 Scout 节点上；不判断它是否在线。"""
    if not str(sn or "").strip():
        return False
    return is_physical_platform(device_platform(sn, node_id))


def device_owner_id(sn: str, node_id: str = "") -> str:
    """设备归属账号。实体设备永远返回空串：它们不参与账号隔离。"""
    platform, nid, recorded_owner = _device_facts(sn, node_id)
    if is_physical_platform(platform):
        return ""
    owner = node_owner_id(nid) if nid else ""
    return owner if owner and (not recorded_owner or recorded_owner == owner) else ""


def require_owned_device(sn: str) -> str:
    uid = current_user_id()
    if not uid:
        raise HTTPException(status_code=401, detail="请先登录")
    if is_physical_device(sn):
        # 插在电脑上就默认可用；不追问它曾属于哪个账号。
        return uid
    if device_owner_id(sn) != uid:
        raise HTTPException(status_code=404, detail="没有这个设备")
    return uid
