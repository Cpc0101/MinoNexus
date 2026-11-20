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
    check_account_callback_preserves_app_scope()
    check_multi_slot_accounts()
    check_template_allocation_slots()
    check_lease_scope_validation()
    check_debug_case_target_resolution()


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


def check_account_callback_preserves_app_scope():
    from types import SimpleNamespace
    from unittest.mock import patch
    from mino_nexus.loop import script_callback as callback
    from mino_nexus.resources import account_data
    from mino_nexus.resources.accounts import app_scope, lease
    from mino_nexus.resources import gmail_alias_lease, pool_account_store

    ctx = SimpleNamespace(
        run_id="r", case_id="c", sn="s", env_profile="test", project_id="p",
        app_id="app-a", platform="web", target_package="pkg:app-a:web",
        env_surface="surface:app-a:web", account_slot="default",
        leased_accounts=[], leased_account_leases=[],
        leased_account_by_slot={}, picked_account={}, resource_lease={},
        resource_env={}, accounts_brief="",
    )
    row = {
        "id": "acct-a", "account_id": "acct-a", "app_ids": ["app-a"],
        "email": "acct-a@example.invalid", "password": "fixture-password",
        "otp": "fixture-otp", "platform": "web",
        "target_package": "pkg:app-a:web", "env_surface": "surface:app-a:web",
    }
    with patch.object(lease.ps, "project_env", return_value={"channels": []}), \
         patch.object(app_scope, "validate_project_apps"), \
         patch.object(gmail_alias_lease, "enrich_gmail_alias_account_row", side_effect=lambda src, *a, **kw: dict(src)), \
         patch.object(pool_account_store, "read_leased_account_credentials", return_value={"password": "fixture-password", "otp": "fixture-otp"}), \
         patch.object(lease, "current_lease_is_active", return_value=True), \
         patch.object(account_data, "record_request"), \
         patch.object(lease, "account_login_policy", return_value={}):
        assert lease.apply_lease_to_ctx(
            ctx, row, project_id="p", slot="default", app_id="app-a", platform="web",
            target_package="pkg:app-a:web", env_surface="surface:app-a:web",
        )
        assert lease.leased_account_for_slot(ctx, "default")["app_ids"] == ["app-a"]
        admission = SimpleNamespace(
            ctx=ctx, case={}, project_id="p", app_id="app-a", run_id="r",
            session_id="s", case_id="c",
        )
        result = callback._account_impl(
            admission, {"slot": "default", "app_id": "app-a", "platform": "web"}
        )
        assert result["id"] == "acct-a" and result["app_id"] == "app-a"
    print("PASS: account callbacks preserve and re-check app scope without IndexError")


def check_debug_case_target_resolution():
    from unittest.mock import patch
    from mino_nexus.assistant import assistant_targets
    from mino_nexus.assistant.assistant_tools import _resolve_one, get_tool
    from mino_nexus.resources import project_store as ps
    from mino_nexus.resources.device_targets import target_channel
    from mino_nexus.resources.project_env import default_project_env

    spec = get_tool("debug.case")
    assert spec is not None
    resolvers = spec.get("param_resolvers") or {}
    assert resolvers.get("project_id") == "project.by_name"
    assert resolvers.get("app_id") == "app.by_name"
    assert resolvers.get("env_surface") == "channel.by_name"
    assert resolvers.get("sn") == "device.by_alias"

    project_id = "PRJ001"
    app_id = "APP003"
    env_surface = "web.app_APP003"
    app = {"id": app_id, "project_id": project_id}
    env = default_project_env()
    env["channels"] = [
        {"id": "web", "kind": "web", "platform": "web", "field": "base_url"},
        {"id": env_surface, "app_id": app_id, "kind": "web", "platform": "web",
         "alias": "hi3d_overseas", "field": "base_url"},
    ]
    env["profiles"]["test"] = {env_surface: {"base_url": "https://example.invalid"}}
    candidates = {"items": [{"id": env_surface, "label": "hi3d_overseas"}],
                  "unavailable": [], "env_profile": "test"}
    scope = {"project_id": project_id, "app_id": app_id, "env_profile": "test"}
    with patch.object(ps, "require_app", return_value=app), \
         patch.object(ps, "project_env", return_value=env), \
         patch.object(assistant_targets, "channel_candidates", return_value=candidates):
        for raw in ("web", env_surface, "hi3d_overseas"):
            ident, _label, options, ambiguous = _resolve_one(
                "channel.by_name", raw, user_id="fixture-user", scope=scope
            )
            assert ident == env_surface, (raw, ident, options, ambiguous)

    channel = target_channel(
        app, {"platform": "web"}, surface=env_surface, profile="test", env_doc=env
    )
    assert channel["id"] == env_surface
    print("PASS: debug.case resolves app-scoped channels before global channels")


