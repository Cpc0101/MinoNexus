"""Cross-run failure signatures with explicit comparability boundaries."""
from __future__ import annotations

from collections import defaultdict
import re
from typing import Any, Iterable

from mino_nexus.contract.execution_report import result
from mino_nexus.core.stable_digest import digest


_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_ISO_RE = re.compile(r"\b20\d{2}-\d{2}-\d{2}[T ][0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?\b")
_EPOCH_RE = re.compile(r"\b(?:1[7-9]|2[0-1])\d{11}\b")
_REQUEST_ID_RE = re.compile(r"(?i)\b(request[_-]?id|trace[_-]?id|span[_-]?id)\s*[:=]\s*[A-Za-z0-9_.:-]+")
_EPHEMERAL_PORT_RE = re.compile(r"(?<=\d):\d{2,5}\b")


def canonicalize_failure_text(value: Any) -> str:
    """Remove volatile execution IDs while retaining the stable failure semantics."""
    text = str(value or "").strip()
    if not text:
        return ""
    text = _UUID_RE.sub("<uuid>", text)
    text = _ISO_RE.sub("<timestamp>", text)
    text = _EPOCH_RE.sub("<timestamp>", text)
    text = _REQUEST_ID_RE.sub(lambda match: f"{match.group(1)}=<request-id>", text)
    text = _EPHEMERAL_PORT_RE.sub(":<port>", text)
    return " ".join(text.split())


def _nested_text(value: Any, *paths: tuple[str, ...]) -> str:
    for path in paths:
        current: Any = value
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if current is not None and str(current).strip():
            return str(current)
    return ""


def _case_platform(doc: dict[str, Any], case: dict[str, Any]) -> str:
    sn = str(case.get("sn") or doc.get("sn") or "")
    return str((doc.get("platforms_by_sn") or {}).get(sn) or doc.get("platform") or "")


def _case_source_identity(doc: dict[str, Any], case: dict[str, Any]) -> str:
    frozen = case.get("frozen_identity")
    if isinstance(frozen, dict) and frozen:
        return digest(frozen)
    if case.get("source_hash"):
        return digest({"source_hash": case.get("source_hash"), "revision": case.get("source_revision_id") or case.get("revision")})
    return ""


def _fixture_digest(doc: dict[str, Any], case: dict[str, Any]) -> str:
    inputs = case.get("inputs")
    if inputs is None and isinstance(doc.get("case_inputs"), dict):
        inputs = doc["case_inputs"].get(str(case.get("case_id") or ""))
    return digest(inputs) if inputs not in (None, {}, []) else ""


