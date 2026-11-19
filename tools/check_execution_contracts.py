#!/usr/bin/env python3
"""Offline admission/projection smoke. No server, device, or business database."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def check():
    from mino_nexus.resources.app_automation import get_automation_config
    from mino_nexus.resources.device_targets import bind_targets, target_channel
    from mino_nexus.resources.project_env import default_project_env
    from mino_nexus.contract.execution.task_target import for_device
    from mino_nexus.contract.execution_report import task_report

    app = {"id": "APP", "project_id": "PRJ", "env": {"automation": {"env_surface": "web.b"}}}
    assert get_automation_config(app, hydrate_cases=False)["env_surface"] == "web.b"
    env = default_project_env()
    env["channels"] = [
        {"id": "web.a", "app_id": "APP", "kind": "web", "platform": "web", "field": "base_url"},
        {"id": "web.b", "app_id": "APP", "kind": "web", "platform": "web", "field": "base_url"},
        {"id": "ios.app", "app_id": "APP", "kind": "app", "platform": "ios", "field": "bundle"},
    ]
    env["profiles"]["test"] = {"web.a": {"base_url": "https://same.invalid"},
                               "web.b": {"base_url": "https://same.invalid"},
                               "ios.app": {"bundle": "com.example.ios"}}
    targets = {}
    for sn, platform in (("webnode", "web"), ("phone", "ios")):
        channel = target_channel(app, {"platform": platform}, profile="test", env_doc=env)
        targets[sn] = {"platform": platform, "target_platform": platform, "target_surface": channel["id"]}
    frozen = bind_targets(app, targets, profile="test", env_doc=env)
    doc = json.loads(json.dumps({"platform": "mixed", "package": "", "env_surface": "", "device_targets": frozen}))
    assert for_device(doc, "webnode") == {"platform": "web", "env_surface": "web.b", "package": "https://same.invalid"}
    assert for_device(doc, "phone") == {"platform": "ios", "env_surface": "ios.app", "package": "com.example.ios"}
    env["profiles"]["test"]["web.b"]["base_url"] = "https://changed.invalid"
    assert for_device(doc, "webnode")["package"] == "https://same.invalid"
    try:
        target_channel(app, {"platform": "ios"}, surface="web.b", profile="test", env_doc=env)
    except ValueError:
        pass
    else:
        raise AssertionError("Explicit incompatible channel must remain rejected")
    for sn in doc["device_targets"]:
        assert for_device(doc, sn)["platform"] != "mixed"
    legacy = {"platform": "web", "package": "https://legacy.invalid", "env_surface": "web"}
    assert for_device(legacy, "webnode")["package"] == legacy["package"]
    report = task_report({"cases": [{"status": "blocked", "reason_code": "nexus_rejected", "execution_started": "no",
                                    "failure_stage": "admission", "run_purpose": "regression"}]})
    assert report["formal"]["failed"] == 0 and report["formal"]["blocked"] == 1
    assert report["formal"]["pass_rate"] is None
    print("PASS: default channel, mixed targets, frozen recovery, incompatible channel, legacy target, blocked statistics")
    check_cached_account()


def check_cached_account():
    from types import SimpleNamespace
    from unittest.mock import patch
    from mino_nexus.loop import script_callback as callback
    from mino_nexus.loop.script_runtime import ScriptAbort
    from mino_nexus.resources.accounts import lease
    from mino_nexus.resources import account_data, account_data_match

    ctx = SimpleNamespace(picked_account={"id": "cached", "otp": "fixture"}, resource_lease={})
    admission = SimpleNamespace(ctx=ctx, case={}, project_id="p", app_id="a", run_id="r", session_id="s", case_id="c")
    with patch.object(lease, "current_lease_is_active", return_value=False) as valid, \
         patch.object(lease, "lease_script_account", side_effect=AssertionError("Must not silently replace a lost lease")), \
         patch.object(account_data, "record_request") as requested, \
         patch.object(account_data_match, "rank_account_data", side_effect=lambda project, rows, *a, **kw: rows), \
         patch.object(callback, "_login_policy", return_value={}):
        for params in ({}, {"requirements": {"all": []}}):
            try:
                callback._account_impl(admission, params)
            except ScriptAbort as exc:
                assert exc.reason_code == "resource_account_missing"
            else:
                raise AssertionError("Inactive cached lease returned credentials")
        assert requested.call_count == 0
        valid.return_value = True
        result = callback._account_impl(admission, {})
        assert result["id"] == "cached" and requested.call_count == 1
    print("PASS: cached account lease is checked; lost lease blocks without reallocating or returning credentials")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="nexus-contract-") as directory:
        os.environ["MINO_NEXUS_DATA_DIR"] = directory
        check()
