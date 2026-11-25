#!/usr/bin/env python3
"""Offline quality-contract smoke; no device, server, model, or business database."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mino_nexus.contract.agent_workflow import transition
from mino_nexus.contract.case_receipts import project, require_verified_unit
from mino_nexus.contract.execution_report import task_report

START = datetime(2026, 11, 24, tzinfo=timezone.utc)


def at(seconds):
    return (START + timedelta(seconds=seconds)).isoformat()


def rejects(action, message):
    try:
        action()
    except ValueError as exc:
        assert message in str(exc), str(exc)
    else:
        raise AssertionError(f"Expected rejection: {message}")


def workflow():
    return transition(None, "init", "quality", {
        "scope": "fixture only", "source_read_id": "fixture-read",
        "source": {"kind": "fixture", "reference": "offline"}, "expected_units": ["A", "B"],
        "stage_time_budget_sec": 10, "total_time_budget_sec": 30}, at(0))[0]


def task():
    case = {"case_id": "A", "sn": "web-fixture", "status": "pass", "record_source": "scout", "sealed": True,
            "attempt_id": "attempt-A", "session_id": "session-A", "revision": "fixture-revision", "source_hash": "fixture-hash",
            "run_purpose": "verification", "summary": "All original expectations checked", "elapsed_ms": 12000,
            "summary_version": 7, "last_event_seq": 15, "detail_availability": "available",
            "assertions": {"total": 2, "pass": 2, "fail": 0, "unknown": 0},
            "source_coverage": {"expected": 2, "covered": ["S1", "E1"], "missing": []}}
    case["test_report"] = {"run_purpose": "verification", "context": {k: case[k] for k in
                             ("case_id", "attempt_id", "session_id", "revision", "source_hash")},
                           "counts": {"total": 1, "pass": 1, "fail": 0, "unknown": 0, "not_run": 0}}
    case["test_report"]["context"]["task_id"] = "task-fixture"
    return {"task_id": "task-fixture", "run_id": "task-fixture", "revision": "fixture-revision",
            "execution_kind": "case", "status": "done", "cases": [case]}


def check_workflow():
    state = workflow()
    _, initial = transition(state, "get", "quality", {}, at(0))
    assert initial["time_budget"]["remaining_sec"] == 30
    start = {"attempt_id": "a1", "unit": "A", "stage": "analysis", "action": "Read fixture source", "evidence": []}
    state, allowed = transition(state, "start", "quality", start, at(0))
    assert allowed["allowed"] and allowed["time_budget"]["remaining_sec"] == 30
    _, overdue = transition(state, "get", "quality", {}, at(11))
    assert overdue["time_budget"]["overdue_attempts"][0]["attempt_id"] == "a1"
    _, active = transition(state, "start", "quality", {**start, "attempt_id": "a2"}, at(11))
    assert not active["allowed"] and active["requires_visible_receipt"]
    request = {"question": "Which fixture endpoint applies?", "missing": "endpoint", "checked_sources": ["fixture source has no endpoint"], "affected_units": ["A"]}
    finish = {"attempt_id": "a1", "outcome": "blocked", "blocker": "endpoint_unknown", "help_request": request}
    state, done = transition(state, "finish", "quality", finish, at(11))
    assert done["requires_visible_receipt"] and done["time_budget"]["used_sec"] == 11
    same, _ = transition(state, "finish", "quality", finish, at(12))
    assert same == state
    _, help_doc = transition(state, "get", "quality", {}, at(1000))
    assert help_doc["help_requests"][0]["question"] == request["question"]
    assert help_doc["time_budget"]["used_sec"] == 11  # between-attempt user wait is excluded
    _, rejected = transition(state, "start", "quality", {**start, "attempt_id": "a2"}, at(1001))
    assert rejected["reason"] == "time_budget_exhausted"
    assert not rejected["coverage"]["all_verified"]
    state, independent = transition(state, "start", "quality", {**start, "attempt_id": "b1", "unit": "B"}, at(1001))
    assert independent["allowed"]
    state, _ = transition(state, "finish", "quality", {"attempt_id": "b1", "outcome": "progress", "evidence": ["fixture read"]}, at(1021))
    _, total = transition(state, "start", "quality", {**start, "attempt_id": "b2", "unit": "B", "stage": "authoring"}, at(1022))
    assert total["reason"] == "time_budget_exhausted" and total["next"] == "report_coverage_gap_not_complete"
    assert total["time_budget"]["used_sec"] == 31
    rejects(lambda: transition(state, "init", "quality", {"scope": "fixture only", "source_read_id": "fixture-read",
        "source": {"kind": "fixture", "reference": "offline"}, "expected_units": ["A", "B"],
        "stage_time_budget_sec": 100}, at(1023)), "time budget differs")
    rejects(lambda: transition(None, "init", "quality", {"stage_time_budget_sec": True, "scope": "fixture only",
        "expected_units": ["A"], "source_read_id": "fixture", "source": {"kind": "fixture", "reference": "offline"}}, at(0)), "Invalid time budgets")
    rejects(lambda: transition(state, "get", "quality", {}, "2026-11-24T00:00:00"), "timestamp")
    legacy = deepcopy(state)
    legacy.pop("stage_time_budget_sec"); legacy.pop("total_time_budget_sec")
    _, legacy_doc = transition(legacy, "get", "quality", {}, at(1100))
    assert legacy_doc["time_budget"]["stage_limit_sec"] == 300
    fresh = workflow()
    fresh, _ = transition(fresh, "start", "quality", start, at(0))
    rejects(lambda: transition(fresh, "finish", "quality", {**finish, "help_request": {**request, "affected_units": ["Z"]}}, at(1)), "affected_units")
    rejects(lambda: transition(fresh, "finish", "quality", {**finish, "outcome": "complete", "evidence": ["x"]}, at(1)), "blocked or deferred")
    rejects(lambda: transition(fresh, "finish", "quality", finish, at(-1)), "before it started")
    fresh, _ = transition(fresh, "finish", "quality", finish, at(1))
    fresh, _ = transition(fresh, "start", "quality", {**start, "attempt_id": "a2", "evidence": ["new fixture endpoint"]}, at(2))
    _, current = transition(fresh, "get", "quality", {}, at(2))
    assert not current["help_requests"]  # old question is not reported as a new active request
    print("PASS: elapsed budgets, active overdue, independent continuation, immutable budgets, wait exclusion, help validation, legacy defaults")


def check_receipts():
    doc = task()
    receipt = project(doc)["case_receipts"][0]
    assert receipt["status"] == "PASS" and receipt["formal_verdict"] == "PASS"
    assert project(doc)["case_receipts"][0]["receipt_id"] == receipt["receipt_id"]
    assert require_verified_unit(doc, "A") == [receipt["receipt_id"]]
    assert task_report(doc)["formal"]["passed"] == 1
    for mutate in (
        lambda c: c.update(sealed=False),
        lambda c: c["assertions"].update(fail=1),
        lambda c: c["source_coverage"].update(missing=["E1"]),
        lambda c: c["test_report"]["counts"].update(total=0, **{"pass": 0}),
        lambda c: c["test_report"]["counts"].update(not_run=1),
        lambda c: c["test_report"]["context"].update(source_hash="other"),
        lambda c: c["test_report"]["context"].update(attempt_id="other"),
        lambda c: c["test_report"]["context"].update(task_id="other"),
        lambda c: c.update(record_source="legacy"),
    ):
        incomplete = deepcopy(doc);mutate(incomplete["cases"][0])
        assert project(incomplete)["case_receipts"][0]["status"] == "UNVERIFIED"
        rejects(lambda: require_verified_unit(incomplete, "A"), "complete_scout_evidence")
    missing_expectation = deepcopy(doc)
    missing_expectation["cases"][0]["test_report"]["counts"] = {}
    assert task_report(missing_expectation)["formal"]["unverified"] == 1
    partial_multi = deepcopy(doc);other = deepcopy(doc["cases"][0]);other.update(sn="other-device", status="blocked")
    partial_multi["cases"].append(other)
    rejects(lambda: require_verified_unit(partial_multi, "A"), "complete_scout_evidence")
    rejects(lambda: require_verified_unit(doc, "B"), "complete_scout_evidence")
    for kind in ("debug", "prepare"):
        auxiliary = deepcopy(doc)
        if kind == "debug": auxiliary["execution_kind"] = "debug"
        else: auxiliary["source_context"] = {"purpose": "prepare"}
        assert project(auxiliary)["case_receipts"][0]["formal_verdict"] is None
        rejects(lambda: require_verified_unit(auxiliary, "A"), "complete_scout_evidence")
    live = deepcopy(doc);live["cases"][0]["status"] = "running"
    observed = project(live)
    assert observed["execution_pending"] and observed["status_issue"] and not observed["case_receipts"]
    updated = deepcopy(doc);updated["cases"][0]["summary_version"] += 1
    assert project(updated)["case_receipts"][0]["receipt_id"] != receipt["receipt_id"]
    updated["cases"][0].update(status="blocked", summary='password="fixture secret" token=fixture-token {"otp": "123456"} user=fixture@example.invalid Bearer fixture-bearer')
    safe = project(updated)["case_receipts"][0]
    assert all(value not in safe["text"] for value in ("fixture secret", "fixture-token", "123456", "fixture@example.invalid", "fixture-bearer"))
    assert receipt["template_version"] == 2
    assert all(label in receipt["text"] for label in ("执行：", "环境：", "预期：", "实际：", "证据：", "可用性：", "原因码：", "下一步：", "回执："))
    live["cases"][0].update(summary="登录错误", error="密码错误", last_evidence="", evidence_summary="")
    assert project(live)["active_cases"][0]["last_evidence"] == ""
    blocked = deepcopy(doc); blocked["cases"][0].update(status="blocked", summary="部分断言通过，缺历史样本")
    assert project(blocked)["case_receipts"][0]["status"] == "BLOCKED"
    assert not project(doc)["delivery"]["acknowledged"]
    print("PASS: full-evidence verdict, terminal receipts, deterministic deduplication, incomplete/context-mismatched/multi-device rejection, purpose separation, status inconsistency, redaction")


if __name__ == "__main__":
    check_workflow()
    check_receipts()
    print("Offline contracts only; no claim of real business-case verification or client delivery.")
