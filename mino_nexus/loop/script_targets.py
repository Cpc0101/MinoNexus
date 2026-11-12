"""按需求 ID 和用例 ID 组脚本目标。用例正文仍走原来的 project_cases 路径。"""
from __future__ import annotations

import re
from typing import Any

_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_WALL_BUDGET_SEC = 600


def normalize_script_targets(raw: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise ValueError("script_targets 需要至少一条需求")
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("script_targets 格式不对")
        rid = str(item.get("requirement_id") or "").strip()
        _check_id(rid, "需求 ID")
        case_ids = item.get("case_ids") or []
        if not isinstance(case_ids, list):
            raise ValueError("case_ids 必须是列表")
        clean: list[str] = []
        for cid in case_ids:
            text = str(cid or "").strip()
            _check_id(text, "用例 ID")
            clean.append(text)
        out.append({"requirement_id": rid, "case_ids": clean})
    return out


def expand_script_targets(
    project_id: str,
    targets: list[dict[str, Any]],
    *,
    run_id: str,
    sn: str,
    platform: str,
    repo_url: str,
    branch: str,
    revision: str,
) -> list[dict[str, Any]]:
    from mino_nexus.casework.case_import import list_project_requirements
    from mino_nexus.loop.script_dispatch import list_requirement_scripts

    known = {str(row.get("id") or ""): row for row in list_project_requirements(project_id)}
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for target in targets:
        rid = str(target["requirement_id"])
        row = known.get(rid)
        if row is None:
            raise ValueError(f"需求 {rid} 还没有登记")
        wanted = [str(cid) for cid in target.get("case_ids") or []]
        listed: dict[str, str] = {}
        if not wanted:
            if not str(sn or "").strip():
                raise ValueError("列出需求下的脚本需要一台设备")
            if not str(repo_url or "").strip():
                raise ValueError("项目还没有配置用例脚本仓库")
            found = list_requirement_scripts(
                sn=sn, platform=platform, repo_url=repo_url, branch=branch,
                revision=revision, requirement_id=rid, run_id=run_id,
            )
            if not found:
                raise ValueError(f"需求 {rid} 的目录里没有脚本")
            wanted = [item["case_id"] for item in found]
            listed = {item["case_id"]: item["script_path"] for item in found}
        title = str(row.get("title") or rid)
        for cid in wanted:
            if cid in seen:
                raise ValueError(f"用例 {cid} 在本次下发里重复")
            seen.add(cid)
            cases.append({
                "case_id": cid,
                "name": cid,
                "title": cid,
                "module": title,
                "requirement_id": rid,
                "script_path": listed.get(cid) or f"tests/{rid}/{cid}.py",
                "platform": platform,
                "project_id": project_id,
                "wall_budget_sec": _WALL_BUDGET_SEC,
                "precondition": "",
                "steps": [],
                "expected": [],
            })
    return cases


def _check_id(value: str, label: str) -> None:
    if not _ID.fullmatch(value):
        raise ValueError(f"{label} 须以字母开头，只含字母、数字、下划线或连字符，最长 64 位")
