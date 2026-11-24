#!/usr/bin/env python3
"""Disposable contracts for tag selection and cross-run Web failure analysis."""
from __future__ import annotations

import copy
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def rejected(fn, code: str) -> None:
    try:
        fn()
    except ValueError as exc:
        assert str(exc) == code, str(exc)
    else:
        raise AssertionError("expected " + code)


def check_tag_selection() -> None:
    from mino_nexus.casework.case_selection import select_cases_by_tags
    from mino_nexus.casework.case_store import normalize_case_tags
    from mino_nexus.casework import run_store

    assert normalize_case_tags("#Smoke, web，SMOKE,#") == ["Smoke", "web"]
    assert normalize_case_tags(("api", "API", "#e2e")) == ["api", "e2e"]
    rejected(lambda: normalize_case_tags(["x" * 65]), "case_tag_too_long")
    rejected(lambda: normalize_case_tags([str(i) for i in range(33)]), "too_many_case_tags")

    cases = [
        {"case_id": "c1", "name": "one", "tags": ["#Smoke", "Web"], "source_revision_id": "r1"},
        {"case_id": "c2", "name": "two", "tags": ["regression"], "source_revision_id": "r2"},
        {"case_id": "c3", "name": "three", "tags": ["SMOKE", "API"], "source_revision_id": "r3"},
        {"case_id": "c4", "name": "four", "tags": ["smoke", "web"], "source_revision_id": "r4"},
        {"case_id": "c4", "name": "duplicate", "tags": ["smoke"]},
    ]
    selected, plan = select_cases_by_tags(
        cases, include_tags=["smoke", "web", "SMOKE"], exclude_tags=["#WEB"], source_revision="repo-rev",
    )
    assert [case["case_id"] for case in selected] == ["c3"]
    assert plan["filters"] == {"include_tags": ["smoke", "web"], "exclude_tags": ["WEB"]}
    assert plan["matched_count"] == 1 and plan["selected_case_ids"] == ["c3"]
    assert plan["selected_case_revisions"] == {"c3": "r3"}
    assert plan["source_revision"] == "repo-rev"
    assert plan["skipped"] == [
        {"case_id": "c1", "reason": "tag_excluded"},
        {"case_id": "c2", "reason": "tag_not_included"},
        {"case_id": "c4", "reason": "tag_excluded"},
    ]

    paged, paged_plan = select_cases_by_tags(
        cases, include_tags="smoke", exclude_tags="", start_index=1, source_revision="repo-rev",
    )
    assert [case["case_id"] for case in paged] == ["c3", "c4"]
    assert paged_plan["matched_count"] == 3 and paged_plan["selected_case_ids"] == ["c3", "c4"]

    empty, empty_plan = select_cases_by_tags([], include_tags=["smoke"], source_revision="repo-rev")
    assert empty == [] and empty_plan["matched_count"] == 0 and empty_plan["selected_case_ids"] == []

    frozen_selection = copy.deepcopy(plan)
    previous = SimpleNamespace(
        payload={
            "run_id": "run-freeze",
            "owner_user_id": "fixture",
            "app_id": "app",
            "status": "running",
            "started_at": "2026-01-01T00:00:00+00:00",
            "execution_selection": frozen_selection,
            "cases": [{"execution_unit_id": "unit-1", "case_id": "c1", "sn": "web-fixture"}],
        },
        status="running",
    )

    class FakeSession:
        def execute(self, *_args, **_kwargs):
            return None

        def get(self, _model, _key):
            return previous

        def merge(self, _row):
            return None

    @contextmanager
    def session_scope():
        yield FakeSession()

    incoming = {
        "run_id": "run-freeze",
        "owner_user_id": "fixture",
        "app_id": "app",
        "status": "running",
        "started_at": "2026-01-01T00:00:00+00:00",
        "execution_selection": {"filters": {"include_tags": ["mutated"], "exclude_tags": []}},
        "cases": [{"execution_unit_id": "unit-1", "case_id": "c1", "sn": "web-fixture", "status": "running"}],
    }
    with patch("mino_nexus.core.database.session_scope", session_scope), \
         patch("mino_nexus.casework.run_write_safety.merge", return_value=[]), \
         patch("mino_nexus.casework.write_reconciliation.merge_receipts", return_value={}), \
         patch("mino_nexus.resources.device_admission.admit_in_transaction"):
        run_store._upsert_run(incoming)
    assert incoming["execution_selection"] == frozen_selection


