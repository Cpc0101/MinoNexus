"""Advisory object references. No inventory verification or recovery decisions."""
from __future__ import annotations

import json
import time
from typing import Any
from sqlalchemy import text
from mino_nexus.core.database import session_scope
from mino_nexus.models.account_data import AccountData, AccountDataRequest
from mino_nexus.models.pool_account import PoolAccount


def validate_data_need(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError("data_need 必须是最多 2000 字的描述")
    return value.strip()


def validate_data(value: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, limit in {"data_id": 128, "app_id": 128, "kind": 64, "object_ref": 2048,
                       "title": 300, "source_task_id": 128}.items():
        raw = value.get(key, "")
        if not isinstance(raw, str) or len(raw) > limit:
            raise ValueError(f"{key} 必须是最多 {limit} 字的字符串")
        out[key] = raw.strip()
    if not all(out[key] for key in ("data_id", "kind", "object_ref")):
        raise ValueError("缺少 data_id、kind 或 object_ref")
    attrs = value.get("attributes", {})
    if not isinstance(attrs, dict):
        raise ValueError("attributes 必须是对象")
    try:
        encoded = json.dumps(attrs, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (ValueError, TypeError) as exc:
        raise ValueError("attributes 必须是 JSON 对象") from exc
    if len(encoded.encode("utf-8")) > 8192:
        raise ValueError("attributes 最多 8192 字节")
    out["attributes"] = attrs
    return out


def _account(db, project_id: str, account_id: str):
    row = db.query(PoolAccount).filter_by(project_id=project_id, id=account_id).one_or_none()
    if row is None:
        raise KeyError("account_not_found")
    return row


def _public(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def list_data(project_id: str, account_id: str, *, app_id: str = "", kind: str = "", limit: int = 50, offset: int = 0,
              request_limit: int = 20, request_offset: int = 0):
    with session_scope() as db:
        _account(db, project_id, account_id)
        query = db.query(AccountData).filter_by(project_id=project_id, account_id=account_id)
        if app_id:
            query = query.filter_by(app_id=app_id)
        if kind:
            query = query.filter_by(kind=kind)
        requests = db.query(AccountDataRequest).filter_by(project_id=project_id, account_id=account_id)
        if app_id:
            requests = requests.filter_by(app_id=app_id)
        return {"items": [_public(row) for row in query.order_by(AccountData.updated_at.desc(), AccountData.data_id).offset(offset).limit(limit)],
                "total": query.count(),
                "requests": [_public(row) for row in requests.order_by(AccountDataRequest.updated_at.desc(), AccountDataRequest.run_id, AccountDataRequest.session_id).offset(request_offset).limit(request_limit)],
                "requests_total": requests.count(),
                "advisory": True}


def save_data(project_id: str, account_id: str, value: dict[str, Any]):
    value = validate_data(value)
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        _account(db, project_id, account_id)
        if value["app_id"]:
            from mino_nexus.models.project import App
            if db.query(App).filter_by(id=value["app_id"], project_id=project_id).one_or_none() is None:
                raise ValueError("app_id 不属于当前项目")
        row = db.query(AccountData).filter_by(project_id=project_id, account_id=account_id, data_id=value["data_id"]).one_or_none()
        if row is None:
            row = AccountData(project_id=project_id, account_id=account_id)
            db.add(row)
        for key, val in value.items():
            setattr(row, key, val)
        row.updated_at = int(time.time() * 1000)
        db.flush()
        return _public(row)


def delete_data(project_id: str, account_id: str, data_id: str):
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        _account(db, project_id, account_id)
        return bool(db.query(AccountData).filter_by(project_id=project_id, account_id=account_id, data_id=data_id).delete())


def record_request(project_id: str, account_id: str, *, run_id: str, session_id: str, app_id: str, case_id: str, data_need: str):
    need = validate_data_need(data_need)
    if not need:
        return
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        _account(db, project_id, account_id)
        keys = dict(project_id=project_id, account_id=account_id, run_id=run_id, session_id=session_id)
        row = db.query(AccountDataRequest).filter_by(**keys).one_or_none()
        if row is None:
            row = AccountDataRequest(**keys)
            db.add(row)
        row.app_id, row.case_id, row.data_need = app_id, case_id, need
        row.updated_at = int(time.time() * 1000)
