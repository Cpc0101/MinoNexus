"""脚本仓库的应用清单：agent 把 config.yaml 的 apps 同步过来，供 Workbench 只读展示和 case_run 核对。

脚本仓认 key（platform / package / url）；Nexus 认 app_id。同步时若未带 app_id，
按 key 对齐或自动创建项目下的应用，并把映射写进清单。otp 等秘密不得进入同步载荷。
"""
from __future__ import annotations

import re
import time
from typing import Any

_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
_APP_ID = re.compile(r"^[A-Za-z0-9_.:\-]{1,120}$")
_PLATFORMS = frozenset({"android", "ios", "web", "mac", "win"})
_IDENTITY = ("package", "bundle_id", "url")
_SECRET_WORDS = ("password", "passwd", "token", "otp", "secret", "key", "credential")
_MAX_APPS = 32


def validate_apps(apps: Any) -> dict[str, dict[str, Any]]:
    """返回清洗后的 apps；不合法抛 ValueError，message 指明哪个应用哪一项。

    app_id 可选：缺省时由 save() 按 key 对齐或创建 Nexus 应用后回填。
    """
    if not isinstance(apps, dict) or not apps:
        raise ValueError("apps 必须是非空对象")
    if len(apps) > _MAX_APPS:
        raise ValueError(f"apps 最多 {_MAX_APPS} 个应用")
    out: dict[str, dict[str, Any]] = {}
    for key, row in apps.items():
        name = str(key)
        if not _KEY.match(name):
            raise ValueError(f"应用 key {name!r} 不合法：要求英文字母开头，只含英文字母、数字、下划线，最长 32")
        if not isinstance(row, dict):
            raise ValueError(f"应用 {name}：内容必须是对象")
        _no_secrets(name, row)
        platform = str(row.get("platform") or "").strip().lower()
        if platform not in _PLATFORMS:
            raise ValueError(f"应用 {name}：platform 必须是 {'/'.join(sorted(_PLATFORMS))}")
        clean: dict[str, Any] = {"platform": platform}
        app_id = row.get("app_id")
        if app_id is not None and str(app_id).strip():
            if not isinstance(app_id, str) or not _APP_ID.match(app_id.strip()):
                raise ValueError(f"应用 {name}：app_id 不合法")
            clean["app_id"] = app_id.strip()
        _identity(name, row, clean, allowed={"platform", "app_id", "envs", *_IDENTITY})
        envs = row.get("envs")
        if envs is not None:
            if not isinstance(envs, dict):
                raise ValueError(f"应用 {name}：envs 必须是对象")
            clean["envs"] = {}
            for env, block in envs.items():
                label = f"{name}.envs.{env}"
                if not isinstance(block, dict):
                    raise ValueError(f"应用 {label}：内容必须是对象")
                _no_secrets(label, block)
                picked: dict[str, Any] = {}
                _identity(label, block, picked, allowed=set(_IDENTITY))
                clean["envs"][str(env)] = picked
        out[name] = clean
    return out


def _no_secrets(label: str, row: dict) -> None:
    for field in row:
        low = str(field).lower()
        if any(word in low for word in _SECRET_WORDS):
            raise ValueError(
                f"应用 {label}：不允许出现敏感字段 {field}（otp.code 只留在脚本仓，由 Scout 读取；账号密码由 Nexus 号池管理）"
            )


def _identity(label: str, row: dict, into: dict, *, allowed: set[str]) -> None:
    for field in row:
        if field not in allowed:
            raise ValueError(
                f"应用 {label}：不支持的字段 {field}，只允许 platform / app_id / package / bundle_id / url / envs"
            )
    for field in _IDENTITY:
        if field not in row:
            continue
        value = row[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"应用 {label}：{field} 必须是非空字符串")
        value = value.strip()
        if field == "url" and not re.match(r"^https?://\S+$", value):
            raise ValueError(f"应用 {label}：url 必须以 http:// 或 https:// 开头")
        into[field] = value


def get(project_id: str) -> dict[str, Any]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.project_repo_apps import ProjectRepoApps

    pid = str(project_id or "").strip()
    empty = {"project_id": pid, "revision": "", "branch": "", "apps": {}, "synced_by": "", "synced_at": 0}
    with session_scope() as db:
        row = db.get(ProjectRepoApps, pid)
        if row is None:
            return empty
        return {"project_id": pid, "revision": str(row.revision or ""), "branch": str(row.branch or ""),
                "apps": dict(row.apps or {}), "synced_by": str(row.synced_by or ""), "synced_at": int(row.synced_at or 0)}


