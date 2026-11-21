"""Project membership: shared project data, personal execution nodes."""
from __future__ import annotations
import re
import time
from typing import Any
from sqlalchemy import func
from mino_nexus.core.database import session_scope
from mino_nexus.models.auth import User
from mino_nexus.models.project import Project
from mino_nexus.models.project_member import ProjectMember


def _uid(sess: dict[str, Any] | None) -> str:
    uid = str((sess or {}).get("user_id") or "").strip()
    if not uid:
        raise PermissionError("login_required")
    return uid


def require_project_access(project_id: str, sess: dict[str, Any] | None) -> None:
    uid = _uid(sess)
    with session_scope() as db:
        row = db.get(Project, project_id)
        if row is None or (row.created_by != uid and db.get(ProjectMember, (project_id, uid)) is None):
            raise KeyError("project_not_found")


def require_maintainer(project_id: str, sess: dict[str, Any] | None) -> None:
    require_project_access(project_id, sess)


def require_owner(project_id: str, sess: dict[str, Any] | None) -> None:
    require_project_access(project_id, sess)
    with session_scope() as db:
        if db.get(Project, project_id).created_by != _uid(sess):
            raise PermissionError("project_owner_required")


def visible_project_ids(sess: dict[str, Any]) -> set[str]:
    uid = _uid(sess)
    with session_scope() as db:
        return {r.id for r in db.query(Project).filter(Project.created_by == uid)} | {r.project_id for r in db.query(ProjectMember).filter(ProjectMember.user_id == uid)}


def _timestamp_ms(value: Any) -> int:
    """API 时间戳统一为毫秒；兼容早期以秒存储的项目创建时间。"""
    try:
        timestamp = int(value or 0)
    except (TypeError, ValueError):
        return 0
    if 0 < timestamp < 1_000_000_000_000:
        return timestamp * 1000
    return timestamp


def list_members(project_id: str, sess: dict[str, Any]) -> dict[str, Any]:
    require_project_access(project_id, sess)
    with session_scope() as db:
        project = db.get(Project, project_id)
        owner = db.get(User, project.created_by)
        members = [{"user_id": project.created_by, "email": owner.email if owner else "", "role": "owner", "added_at": _timestamp_ms(project.created_at)}]
        for row, user in db.query(ProjectMember, User).join(User, User.user_id == ProjectMember.user_id).filter(ProjectMember.project_id == project_id).order_by(ProjectMember.added_at_ms):
            members.append({"user_id": row.user_id, "email": user.email, "role": "member", "added_at": _timestamp_ms(row.added_at_ms)})
        return {"members": members, "can_manage": project.created_by == _uid(sess)}


def add_member(project_id: str, sess: dict[str, Any], email: str) -> dict[str, Any]:
    require_owner(project_id, sess)
    email = str(email or "").strip().lower()
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValueError("请输入有效邮箱")
    with session_scope() as db:
        user = db.query(User).filter(func.lower(User.email) == email).first()
        if user is None:
            raise KeyError("该邮箱尚未注册")
        if user.user_id == _uid(sess):
            raise ValueError("该账号已是项目所有者")
        if db.get(ProjectMember, (project_id, user.user_id)) is None:
            db.add(ProjectMember(project_id=project_id, user_id=user.user_id, added_by=_uid(sess), added_at_ms=int(time.time()*1000)))
    return list_members(project_id, sess)


def remove_member(project_id: str, sess: dict[str, Any], user_id: str) -> dict[str, Any]:
    require_owner(project_id, sess)
    if user_id == _uid(sess):
        raise ValueError("不能移除项目所有者")
    with session_scope() as db:
        db.query(ProjectMember).filter_by(project_id=project_id, user_id=user_id).delete()
    return list_members(project_id, sess)
