"""账号的应用范围是选号硬约束，与业务数据偏好分开。"""
from __future__ import annotations


def normalize_app_ids(value) -> list[str]:
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("account_app_ids_invalid")
    if any(not isinstance(item, str) or not item.strip() or len(item) > 80 for item in value):
        raise ValueError("account_app_ids_invalid")
    return list(dict.fromkeys(item.strip() for item in value))


def account_matches_app(row: dict, app_id: str = "") -> bool:
    ids = normalize_app_ids(row.get("app_ids", []))
    return not ids or bool(app_id) and app_id in ids


def validate_project_apps(project_id: str, app_ids: list[str]) -> None:
    from mino_nexus.resources.project_store import require_app

    for app_id in app_ids:
        try:
            app = require_app(app_id)
        except KeyError as exc:
            raise ValueError("account_app_not_in_project") from exc
        if str(app.get("project_id") or "") != project_id:
            raise ValueError("account_app_not_in_project")
