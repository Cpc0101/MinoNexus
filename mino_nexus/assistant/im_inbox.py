"""LangBot 入站收件箱与「接管」开关。

三条约束来自 `docs/架构/V4/mino接入社交软件实现24小时在线.md` §0：

1. **渠道消息正文真源在 Scout**，Nexus 不落原话；本模块只存路由元数据，
   要正文时经 `plugin.im.history` 现取。
2. 默认仍由 Nexus 内建助手答复；只有打开接管（handoff）后才改由 MCP agent 拉取回复。
3. 只服务 LangBot 渠道（`langbot:feishu` / `langbot:wecom` / `langbot:dingtalk`）。
"""
from __future__ import annotations

import time
from typing import Any

from mino_nexus.core.database import session_scope
from mino_nexus.core.log import SLog

TAG = "ImInbox"

LANGBOT_PLATFORMS = ("feishu", "wecom", "dingtalk")
KEEP_PER_USER = 200
KEEP_SECONDS = 7 * 86400
HANDOFF_KEY = "im_handoff"

# 可以回传选定的回复上下文键；binding_key、delivery_key 等内部凭据不外泄。
_REPLY_KEYS = ("instance_id", "tenant", "sender_id", "thread_id", "source_message_id", "transport")


def _now() -> int:
    return int(time.time())


def langbot_channel(channel: str) -> bool:
    """入站 channel 是裸平台名（feishu / wecom / dingtalk）。

    Scout 的 im_bridge 只放行这三个裸名，绑定链路也用裸名；langbot: 前缀只在
    展示与配置侧出现，这里两种写法都认，入库统一写成带前缀，避免同一渠道两种键。
    """
    value = str(channel or "").strip().lower()
    if not value:
        return False
    name = value.removeprefix("langbot:")
    return name in LANGBOT_PLATFORMS


def platform_of(channel: str) -> str:
    return str(channel or "").strip().lower().removeprefix("langbot:")


def channel_key(channel: str) -> str:
    """入库键：统一 langbot:<平台>；非 LangBot 渠道原样返回。"""
    value = str(channel or "").strip()
    if not langbot_channel(value):
        return value
    return f"langbot:{platform_of(value)}"


def normalize_channels(channels: Any) -> list[str]:
    out: list[str] = []
    for raw in channels or []:
        value = platform_of(str(raw or "").strip())
        if not value:
            continue
        if value not in LANGBOT_PLATFORMS:
            raise ValueError(f"LangBot 只有 feishu / wecom / dingtalk，收到 {value}")
        if value not in out:
            out.append(value)
    return out


# ---------------------------------------------------------------- 收件箱


def record(owner: str, inbound: dict[str, Any], key: str) -> str:
    """落一条入站路由；非 LangBot 渠道直接忽略。返回条目 id（即去重 key）。"""
    channel = str(inbound.get("channel") or "")
    if not owner or not key or not langbot_channel(channel):
        return ""
    from mino_nexus.models.im_inbox import ImInboxItem

    reply_ctx = inbound.get("reply_ctx")
    with session_scope() as db:
        if db.get(ImInboxItem, key) is not None:
            return key
        db.add(ImInboxItem(
            id=key,
            user_id=owner,
            node_id=str(inbound.get("node_id") or ""),
            channel=channel_key(channel),
            instance_id=str(inbound.get("instance_id") or ""),
            tenant=str(inbound.get("tenant") or ""),
            chat_id=str(inbound.get("chat_id") or ""),
            chat_type="group" if inbound.get("chat_type") == "group" else "private",
            sender_id=str(inbound.get("sender_id") or ""),
            msg_id=str(inbound.get("msg_id") or ""),
            event_type=str(inbound.get("event_type") or "message"),
            reply_ctx={str(k): str(v) for k, v in (reply_ctx or {}).items()} if isinstance(reply_ctx, dict) else {},
            status="pending",
            handled_by="",
            handled_at=0,
            created_at=_now(),
        ))
        _prune(db, owner)
    SLog.i(TAG, f"入站入箱 channel={channel} chat={inbound.get('chat_id')}")
    return key


def _prune(db, owner: str) -> None:
    """只清理已处理的历史，待处理条目不因条数被丢弃。"""
    from mino_nexus.models.im_inbox import ImInboxItem

    query = db.query(ImInboxItem).filter(ImInboxItem.user_id == owner, ImInboxItem.status == "handled")
    query.filter(ImInboxItem.created_at < _now() - KEEP_SECONDS).delete(synchronize_session=False)
    stale = [row.id for row in db.query(ImInboxItem.id).filter(
        ImInboxItem.user_id == owner, ImInboxItem.status == "handled"
    ).order_by(ImInboxItem.created_at.desc()).offset(KEEP_PER_USER).all()]
    if stale:
        db.query(ImInboxItem).filter(ImInboxItem.id.in_(stale)).delete(synchronize_session=False)


