"""凭据脱敏：对外响应、审计日志和导入预览不得携带明文凭据。"""
from __future__ import annotations

import re
from typing import Any

_MARKER = "[REDACTED]"

_EXACT_CREDENTIAL_KEYS = frozenset({
    "password",
    "passwd",
    "pwd",
    "otp",
    "sms_code",
    "smscode",
    "totp",
    "secret",
    "secrets",
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "api_key",
    "apikey",
    "private_key",
    "client_secret",
    "authorization",
    "cookie",
    "cookies",
    "session_key",
    "storage_state",
    "session_storage",
    "credential",
    "credentials",
    "channel_secrets",
})

_CREDENTIAL_FALSE_POSITIVES = frozenset({
    "credential_fields",
    "credential_fields_changed",
    "has_password",
    "has_otp",
    "password_configured",
    "otp_configured",
})

_TEXT_PATTERN = re.compile(
    r"(?i)\b("
    r"password|passwd|pwd|otp|sms[_ -]?code|totp|secret|token|access[_ -]?token|"
    r"refresh[_ -]?token|id[_ -]?token|api[_ -]?key|private[_ -]?key|"
    r"client[_ -]?secret|authorization|cookie|session[_ -]?key|storage[_ -]?state"
    r")\b(\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)"
)


def _normal_key(key: Any) -> str:
    return re.sub(r"[\s-]+", "_", str(key or "").strip().lower())


def _is_credential_key(key: Any) -> bool:
    normalized = _normal_key(key)
    if normalized in _CREDENTIAL_FALSE_POSITIVES:
        return False
    if normalized in _EXACT_CREDENTIAL_KEYS:
        return True
    return normalized.endswith((
        "_password", "_passwd", "_pwd", "_otp", "_token", "_secret",
        "_api_key", "_apikey", "_private_key", "_session_key", "_cookies",
    ))


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (dict, list, tuple, set)):
        return bool(value)
    return bool(value)


def scrub_credential_text(text: Any) -> str:
    """清洗自由文本里的 key=value / key: value；不处理手机号、邮箱等审计字段。"""
    raw = "" if text is None else str(text)
    if not raw:
        return ""
    return _TEXT_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}{_MARKER}", raw)


def sanitize_credentials(value: Any) -> Any:
    """递归返回不含凭据值的副本，只保留 ``*_configured`` 状态。"""
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if _is_credential_key(key):
                if _has_value(item):
                    out[f"{_normal_key(key)}_configured"] = True
                continue
            out[key] = sanitize_credentials(item)
        return out
    if isinstance(value, list):
        return [sanitize_credentials(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_credentials(item) for item in value]
    if isinstance(value, set):
        return [sanitize_credentials(item) for item in sorted(value, key=str)]
    if isinstance(value, str):
        return scrub_credential_text(value)
    return value
