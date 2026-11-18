"""IM 入站：去重、绑定、群聊 @、限流，然后交给助手。"""
from __future__ import annotations

import time
import re
import hashlib
import json
import threading
from mino_nexus.platform.identity import execution_user
from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.assistant import store as store
from mino_nexus.assistant.assistant_agent import handle_utterance

TAG = "AssistantGateway"
_WINDOW = 60
_LIMIT = 20
_SOURCE_SNAPSHOT_WAIT_SEC = 20.0
_hits: dict[str, list[float]] = {}
_hit_lock = threading.Lock()


def _failure_diagnostic(exc: Exception, stage: str) -> dict[str, Any]:
    """只记录固定原因码和代码位置，不记录异常正文、聊天或凭据。"""
    reasons = {
        "缺少执行账号，已拒绝执行": "execution_user_missing",
        "执行账号不匹配": "execution_user_mismatch",
        "会话已被新消息打断，旧解析结果不再执行": "generation_superseded",
        "入站处理租约已失效，禁止继续写操作": "inbound_lease_lost",
        "群策略已撤销": "group_policy_revoked",
    }
    reason = "internal_error"
    if isinstance(exc, store.ImBindingError):
        reason = exc.reason
    elif isinstance(exc, PermissionError):
        reason = reasons.get(str(exc), "permission_denied")
    sites = []
    tb = exc.__traceback__
    while tb is not None:
        frame = tb.tb_frame
        sites.append(f"{frame.f_globals.get('__name__', '').rsplit('.', 1)[-1]}.{frame.f_code.co_name}:{tb.tb_lineno}")
        tb = tb.tb_next
    return {"error_type": type(exc).__name__, "error_reason": reason,
            "error_stage": stage, "error_site": " > ".join(sites[-3:])}


def _limited_unlocked(user_id: str) -> bool:
    now = time.time()
    rows = [t for t in _hits.get(user_id, []) if now - t < _WINDOW]
    if len(rows) >= _LIMIT:
        _hits[user_id] = rows
        return True
    rows.append(now)
    _hits[user_id] = rows
    return False


def _limited(user_id: str) -> bool:
    with _hit_lock:
        return _limited_unlocked(user_id)


def validated_source(inbound: dict[str, Any]) -> str:
    return _source_validation(inbound)[0]


from mino_nexus.resources.node_source import source_validation as _source_validation  # noqa: E402
def _bind_authorized_owner(inbound: dict[str, Any], owner: str) -> str:
    """Only the verified channel login user inherits the authenticated node owner."""
    channel = inbound.get("channel")
    identity_fields = {
        "feishu_cli": ("cli", "feishu", "authorized_sender_id"),
        "wechat": ("bot", "wechat", "account_id"),
    }
    if channel not in identity_fields or inbound.get("chat_type") != "private":
        return ""
    if validated_source(inbound) != owner:
        return ""
    from mino_nexus.resources.node_registry import get_registry
    node = get_registry().get_node(str(inbound.get("node_id") or ""))
    if node is None:
        return ""
    kind, plugin_id, identity_field = identity_fields[channel]
    for plugin in node.plugins or []:
        if plugin.get("class") != kind or plugin.get("id") != plugin_id:
            continue
        status = plugin.get("status") or {}
        sender = status.get(identity_field)
        if (not sender or sender != inbound.get("sender_id")
                or status.get("instance_id") != inbound.get("instance_id")):
            return ""
        return store.bind_identity(owner, channel=channel, tenant=str(inbound.get("tenant") or ""),
            sender_id=sender, instance_id=inbound["instance_id"], node_id=inbound["node_id"])
    return ""


def send_to_chat(*, channel: str, node_id: str, chat_id: str, sender_id: str, reply_ctx: dict[str, Any], text: str, delivery_id: str = "", card: dict | None = None) -> dict:
    """按渠道把一段文字发回会话。微信走 iLink 插件，飞书 / 企微 / 钉钉经 LangBot。
    reply_ctx 原样带回：Scout 靠它知道往哪发（微信的 context_token、LangBot 的 bot_uuid / target）。"""
    if not text:
        return
    from mino_nexus.resources.node_plugin_call import call_plugin

    kind, plugin_id = ("cli", "feishu") if channel == "feishu_cli" else (("bot", "wechat") if channel == "wechat" else ("im", "langbot"))
    return call_plugin(
        kind=kind,
        plugin_id=plugin_id,
        node_id=node_id,
        capability_id="plugin.im.send",
        params={
            "channel": channel,
            "chat_id": chat_id,
            "sender_id": sender_id,
            "reply_ctx": reply_ctx if isinstance(reply_ctx, dict) else {},
            "text": text,
            "delivery_id": delivery_id,
            "card": card or {},
        },
        timeout=30,
    )


