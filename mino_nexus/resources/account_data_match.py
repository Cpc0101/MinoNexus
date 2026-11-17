"""Match remembered references as preferences; absence never excludes accounts."""
from __future__ import annotations
import json
from mino_nexus.core.database import session_scope
from mino_nexus.models.account_data import AccountData


def validate_data_requirements(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError("data_requirements 必须是最多 20 项的列表")
    out = []
    for row in value:
        if not isinstance(row, dict) or set(row) - {"kind", "object_ref", "attributes"}:
            raise ValueError("数据偏好只允许 kind/object_ref/attributes")
        kind = row.get("kind", "")
        ref = row.get("object_ref", "")
        attrs = row.get("attributes", {})
        if not isinstance(kind, str) or not kind.strip() or len(kind) > 64 or not isinstance(ref, str) or len(ref) > 2048 or not isinstance(attrs, dict):
            raise ValueError("数据偏好格式无效")
        if any(not isinstance(k, str) or type(v) not in (str, int, float, bool, type(None)) for k, v in attrs.items()):
            raise ValueError("数据偏好的 attributes 只接受简单字段")
        out.append({"kind": kind.strip(), "object_ref": ref, "attributes": attrs})
    if len(json.dumps(out, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()) > 8192:
        raise ValueError("数据偏好最多 8192 字节")
    return out


def data_matches(rows, requirements):
    matched = []
    count = 0
    for need in requirements:
        found = [row for row in rows if row["kind"] == need["kind"]
                 and (not need["object_ref"] or row["object_ref"] == need["object_ref"])
                 and all(key in row["attributes"] and (type(row["attributes"][key]) is type(value) or type(row["attributes"][key]) in (int, float) and type(value) in (int, float))
                         and row["attributes"][key] == value for key, value in need["attributes"].items())]
        if found:
            count += 1
            for row in found:
                if row not in matched:
                    matched.append(row)
    return count, matched[:20]


def rank_account_data(project_id, rows, requirements, *, app_id=""):
    needs = validate_data_requirements(requirements)
    if not needs or not rows:
        return rows
    ids = [row.get("id") or row.get("account_id") for row in rows]
    by_account = {}
    with session_scope() as db:
        query = db.query(AccountData).filter(AccountData.project_id == project_id, AccountData.account_id.in_(ids))
        if app_id:
            query = query.filter(AccountData.app_id == app_id)
        for item in query.order_by(AccountData.updated_at.desc(), AccountData.data_id):
            by_account.setdefault(item.account_id, []).append({column.name: getattr(item, column.name) for column in item.__table__.columns})
    ranked = []
    for row in rows:
        count, matches = data_matches(by_account.get(row.get("id") or row.get("account_id"), []), needs)
        ranked.append({**row, "data_match_count": count, "data_matches": matches})
    return sorted(ranked, key=lambda row: -row["data_match_count"])
