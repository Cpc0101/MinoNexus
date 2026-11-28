"""Read-only receipts from the task index; transport delivery remains client-owned."""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime

from mino_nexus.contract.execution_report import purpose, result


def _safe_text(value: object, limit: int = 400) -> str:
    value = str(value or "")
    value = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [hidden]", value)
    value = re.sub(r"(?i)(password|passwd|token|secret|otp|验证码|密码)[\"\']?\s*[:=：]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,，;；]+)", r"\1=[hidden]", value)
    value = re.sub(r"[\w.+%-]+@[\w.-]+\.[A-Za-z]{2,}", "[email hidden]", value)
    value = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[phone hidden]", value)
    return " ".join(value.split())[:limit]


def _elapsed_seconds(case: dict, task: dict, now: datetime) -> float | None:
    for key in ("elapsed_ms", "duration_ms"):
        value = case.get(key)
        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
            return round(float(value) / 1000, 1)
    started = str(case.get("started_at") or task.get("started_at") or "").strip()
    if not started:
        return None
    try:
        begin = datetime.fromisoformat(started.replace("Z", "+00:00"))
    except ValueError:
        return None
    if begin.tzinfo is None:
        begin = begin.replace(tzinfo=now.tzinfo)
    return max(0.0, round((now - begin).total_seconds(), 1))


def _active_case(case: dict, task: dict, identity: dict, index: int, total: int, now: datetime) -> dict:
    elapsed = _elapsed_seconds(case, task, now)
    step = case.get("case_step")
    step_text = ""
    steps = case.get("steps") if isinstance(case.get("steps"), list) else []
    if type(step) is int and 0 < step <= len(steps):
        step_text = _safe_text(steps[step - 1], 120)
    waiting = _safe_text(case.get("waiting_on") or case.get("blocked_on"), 160)
    evidence = _safe_text(
        case.get("last_evidence") or case.get("evidence_summary"), 200,
    )
    parts = [f"[{index}/{total}] {identity['case_id']} | {(case.get('status') or 'pending').upper()}"]
    parts.append(f"{elapsed:.0f}s" if elapsed is not None else "elapsed: unknown")
    parts.append("step: " + (f"{step} {step_text}".strip() if step else "not reported"))
    if waiting:
        parts.append("waiting: " + waiting)
    parts.append("evidence: " + (evidence or "no new evidence"))
    return {**identity, "status": case.get("status"), "case_step": step, "step": step_text,
            "last_event_seq": case.get("last_event_seq"), "status_sync_at": case.get("status_sync_at"),
            "started_at": case.get("started_at") or task.get("started_at") or "",
            "elapsed_ms": int(elapsed * 1000) if elapsed is not None else None,
            "waiting_on": waiting, "last_evidence": evidence,
            "observed_at": now.isoformat(timespec="seconds"),
            "heartbeat_due_sec": 60, "heartbeat": " | ".join(parts),
            "next_action": "emit_visible_heartbeat"}


_SCRIPT = {"locator_missing", "control_unknown", "locator_not_found", "locator_ambiguous", "method_unknown",
           "script_failed", "check_empty", "assertion_missing", "model_policy_denied", "model_policy_invalid",
           "model_budget_exceeded", "ocr_low_confidence", "ocr_config_invalid"}
_PRECONDITION = {"data_missing", "fact_unavailable", "prerequisite_unavailable", "resource_account_missing", "resource_busy",
                 "otp_requires_human", "otp_environment_unavailable", "nexus_rejected", "nexus_unreachable"}
_PRODUCT = {"assertion_failed", "vlm_failed", "page_mismatch"}
_DISPOSITION = {
    "script_repair": ("脚本或定位", "只改定位或脚本后，正式重跑这些 case_id；不改原文预期"),
    "product_mismatch": ("产品与原文不符", "保持 FAIL 和封存证据，不为了通过而改断言；继续独立用例"),
    "source_repair": ("原文映射", "按 source_ref 修正映射；页面打开或登录通过不能充当该预期"),
    "precondition": ("前置不足", "界面里能建立的前置走准备任务后恢复原用例；历史样本不能现造，向用户求助并继续独立用例"),
    "unconfirmed": ("尚未归类", "先读封存记录再归类，不直接重跑"),
    "build_mismatch": ("被测构建不符", "安装需求指定的包并重新 version()；没有安装包路径就向用户求助，不要把构建不符写成产品失败"),
    "excluded": ("未执行", "核对排除依据；缺前置不要记成跳过"),
    "verified": ("已按原文验证", "保留封存证据，继续独立条目"),
    "accepted_difference": ("可接受差异", "空格、大小写或连写与用例不同，且需求未强制该写法；更新用例后重跑，不记产品缺陷"),
    "precondition_historical": ("历史样本不足", "不能现造历史数据；记为缺口草稿并向用户求助，不要重跑"),
}


def _disposition(case: dict, status: str) -> str:
    if status == "PASS":
        differences = case.get("accepted_differences") if isinstance(case.get("accepted_differences"), list) else []
        return "accepted_difference" if differences else "verified"
    if status == "SKIPPED":
        return "excluded"
    report = case.get("test_report") if isinstance(case.get("test_report"), dict) else {}
    reason = str(case.get("reason_code") or report.get("reason_code") or "")
    if reason in {"tested_build_mismatch", "tested_build_unverified", "app_build_mismatch", "app_build_unverified"}:
        return "build_mismatch"
    attr = str(report.get("attribution") or "")
    if attr == "automation" or reason in _SCRIPT:
        return "script_repair"
    if attr == "suspected_product" or reason in _PRODUCT:
        return "product_mismatch"
    if attr == "source_mismatch" or reason.startswith("source_"):
        return "source_repair"
    if reason in {"fact_unavailable", "historical_sample_missing"}:
        return "precondition_historical"
    if attr == "blocked" or reason in _PRECONDITION or status == "BLOCKED":
        return "precondition"
    return "unconfirmed"


