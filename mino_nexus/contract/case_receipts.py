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
               "purpose": reporting}
        receipt_id = hashlib.sha256(json.dumps(key, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        expectation = _safe_text(case.get("expectation") or case.get("expected_result")) or "未上报；需回读锁定原文"
        environment = _safe_text(case.get("env_profile") or task.get("env_profile") or case.get("channel") or task.get("channel")) or "未上报"
        evidence = _safe_text(case.get("last_evidence") or case.get("evidence_summary")) or "摘要未提供证据引用；需读取 Scout 封存记录"
        next_action = {"PASS": "保留封存证据，继续独立条目", "FAIL": "核对原文与封存记录；根因未确认，不直接归因产品或脚本",
                       "BLOCKED": "登记缺口、已查来源与受影响条目，询问所需条件；继续独立条目",
                       "SKIPPED": "核对不适用或排除依据；缺资源不应记为跳过",
                       "UNVERIFIED": "补齐原文覆盖和 Scout 断言证据，不计正式通过"}.get(status, "保留当前记录，核对剩余工作")
        lines = [f"[{index}/{len(cases)} 执行单元] {identity['case_id']} | {reporting.upper()} | {status} | " + (f"{elapsed / 1000:.1f}s" if elapsed is not None else "耗时未知"),
                 "执行：" + " | ".join(f"{k}={_safe_text(v) or '未上报'}" for k, v in identity.items()),
                 f"环境：{environment} | 预期：{expectation}", f"实际：{summary or '未上报'}",
                 f"证据：{evidence} | 可用性：{_safe_text(case.get('detail_availability')) or '未确认'}",
                 f"原因码：{reason or '未上报'} | 根因：未确认", f"下一步：{next_action}", f"回执：{receipt_id}"]
        receipts.append({**identity, "receipt_id": receipt_id, "position": index, "total": len(cases),
                         "purpose": reporting, "status": status, "raw_status": case.get("status"),
                         "formal_verdict": status if reporting == "formal" else None,
                         "duration_ms": elapsed, "summary": summary, "reason_code": reason,
                         "summary_version": case.get("summary_version"), "last_event_seq": case.get("last_event_seq"),
                         "detail_availability": case.get("detail_availability"), "requires_visible_receipt": True,
                         "template_version": 2, "environment": environment, "expectation": expectation,
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
