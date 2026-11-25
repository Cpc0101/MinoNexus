from __future__ import annotations

from mino_nexus.loop import script_targets


def _patch(monkeypatch, stored):
    monkeypatch.setattr(
        "mino_nexus.casework.case_import.list_project_requirements",
        lambda project_id: [{"id": "REQ-1", "title": "需求一"}],
    )
    monkeypatch.setattr(
        "mino_nexus.casework.case_store.get_case",
        lambda project_id, case_id: stored.get(case_id),
    )


def _expand(platform="web"):
    return script_targets.expand_script_targets(
        "PRJ001",
        [{"requirement_id": "REQ-1", "case_ids": ["CASE-1"]}],
        run_id="r1",
        sn="sn1",
        platform=platform,
        repo_url="https://example.com/repo.git",
        branch="main",
        revision="abc",
    )


def test_web_script_target_gets_baseline_capabilities_without_stored_case(monkeypatch):
    _patch(monkeypatch, {})
    case = _expand()[0]
    assert case["web_capabilities"] == list(script_targets.DEFAULT_WEB_CAPABILITIES)
    assert "navigation" in case["web_capabilities"]


def test_web_script_target_prefers_stored_case_grant(monkeypatch):
    _patch(monkeypatch, {"CASE-1": {"web_capabilities": ["navigation"]}})
    assert _expand()[0]["web_capabilities"] == ["navigation"]


def test_non_web_script_target_gets_no_grant(monkeypatch):
    _patch(monkeypatch, {})
    assert _expand(platform="android")[0]["web_capabilities"] == []


def test_seed_case_keeps_web_capabilities():
    from mino_nexus.casework.run_store import seed_case

    case = seed_case("run1", {"case_id": "CASE-1", "web_capabilities": ["navigation", "clipboard"]}, 0, sn="sn1")
    assert case["web_capabilities"] == ["navigation", "clipboard"]
    plain = seed_case("run1", {"case_id": "CASE-2"}, 0, sn="sn1")
    assert plain["web_capabilities"] == []
