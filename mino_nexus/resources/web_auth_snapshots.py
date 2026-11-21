"""Encrypted, leased-account-only authentication storage."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from urllib.parse import urlsplit

from mino_nexus.core.database import session_scope
from mino_nexus.models.pool_account import PoolAccount
from mino_nexus.models.web_auth_snapshot import WebAuthSnapshot
from mino_nexus.resources.credential_cipher import decrypt_secret, encrypt_secret

MAX_BYTES = 196608
IDENTITY_FIELDS = {"username", "email", "phone"}


def origin(url: str) -> str:
    try:
        p = urlsplit(url)
        if p.scheme not in {"https", "http"} or not p.hostname or p.username or p.password:
            raise ValueError()
        port = p.port
        host = p.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        suffix = f":{port}" if port and port != (443 if p.scheme == "https" else 80) else ""
        return f"{p.scheme}://{host}{suffix}"
    except (ValueError, TypeError):
        raise ValueError("web_auth_origin_invalid") from None


def scope(ctx, account: dict) -> dict:
    target = str(account.get("target_package") or getattr(ctx, "target_package", "") or "")
    result = {
        "project_id": str(account.get("project_id") or ctx.project_id),
        "app_id": str(account.get("app_id") or ctx.app_id),
        "account_id": str(account.get("id") or account.get("account_id") or ""),
        "env": str(account.get("env") or getattr(ctx, "env_profile", "") or "test"),
        "surface": str(account.get("env_surface") or getattr(ctx, "env_surface", "") or ""),
        "origin": origin(target),
    }
    result["scope_id"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result


def _account(db, s, expected_lease=None):
    row = db.query(PoolAccount).filter_by(project_id=s["project_id"], id=s["account_id"]).one_or_none()
    if row is None or s["app_id"] not in (row.app_ids or []) or row.env != s["env"]:
        raise ValueError("web_auth_account_scope_mismatch")
    if expected_lease is not None:
        from mino_nexus.resources.resource_pool import lease_expired
        held = row.lease or {}
        if lease_expired(held) or any(str(held.get(k) or "") != str(v) for k, v in expected_lease.items()):
            raise ValueError("resource_account_missing")
    return row


def validate_payload(payload, expected_origin):
    try:
        if not isinstance(payload, dict) or set(payload) != {"storage_state", "session_storage", "identity_field", "identity_value"}:
            raise ValueError()
        if payload["identity_field"] not in IDENTITY_FIELDS or not isinstance(payload["identity_value"], str) or not payload["identity_value"]:
            raise ValueError()
        state = payload["storage_state"]
        if not isinstance(state, dict) or set(state) != {"cookies", "origins"}:
            raise ValueError()
        if not isinstance(state["cookies"], list) or not isinstance(state["origins"], list):
            raise ValueError()
        host = urlsplit(expected_origin).hostname
        for cookie in state["cookies"]:
            if not isinstance(cookie, dict) or not isinstance(cookie.get("domain"), str):
                raise ValueError()
            domain = cookie["domain"].lstrip(".").lower()
            if not domain or not (host == domain or (cookie["domain"].startswith(".") and host.endswith("." + domain))):
                raise ValueError()
            if not all(isinstance(cookie.get(k), str) for k in ("name", "value", "path")):
                raise ValueError()
        for item in state["origins"]:
            if not isinstance(item, dict) or set(item) != {"origin", "localStorage"} or item["origin"] != expected_origin:
                raise ValueError()
            if not isinstance(item["localStorage"], list) or any(not isinstance(v, dict) or set(v) != {"name", "value"} or not all(isinstance(x, str) for x in v.values()) for v in item["localStorage"]):
                raise ValueError()
        sessions = payload["session_storage"]
        if not isinstance(sessions, dict) or set(sessions) - {expected_origin}:
            raise ValueError()
        if any(not isinstance(values, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in values.items()) for values in sessions.values()):
            raise ValueError()
        raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        if len(raw.encode()) > MAX_BYTES:
            raise ValueError()
        return raw
    except (ValueError, TypeError, KeyError):
        raise ValueError("web_auth_snapshot_invalid") from None


def save(s, payload, ttl_seconds, *, expected_lease):
    if type(ttl_seconds) is not int or not 60 <= ttl_seconds <= 604800:
        raise ValueError("web_auth_ttl_invalid")
    raw = validate_payload(payload, s["origin"])
    with session_scope() as db:
        from sqlalchemy import text
        db.execute(text("BEGIN IMMEDIATE"))
        account = _account(db, s, expected_lease)
        if str(getattr(account, payload["identity_field"]) or "") != payload["identity_value"]:
            raise ValueError("web_auth_identity_mismatch")
        now = int(time.time())
        credential = encrypt_secret(raw, project_id=s["project_id"], account_id=s["account_id"], field="web_auth:" + s["scope_id"])
        revision = uuid.uuid4().hex
        # SQLite upsert is atomic even when requests arrive on different workers.
        from sqlalchemy.dialects.sqlite import insert
        values = {**s, "credential": credential, "revision": revision, "created_at": now,
                  "expires_at": now + ttl_seconds, "invalidated_at": 0}
        stmt = insert(WebAuthSnapshot).values(**values)
        db.execute(stmt.on_conflict_do_update(index_elements=["scope_id"], set_=values))
        return {"state": "available", "revision": revision, "expires_at": now + ttl_seconds}


def get(s, *, expected_lease):
    with session_scope() as db:
        account = _account(db, s, expected_lease)
        row = db.query(WebAuthSnapshot).filter_by(scope_id=s["scope_id"]).one_or_none()
        if row is None:
            return {"state": "missing"}
        meta = {"revision": row.revision, "expires_at": row.expires_at}
        if row.invalidated_at:
            return {**meta, "state": "invalidated"}
        if row.expires_at <= int(time.time()):
            return {**meta, "state": "expired"}
        raw = decrypt_secret(row.credential, project_id=s["project_id"], account_id=s["account_id"], field="web_auth:" + s["scope_id"])
        payload = json.loads(raw)
        validate_payload(payload, s["origin"])
        if str(getattr(account, payload["identity_field"]) or "") != payload["identity_value"]:
            return {**meta, "state": "invalidated"}
        return {**meta, "state": "available", "credential": payload}


def invalidate(s, revision, *, expected_lease):
    if not isinstance(revision, str) or not revision:
        raise ValueError("web_auth_revision_required")
    with session_scope() as db:
        from sqlalchemy import text
        db.execute(text("BEGIN IMMEDIATE"))
        _account(db, s, expected_lease)
        changed = db.query(WebAuthSnapshot).filter_by(scope_id=s["scope_id"], revision=revision).update(
            {"invalidated_at": int(time.time())}, synchronize_session=False)
        return {"invalidated": bool(changed)}


def statuses(project_id, app_id="", account_ids=None):
    """Public metadata only; never decrypt on list/detail endpoints."""
    now = int(time.time())
    account_ids = [str(item) for item in (account_ids or []) if str(item)]
    if not account_ids:
        return {}
    with session_scope() as db:
        query = db.query(WebAuthSnapshot).filter(WebAuthSnapshot.project_id == project_id,
            WebAuthSnapshot.account_id.in_(account_ids))
        if app_id:
            query = query.filter(WebAuthSnapshot.app_id == app_id)
        rows = query.all()
        result = {}
        for row in rows:
            result.setdefault(row.account_id, []).append({"env": row.env, "surface": row.surface,
                "origin": row.origin, "expires_at": row.expires_at,
                "state": "invalidated" if row.invalidated_at else "expired" if row.expires_at <= now else "available"})
        return result