def mark_handled(owner: str, key: str, by: str) -> bool:
    """把一条置为已处理；已处理的不再改归属。"""
    from mino_nexus.models.im_inbox import ImInboxItem

    if not owner or not key:
        return False
    with session_scope() as db:
        row = db.get(ImInboxItem, key)
        if row is None or row.user_id != owner or row.status != "pending":
            return False
        row.status = "handled"
        row.handled_by = str(by or "")[:16]
        row.handled_at = _now()
    return True


def pending(owner: str, *, channel: str = "", limit: int = 20, only_pending: bool = True) -> list[dict[str, Any]]:
    from mino_nexus.models.im_inbox import ImInboxItem

    count = max(1, min(int(limit or 20), 50))
    with session_scope() as db:
        query = db.query(ImInboxItem).filter(ImInboxItem.user_id == owner)
        if channel:
            # 入站按裸名进来、入库写 langbot:<平台>，两种写法都要能筛到。
            wanted = channel_key(str(channel))
            plain = platform_of(str(channel))
            query = query.filter(ImInboxItem.channel.in_([wanted, plain]))
        if only_pending:
            query = query.filter(ImInboxItem.status == "pending")
        rows = query.order_by(ImInboxItem.created_at.asc()).limit(count).all()
        return [_row(row) for row in rows]


def ack(owner: str, ids: list[str]) -> tuple[list[str], list[str]]:
    """标记处理完成；返回 (已完成, 不存在或不属于本人)。"""
    from mino_nexus.models.im_inbox import ImInboxItem

    wanted = [str(value) for value in ids or []][:50]
    done: list[str] = []
    missing: list[str] = []
    with session_scope() as db:
        for value in wanted:
            row = db.get(ImInboxItem, value)
            if row is None or row.user_id != owner:
                missing.append(value)
                continue
            if row.status == "pending":
                row.status = "handled"
                row.handled_by = "mcp"
                row.handled_at = _now()
            done.append(value)
    return done, missing


def _row(row) -> dict[str, Any]:
    """内部形状：保留完整 reply_ctx（含 binding_key），供读正文时复核绑定。"""
    ctx = row.reply_ctx if isinstance(row.reply_ctx, dict) else {}
    return {
        "id": row.id,
        "channel": row.channel,
        "platform": platform_of(row.channel),
        "node_id": row.node_id,
        "instance_id": row.instance_id,
        "tenant": row.tenant,
        "chat_id": row.chat_id,
        "chat_type": row.chat_type,
        "sender_id": row.sender_id,
        "msg_id": row.msg_id,
        "event_type": row.event_type,
        "status": row.status,
        "handled_by": row.handled_by,
        "created_at": int(row.created_at or 0),
        "reply_ctx": {str(key): str(value) for key, value in ctx.items()},
    }


def public(row: dict[str, Any]) -> dict[str, Any]:
    """对外形状：`reply_ctx` 只回传回复需要的键，内部凭据不外泄。"""
    ctx = row.get("reply_ctx") if isinstance(row.get("reply_ctx"), dict) else {}
    out = {key: value for key, value in row.items() if key != "reply_ctx"}
    out["reply_ctx"] = {key: str(ctx.get(key) or "") for key in _REPLY_KEYS}
    return out


# ---------------------------------------------------------------- 接管开关


def handoff(owner: str) -> dict[str, Any]:
    from mino_nexus.models.assistant import AssistantProfile

    with session_scope() as db:
        row = db.get(AssistantProfile, owner)
        extra = row.extra if row is not None and isinstance(row.extra, dict) else {}
    state = extra.get(HANDOFF_KEY) if isinstance(extra.get(HANDOFF_KEY), dict) else {}
    try:
        channels = normalize_channels(state.get("channels"))
    except ValueError:
        channels = []
    return {"enabled": bool(state.get("enabled")), "channels": channels}


def set_handoff(owner: str, enabled: bool, channels: Any = None) -> dict[str, Any]:
    from mino_nexus.models.assistant import AssistantProfile

    normalized = normalize_channels(channels)
    with session_scope() as db:
        row = db.get(AssistantProfile, owner)
        if row is None:
            row = AssistantProfile(user_id=owner)
            db.add(row)
        extra = dict(row.extra or {})
        extra[HANDOFF_KEY] = {"enabled": bool(enabled), "channels": normalized}
        row.extra = extra
    SLog.i(TAG, f"接管开关 enabled={bool(enabled)} channels={normalized}")
    return handoff(owner)


def takeover(owner: str, channel: str) -> bool:
    """这条渠道的入站是否交给外部 agent（内建助手跳过回复）。"""
    if not langbot_channel(channel):
        return False
    state = handoff(owner)
    if not state["enabled"]:
        return False
    return not state["channels"] or platform_of(channel) in state["channels"]