def _send(inbound: dict[str, Any], text: str, card: dict | None = None, stream=None, *, turn_id: str = "") -> None:
    from mino_nexus.assistant.assistant_cards import split_text
    from mino_nexus.assistant.assistant_envelope import capabilities
    from mino_nexus.assistant import assistant_conversation as conversation
    from mino_nexus.platform.llm.dispatch_log import ctx
    capability = capabilities(inbound.get("capabilities"))
    if card and card.get("card_type") == "clarification" and (card.get("request") or {}).get("id"):
        text += "\n\n请求编号：#" + card["request"]["id"][-8:]
    origin = conversation.origin_for(str(inbound.get("channel") or ""), str(inbound.get("chat_id") or ""),
                                     str(inbound.get("node_id") or ""), inbound.get("reply_ctx") or {})
    request = (card or {}).get("request") or {}
    if card and inbound.get("chat_type") == "group":
        policy = conversation.group_policy(origin)
        if not policy.get("enabled"):
            raise PermissionError("群策略已撤销")
        if card.get("card_type") == "notification" and not policy.get("disclose_results"):
            text = "本次请求状态：" + str((card.get("header") or {}).get("title") or "已处理") + "。详细结果请在私聊或 Workbench 查看。"
            card = None
    segments = split_text(text, capability["max_characters"], capability["length_unit"])
    if turn_id:
        store.record_turn_delivery(turn_id, text, "prepared")
    for index, segment in enumerate(segments):
        scope = str(ctx().get("assistant_inbound_key") or inbound.get("msg_id") or "")
        delivery_id = hashlib.sha256(json.dumps([origin, scope, index, segment], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        try:
            result = stream.finish(segment, card) if stream is not None and index == 0 else None
            if result is None:
                result = send_to_chat(channel=origin["channel"], node_id=origin["node_id"], chat_id=origin["chat_id"],
                sender_id=origin["sender_id"], reply_ctx=inbound.get("reply_ctx") or {}, text=segment,
                delivery_id=delivery_id, card=card if index == 0 else None)
        except Exception:
            if turn_id:
                store.record_turn_delivery(turn_id, text, "partially_sent" if index else "failed", sent_segments=index,
                                           sent_text="\n\n".join(segments[:index]))
            raise
        if turn_id:
            store.record_turn_delivery(turn_id, text, "sent" if index + 1 == len(segments) else "partially_sent", sent_segments=index + 1,
                                       sent_text="\n\n".join(segments[:index + 1]))
        if request:
            conversation.reference(origin, request.get("id", ""), int(request.get("revision") or 0),
                                   message_id=delivery_id, provider_id=str(result.get("provider_message_id") or delivery_id))


def _ack(inbound: dict[str, Any]) -> bool:
    if not inbound.get("delivery_key"):
        return True
    from mino_nexus.resources.node_plugin_call import call_plugin
    channel = str(inbound.get("channel") or "")
    try:
        call_plugin(kind="cli" if channel == "feishu_cli" else ("bot" if channel == "wechat" else "im"), plugin_id="feishu" if channel == "feishu_cli" else ("wechat" if channel == "wechat" else "langbot"),
                    node_id=str(inbound.get("node_id") or ""), capability_id="plugin.im.ack", params={
                        "channel": channel, "chat_id": inbound.get("chat_id"), "sender_id": inbound.get("sender_id"),
                        "reply_ctx": inbound.get("reply_ctx") or {}, "delivery_key": inbound["delivery_key"]}, timeout=10)
    except Exception as exc:
        # ACK 失败不代表消息业务处理失败。保留 done/uncertain，重投只重试 ACK。
        reason = str(exc) if str(exc) in {"request_key_conflict", "request_inflight"} else type(exc).__name__
        SLog.w(TAG, f"入站确认失败，等待渠道重投后重试 ACK：{reason}")
        return False
    return True


def _shortcut(inbound: dict[str, Any], user_id: str, text: str) -> str:
    raw = text.strip()
    if raw in ("/帮助", "/help"):
        return "可以说：有哪些项目、设备在不在线、最近任务、在某台设备上跑用例。绑定：先在 Workbench 生成绑定码，再发 /绑定 和那 6 位数字。"
    if raw.startswith("/绑定"):
        code = raw.replace("/绑定", "", 1).strip()
        uid = store.consume_bind_code(
            code,
            channel=str(inbound.get("channel") or ""),
            tenant=str(inbound.get("tenant") or ""),
            sender_id=str(inbound.get("sender_id") or ""),
            instance_id=str(inbound.get("instance_id") or ""),
            node_id=str(inbound.get("node_id") or ""),
        )
        if not uid:
            return "绑定码无效或已过期。请在 Workbench 的 Scout 节点对应渠道配置中重新生成。"
        if user_id and user_id == uid:
            return "你已经绑定过了，不用再绑定，直接说事就行。"
        return "已绑定。之后我会按你的账号查询和执行。"
    if raw in ("/状态", "/status") and user_id:
        profile = store.get_profile(user_id)
        return "助手已绑定，当前已启用。" if profile["enabled"] else "助手已绑定，当前已停用。"
    return ""


def handle_inbound(inbound: dict[str, Any]) -> None:
    """对话入站已移除；旧 Scout 事件在 node_registry 忽略，不再进入助手。"""
    del inbound


def confirmed_history(inbound: dict[str, Any], binding: dict[str, Any], *,
                      limit: int = 8, text_limit: int = 500) -> list[dict[str, Any]]:
    """仅接受当前绑定之后、同实例/租户/实际发送者的本地上下文。"""
    out = []
    rows = inbound.get("recent_turns")
    for row in rows[-max(1, min(limit, 100)):] if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("role") not in ("user", "assistant"):
            continue
        if any(str(row.get(k) or "") != str(inbound.get(k) or "")
               for k in ("instance_id", "tenant", "sender_id", "thread_id")):
            continue
        try:
            ts = int(row.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if not int(binding.get("bound_at") or 0) < ts <= int(time.time()):
            continue
        out.append({"role": row["role"], "text": str(row.get("text") or "")[:text_limit], "ts": ts})
    return out
