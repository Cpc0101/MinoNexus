"""插件快照 → IM 渠道视图：纯 dict 变换。"""
from __future__ import annotations

from typing import Any


def im_route(channel: str) -> tuple[str, str, str, str]:
    """渠道名 → (插件 class, 插件 id, 节点快照渠道名, Scout 渠道名)。

    只接受已建模的三类宿主：飞书 CLI、微信 iLink、LangBot（飞书/企微/钉钉）。
    发送、入站确认、历史读取、收件箱必须共用这一张映射，避免各自拼 kind/plugin_id。
    """
    key = str(channel or "").strip()
    if key == "feishu_cli":
        return "cli", "feishu", "feishu_cli", "feishu_cli"
    if key == "wechat":
        return "bot", "wechat", "wechat", "wechat"
    name = key.removeprefix("langbot:")
    if name in ("feishu", "wecom", "dingtalk"):
        return "im", "langbot", f"langbot:{name}", name
    raise ValueError("渠道只能是 feishu_cli、wechat、feishu、wecom 或 dingtalk")


def normalize_im_channels(plugins: Any) -> list[dict[str, Any]]:
    channels = []
    for plugin in plugins or []:
        if not isinstance(plugin, dict):
            continue
        pid = str(plugin.get("id") or "")
        status = plugin.get("status") if isinstance(plugin.get("status"), dict) else {}
        installed = plugin.get("installed") if isinstance(plugin.get("installed"), bool) else None
        running = status.get("state") == "running" if status.get("state") else None
        if pid == "feishu" and plugin.get("class") == "cli":
            entries = [("feishu_cli", status)]
        elif pid == "wechat" and plugin.get("class") == "bot":
            entries = [("wechat", status)]
        elif pid == "langbot" and plugin.get("class") == "im":
            platforms = plugin.get("platforms") if isinstance(plugin.get("platforms"), dict) else {}
            entries = [(f"langbot:{platform}", state) for platform, state in platforms.items() if isinstance(state, dict)]
            if not entries:
                entries = [("langbot", {})]
        else:
            continue
        for channel, state in entries:
            reported = isinstance(state.get("connected"), bool)
            verified = pid not in ("langbot", "feishu") or state.get("connection_verified") is True
            connected = state.get("connected") if reported and verified else None
            holding = state.get("holding") if isinstance(state.get("holding"), bool) else None
            error = str(state.get("error") or status.get("error") or "")[:200]
            try:
                last_message = int(state.get("last_message_at") or 0)
            except (TypeError, ValueError):
                last_message = 0
            if installed is False:
                connection_state = "not_installed"
            elif state.get("enabled") is False:
                connection_state = "disabled"
            elif error:
                connection_state = "error"
            elif holding is False:
                connection_state = "yielded"
            elif not reported:
                connection_state = "unreported"
            elif not verified:
                connection_state = "pending"
            elif connected:
                connection_state = "connected"
            else:
                connection_state = "disconnected"
            channels.append({
                "channel": channel,
                "instance_id": str(state.get("instance_id") or ""),
                "installed": installed,
                "running": running,
                "process_state": str(status.get("state") or ""),
                "enabled": state.get("enabled"),
                "connected": connected,
                "holding": holding,
                "connection_state": connection_state,
                "connection_verified": reported and verified,
                "connection_source": str(state.get("connection_source") or ""),
                "capabilities": state.get("capabilities") if isinstance(state.get("capabilities"), dict) else {},
                "error": error,
                "last_message_at": last_message,
            })
    return channels
