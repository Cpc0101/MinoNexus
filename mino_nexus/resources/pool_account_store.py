"""号池账号 SQLite 真源；从 projects.env.test_accounts 一次性迁入。"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from mino_nexus.core.database import session_scope
from mino_nexus.models.pool_account import PoolAccount, PoolAccountFacet
from mino_nexus.models.account_data import AccountData, AccountDataRequest
from mino_nexus.resources.credential_cipher import decrypt_secret, encrypt_secret


def _now() -> int:
    return int(time.time())


def _facets_for_account(db, project_id: str, account_id: str, *, apply_observations: bool = True) -> dict[str, str]:
    rows = (
        db.query(PoolAccountFacet)
        .filter(
            PoolAccountFacet.project_id == project_id,
            PoolAccountFacet.account_id == account_id,
        )
        .all()
    )
    facets = {str(r.facet_key): str(r.facet_value or "unknown") for r in rows}
    if not apply_observations:
        return facets
    from mino_nexus.resources.account_fact_scope import overlay
    return overlay(db, project_id, account_id, facets)


def _registered_at_iso(ts: int) -> str:
    if not ts or int(ts) <= 0:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).astimezone().isoformat(timespec="seconds")
    except (OSError, ValueError, OverflowError):
        return ""


def _account_to_dict(
    row: PoolAccount,
    facets: dict[str, str],
    *,
    include_credentials: bool = False,
) -> dict[str, Any]:
    created = int(row.created_at or 0)
    out = {
        "id": row.id,
        "account_id": row.id,
        "display_name": row.display_name or "",
        "name": row.display_name or "",
        "env": row.env or "test",
        "kind": row.kind or "mixed",
        "phone": row.phone or "",
        "email": row.email or "",
        "username": row.username or "",
        "has_password": bool(row.password),
        "has_otp": bool(row.otp),
        "profile_id": row.profile_id or "",
        "app_ids": list(row.app_ids or []),
        "note": row.note or "",
        "locked": bool(row.locked),
        "lease": row.lease if isinstance(row.lease, dict) else {},
        "facets": dict(facets),
        "created_at": created,
        "registered_at": _registered_at_iso(created),
        "updated_at": int(row.updated_at or 0),
    }
    if include_credentials:
        out["password"] = decrypt_secret(
            row.password,
            project_id=str(row.project_id),
            account_id=str(row.id),
            field="password",
        )
        out["otp"] = decrypt_secret(
            row.otp,
            project_id=str(row.project_id),
            account_id=str(row.id),
            field="otp",
        )
    return out


def _read_credentials(
    row: PoolAccount,
    *,
    fields: tuple[str, ...],
) -> dict[str, str]:
    """只解密请求的字段；调用方必须先完成账号范围校验。"""
    out: dict[str, str] = {}
    for field in fields:
        if field not in {"password", "otp"}:
            raise ValueError("credential_field_invalid")
        out[field] = decrypt_secret(
            getattr(row, field),
            project_id=str(row.project_id),
            account_id=str(row.id),
            field=field,
        )
    return out


def _write_facets(db, project_id: str, account_id: str, facets: dict[str, Any]) -> None:
    db.query(PoolAccountFacet).filter(
        PoolAccountFacet.project_id == project_id,
        PoolAccountFacet.account_id == account_id,
    ).delete(synchronize_session=False)
    for key, val in (facets or {}).items():
        k = str(key or "").strip()
        if not k:
            continue
        v = str(val or "").strip().lower() or "unknown"
        db.add(
            PoolAccountFacet(
                project_id=project_id,
                account_id=account_id,
                facet_key=k[:32],
                facet_value=v[:64],
            )
        )


def _upsert_row(
    db,
    project_id: str,
    row: dict[str, Any],
    *,
    allow_legacy_empty_scope: bool = False,
) -> None:
    aid = str(row.get("account_id") or row.get("id") or "").strip()
    if not aid:
        return
    pid = str(project_id or "").strip()
    if not pid:
        raise ValueError("project_id_required")
    now = _now()
    existing = (
        db.query(PoolAccount)
        .filter(PoolAccount.project_id == project_id, PoolAccount.id == aid)
        .one_or_none()
    )
    if existing is None:
        existing = PoolAccount(id=aid, project_id=project_id, created_at=now)
        db.add(existing)
    from mino_nexus.resources.accounts.app_scope import normalize_app_ids
    raw_app_ids = row.get("app_ids") if "app_ids" in row else (existing.app_ids or [])
    if allow_legacy_empty_scope and raw_app_ids == []:
        app_ids = []
    else:
        app_ids = normalize_app_ids(raw_app_ids, required=True)
    if (existing.lease or {}).get("run_id") and app_ids != (existing.app_ids or []):
        raise ValueError("account_busy; cannot change app_ids while leased")
    existing.app_ids = app_ids
    existing.env = str(row.get("env") or "test")[:32]
    existing.display_name = str(row.get("display_name") or "")[:80]
    existing.phone = str(row.get("phone") or "")[:32]
    existing.email = str(row.get("email") or "")[:80]
    existing.username = str(row.get("username") or "")[:80]
    # 不能截断密文；长度约束在调用方规范化明文时执行。
    if "password" in row:
        existing.password = encrypt_secret(
            row.get("password"),
            project_id=pid,
            account_id=aid,
            field="password",
        )
    if "otp" in row:
        existing.otp = encrypt_secret(
            row.get("otp"),
            project_id=pid,
            account_id=aid,
            field="otp",
        )
    existing.kind = str(row.get("kind") or "mixed")[:24]
    existing.profile_id = str(row.get("profile_id") or "")[:40]
    existing.note = str(row.get("note") or "")[:200]
    existing.locked = 1 if row.get("locked") else 0
    lease = row.get("lease") if isinstance(row.get("lease"), dict) else {}
    previous_lease = existing.lease if isinstance(existing.lease, dict) else {}
    existing.lease = previous_lease if previous_lease.get("run_id") else lease
    existing.updated_at = now
    facets = row.get("facets") if isinstance(row.get("facets"), dict) else {}
    _write_facets(db, project_id, aid, facets)


def count_accounts(project_id: str) -> int:
    pid = str(project_id or "").strip()
    if not pid:
        return 0
    with session_scope() as db:
        return db.query(PoolAccount).filter(PoolAccount.project_id == pid).count()





def list_accounts(project_id: str, env_doc: dict | None = None) -> list[dict[str, Any]]:
    pid = str(project_id or "").strip()
    if not pid:
        return []
    with session_scope() as db:
        accounts = (
            db.query(PoolAccount)
            .filter(PoolAccount.project_id == pid)
            .order_by(PoolAccount.updated_at.desc())
            .all()
        )
        out: list[dict[str, Any]] = []
        for acc in accounts:
            facets = _facets_for_account(db, pid, acc.id)
            out.append(_account_to_dict(acc, facets))
        return out


def get_account(project_id: str, account_id: str) -> dict[str, Any] | None:
    """读取单个账号的脱敏视图；不会批量解密号池。"""
    pid = str(project_id or "").strip()
    aid = str(account_id or "").strip()
    if not pid or not aid:
        return None
    with session_scope() as db:
        acc = (
            db.query(PoolAccount)
            .filter(PoolAccount.project_id == pid, PoolAccount.id == aid)
            .one_or_none()
        )
        if acc is None:
            return None
        return _account_to_dict(acc, _facets_for_account(db, pid, aid))


def read_account_credentials(
    project_id: str,
    account_id: str,
    *,
    fields: tuple[str, ...] = ("password", "otp"),
    app_id: str = "",
) -> dict[str, str] | None:
    """按账号和字段解密；应用范围不匹配时拒绝读取。"""
    pid = str(project_id or "").strip()
    aid = str(account_id or "").strip()
    app_id = str(app_id or "").strip()
    requested = tuple(dict.fromkeys(str(field) for field in fields))
    if not pid or not aid or not app_id or not requested:
        return None
    with session_scope() as db:
        row = (
            db.query(PoolAccount)
            .filter(PoolAccount.project_id == pid, PoolAccount.id == aid)
            .one_or_none()
        )
        if row is None:
            return None
        from mino_nexus.resources.accounts.app_scope import account_matches_app
        if not account_matches_app({"app_ids": row.app_ids or []}, app_id):
            return None
        return _read_credentials(row, fields=requested)


def read_leased_account_credentials(
    project_id: str,
    account_id: str,
    *,
    run_id: str,
    case_id: str = "",
    sn: str = "",
    app_id: str = "",
    fields: tuple[str, ...] = ("password", "otp"),
) -> dict[str, str] | None:
    """严格校验当前租约后，按字段返回单账号明文。"""
    pid = str(project_id or "").strip()
    aid = str(account_id or "").strip()
    rid = str(run_id or "").strip()
    requested = tuple(dict.fromkeys(str(field) for field in fields))
    if not pid or not aid or not rid or not requested:
        return None
    with session_scope() as db:
        row = (
            db.query(PoolAccount)
            .filter(PoolAccount.project_id == pid, PoolAccount.id == aid)
            .one_or_none()
        )
        if row is None:
            return None
        lease = row.lease if isinstance(row.lease, dict) else {}
        if str(lease.get("run_id") or "").strip() != rid:
            return None
        lease_case = str(lease.get("case_id") or "").strip()
        if lease_case and lease_case != str(case_id or "").strip():
            return None
        lease_sn = str(lease.get("sn") or "").strip()
        if lease_sn and lease_sn != str(sn or "").strip():
            return None
        from mino_nexus.resources.resource_pool import lease_expired
        if lease_expired(lease):
            return None
        from mino_nexus.resources.accounts.app_scope import account_matches_app
        # 多账号租约中调用方 ctx.app_id 可能属于另一个 slot；凭证应按租约
        # 自身声明的应用范围校验，旧租约没有该字段时才回退到调用参数。
        scope_app_id = str(lease.get("app_id") or app_id or "").strip()
        if not scope_app_id:
            return None
        if not account_matches_app({"app_ids": row.app_ids or []}, scope_app_id):
            return None
        return _read_credentials(row, fields=requested)


def replace_all(project_id: str, rows: list[dict[str, Any]]) -> None:
    pid = str(project_id or "").strip()
    if not pid:
        return
    with session_scope() as db:
        from sqlalchemy import text
        db.execute(text("BEGIN IMMEDIATE"))
        held = db.query(PoolAccount).filter(PoolAccount.project_id == pid).all()
        legacy_empty_scope_ids = {
            str(row.id)
            for row in held
            if not list(row.app_ids or [])
        }
        if any((row.lease or {}).get("run_id") for row in held):
            raise ValueError("resource_busy: 存在未释放的账号租约，暂不能整表替换号池")
        retained = {str(row.get("account_id") or row.get("id") or "") for row in rows or []}
        from mino_nexus.models.web_auth_snapshot import WebAuthSnapshot
        for model in (AccountData, AccountDataRequest, WebAuthSnapshot):
            db.query(model).filter(model.project_id == pid, model.account_id.notin_(retained)).delete(synchronize_session=False)
        removed = {str(row.id) for row in held if str(row.id) not in retained}
        if removed:
            db.query(PoolAccountFacet).filter(
                PoolAccountFacet.project_id == pid,
                PoolAccountFacet.account_id.in_(removed),
            ).delete(synchronize_session=False)
            db.query(PoolAccount).filter(
                PoolAccount.project_id == pid,
                PoolAccount.id.in_(removed),
            ).delete(synchronize_session=False)
        for row in rows or []:
            aid = str(row.get("account_id") or row.get("id") or "").strip()
            _upsert_row(db, pid, row, allow_legacy_empty_scope=aid in legacy_empty_scope_ids)


def upsert_one(project_id: str, row: dict[str, Any]) -> dict[str, Any] | None:
    pid = str(project_id or "").strip()
    if not pid:
        return None
    with session_scope() as db:
        from sqlalchemy import text
        db.execute(text("BEGIN IMMEDIATE"))
        _upsert_row(db, pid, row)
        db.flush()
        aid = str(row.get("account_id") or row.get("id") or "").strip()
        acc = (
            db.query(PoolAccount)
            .filter(PoolAccount.project_id == pid, PoolAccount.id == aid)
            .one_or_none()
        )
        if not acc:
            return None
        facets = _facets_for_account(db, pid, acc.id)
        return _account_to_dict(acc, facets)


def delete_one(project_id: str, account_id: str) -> bool:
    pid = str(project_id or "").strip()
    aid = str(account_id or "").strip()
    if not pid or not aid:
        return False
    with session_scope() as db:
        from sqlalchemy import text
        db.execute(text("BEGIN IMMEDIATE"))
        held = db.query(PoolAccount).filter(PoolAccount.project_id == pid, PoolAccount.id == aid).one_or_none()
        if held is not None and (held.lease or {}).get("run_id"):
            raise ValueError("resource_busy: 该账号租约尚未释放，不能删除账号")
        for model in (AccountData, AccountDataRequest):
            db.query(model).filter_by(project_id=pid, account_id=aid).delete(synchronize_session=False)
        db.query(PoolAccountFacet).filter(
            PoolAccountFacet.project_id == pid,
            PoolAccountFacet.account_id == aid,
        ).delete(synchronize_session=False)
        n = (
            db.query(PoolAccount)
            .filter(PoolAccount.project_id == pid, PoolAccount.id == aid)
            .delete(synchronize_session=False)
        )
        return n > 0


def set_account_lease(project_id: str, account_id: str, lease: dict[str, Any] | None,
                      *, expected_lease: dict[str, Any] | None = None) -> bool:
    """只更新 lease 列，避免 replace_all 整表重写。"""
    pid = str(project_id or "").strip()
    aid = str(account_id or "").strip()
    if not pid or not aid:
        return False
    payload = lease if isinstance(lease, dict) else {}
    with session_scope() as db:
        from sqlalchemy import text
        db.execute(text("BEGIN IMMEDIATE"))
        row = (
            db.query(PoolAccount)
            .filter(PoolAccount.project_id == pid, PoolAccount.id == aid)
            .one_or_none()
        )
        if row is None:
            return False
        from mino_nexus.records.write_state import protected
        old_lease = row.lease if isinstance(row.lease, dict) else {}
        if expected_lease is not None and old_lease != expected_lease:
            return False
        if protected(str(old_lease.get("run_id") or ""), db=db, sn=str(old_lease.get("sn") or "")):
            return False
        row.lease = payload
        row.updated_at = _now()
        return True


def try_claim_account_lease(
    project_id: str,
    account_id: str,
    lease: dict[str, Any],
    *, requirements: dict[str, Any] | None = None,
    field_defs: dict[str, dict[str, Any]] | None = None,
    app_id: str = "",
) -> bool:
    """原子占号：仅当未被其它 run/设备占用（或同 run+同 sn 续租）时写入。"""
    from mino_nexus.resources.resource_pool import lease_blocks_other_holder

    from mino_nexus.resources.resource_claim_tx import reset_claim_failure
    reset_claim_failure()
    pid = str(project_id or "").strip()
    aid = str(account_id or "").strip()
    payload = dict(lease or {})
    if not pid or not aid or not str(payload.get("run_id") or "").strip():
        return False
    rid = str(payload.get("run_id") or "").strip()
    sn = str(payload.get("sn") or "").strip()
    with session_scope() as db:
        from sqlalchemy import text
        db.execute(text("BEGIN IMMEDIATE"))
        row = (
            db.query(PoolAccount)
            .filter(PoolAccount.project_id == pid, PoolAccount.id == aid)
            .one_or_none()
        )
        if row is None:
            return False
        if row.locked:
            return False
        from mino_nexus.resources.accounts.app_scope import account_matches_app
        if not account_matches_app({"app_ids": row.app_ids or []}, app_id):
            from mino_nexus.resources.resource_claim_tx import deny_claim
            deny_claim("resource_account_scope_mismatch", "account_app_not_in_scope", account_id=aid)
            return False
        facets = _facets_for_account(db, pid, aid)
        if facets.get("health") in {"dirty", "quarantine", "bad"}:
            return False
        if requirements is not None:
            from mino_nexus.resources.resource_pool import account_facet_values_for_match, match_requirements
            env = str(requirements.get("env") or "")
            if env and row.env != env:
                return False
            values = account_facet_values_for_match(_account_to_dict(row, facets))
            if not match_requirements(values, requirements, field_defs=field_defs)[0]:
                return False
        existing = row.lease if isinstance(row.lease, dict) else {}
        from mino_nexus.records.write_state import protected
        from mino_nexus.resources.resource_claim_tx import deny_claim, holder_reason
        holder = str(existing.get("run_id") or "")
        holder_sn = str(existing.get("sn") or "")
        def lease_detail():
            from mino_nexus.models.scout_record import ScoutRecord
            def session_id(value):
                if value.get("session_id"):
                    return str(value["session_id"])
                rows = db.query(ScoutRecord).filter_by(task_id=str(value.get("run_id") or ""),
                    sn=str(value.get("sn") or ""), case_id=str(value.get("case_id") or "")).all()
                return rows[0].session_id if len(rows) == 1 else ""
            return {"account_id": aid, "holder_run_id": holder, "holder_session_id": session_id(existing),
                    "holder_slot": str(existing.get("slot") or "default"),
                    "request_run_id": rid, "request_session_id": session_id(payload),
                    "request_slot": str(payload.get("slot") or "default"),
                    "recommended_action": "复用持有者执行会话；需新会话时显式结束旧会话并核验停止及租约释放，或显式使用其他账号。租约到期不证明停止。"}
        if protected(holder, db=db, sn=holder_sn):
            info = holder_reason(db, holder, holder_sn)
            deny_claim("resource_isolated", info.get("reason_code") or "write_safety_isolated",
                       **{**{key: value for key, value in info.items() if key != "reason_code"}, **lease_detail()})
            return False
        slot_changed = bool(holder) and str(existing.get("slot") or "default") != str(payload.get("slot") or "default")
        scope_changed = bool(holder) and any(existing.get(key) and payload.get(key) and existing[key] != payload[key]
            for key in ("case_id", "session_id", "app_id", "platform", "target_package", "env_surface"))
        if lease_blocks_other_holder(existing, run_id=rid, sn=sn) or slot_changed or scope_changed:
            code = "resource_account_lease_held" if holder != rid else "account_slot_scope_mismatch"
            deny_claim("resource_busy", code, **lease_detail())
            return False
        # The durable slot binding survives a callback-context loss. It cannot
        # acquire a second account while its first account is still held.
        for bound in db.query(PoolAccount).filter(PoolAccount.project_id == pid, PoolAccount.id != aid).all():
            old = bound.lease or {}
            if (old.get("run_id") == rid and str(old.get("slot") or "default") == str(payload.get("slot") or "default")
                    and str(old.get("sn") or "") == sn and str(old.get("case_id") or "") == str(payload.get("case_id") or "")):
                deny_claim("resource_busy", "account_slot_identity_mismatch",
                           account_id=aid, holder_account_id=bound.id, holder_run_id=rid,
                           holder_session_id=str(old.get("session_id") or ""), holder_slot=str(old.get("slot") or "default"),
                           request_run_id=rid, request_session_id=str(payload.get("session_id") or ""),
                           request_slot=str(payload.get("slot") or "default"),
                           recommended_action="复用该 slot 的原账号；更换账号必须显式结束原租约或使用独立 slot。")
                return False
        from mino_nexus.resources.resource_claim_tx import claim_in_transaction
        if not claim_in_transaction(db, aid):
            return False
        row.lease = payload
        row.updated_at = _now()
        return True


def clear_leases_for_run(project_id: str, run_id: str) -> int:
    """清空指定 run 在号池上的租约，返回释放条数。"""
    from mino_nexus.resources.resource_allocation_log import append_allocation_log
    pid = str(project_id or "").strip()
    rid = str(run_id or "").strip()
    if not pid or not rid:
        return 0
    n = 0
    audit_rows = []
    with session_scope() as db:
        from mino_nexus.records.write_state import protected
        from sqlalchemy import text
        db.execute(text("BEGIN IMMEDIATE"))
        rows = db.query(PoolAccount).filter(PoolAccount.project_id == pid).all()
        for row in rows:
            lease = row.lease if isinstance(row.lease, dict) else {}
            if str(lease.get("run_id") or "").strip() != rid:
                continue
            # Unknown device identity retains the conservative whole-run guard.
            if protected(rid, db=db, sn=str(lease.get("sn") or "")):
                continue
            try:
                from mino_nexus.resources.resource_allocation_log import append_allocation_log
                from mino_nexus.resources.project_env import account_ident as _ident_fn

                stub = {
                    "id": row.id,
                    "phone": row.phone,
                    "email": row.email,
                    "username": row.username,
                    "display_name": row.display_name,
                }
                ident = _ident_fn(stub)
                audit_rows.append(dict(
                    project_id=pid,
                    action="lease_release",
                    message=f"跑批结束释放 {ident}",
                    run_id=rid,
                    case_id=str(lease.get("case_id") or "")[:80],
                    sn=str(lease.get("sn") or "")[:64],
                    account_id=str(row.id or ""),
                    account_ident=ident,
                    detail={"reason": "run_end"},
                ))
            except Exception:
                pass
            row.lease = {}
            row.updated_at = _now()
            n += 1
    for audit in audit_rows:
        try:
            append_allocation_log(**audit)
        except Exception:
            pass
    return n


def env_without_test_accounts_blob(doc: dict[str, Any]) -> dict[str, Any]:
    """号池真源在 pool_accounts 表，env JSON 不再重复整表。"""
    out = dict(doc or {})
    out.pop("test_accounts", None)
    return out
