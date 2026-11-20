"""测试账号凭据的字段级加密与历史明文迁移。

数据库只保存密文；明文仅在已认证调用链内按字段解密。主密钥不进入
``mino.db``，也不随数据库备份传播。
"""
from __future__ import annotations

import base64
import json
import os
import re
import stat
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_PREFIX = "enc:v1:"
_SCHEMA_VERSION = "v1"
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class CredentialCipherError(RuntimeError):
    """可安全对外展示的凭据密钥错误，不携带密钥、密文或底层异常。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CredentialKeyring:
    keys: dict[str, bytes]
    active_version: str


def _decode_key(raw: str) -> bytes:
    value = str(raw or "").strip()
    if not value:
        raise CredentialCipherError("credential_master_key_invalid")
    try:
        padded = value + "=" * (-len(value) % 4)
        key = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        raise CredentialCipherError("credential_master_key_invalid") from None
    if len(key) != 32:
        raise CredentialCipherError("credential_master_key_invalid")
    return key


def _validate_key_version(value: str) -> str:
    version = str(value or "").strip()
    if not _VERSION_PATTERN.fullmatch(version):
        raise CredentialCipherError("credential_key_version_invalid")
    return version


def _private_key_file(path: Path) -> bytes:
    """读取密钥文件，拒绝符号链接和 group/world 可读权限。"""
    raw = path.expanduser()
    try:
        info = raw.lstat()
    except FileNotFoundError:
        raise CredentialCipherError("credential_master_key_file_missing") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise CredentialCipherError("credential_master_key_file_invalid")
    if os.name != "nt" and info.st_mode & 0o077:
        raise CredentialCipherError("credential_master_key_file_permissions")
    try:
        return _decode_key(raw.read_text(encoding="utf-8"))
    except OSError:
        raise CredentialCipherError("credential_master_key_file_unreadable") from None


def _default_development_key_file() -> Path:
    from mino_nexus.core.paths import data_dir

    secret_dir = data_dir() / "secrets"
    secret_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(secret_dir, 0o700)
    path = secret_dir / "credential-master.key"
    try:
        info = path.lstat()
    except FileNotFoundError:
        key = AESGCM.generate_key(bit_length=256)
        encoded = base64.urlsafe_b64encode(key).decode("ascii") + "\n"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            pass
        else:
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                path.unlink(missing_ok=True)
                raise
    else:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise CredentialCipherError("credential_master_key_file_invalid")
    return path


@lru_cache(maxsize=8)
def _load_keyring(
    mode: str,
    data_root: str,
    master_key: str,
    master_key_file: str,
    keys_json: str,
    requested_version: str,
) -> CredentialKeyring:
    sources = [bool(master_key), bool(master_key_file), bool(keys_json)]
    if sum(sources) > 1:
        raise CredentialCipherError("credential_key_source_conflict")

    if keys_json:
        try:
            raw = json.loads(keys_json)
        except json.JSONDecodeError:
            raise CredentialCipherError("credential_keys_json_invalid") from None
        if not isinstance(raw, dict) or not raw:
            raise CredentialCipherError("credential_keys_json_invalid")
        keys = {
            _validate_key_version(str(version)): _decode_key(str(key))
            for version, key in raw.items()
        }
        active = _validate_key_version(requested_version or "1")
        if active not in keys:
            raise CredentialCipherError("credential_active_key_missing")
        return CredentialKeyring(keys=keys, active_version=active)

    active = _validate_key_version(requested_version or "1")
    if master_key:
        key = _decode_key(master_key)
    elif master_key_file:
        key = _private_key_file(Path(master_key_file))
    elif mode == "development":
        # data_root 参与缓存键；实际目录选择仍由 core.paths 的环境标记保证隔离。
        key = _private_key_file(_default_development_key_file())
    else:
        raise CredentialCipherError("credential_master_key_missing")
    return CredentialKeyring(keys={active: key}, active_version=active)


def _keyring() -> CredentialKeyring:
    from mino_nexus.core.paths import get_app_data_dir
    from mino_nexus.core.runtime_mode import is_development

    mode = "development" if is_development() else "production"
    return _load_keyring(
        mode,
        str(get_app_data_dir()),
        os.environ.get("MINO_NEXUS_CREDENTIAL_MASTER_KEY", ""),
        os.environ.get("MINO_NEXUS_CREDENTIAL_MASTER_KEY_FILE", ""),
        os.environ.get("MINO_NEXUS_CREDENTIAL_KEYS_JSON", ""),
        os.environ.get("MINO_NEXUS_CREDENTIAL_KEY_VERSION", ""),
    )


def _aad(*, project_id: str, account_id: str, field: str, key_version: str) -> bytes:
    payload = {
        "account_id": str(account_id),
        "field": str(field),
        "key_version": str(key_version),
        "project_id": str(project_id),
        "schema": _SCHEMA_VERSION,
    }
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")


def encrypt_secret(value: Any, *, project_id: str, account_id: str, field: str) -> str:
    """加密一个凭据字段；空值保持空字符串。"""
    plain = "" if value is None else str(value)
    if not plain:
        return ""
    ring = _keyring()
    version = ring.active_version
    nonce = os.urandom(12)
    ciphertext = AESGCM(ring.keys[version]).encrypt(
        nonce,
        plain.encode("utf-8"),
        _aad(project_id=project_id, account_id=account_id, field=field, key_version=version),
    )
    payload = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii").rstrip("=")
    return f"{_PREFIX}{version}:{payload}"


def decrypt_secret(value: Any, *, project_id: str, account_id: str, field: str) -> str:
    """解密字段；历史上未加密的值只读兼容，新写入始终走加密。"""
    token = "" if value is None else str(value)
    if not token:
        return ""
    if not token.startswith(_PREFIX):
        return token
    try:
        version, payload = token[len(_PREFIX):].split(":", 1)
        version = _validate_key_version(version)
        ring = _keyring()
        key = ring.keys.get(version)
        if key is None:
            raise CredentialCipherError("credential_key_version_missing")
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        if len(raw) <= 12:
            raise ValueError("short")
        plain = AESGCM(key).decrypt(
            raw[:12],
            raw[12:],
            _aad(project_id=project_id, account_id=account_id, field=field, key_version=version),
        )
        return plain.decode("utf-8")
    except CredentialCipherError:
        raise
    except Exception:
        raise CredentialCipherError("credential_decrypt_failed") from None


def is_encrypted_secret(value: Any) -> bool:
    return str(value or "").startswith(_PREFIX)


def credential_encryption_status() -> dict[str, Any]:
    """只报告配置状态，不返回密钥、指纹或任何凭据值。"""
    ring = _keyring()
    return {
        "configured": True,
        "active_key_version": ring.active_version,
        "available_key_versions": sorted(ring.keys),
    }


def reset_credential_cipher_cache() -> None:
    """仅供进程内测试或运维代码在切换环境变量后清空缓存。"""
    _load_keyring.cache_clear()
