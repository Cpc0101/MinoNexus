"""凭据落库迁移与状态统计；不输出任何凭据值。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from mino_nexus.core.database import session_scope
from mino_nexus.models.pool_account import PoolAccount
from mino_nexus.resources.credential_cipher import encrypt_secret, is_encrypted_secret


def _field_stats(value: Any) -> str:
    if not str(value or ""):
        return "empty"
    return "encrypted" if is_encrypted_secret(value) else "plaintext"


def credential_database_status() -> dict[str, Any]:
    """统计密码/OTP 的密文状态；不返回项目、账号或凭据内容。"""
    counts = {
        "accounts": 0,
        "password": {"empty": 0, "encrypted": 0, "plaintext": 0},
        "otp": {"empty": 0, "encrypted": 0, "plaintext": 0},
    }
    with session_scope() as db:
        for row in db.query(PoolAccount).all():
            counts["accounts"] += 1
            counts["password"][_field_stats(row.password)] += 1
            counts["otp"][_field_stats(row.otp)] += 1
    return counts


def migrate_plaintext_credentials(*, dry_run: bool = False) -> dict[str, Any]:
    """把历史明文密码/OTP 原地转为 AES-GCM 密文。

    调用方必须持有数据目录服务锁并先完成一致备份。已加密字段不会重复加密。
    """
    migrated = {"password": 0, "otp": 0}
    skipped = {"password": 0, "otp": 0}
    with session_scope() as db:
        if not dry_run:
            db.execute(text("BEGIN IMMEDIATE"))
        rows = db.query(PoolAccount).all()
        for row in rows:
            for field in ("password", "otp"):
                value = getattr(row, field)
                if not str(value or ""):
                    continue
                if is_encrypted_secret(value):
                    skipped[field] += 1
                    continue
                migrated[field] += 1
                if not dry_run:
                    setattr(
                        row,
                        field,
                        encrypt_secret(
                            value,
                            project_id=str(row.project_id),
                            account_id=str(row.id),
                            field=field,
                        ),
                    )
        if dry_run:
            db.rollback()
    return {
        "dry_run": bool(dry_run),
        "accounts_scanned": len(rows),
        "migrated": migrated,
        "already_encrypted": skipped,
    }


def backup_sqlite_database(data_root: Path) -> Path | None:
    """使用 SQLite backup API 生成一致备份；源不存在时返回 None。"""
    source = Path(data_root) / "mino.db"
    if not source.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = source.with_name(f"{source.name}.pre-credential-encryption-{stamp}.backup")
    src = sqlite3.connect(source)
    try:
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        src.close()
    return destination