def _account_row(account_id: str, *, slot: str, app_id: str, platform: str, project_id: str = "p") -> tuple[dict, dict]:
    account = {
        "id": account_id,
        "account_id": account_id,
        "slot": slot,
        "app_id": app_id,
        "platform": platform,
        "target_package": f"pkg:{app_id}:{platform}",
        "env_surface": f"surface:{app_id}:{platform}",
        "email": f"{account_id}@example.invalid",
    }
    row_lease = {
        "account_id": account_id,
        "project_id": project_id,
        "run_id": "r",
        "case_id": "c",
        "sn": "s",
        "slot": slot,
        "app_id": app_id,
        "platform": platform,
        "target_package": account["target_package"],
        "env_surface": account["env_surface"],
    }
    return account, row_lease


def check_multi_slot_accounts():
    from types import SimpleNamespace
    from unittest.mock import patch
    from mino_nexus.runtime.run_context import RunContext
    from mino_nexus.resources import app_automation, project_env
    from mino_nexus.resources.accounts import app_scope, lease

    ctx = RunContext(
        run_id="r", case_id="c", sn="s", env_profile="test", project_id="p",
        app_id="app-a", platform="web", target_package="pkg:app-a:web",
        env_surface="surface:app-a:web",
    )
    apps = {
        "app-a": {"id": "app-a", "project_id": "p", "platforms": ["web", "android"]},
        "app-b": {"id": "app-b", "project_id": "p", "platforms": ["web", "ios"]},
    }

    def require_app(app_id):
        if app_id not in apps:
            raise KeyError(app_id)
        return dict(apps[app_id])

    calls = []

    def allocate(scoped_ctx, _case):
        account_id = f"acct-{len(calls) + 1}"
        calls.append((scoped_ctx.account_slot, scoped_ctx.app_id, scoped_ctx.platform))
        account, row_lease = _account_row(
            account_id,
            slot=scoped_ctx.account_slot,
            app_id=scoped_ctx.app_id,
            platform=scoped_ctx.platform,
        )
        scoped_ctx.leased_accounts = [
            dict(row) for row in scoped_ctx.leased_accounts
        ] + [account]
        scoped_ctx.leased_account_leases = [
            dict(row) for row in scoped_ctx.leased_account_leases
        ] + [row_lease]
        lease._rebuild_account_views(scoped_ctx)
        return account, ""

    with patch.object(lease.ps, "require_app", side_effect=require_app), \
         patch.object(lease.ps, "project_env", return_value={"channels": []}), \
         patch.object(app_scope, "validate_project_apps"), \
         patch.object(project_env, "resolve_surface_for_app", side_effect=lambda _doc, **kw: f"surface:{kw['app_id']}:{kw['platform']}"), \
         patch.object(app_automation, "package_for_app", side_effect=lambda app, **kw: f"pkg:{app['id']}:{kw['platform']}"), \
         patch.object(lease, "lease_script_account", side_effect=allocate), \
         patch.object(lease, "current_lease_is_active", return_value=True):
        buyer, buyer_ctx, err = lease.lease_scoped_script_account(
            ctx, {}, slot="buyer", app_id="app-a", platform="web"
        )
        admin, admin_ctx, admin_err = lease.lease_scoped_script_account(
            ctx, {}, slot="admin", app_id="app-b", platform="ios"
        )
        ops, ops_ctx, ops_err = lease.lease_scoped_script_account(
            ctx, {}, slot="ops", app_id="app-b", platform="web"
        )
        again, _again_ctx, again_err = lease.lease_scoped_script_account(
            ctx, {}, slot="buyer", app_id="app-a", platform="web"
        )
        mismatch, _mismatch_ctx, mismatch_err = lease.lease_scoped_script_account(
            ctx, {}, slot="buyer", app_id="app-b", platform="web"
        )
        empty_platform, _empty_ctx, empty_platform_err = lease.lease_scoped_script_account(
            ctx, {}, slot="buyer", app_id="app-a", platform=""
        )

        assert not err and not admin_err and not ops_err and not again_err
        assert buyer["id"] == again["id"] == "acct-1"
        assert admin["id"] == "acct-2" and ops["id"] == "acct-3"
        assert buyer_ctx.app_id == "app-a" and buyer_ctx.platform == "web"
        assert admin_ctx.app_id == "app-b" and admin_ctx.platform == "ios"
        assert ops_ctx.app_id == "app-b" and ops_ctx.platform == "web"
        assert buyer_ctx.picked_account["id"] == "acct-1"
        assert admin_ctx.picked_account["id"] == "acct-2"
        assert ops_ctx.picked_account["id"] == "acct-3"
        assert calls == [("buyer", "app-a", "web"), ("admin", "app-b", "ios"), ("ops", "app-b", "web")]
        assert mismatch_err == "account_slot_scope_mismatch" and mismatch is None
        assert empty_platform_err == "account_platform_not_supported" and empty_platform is None
        assert ctx.leased_account_by_slot == {"buyer": "acct-1", "admin": "acct-2", "ops": "acct-3"}
        assert lease.leased_account_for_slot(ctx, "buyer")["id"] == "acct-1"
        assert lease.leased_account_for_slot(ctx, "admin")["id"] == "acct-2"

        before = json.loads(json.dumps(ctx.leased_account_leases))
        with patch.object(lease, "lease_script_account", return_value=(None, "no_account")):
            failed, _failed_ctx, failed_err = lease.lease_scoped_script_account(
                ctx, {}, slot="new-slot", app_id="app-a", platform="android"
            )
        assert failed is None and failed_err == "no_account"
        assert ctx.leased_account_leases == before

        replacement = {"id": "replacement", "app_id": "app-a"}
        assert not lease.apply_lease_to_ctx(
            ctx, replacement, project_id="p", slot="buyer",
            app_id="app-a", platform="web",
        )
        assert lease.leased_account_for_slot(ctx, "buyer")["id"] == "acct-1"

    default_ctx = SimpleNamespace(
        leased_accounts=[], leased_account_leases=[], leased_account_by_slot={},
        picked_account={}, resource_lease={}, resource_env={}, accounts_brief="",
    )
    for slot in ("buyer", "default", "ops"):
        account, row_lease = _account_row(
            f"acct-{slot}", slot=slot, app_id="app-a", platform="web"
        )
        default_ctx.leased_accounts.append(account)
        default_ctx.leased_account_leases.append(row_lease)
    lease._rebuild_account_views(default_ctx)
    assert default_ctx.picked_account["id"] == "acct-default"
    assert default_ctx.resource_lease["slot"] == "default"

    print("PASS: account slots are idempotent, cross-app/platform, independent, scope-checked, and failure-atomic")


