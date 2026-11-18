"""用例行的源视图与编译就绪判定。

只做"这份文本/产物是否还对得上"的纯比较，不读写库——因此资源域也能用。
"""
from __future__ import annotations

import copy

from mino_nexus.core.stable_digest import digest

SOURCE_FIELDS = ("name", "platform", "precondition", "steps", "expected", "steps_raw", "expected_raw")


def source_view(case) -> dict:
    return {key: copy.deepcopy(case.get(key)) for key in SOURCE_FIELDS}


def readiness(case):
    if not case.get("source_revision_id"):
        return None  # Historical records remain readable; no invented provenance.
    if case.get("source_digest") != digest(source_view(case)):
        return "compile_source_changed"
    job, artifact = case.get("compile_job") or {}, case.get("effective_artifact") or {}
    if (job.get("status") != "succeeded" or job.get("job_id") != artifact.get("job_id")
            or artifact.get("revision_id") != case.get("source_revision_id")
            or job.get("job_id") != case.get("latest_compile_job_id")):
        return "compile_artifact_unavailable" if job.get("status") == "succeeded" else "compile_" + str(job.get("status") or "required")
    from mino_nexus.contract.build_identity import build_identity

    current = build_identity()
    # 版本号也是编译产物的一部分：程序升级后旧 job 不能继续冒充当前产物。
    # 只比 compiler_contract 会漏掉“版本已升、合同常量未变”的发布。
    if any((job.get("build") or {}).get(key) != current[key]
            for key in ("nexus_version", "compiler_contract", "resource_catalog_version")):
        return "compile_contract_changed"
    return None


def step_bundle(case: dict | None, case_step: int) -> dict:
    """Read a captured step without importing the retired compiler."""
    if not isinstance(case, dict) or case_step <= 0:
        return {}
    meta = case.get("meta") if isinstance(case.get("meta"), dict) else {}
    plans = meta.get("step_program_keys")
    bundle = plans.get(str(case_step)) if isinstance(plans, dict) else None
    return copy.deepcopy(bundle) if isinstance(bundle, dict) else {}


def stored_step_summary(case: dict | None) -> dict:
    """Historical plans are readable, but do not prove Scout script readiness."""
    meta = case.get("meta") if isinstance(case, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    warnings = meta.get("key_compile_warnings")
    warnings = warnings if isinstance(warnings, list) else []
    plans = meta.get("step_program_keys")
    plans = plans if isinstance(plans, dict) else {}
    return {
        "telemetry": "muted", "status": "unknown", "runnable_now": False,
        "reason_code": "case_compilation_removed",
        "reasons": ["历史编译记录不代表脚本可执行，请使用 Scout 检查和验证当前版本"],
        "warning_count": len(warnings),
        "fallback_count": sum(1 for item in warnings if isinstance(item, dict) and item.get("fallback")),
        "warnings_preview": copy.deepcopy(warnings[:12]),
        "steps": copy.deepcopy(plans),
    }
