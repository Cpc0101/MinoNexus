"""Bounded Scout business index; no arbitrary event data in summaries."""
from __future__ import annotations

ATTRIBUTIONS = {"", "automation", "source_mismatch", "suspected_product", "blocked", "unknown"}


def normalize_test_report(raw):
    if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw.get("version") != 1:
        raise ValueError("record_report_invalid")
    purpose = raw.get("run_purpose")
    if not isinstance(purpose, str) or purpose not in {"authoring", "verification", "regression"}:
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
    return {"version": 1, "run_purpose": purpose, "items": result, "items_truncated": raw.get("items_truncated") is True,
            "counts": business_counts,
            "attribution": attr(raw.get("attribution")),
            "reason_code": str(raw.get("reason_code") or "")[:120], "model_usage": model_usage, **failure_index}