def check_template_allocation_slots():
    from types import SimpleNamespace
    from unittest.mock import patch
    from mino_nexus.resources import account_allocate
    from mino_nexus.resources.accounts import lease

    default_account, default_lease = _account_row(
        "acct-default", slot="default", app_id="app-a", platform="web"
    )
    ctx = SimpleNamespace(
        run_id="r", case_id="c", sn="s", env_profile="test", project_id="p",
        app_id="app-b", platform="ios", target_package="pkg:app-b:ios",
        env_surface="surface:app-b:ios", account_slot="admin",
        leased_accounts=[default_account], leased_account_leases=[default_lease],
        leased_account_by_slot={}, picked_account={}, resource_lease={},
        resource_env={}, accounts_brief="",
    )
    lease._rebuild_account_views(ctx)

    admin_candidate = {
        "id": "acct-admin",
        "account_id": "acct-admin",
        "email": "admin@example.invalid",
        "username": "admin-login",
        "password": "fixture-password",
        "facets": {"role": "ops"},
    }
    seen_exclusions = []

    def fake_pick(rows, **kwargs):
        seen_exclusions.append(kwargs.get("excluded_account_ids"))
        return [admin_candidate]

    def apply_admin(target_ctx, row, **kwargs):
        account, row_lease = _account_row(
            "acct-admin",
            slot=kwargs["slot"],
            app_id=kwargs["app_id"],
            platform=kwargs["platform"],
        )
        account.update({
            "email": row["email"],
            "username": row["username"],
            "password": row["password"],
            "facets": dict(row.get("facets") or {}),
        })
        target_ctx.leased_accounts = [
            dict(item) for item in target_ctx.leased_accounts
        ] + [account]
        target_ctx.leased_account_leases = [
            dict(item) for item in target_ctx.leased_account_leases
        ] + [row_lease]
        lease._rebuild_account_views(target_ctx)
        return True

    template = {
        "id": "admin-template",
        "label": "运营管理员",
        "credential_fields": ["email", "username", "password"],
    }
    with patch.object(account_allocate, "get_template", return_value=template), \
         patch.object(account_allocate, "project_enabled_templates", return_value=set()), \
         patch.object(account_allocate, "template_facet_extensions", return_value={}), \
         patch.object(account_allocate, "list_test_accounts", return_value=[default_account, admin_candidate]), \
         patch.object(account_allocate, "pick_test_accounts", side_effect=fake_pick), \
         patch.object(account_allocate, "_mark_leased", return_value=True), \
         patch.object(account_allocate, "apply_lease_to_ctx", side_effect=apply_admin):
        grant, err = account_allocate.allocate_by_template(
            ctx,
            project_id="p",
            env_doc={},
            template_id="admin-template",
            env_profile="test",
            run_id="r",
        )

    assert not err
    assert seen_exclusions == [{"acct-default"}]
    assert grant["account_id"] == "acct-admin"
    assert grant["email"] == "admin@example.invalid"
    assert grant["username"] == "admin-login"
    assert ctx.picked_account["id"] == "acct-default"
    assert ctx.leased_account_by_slot == {
        "default": "acct-default",
        "admin": "acct-admin",
    }
    print("PASS: template allocation excludes other slots and returns the current slot, not the default view")