def _case(case_id: str, status: str, reason: str, *, build: str, identity: str, fixture: str, stage: str = "assertion") -> dict:
    failure = {
        "stage": stage if status == "fail" else "",
        "step": "4",
        "reason_code": reason if status == "fail" else "",
        "actual": f"expected value, request_id=volatile-{case_id}" if status == "fail" else "",
    }
    return {
        "case_id": case_id,
        "execution_unit_id": "unit-" + case_id,
        "attempt_id": "attempt-" + case_id,
        "sn": "web-fixture",
        "status": status,
        "run_purpose": "regression",
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:01:00+00:00",
        "source_revision_id": "rev-1",
        "frozen_identity": {"source_digest": identity, "case_revision_id": "rev-1"},
        "inputs": {"fixture": fixture},
        "test_report": {"failure": failure},
    }


def _run(run_id: str, started_at: str, case: dict, **overrides) -> dict:
    doc = {
        "run_id": run_id,
        "app_id": "app-web",
        "platform": "web",
        "platforms_by_sn": {"web-fixture": "web"},
        "environment_baseline_digest": "env-1",
        "env_profile": "test",
        "env_surface": "web-test",
        "tested_build": "build-1",
        "execution_mode": "agent",
        "started_at": started_at,
        "cases": [case],
    }
    doc.update(overrides)
    if case.get("status") == "pass":
        case.update(record_source="scout", sealed=True, task_id=run_id,
                    session_id=run_id + "::" + case["case_id"], revision="fixture-revision",
                    source_hash=case["frozen_identity"]["source_digest"],
                    assertions={"total": 1, "pass": 1, "fail": 0, "unknown": 0},
                    source_coverage={"expected": 1, "covered": ["E1"], "missing": []})
        case["test_report"].update(
            counts={"total": 1, "pass": 1, "fail": 0, "unknown": 0, "not_run": 0},
            context={key: case[key] for key in
                     ("task_id", "case_id", "session_id", "attempt_id", "revision", "source_hash")})
    return doc


