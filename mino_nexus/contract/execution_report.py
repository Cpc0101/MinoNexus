"""Pure task projection: execution units, expectations and assertions never mix."""
from __future__ import annotations
from datetime import datetime

LIVE = {"pending", "queued", "running", "accepted", "cancel_requested"}
ALIASES = {"product_fail": "fail", "tool_blocked": "blocked", "env_blocked": "blocked",
           "skip": "skipped", "unverified": "unverified", "unknown": "unverified",
           "untestable": "unverified", "unverifiable": "unverified", "unexecutable": "unverified"}
STATES = ("passed", "failed", "blocked", "declined", "skipped", "cancelled", "unverified", "pending")


def purpose(case, doc):
    if doc.get("execution_kind") == "debug":
        return "authoring"
    if (doc.get("source_context") or {}).get("purpose") == "prepare":
        return "preparation"
    value = case.get("run_purpose") or (case.get("test_report") or {}).get("run_purpose")
    return "authoring" if value == "authoring" else "formal" if value in {"verification", "regression"} or not value else "unclassified"


def has_formal_evidence(case, doc=None):
    doc = doc or {}
    report = case.get("test_report") or {}
    counts, assertions = report.get("counts") or {}, case.get("assertions") or {}
    context, coverage = report.get("context") or {}, case.get("source_coverage") or {}
    expected_context = {"case_id": case.get("case_id"), "session_id": case.get("session_id"),
                        "attempt_id": case.get("attempt_id"), "revision": case.get("revision") or doc.get("revision"),
                        "source_hash": case.get("source_hash")}
    task_id = doc.get("task_id") or doc.get("run_id") or case.get("task_id")
    if task_id:
        expected_context["task_id"] = task_id
    return (case.get("record_source") == "scout" and case.get("sealed") is True
            and all(type(values.get("total")) is int and values["total"] > 0
                    and type(values.get("pass")) is int and values["pass"] == values["total"]
                    and all(type(values.get(k)) is int and values[k] == 0 for k in zero_keys)
                    for values, zero_keys in ((counts, ("fail", "unknown", "not_run")), (assertions, ("fail", "unknown"))))
            and type(coverage.get("expected")) is int and coverage["expected"] > 0 and coverage.get("missing") == []
            and bool(context.get("task_id"))
            and all(bool(expected) and context.get(key) == expected for key, expected in expected_context.items()))


def result(case, doc=None):
    status = ALIASES.get(case.get("status"), case.get("status"))
    if status in LIVE:
        return "pending"
    if status == "pass":
        if purpose(case, doc or {}) == "formal":
            if not has_formal_evidence(case, doc):
                return "unverified"
        elif case.get("record_source") == "scout":
            assertions = case.get("assertions") or {}
            if case.get("sealed") is not True or not assertions.get("total") or assertions.get("fail") or assertions.get("unknown"):
                return "unverified"
    return {"pass": "passed", "fail": "failed"}.get(status, status if status in STATES else "unverified")


def bucket(cases, doc=None):
    out = {"total": len(cases), "completed": 0, "executed": 0, "not_run": 0, "execution_unknown": 0, **dict.fromkeys(STATES, 0)}
    for case in cases:
        state = result(case, doc)
        out[state] += 1
        out["completed"] += state != "pending"
        # execution_started is a DEVICE WRITE receipt, not proof the script ran.
        if case.get("started_at") or case.get("status") == "running" or (case.get("assertions") or {}).get("total", 0) > 0:
            out["executed"] += 1
        elif case.get("status") in {"pending", "queued", "accepted"} or case.get("execution_started") == "no" and (case.get("failure_stage") in {"admission", "preflight"} or case.get("status") in {"skipped", "skip", "cancelled"}):
            out["not_run"] += 1
        else:
            out["execution_unknown"] += 1
    out["judged"] = out["passed"] + out["failed"]
    out["pass_rate"] = int(out["passed"] / out["judged"] * 100 + 0.5) if out["judged"] else None
    return out


def outcome(cases, doc=None):
    selected = [c for c in cases if purpose(c, doc or {}) == "formal"]
    if not selected:
        return "done"
    counts = bucket(selected, doc)
    if counts["pending"]:
        return "running"
    for key, status in (("failed", "fail"), ("blocked", "blocked"), ("cancelled", "cancelled"), ("declined", "declined")):
        if counts[key]:
            return status
    if counts["passed"] == counts["total"]:
        return "pass"
    return "skipped" if counts["skipped"] == counts["total"] else "unverified"


def timestamp(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, OverflowError, OSError):
        return None


def task_report(doc):
    cases = [c for c in doc.get("cases") or [] if isinstance(c, dict)]
    groups = {key: bucket([c for c in cases if purpose(c, doc) == key], doc) for key in ("formal", "authoring", "preparation", "unclassified")}
    expected = {"total": 0, "pass": 0, "fail": 0, "unknown": 0, "not_run": 0, "cases_reported": 0, "cases_missing": 0}
    assertions = {"total": 0, "pass": 0, "fail": 0, "unknown": 0, "cases_reported": 0, "cases_missing": 0}
    auxiliary = dict(assertions)
    for case in cases:
        if purpose(case, doc) != "formal":
            continue
        for target, raw in ((expected, (case.get("test_report") or {}).get("counts")), (assertions, case.get("assertions")), (auxiliary, (case.get("test_report") or {}).get("auxiliary_assertions"))):
            keys = [k for k in target if not k.startswith("cases_")]
            valid = isinstance(raw, dict) and all(type(raw.get(k)) is int and raw[k] >= 0 for k in keys) and raw["total"] == sum(raw[k] for k in keys if k != "total")
            target["cases_reported" if valid else "cases_missing"] += 1
            if valid:
                for key in keys:
                    target[key] += raw[key]
    starts = [timestamp(c.get("started_at")) for c in cases]
    ends = [timestamp(c.get("finished_at")) for c in cases if c.get("started_at")]
    known_starts = [s for s in starts if s is not None]
    known_ends = [e for e in ends if e is not None]
    issue = "" if all(not c.get(k) or timestamp(c[k]) is not None for c in cases for k in ("started_at", "finished_at")) else "timestamp_timezone_missing_or_invalid"
    if any(timestamp(c.get("finished_at")) is not None and timestamp(c.get("started_at")) is not None and timestamp(c["finished_at"]) < timestamp(c["started_at"]) for c in cases):
        issue = "timestamp_order_invalid"
    duration = int((max(known_ends) - min(known_starts)) * 1000 + 0.5) if not issue and not bucket(cases, doc)["execution_unknown"] and bucket(cases, doc)["executed"] == len(known_starts) and known_starts and len(known_ends) == len(known_starts) and all(result(c, doc) != "pending" for c in cases) else None
    return {"version": 1, "unit": "case_execution", "execution_units": bucket(cases, doc), **groups,
            "expectations": expected, "assertions": assertions, "auxiliary_assertions": auxiliary, "result_status": outcome(cases, doc),
            "timing": {"submitted_at": doc.get("created_at") or doc.get("started_at") or None,
                       "execution_started_at": min((c["started_at"] for c in cases if timestamp(c.get("started_at")) == min(known_starts)), default=None) if known_starts else None,
                       "execution_finished_at": max((c["finished_at"] for c in cases if timestamp(c.get("finished_at")) == max(known_ends)), default=None) if duration is not None else None,
                       "duration_ms": duration, "issue": issue},
            "source": {"task_id": doc.get("run_id") or doc.get("task_id") or "", "parent_task_id": (doc.get("source_context") or {}).get("parent_task_id") or doc.get("derived_from") or ""}}