def check_lease_scope_validation():
    from types import SimpleNamespace
    from unittest.mock import patch
    from mino_nexus.resources.accounts import app_scope, lease
    from mino_nexus.resources import otp_resolve

    account, row_lease = _account_row(
        "acct-buyer", slot="buyer", app_id="app-a", platform="web"
    )
    ctx = SimpleNamespace(
        run_id="r", case_id="c", sn="s", env_profile="test", project_id="p",
        app_id="app-a", platform="web", target_package=account["target_package"],
        env_surface=account["env_surface"], leased_accounts=[account],
        leased_account_leases=[row_lease], picked_account=account, resource_lease=row_lease,
    )
    db_row = {"id": "acct-buyer", "env": "test", "lease": dict(row_lease)}

    with patch.object(lease.ps, "require_app", return_value={"id": "app-a", "project_id": "p"}), \
         patch.object(lease.ps, "project_env", return_value={}), \
         patch.object(lease, "list_test_accounts", return_value=[db_row]), \
         patch.object(lease, "lease_expired", return_value=False):
        assert lease.current_lease_is_active(ctx, "acct-buyer")
        stale = json.loads(json.dumps(db_row))
        stale["lease"]["platform"] = "ios"
        with patch.object(lease, "list_test_accounts", return_value=[stale]):
            assert not lease.current_lease_is_active(ctx, "acct-buyer")
        stale_slot = json.loads(json.dumps(db_row))
        stale_slot["lease"]["slot"] = "admin"
        with patch.object(lease, "list_test_accounts", return_value=[stale_slot]):
            assert not lease.current_lease_is_active(ctx, "acct-buyer")

    restore_ctx = SimpleNamespace(
        run_id="r", case_id="c", sn="s", app_id="app-a", platform="web",
        target_package="pkg:app-a:web", env_surface="surface:app-a:web",
        leased_accounts=[], leased_account_leases=[],
    )
    restore_row = {"id": "acct-buyer", "lease": dict(row_lease, platform="ios")}
    with patch.object(lease.ps, "require_app", return_value={"id": "app-a", "project_id": "p"}), \
         patch.object(lease.ps, "project_env", return_value={}), \
         patch.object(lease, "_purge_expired_leases"), \
         patch.object(lease, "list_test_accounts", return_value=[restore_row]), \
         patch.object(lease, "lease_expired", return_value=False), \
         patch.object(app_scope, "validate_project_apps"), \
         patch.object(app_scope, "account_matches_app", return_value=True):
        rows, err = lease._restore_leases_for_run(restore_ctx, "r", target_slot="buyer")
    assert rows == [] and err == "account_slot_scope_mismatch"

    renewed = []
    lease_ctx = SimpleNamespace(
        run_id="r", env_profile="test", app_id="app-a", case_id="c", sn="s",
        leased_account_leases=[row_lease, _account_row(
            "acct-admin", slot="admin", app_id="app-b", platform="ios"
        )[1]],
    )
    with patch.object(lease, "_mark_leased", side_effect=lambda *a, **kw: renewed.append(kw.get("slot")) or True):
        assert lease.renew_run_lease(lease_ctx)
    assert renewed == ["buyer", "admin"]

    policies = {
        "app-a": {"mode": "fixed", "fixed": "111111"},
        "app-b": {"mode": "fixed", "fixed": "222222"},
    }
    slots = [
        SimpleNamespace(app_id="app-a", env_profile="test", env_surface="surface:app-a:web"),
        SimpleNamespace(app_id="app-b", env_profile="test", env_surface="surface:app-b:ios"),
    ]
    seen = []

    def fake_otp_context(scoped_ctx):
        seen.append((scoped_ctx.app_id, scoped_ctx.env_surface))
        return {}, {}, policies[scoped_ctx.app_id]

    with patch.object(otp_resolve, "otp_context", side_effect=fake_otp_context):
        assert otp_resolve.resolve_otp(slots[0], {"id": "acct-a"})[0] == "111111"
        assert otp_resolve.resolve_otp(slots[1], {"id": "acct-b"})[0] == "222222"
    assert seen == [("app-a", "surface:app-a:web"), ("app-b", "surface:app-b:ios")]

    print("PASS: active leases verify slot/app/platform scope, all slots renew, and OTP follows each slot")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="nexus-contract-") as directory:
        os.environ["MINO_NEXUS_DATA_DIR"] = directory
        check()
