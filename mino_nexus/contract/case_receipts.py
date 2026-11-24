"""Read-only receipts from the task index; transport delivery remains client-owned."""
from __future__ import annotations

import hashlib
import json
import math
import re

from mino_nexus.contract.execution_report import purpose, result


def _safe_text(value: object, limit: int = 400) -> str:
    value = str(value or "")
    value = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [hidden]", value)
    value = re.sub(r"(?i)(password|passwd|token|secret|otp|验证码|密码)[\"\']?\s*[:=：]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,，;；]+)", r"\1=[hidden]", value)
    value = re.sub(r"[\w.+%-]+@[\w.-]+\.[A-Za-z]{2,}", "[email hidden]", value)
    value = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[phone hidden]", value)
    return " ".join(value.split())[:limit]


def project(task: dict) -> dict:
    receipts, active = [], []
    cases = [c for c in task.get("cases") or [] if isinstance(c, dict)]
    for index, case in enumerate(cases, 1):
        state = result(case, task)
        reporting = purpose(case, task)
        identity = {"task_id": task.get("task_id") or task.get("run_id") or "",
                    "case_id": case.get("case_id") or "", "sn": case.get("sn") or "",
                    "attempt_id": case.get("attempt_id") or "", "session_id": case.get("session_id") or "",
                    "revision": case.get("revision") or task.get("revision") or "",
                    "source_hash": case.get("source_hash") or ""}
        if state == "pending":
            active.append({**identity, "status": case.get("status"), "case_step": case.get("case_step"),
                           "last_event_seq": case.get("last_event_seq"), "status_sync_at": case.get("status_sync_at")})
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
        receipts.append({**identity, "receipt_id": receipt_id, "position": index, "total": len(cases),
                         "purpose": reporting, "status": status, "raw_status": case.get("status"),
                         "formal_verdict": status if reporting == "formal" else None,
                         "duration_ms": elapsed, "summary": summary, "reason_code": reason,
                         "summary_version": case.get("summary_version"), "last_event_seq": case.get("last_event_seq"),
                         "detail_availability": case.get("detail_availability"), "requires_visible_receipt": True,
                         "text": f"[{index}/{len(cases)}] {identity['case_id']} | {reporting.upper()} | {status}"
                                 + (f" | {elapsed / 1000:.1f}s" if elapsed is not None else "")
                                 + (f" | {summary}" if summary else "")})
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