def project(task: dict) -> dict:
    receipts, active = [], []
    cases = [c for c in task.get("cases") or [] if isinstance(c, dict)]
    now = datetime.now().astimezone()
    for index, case in enumerate(cases, 1):
        state = result(case, task)
        reporting = purpose(case, task)
        identity = {"task_id": task.get("task_id") or task.get("run_id") or "",
                    "case_id": case.get("case_id") or "", "sn": case.get("sn") or "",
                    "attempt_id": case.get("attempt_id") or "", "session_id": case.get("session_id") or "",
                    "revision": case.get("revision") or task.get("revision") or "",
                    "source_hash": case.get("source_hash") or ""}
        if state == "pending":
            active.append(_active_case(case, task, identity, index, len(cases), now))
            continue
        status = {"passed": "PASS" if reporting == "formal" else "OBSERVED", "failed": "FAIL"}.get(state, state.upper())
        summary = _safe_text(case.get("summary") or case.get("error"))
        reason = _safe_text(case.get("reason_code"), 128)
        if case.get("status") == "pass" and status == "UNVERIFIED":
            reason = "formal_evidence_incomplete"
        elapsed = case.get("duration_ms")
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
            elapsed = case.get("elapsed_ms")
        elapsed = elapsed if type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0 else None
        key = {**identity, "position": index, "status": status, "summary_version": case.get("summary_version"),
               "last_event_seq": case.get("last_event_seq"), "summary": summary, "reason_code": reason,
               "purpose": reporting, "duration_ms": elapsed,
               "evidence": _safe_text(case.get("last_evidence") or case.get("evidence_summary")),
               "detail_availability": case.get("detail_availability"),
               "environment": _safe_text(case.get("env_profile") or task.get("env_profile") or case.get("channel") or task.get("channel")),
               "expectation": _safe_text(case.get("expectation") or case.get("expected_result")),
               "accepted_differences": case.get("accepted_differences") if isinstance(case.get("accepted_differences"), list) else []}
        receipt_id = hashlib.sha256(json.dumps(key, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        expectation = _safe_text(case.get("expectation") or case.get("expected_result")) or "未上报；需回读锁定原文"
        environment = _safe_text(case.get("env_profile") or task.get("env_profile") or case.get("channel") or task.get("channel")) or "未上报"
        evidence = _safe_text(case.get("last_evidence") or case.get("evidence_summary")) or "摘要未提供证据引用；需读取 Scout 封存记录"
        disposition = "unconfirmed" if status == "UNVERIFIED" else _disposition(case, status)
        label, next_action = _DISPOSITION[disposition]
        if status == "UNVERIFIED":
            label, next_action = "证据不足", "补齐原文覆盖和 Scout 断言证据，不计正式通过"
        build = _safe_text(case.get("tested_build"), 120) or "未读取"
        differences = case.get("accepted_differences") if isinstance(case.get("accepted_differences"), list) else []
        difference_text = "；".join(_safe_text(f"{item.get('expected')} → {item.get('actual')}", 80) for item in differences if isinstance(item, dict)) or "无"
        lines = [f"[{index}/{len(cases)} 执行单元] {identity['case_id']} | {reporting.upper()} | {status} | " + (f"{elapsed / 1000:.1f}s" if elapsed is not None else "耗时未知"),
                 "执行：" + " | ".join(f"{k}={_safe_text(v) or '未上报'}" for k, v in identity.items()),
                 f"环境：{environment} | 被测构建：{build} | 预期：{expectation}", f"实际：{summary or '未上报'}",
                 f"可接受差异：{difference_text}",
                 f"证据：{evidence} | 可用性：{_safe_text(case.get('detail_availability')) or '未确认'}",
                 f"原因码：{reason or '未上报'} | 归属：{label}", f"下一步：{next_action}", f"回执：{receipt_id}"]
        receipts.append({**identity, "receipt_id": receipt_id, "position": index, "total": len(cases),
                         "purpose": reporting, "status": status, "raw_status": case.get("status"),
                         "formal_verdict": status if reporting == "formal" else None,
                         "duration_ms": elapsed, "summary": summary, "reason_code": reason,
                         "disposition": disposition, "tested_build": build,
                         "summary_version": case.get("summary_version"), "last_event_seq": case.get("last_event_seq"),
                         "detail_availability": case.get("detail_availability"), "requires_visible_receipt": True,
                         "template_version": 2, "environment": environment, "expectation": expectation,
                         "accepted_differences": differences,
                         "evidence_summary": evidence, "next_action": next_action,
                         "text": "\n".join(lines)})
    return {"case_receipts": receipts, "active_cases": active,
            "execution_pending": bool(active),
            "status_issue": "task_terminal_with_active_cases" if active and task.get("status") not in
                            {"pending", "queued", "running", "accepted", "cancel_requested"} else "",
            "delivery": {"mode": "snapshot", "deduplicate_by": "receipt_id", "acknowledged": False,
                         "owner": "calling_agent_or_client", "requires_visible_receipt": True}}


def require_verified_unit(task: dict, unit: str) -> list[str]:
    matching = [c for c in task.get("cases") or [] if c.get("case_id") == unit]
    if not matching or any(purpose(c, task) != "formal" or result(c, task) != "passed" for c in matching):
        raise ValueError("formal_verification_requires_complete_scout_evidence")
    return [r["receipt_id"] for r in project(task)["case_receipts"] if r["case_id"] == unit]
