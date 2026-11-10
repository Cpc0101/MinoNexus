"""开跑前的准入检查：只判"这份用例现在能不能派给 Scout"。

用例正文由 Scout 按脚本执行，Nexus 不再编译出可序列化产物，因此这里不再有
"编译是否就绪"这一档。保留的是：用例是否还在、平台是否声明、前置是否需要
人工准备（资源域判定）。
"""
from __future__ import annotations

from typing import Any


def partition_cases(cases: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """把请求的用例分成"可以下发"和"要先补资源"，不拦整案。"""
    runnable: list[dict[str, Any]] = []
    needs_setup: list[dict[str, Any]] = []
    verdicts: dict[str, dict[str, Any]] = {}
    for case in cases:
        case_id = str(case.get("case_id") or "")
        from mino_nexus.casework.case_semantic_integrity import precondition_diagnostics

        precondition_issues = precondition_diagnostics(case)
        setup_items = [item for item in precondition_issues
                       if str(item.get("class") or "setup") == "setup"]
        risks = [*setup_items, *[item for item in precondition_issues if item not in setup_items]]
        runnable.append(case)
        verdicts[case_id] = {
            "case_id": case_id,
            "name": str(case.get("name") or ""),
            "status": "runnable",
            "class": "ui",
            "reasons": [],
            "blocked_by": [],
            "risks": risks,
            "risk_count": len(risks),
            "attribution_classes": sorted({str(item.get("attribution_class") or "B")
                                           for item in risks if item.get("attribution_class")}),
        }
    report = {
        "runnable": [str(case.get("case_id") or "") for case in runnable],
        "needs_setup": needs_setup,
        "partial": [],
        "hard_blocked": [],
        # Backward-compatible meaning: every case that must not enter this run.
        "blocked": needs_setup,
        "requested_total": len(cases),
        "buckets": {"runnable": len(runnable), "needs_setup": len(needs_setup),
                    "partial": 0, "blocked": 0},
        "compilation_jobs": [],
        "verdicts": verdicts,
    }
    return runnable, report


def prepare_cases_for_execution(project_id: str, case_ids: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from mino_nexus.casework.case_store import list_cases

    available = {str(case.get("case_id")): case for case in list_cases(str(project_id or ""))}
    if not case_ids or any(str(case_id) not in available for case_id in case_ids):
        raise ValueError("所选用例为空、已删除或不属于当前项目；本次未创建任务。")
    return partition_cases([available[str(case_id)] for case_id in dict.fromkeys(case_ids)])


def preflight(app: dict[str, Any], case_ids: list[str], *, recompile_stale: bool = False) -> dict[str, Any]:
    del recompile_stale  # 编译层已移除；沿用签名避免旧调用方失败。
    return prepare_cases_for_execution(str(app.get("project_id") or ""), case_ids)[1]


def rejection_message(report: dict[str, Any]) -> str:
    buckets = report.get("buckets") or {}
    parts: list[str] = []
    if buckets.get("needs_setup"):
        parts.append(f"{buckets['needs_setup']} 条用例的前置/数据未准备")
    if not parts:
        parts.append("所选用例均未通过执行预检")
    return "；".join(parts) + "；本次未创建任务。\n" + blocked_message(report.get("blocked") or [])


def blocked_message(blocked: list[dict[str, Any]]) -> str:
    lines = []
    for case in blocked:
        lines.append(f"{case.get('name') or '未命名用例'}（{case.get('case_id') or '未知编号'}）")
        lines.extend("- " + str(reason) for reason in case.get("reasons") or ["请在 Workbench 查看用例详情。"])
    return "\n".join(lines)