def check_failure_analysis() -> None:
    from mino_nexus.casework.failure_analysis import analyze_failure_history

    docs = []
    serial = 0

    def add(case_id: str, status: str, reason: str = "", *, build="build-1", identity="identity-1", fixture="fixture-1", stage="assertion", **overrides):
        nonlocal serial
        serial += 1
        case = _case(case_id, status, reason, build=build, identity=identity, fixture=fixture, stage=stage)
        case["execution_unit_id"] += f"-{serial}"
        case["attempt_id"] += f"-{serial}"
        at = f"2026-01-{serial:02d}T00:00:00+00:00"
        case["started_at"] = at
        docs.append(_run(f"run-{serial:02d}", at, case, tested_build=build, **overrides))

    add("stable", "fail", "assertion_a")
    add("stable", "fail", "assertion_a")
    add("recover", "fail", "assertion_b")
    add("recover", "pass")
    add("multi", "fail", "assertion_a")
    add("multi", "fail", "assertion_c")
    add("multi", "fail", "assertion_a")
    add("recurrent", "fail", "assertion_e")
    add("recurrent", "pass")
    add("recurrent", "fail", "assertion_e")
    add("stage", "fail", "assertion_f", stage="preparation")
    add("stage", "fail", "assertion_f", stage="verification")
    add("changed", "fail", "assertion_d")
    add(
        "changed", "pass", environment_baseline_digest="env-2", env_profile="pre",
        env_surface="web-pre", build="build-2", identity="identity-2", fixture="fixture-2",
    )

    analysis = analyze_failure_history(docs)
    groups = analysis["groups"]
    by_key = {(group["case_id"], group["signature_parts"]["reason"]): group for group in groups}
    stable = by_key[("stable", "assertion_a")]
    assert stable["classification"] == "stable_repeated_failure"
    assert stable["first_failure"]["run_id"] == "run-01" and len(stable["observations"]) == 2
    recover = by_key[("recover", "assertion_b")]
    assert recover["classification"] == "fail_then_pass"
    assert recover["first_failure"]["run_id"] == "run-03"
    assert [fact["status"] for fact in recover["observations"]] == ["failed", "passed"]
    multi_a = by_key[("multi", "assertion_a")]
    multi_c = by_key[("multi", "assertion_c")]
    assert multi_a["classification"] == "stable_repeated_failure"
    assert multi_a["first_failure"]["run_id"] == "run-05"
    assert multi_c["classification"] == "single_failure"
    assert multi_c["first_failure"]["run_id"] == "run-06"
    assert multi_a["signature"] != multi_c["signature"]
    assert multi_a["first_failure"]["signature_parts"]["reason"] == "assertion_a"
    recurrent = next(group for group in groups if group["case_id"] == "recurrent")
    assert recurrent["classification"] == "recurrent_failure"
    assert [fact["status"] for fact in recurrent["observations"]] == ["failed", "passed", "failed"]
    stage_groups = [group for group in groups if group["case_id"] == "stage"]
    assert len(stage_groups) == 2 and {group["signature_parts"]["stage"] for group in stage_groups} == {"preparation", "verification"}
    assert all(group["classification"] == "single_failure" for group in stage_groups)

    changed_reasons = {
        reason
        for item in analysis["incomparable"] if item["case_id"] == "changed"
        for reason in item["reason_codes"]
    }
    assert {
        "tested_build_changed", "environment_baseline_changed", "env_profile_changed",
        "env_surface_changed", "case_identity_changed", "fixture_digest_changed",
    } <= changed_reasons
    assert analysis["summary"]["stable_repeated_failures"] == 2
    assert analysis["summary"]["recurrent_failures"] == 1
    assert analysis["summary"]["fail_then_pass"] == 1
    assert analysis["summary"]["single_failures"] == 4
    assert analysis["groups"][0]["classification"] == "stable_repeated_failure"
    assert next(group for group in analysis["groups"] if group["case_id"] == "recurrent")["classification"] == "recurrent_failure"

    from mino_nexus.casework.failure_analysis import canonicalize_failure_text
    first = _case("ids", "fail", "HTTP 404 order_id=order-123456 /orders/123456", build="build-1", identity="identity-1", fixture="fixture-1")
    second = _case("ids", "fail", "HTTP 404 order_id=order-987654 /orders/987654", build="build-1", identity="identity-1", fixture="fixture-1")
    third = _case("ids", "fail", "HTTP 500 order_id=order-987654 /orders/987654", build="build-1", identity="identity-1", fixture="fixture-1")
    first_signature = analyze_failure_history([_run("id-1", "2026-02-01T00:00:00+00:00", first)])["groups"][0]["signature"]
    second_signature = analyze_failure_history([_run("id-2", "2026-02-02T00:00:00+00:00", second)])["groups"][0]["signature"]
    third_signature = analyze_failure_history([_run("id-3", "2026-02-03T00:00:00+00:00", third)])["groups"][0]["signature"]
    assert first_signature == second_signature and first_signature != third_signature
    assert "404" in canonicalize_failure_text("HTTP 404") and "500" in canonicalize_failure_text("HTTP 500")
    assert canonicalize_failure_text("line 12:34, HTTP 404, status 500") == "line 12:34, HTTP 404, status 500"
    for endpoint, canonical in (
        ("http://api.example.com:32100/path", "http://api.example.com:<port>/path"),
        ("localhost:43210", "localhost:<port>"),
        ("127.0.0.1:8080", "127.0.0.1:<port>"),
        ("[::1]:9443", "[::1]:<port>"),
        ("web-api:65000", "web-api:<port>"),
    ):
        assert canonicalize_failure_text(endpoint) == canonical
    assert canonicalize_failure_text("host:70000") == "host:70000"

    earlier = _case("time-order", "fail", "timeout", build="build-1", identity="identity-1", fixture="fixture-1")
    earlier["started_at"] = "2026-03-01T08:00:00+08:00"
    later = _case("time-order", "pass", "", build="build-1", identity="identity-1", fixture="fixture-1")
    later["started_at"] = "2026-03-01T00:30:00Z"
    chronological = analyze_failure_history([
        _run("run-later", "2026-03-01T00:30:00Z", later),
        _run("run-earlier", "2026-03-01T00:00:00Z", earlier),
    ])["groups"][0]
    assert chronological["classification"] == "fail_then_pass"
    assert chronological["first_failure"]["run_id"] == "run-earlier"