def case_failure_fact(doc: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    report = case.get("test_report") if isinstance(case.get("test_report"), dict) else {}
    failure = report.get("failure") if isinstance(report.get("failure"), dict) else {}
    stage = str(case.get("failure_stage") or failure.get("stage") or failure.get("failure_stage") or "")
    step = case.get("case_step")
    if step in (None, ""):
        step = failure.get("step") or failure.get("step_id") or report.get("step") or ""
    reason = str(case.get("reason_code") or failure.get("reason_code") or failure.get("reason") or "")
    assertion = _nested_text(
        case,
        ("assertion",),
        ("test_report", "failure", "assertion"),
        ("test_report", "failure", "actual"),
        ("failure", "assertion"),
    )
    summary = str(case.get("summary") or case.get("error") or failure.get("summary") or failure.get("message") or "")
    canonical = {
        "source": str(case.get("source_hash") or case.get("source_revision_id") or ""),
        "step": canonicalize_failure_text(step),
        "reason": canonicalize_failure_text(reason),
        "assertion": canonicalize_failure_text(assertion or summary),
    }
    return {
        "run_id": str(doc.get("run_id") or doc.get("task_id") or ""),
        "case_id": str(case.get("case_id") or ""),
        "execution_unit_id": str(case.get("execution_unit_id") or ""),
        "attempt_id": str(case.get("attempt_id") or ""),
        "session_id": str(case.get("session_id") or case.get("report_run_id") or ""),
        "status": result(case),
        "started_at": str(case.get("started_at") or doc.get("started_at") or ""),
        "finished_at": str(case.get("finished_at") or doc.get("finished_at") or ""),
        "failure_stage": stage,
        "case_step": step,
        "reason_code": reason,
        "assertion": assertion,
        "summary": summary,
        "signature": digest(canonical),
        "signature_parts": canonical,
        "app_id": str(doc.get("app_id") or ""),
        "platform": _case_platform(doc, case),
        "environment_baseline_digest": str(doc.get("environment_baseline_digest") or ""),
        "env_profile": str(doc.get("env_profile") or ""),
        "env_surface": str(doc.get("env_surface") or ""),
        "tested_build": str(case.get("tested_build") or case.get("app_version") or doc.get("tested_build") or doc.get("expected_build") or ""),
        "execution_mode": str(doc.get("execution_mode") or ""),
        "case_identity": _case_source_identity(doc, case),
        "fixture_data": _fixture_digest(doc, case),
    }


def comparability_reasons(left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    checks = (
        ("app_id", "application"),
        ("platform", "platform"),
        ("environment_baseline_digest", "environment_baseline"),
        ("env_profile", "env_profile"),
        ("env_surface", "env_surface"),
        ("tested_build", "tested_build"),
        ("execution_mode", "execution_mode"),
        ("case_identity", "case_identity"),
        ("fixture_data", "fixture_data"),
    )
    for field, label in checks:
        before, after = str(left.get(field) or ""), str(right.get(field) or "")
        if not before or not after:
            reasons.append(label + "_unknown")
        elif before != after:
            reasons.append(label + "_changed")
    return reasons


def analyze_failure_history(run_docs: Iterable[dict[str, Any]], *, case_ids: Iterable[str] | None = None) -> dict[str, Any]:
    """Analyze immutable run documents, oldest first, without replaying execution."""
    wanted = set(str(case_id) for case_id in (case_ids or []) if str(case_id).strip())
    ordered_docs = sorted(
        [doc for doc in run_docs if isinstance(doc, dict)],
        key=lambda doc: (str(doc.get("started_at") or ""), str(doc.get("run_id") or "")),
    )
    by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    runs_scanned = 0
    for doc in ordered_docs:
        if str((doc.get("source_context") or {}).get("purpose") or "") == "prepare":
            continue
        runs_scanned += 1
        for case in doc.get("cases") or []:
            if not isinstance(case, dict):
                continue
            case_id = str(case.get("case_id") or "")
            if not case_id or wanted and case_id not in wanted:
                continue
            purpose = case.get("run_purpose") or (case.get("test_report") or {}).get("run_purpose")
            if purpose not in (None, "", "regression", "verification"):
                continue
            by_case[case_id].append(case_failure_fact(doc, case))

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    incomparable: list[dict[str, Any]] = []
    cases_scanned = 0
    for case_id, facts in sorted(by_case.items()):
        cases_scanned += 1
        ordered = sorted(facts, key=lambda fact: (fact["started_at"], fact["run_id"], fact["execution_unit_id"], fact["attempt_id"]))
        case_groups: list[dict[str, Any]] = []
        for fact in ordered:
            if fact["status"] == "failed":
                key = (case_id, fact["signature"])
                group = groups.get(key)
                if group is None:
                    group = {
                        "case_id": case_id,
                        "signature": fact["signature"],
                        "signature_parts": fact["signature_parts"],
                        "first_failure": fact,
                        "observations": [],
                        "classification": "single_failure",
                    }
                    groups[key] = group
                    case_groups.append(group)
                    group["observations"].append(fact)
                    continue
                reasons = comparability_reasons(group["first_failure"], fact)
                if reasons:
                    incomparable.append({
                        **fact,
                        "compared_to_run_id": group["first_failure"]["run_id"],
                        "reason_codes": reasons,
                    })
                else:
                    group["observations"].append(fact)
            elif fact["status"] == "passed":
                for group in case_groups:
                    reasons = comparability_reasons(group["first_failure"], fact)
                    if reasons:
                        incomparable.append({
                            **fact,
                            "compared_to_run_id": group["first_failure"]["run_id"],
                            "reason_codes": reasons,
                        })
                    else:
                        group["observations"].append(fact)

        for group in case_groups:
            same_failures = sum(
                obs["status"] == "failed" and obs["signature"] == group["signature"]
                for obs in group["observations"]
            )
            recovered_after_failure = any(obs["status"] == "passed" for obs in group["observations"])
            group["classification"] = (
                "fail_then_pass" if recovered_after_failure
                else "stable_repeated_failure" if same_failures >= 2
                else "single_failure"
            )
            group["run_ids"] = list(dict.fromkeys(obs["run_id"] for obs in group["observations"]))

    rendered_groups = sorted(
        groups.values(),
        key=lambda group: (group["classification"] != "stable_repeated_failure", group["case_id"], group["signature"]),
    )
    return {
        "groups": rendered_groups,
        "incomparable": incomparable,
        "summary": {
            "runs_scanned": runs_scanned,
            "cases_scanned": cases_scanned,
            "groups": len(rendered_groups),
            "stable_repeated_failures": sum(group["classification"] == "stable_repeated_failure" for group in rendered_groups),
            "fail_then_pass": sum(group["classification"] == "fail_then_pass" for group in rendered_groups),
            "single_failures": sum(group["classification"] == "single_failure" for group in rendered_groups),
            "incomparable_observations": len(incomparable),
        },
    }


def analyze_app_history(app_id: str, *, case_ids: Iterable[str] | None = None, history_limit: int = 20) -> dict[str, Any]:
    from mino_nexus.casework import run_store

    docs = run_store._fetch_payloads(limit=max(1, min(int(history_limit), 100)), app_id=app_id)
    analysis = analyze_failure_history(reversed(docs), case_ids=case_ids)
    analysis["summary"]["history_limit"] = history_limit
    analysis["summary"]["app_id"] = app_id
    return analysis