def save(project_id: str, revision: str, branch: str, apps: Any, synced_by: str,
         remove: Any = None, owner: dict[str, Any] | None = None) -> dict[str, Any]:
    """按差异更新：apps 里的应用按 key 新增或整项替换，remove 里的 key 删除，其余原样保留。

    未带 app_id 的项会按 key 对齐项目里已有应用，或自动创建后再写入清单。
    """
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.project_repo_apps import ProjectRepoApps

    pid = str(project_id or "").strip()
    drop = [str(key) for key in (remove or [])] if isinstance(remove, (list, tuple)) else []
    if remove is not None and not isinstance(remove, (list, tuple)):
        raise ValueError("remove 必须是应用 key 的数组")
    changed = validate_apps(apps) if apps else {}
    if not changed and not drop:
        raise ValueError("apps 和 remove 不能同时为空")
    overlap = sorted(set(changed) & set(drop))
    if overlap:
        raise ValueError(f"应用 {', '.join(overlap)} 同时出现在 apps 和 remove 里")
    previous = {k: v for k, v in get(pid)["apps"].items() if k not in drop}
    cleaned = dict(previous)
    cleaned.update(changed)
    if not cleaned:
        raise ValueError("同步后项目不能没有任何应用")
    if len(cleaned) > _MAX_APPS:
        raise ValueError(f"apps 最多 {_MAX_APPS} 个应用")
    cleaned = _bind_app_ids(pid, cleaned, previous=previous, owner=owner)
    with session_scope() as db:
        row = db.get(ProjectRepoApps, pid)
        if row is None:
            row = ProjectRepoApps(project_id=pid)
            db.add(row)
        row.revision = str(revision or "").strip()[:120]
        row.branch = str(branch or "").strip()[:200]
        row.apps = cleaned
        row.synced_by = str(synced_by or "")[:120]
        row.synced_at = int(time.time())
        db.flush()
    return get(pid)


def _bind_app_ids(
    project_id: str,
    apps: dict[str, dict[str, Any]],
    *,
    previous: dict[str, dict[str, Any]],
    owner: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    from mino_nexus.resources import project_store as ps

    project_apps = [a for a in ps.list_apps(project_id) if isinstance(a, dict)]
    by_id = {str(a.get("id") or ""): a for a in project_apps if a.get("id")}
    by_name = {}
    for a in project_apps:
        name = str(a.get("name") or "").strip()
        if name and name not in by_name:
            by_name[name] = a
    out: dict[str, dict[str, Any]] = {}
    for key, row in apps.items():
        item = dict(row)
        app_id = str(item.get("app_id") or "").strip()
        if not app_id:
            app_id = str((previous.get(key) or {}).get("app_id") or "").strip()
        if app_id and app_id not in by_id:
            raise ValueError(f"应用 {key}：app_id={app_id} 不在本项目")
        if not app_id:
            existing = by_name.get(key)
            if existing is not None:
                app_id = str(existing.get("id") or "")
            else:
                created = ps.create_app(
                    project_id,
                    name=key,
                    description=f"由脚本仓 key={key} 同步创建",
                    platforms=str(item.get("platform") or ""),
                    owner=owner,
                )
                app_id = str(created.get("id") or "")
                by_id[app_id] = created
                by_name[key] = created
        if not app_id:
            raise ValueError(f"应用 {key}：无法对齐或创建 Nexus 应用")
        item["app_id"] = app_id
        out[key] = item
    return out


def has_app(project_id: str, app_id: str) -> bool | None:
    """项目没有清单返回 None（不校验）；有清单时看 app_id 是否等于其中某个应用的 app_id。"""
    doc = get(project_id)
    if not doc["apps"]:
        return None
    wanted = str(app_id or "").strip()
    return any(str((row or {}).get("app_id") or "") == wanted for row in doc["apps"].values())


def key_for_app(project_id: str, app_id: str) -> str:
    """清单里与 Nexus app_id 对应的脚本仓 key；没有则空串。"""
    wanted = str(app_id or "").strip()
    if not wanted:
        return ""
    for key, row in (get(project_id).get("apps") or {}).items():
        if str((row or {}).get("app_id") or "") == wanted:
            return str(key)
    return ""
