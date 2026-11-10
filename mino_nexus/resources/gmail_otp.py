"""从 Scout 的 Gmail 插件取验证码。

取码本身在 Scout 本机（`plugin.gmail.fetch_otp`）。Nexus 只做账号归属校验、
参数收敛和字段裁剪，不连 IMAP、不保存邮件、不回传正文。
"""
from __future__ import annotations

import math
import re
import time
from typing import Any

OTP_CODE_RE = re.compile(r"\d{4,8}")

# Scout 的失败码翻译成用户能读的一句话；未登记的原样给出。
_ERROR_TEXT = {
    "otp_mail_not_received": "等待窗口内没有收到验证码邮件",
    "otp_mail_filter_no_match": "有邮件，但没有匹配到指定发件人、主题或收件人",
    "otp_mail_code_not_found": "邮件里没有找到 4-8 位验证码",
    "otp_wait_stopped": "取码等待已停止，请缩短等待窗口后重试",
    "otp_request_invalid": "取码参数不合法，请核对收件地址和等待窗口",
}


class OtpError(ValueError):
    """取码失败。code 是给调用方判别用的稳定码，message 是给用户看的。"""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = str(code or "otp_failed")
        self.message = message or _ERROR_TEXT.get(self.code) or self.code
        super().__init__(self.message)


class NodeUnavailable(OtpError):
    def __init__(self, message: str) -> None:
        super().__init__("otp_node_unavailable", message)


def _inbox_address(plugin: dict[str, Any]) -> str:
    values = plugin.get("values") if isinstance(plugin.get("values"), dict) else {}
    return str(values.get("inbox_address") or "").strip()


def _valid_address(value: str) -> bool:
    return bool(value) and "@" in value and not any(ch.isspace() for ch in value)


def fetch_otp(
    *,
    node_id: str = "",
    to_address: str = "",
    since_ts: float | None = None,
    lookback_seconds: int = 600,
    max_wait_ms: int = 15000,
    poll_interval_ms: int = 200,
    subject_contains: str = "",
    from_allowlist: list[str] | None = None,
) -> dict[str, Any]:
    """在当前账号节点上取一封邮件的验证码。

    `to_address` 省略时用该节点 Gmail 配置的收件箱。返回
    `{node_id, code, source, summary, diagnostics}`，不含正文与凭据。
    """
    from mino_nexus.resources.node_plugin_call import (
        PluginCallError,
        call_plugin_with_raw,
        node_for_plugin,
    )

    try:
        node = node_for_plugin("mail", "gmail", node_id)
    except PluginCallError as exc:
        raise NodeUnavailable(str(exc)) from exc

    recipient = str(to_address or "").strip()
    if not recipient:
        for plugin in node.plugins or []:
            if plugin.get("class") == "mail" and plugin.get("id") == "gmail":
                recipient = _inbox_address(plugin)
                break
    if recipient and not _valid_address(recipient):
        raise OtpError("otp_request_invalid", "收件地址不合法")

    now = time.time()
    start = float(since_ts) if since_ts else now - max(10, min(int(lookback_seconds), 86400))
    if not math.isfinite(start) or start > now + 60 or start < now - 86400 * 7:
        raise OtpError("otp_request_invalid", "since_ts 只能取最近 7 天，且不能晚于当前时间")
    wait_ms = max(1000, min(int(max_wait_ms or 15000), 45000))
    interval_ms = max(500, min(int(poll_interval_ms or 3000), 10000))

    try:
        data, raw, summary, _node = call_plugin_with_raw(
            kind="mail", plugin_id="gmail", node_id=node.node_id,
            capability_id="plugin.gmail.fetch_otp",
            params={
                "to_address": recipient, "since_ts": start,
                "deadline_ts": now + wait_ms / 1000.0 + 5,
                "poll_interval_ms": interval_ms, "max_wait_ms": wait_ms,
                "from_allowlist": list(from_allowlist or [])[:10],
                "subject_contains": str(subject_contains or "")[:200],
            },
            timeout=wait_ms / 1000.0 + 10,
        )
    except PluginCallError as exc:
        code = str(exc)
        raise OtpError(code if code in _ERROR_TEXT else "otp_failed", _ERROR_TEXT.get(code, code)) from exc

    code = str(raw.get("code") or data.get("code") or "").strip()
    if not OTP_CODE_RE.fullmatch(code):
        raise OtpError("otp_code_missing", summary or "没有取到验证码，请确认收件邮箱和等待窗口")
    raw_diag = raw.get("otp_diagnostics") if isinstance(raw.get("otp_diagnostics"), dict) else {}
    diagnostics = {key: raw_diag[key] for key in ("connection_attempts", "reconnects", "error_code", "phase")
                   if isinstance(raw_diag.get(key), (str, int, float, bool))}
    return {
        "node_id": node.node_id,
        "code": code,
        "source": "gmail",
        "summary": summary or "已取到验证码",
        "diagnostics": diagnostics,
    }
