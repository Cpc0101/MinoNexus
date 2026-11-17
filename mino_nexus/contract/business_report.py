"""Bounded Scout business index; no arbitrary event data in summaries."""
from __future__ import annotations

ATTRIBUTIONS = {"", "automation", "source_mismatch", "suspected_product", "blocked", "unknown"}


def normalize_test_report(raw):
    if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw.get("version") != 1:
        raise ValueError("record_report_invalid")
    purpose = raw.get("run_purpose")
    if not isinstance(purpose, str) or purpose not in {"authoring", "verification", "regression", "preparation", "unclassified"}:
        raise ValueError("record_report_invalid")
    def counts(value, keys):
        if not isinstance(value, dict):
            raise ValueError("record_report_invalid")
        if any(type(value.get(key)) is not int or not 0 <= value[key] <= 9007199254740991 for key in keys):
            raise ValueError("record_report_invalid")
        return {key: value[key] for key in keys}
    def attr(value):
        return value if isinstance(value, str) and value in ATTRIBUTIONS else "unknown"
    items = raw.get("items")
    if not isinstance(items, list) or len(items) > 100:
        raise ValueError("record_report_invalid")
    result = []
    fields = {"source_ref": 120, "title": 300, "expected": 800, "actual": 800,
              "checker": 80, "artifact_id": 120, "reason_code": 120}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("status"), str) or item.get("status") not in {"pass", "fail", "unknown", "not_run"}:
            raise ValueError("record_report_invalid")
        result.append({**{key: str(item.get(key) or "")[:limit] for key, limit in fields.items()},
                       "status": item["status"], "attribution": attr(item.get("attribution")),
                       "event_seq": item["event_seq"] if type(item.get("event_seq")) is int and 0 <= item["event_seq"] <= 9007199254740991 else 0})
    usage = raw.get("model_usage") or {}
    model_usage = counts(usage, ("calls", "llm_calls", "vlm_calls", "elapsed_ms"))
    business_counts = counts(raw.get("counts"), ("total", "pass", "fail", "unknown", "not_run"))
    if business_counts["total"] != sum(business_counts[key] for key in ("pass", "fail", "unknown", "not_run")) or model_usage["calls"] != model_usage["llm_calls"] + model_usage["vlm_calls"]:
        raise ValueError("record_report_invalid")
    if len(result) > business_counts["total"] or raw.get("items_truncated") is not True and len(result) != business_counts["total"]:
        raise ValueError("record_report_invalid")
    if raw.get("items_truncated") is not True and any(sum(item["status"] == key for item in result) != business_counts[key] for key in ("pass", "fail", "unknown", "not_run")):
        raise ValueError("record_report_invalid")
    model_usage.update(total_tokens=usage.get("total_tokens") if type(usage.get("total_tokens")) is int and 0 <= usage["total_tokens"] <= 9007199254740991 else None,
                       usage_complete=usage.get("usage_complete") is True)
    failure = raw.get("failure")
    failure_index = {"failure": {"source_ref": str(failure.get("source_ref") or "")[:120],
                                  "title": str(failure.get("title") or "")[:300], "actual": str(failure.get("actual") or "")[:800],
                                  "event_seq": failure["event_seq"] if type(failure.get("event_seq")) is int and 0 <= failure["event_seq"] <= 9007199254740991 else 0}} if isinstance(failure, dict) else {}
    optional = {}
    if "context" in raw:
        if not isinstance(raw["context"], dict):
            raise ValueError("record_report_invalid")
        optional["context"] = {key: str(raw["context"].get(key) or "")[:500] for key in ("task_id", "session_id", "attempt_id", "case_id", "node_id", "revision", "script_path", "source_hash")}
    if "auxiliary_assertions" in raw:
        auxiliary = counts(raw["auxiliary_assertions"], ("total", "pass", "fail", "unknown"))
        if auxiliary["total"] != sum(auxiliary[key] for key in ("pass", "fail", "unknown")):
            raise ValueError("record_report_invalid")
        optional["auxiliary_assertions"] = auxiliary
    refs = [item["source_ref"] for item in result]
    if any(not ref for ref in refs) or len(set(refs)) != len(refs):
        raise ValueError("record_report_invalid")
    return {"version": 1, "run_purpose": purpose, **optional, "items": result, "items_truncated": raw.get("items_truncated") is True,
            "counts": business_counts,
            "attribution": attr(raw.get("attribution")),
            "reason_code": str(raw.get("reason_code") or "")[:120], "model_usage": model_usage, **failure_index}


def validate_report_summary(summary, payload):
    """Validate the report against this attempt, never a different execution."""
    from mino_nexus.contract.execution_report import timestamp
    for key in ("started_at", "finished_at"):
        value = summary.get(key)
        if value and timestamp(value) is None:
            raise ValueError("record_timestamp_invalid")
    start, end = timestamp(summary.get("started_at")), timestamp(summary.get("finished_at"))
    if start is not None and end is not None and end < start:
        raise ValueError("record_timestamp_invalid")
    if summary.get("status") in {"queued", "running"} and end is not None:
        raise ValueError("record_timestamp_invalid")
    report = summary.get("test_report") or {}
    for key, value in (report.get("context") or {}).items():
        if value and value != str(summary.get(key) or ""):
            raise ValueError("record_report_identity_mismatch")
    if report and any(item.get("event_seq", 0) > summary.get("last_event_seq", 0) for item in report["items"]):
        raise ValueError("record_report_evidence_mismatch")
    expected_purpose = "preparation" if (payload.get("source_context") or {}).get("purpose") == "prepare" else summary.get("run_purpose")
    if report and expected_purpose and expected_purpose != report.get("run_purpose"):
        raise ValueError("record_report_purpose_mismatch")
    assertions = summary.get("assertions")
    if assertions is not None and (not isinstance(assertions, dict) or any(type(assertions.get(k)) is not int or assertions[k] < 0 for k in ("total", "pass", "fail", "unknown")) or assertions["total"] != sum(assertions[k] for k in ("pass", "fail", "unknown"))):
        raise ValueError("record_assertions_invalid")
    if summary.get("status") == "pass" and (not assertions or not assertions["total"] or assertions["total"] != assertions["pass"] or any((report.get("counts") or {}).get(k) for k in ("fail", "unknown", "not_run")) or (summary.get("source_coverage") or {}).get("missing")):
        raise ValueError("record_pass_without_assertions")