def check_web_runtime_capability_contract() -> None:
    from mino_nexus.core import protocol as P
    from mino_nexus.core.platform_contract import PlatformContractError, runtime_capabilities_contract
    from mino_nexus.loop.script_dispatch import _web_runtime_capability_error

    full = {
        "id": "web.capabilities",
        "executor": "playwright",
        "platform": "web",
        "contract_version": 1,
        "features": list(P.WEB_CAPABILITIES),
    }
    runtime_capabilities_contract([full], system="mac")
    requested = ["navigation", "javascript_execution", "file_upload"]

    def dispatch_error(rows, *, platform="web", capabilities=None):
        return _web_runtime_capability_error(
            SimpleNamespace(runtime_capabilities=rows),
            platform,
            requested if capabilities is None else capabilities,
        )

    assert dispatch_error([full]) == ""
    for rows in (
        None,
        [],
        [full, dict(full)],
        [{**full, "executor": "chromium"}],
        [{**full, "platform": "android"}],
        [{**full, "contract_version": 0}],
        [{**full, "contract_version": "1"}],
        [{**full, "features": None}],
        [{**full, "features": "navigation"}],
        [{**full, "features": [""]}],
        [{**full, "features": [1]}],
        [{**full, "features": ["navigation", "navigation"]}],
        [{**full, "features": list(P.WEB_CAPABILITIES) + ["unknown"] * 4}],
    ):
        assert dispatch_error(rows) == "web_runtime_capability_required", rows

    for features in (
        [],
        list(P.WEB_CAPABILITIES)[:-1],
        list(P.WEB_CAPABILITIES)[:-1] + ["unknown_capability"],
    ):
        assert dispatch_error([{**full, "features": features}]) == "web_runtime_capability_unsupported"
    assert dispatch_error([full], capabilities=["unknown_capability"]) == "web_runtime_capability_unsupported"
    assert dispatch_error(None, platform="android") == ""

    malformed_rows = (
        {**full, "features": []},
        {**full, "features": list(P.WEB_CAPABILITIES)[:-1]},
        {**full, "features": list(P.WEB_CAPABILITIES)[:-1] + ["unknown_capability"]},
        {**full, "features": tuple(P.WEB_CAPABILITIES)},
        {**full, "features": list(P.WEB_CAPABILITIES)[:-1] + [""]},
        {**full, "features": list(P.WEB_CAPABILITIES)[:-1] + ["navigation"]},
        {**full, "features": list(P.WEB_CAPABILITIES) + ["unknown_capability"] * 4},
        {**full, "contract_version": 0},
    )
    for row in malformed_rows:
        try:
            runtime_capabilities_contract([row], system="mac")
        except PlatformContractError as exc:
            assert exc.code == "web_capabilities_contract_invalid", (row, str(exc))
        else:
            raise AssertionError(f"malformed Web capability contract accepted: {row!r}")
    try:
        runtime_capabilities_contract([full, dict(full)], system="mac")
    except PlatformContractError as exc:
        assert exc.code == "runtime_capability_duplicate"
    else:
        raise AssertionError("duplicate Web capability contract rows accepted")

    assert {"javascript_execution", "file_upload"} <= set(P.WEB_CAPABILITIES)
    assert len(P.WEB_CAPABILITIES) == len(set(P.WEB_CAPABILITIES)) <= 20


def check_agent_workflow_units() -> None:
    from mino_nexus.contract.agent_workflow import transition

    source = {"kind": "file", "reference": "fixture.csv", "revision": "1", "content_hash": "hash"}
    args = {
        "scope": "empty fixture", "expected_units": [], "units": [],
        "source_read_id": "read-empty", "source": source,
    }
    state, summary = transition(None, "init", "workflow-empty", args, "2026-01-01T00:00:00Z")
    assert state["expected_units"] == [] and summary["expected_units"] == []
    assert summary["coverage"]["expected"] == 0
    repeated, _ = transition(state, "init", "workflow-empty", args, "2026-01-01T00:01:00Z")
    assert repeated["expected_units"] == []


def check() -> None:
    from mino_nexus import models  # noqa: F401
    from mino_nexus.core import database

    database.Base.metadata.create_all(database.engine)
    database._ready = True  # Empty disposable fixture only; never bootstrap production data.
    check_tag_selection()
    check_failure_analysis()
    check_agent_workflow_units()
    check_web_runtime_capability_contract()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="mino-nexus-web-stability-") as root:
        os.environ["MINO_NEXUS_MODE"] = "development"
        os.environ["MINO_NEXUS_DATA_DIR"] = root
        os.environ["MINO_NEXUS_DEV_DATA_DIR"] = root
        check()
    print("PASS: Web tag selection, frozen execution selection, Agent source units, and cross-run failure analysis")


if __name__ == "__main__":
    main()
