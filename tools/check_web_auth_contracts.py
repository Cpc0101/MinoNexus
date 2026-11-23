#!/usr/bin/env python3
"""Disposable SQLite contract smoke; no server, device or existing database."""
from __future__ import annotations
import copy
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def rejected(fn, code):
    try:
        fn()
    except (ValueError, PermissionError) as exc:
        assert str(exc) == code, str(exc)
    else:
        raise AssertionError("expected " + code)


def check_runtime_contract_diagnostics() -> None:
    from mino_nexus.catalog.models import Capability, Implementation
    from mino_nexus.catalog.registry import (
        framework_contract_diagnostics,
        runtime_contract_diagnostics,
    )
    from mino_nexus.contract.executor_sets import CATALOG_CHILDREN, FRAMEWORK_CHILDREN

    assert 'read_web_auth' not in CATALOG_CHILDREN['confirm_login_state']
    assert set(FRAMEWORK_CHILDREN['confirm_login_state']) >= {'read_web_auth', 'hierarchy', 'screenshot'}
    assert 'hierarchy' not in CATALOG_CHILDREN['operation.check_box']
    assert 'screenshot' not in CATALOG_CHILDREN['operation.check_box']
    assert framework_contract_diagnostics() == []

    local = [Implementation(id='fixture.local', executor='internal')]
    capabilities = {
        'confirm_login_state': Capability(
            id='confirm_login_state', kind='generic', display_name='login', caller='internal',
            phases=['prep', 'do'], implementations=local,
        ),
        'operation.check_box': Capability(
            id='operation.check_box', kind='generic', display_name='check box', caller='internal',
            phases=['prep', 'do'], implementations=local,
        ),
    }

    def child_issues(rows):
        parents = {'confirm_login_state', 'operation.check_box'}
        return [row for row in rows if parents.intersection(row['required_by'])]

    missing = {row['capability_id']: row for row in child_issues(runtime_contract_diagnostics(capabilities))}
    assert missing['tap_element']['reason_code'] == 'capability_missing'
    assert not {'read_web_auth', 'hierarchy', 'screenshot'}.intersection(missing)

    capabilities['tap_element'] = Capability(
        id='tap_element', kind='generic', display_name='tap', caller='case',
        phases=['prep', 'do'], implementations=[Implementation(id='fixture.tap', executor='playwright')],
    )
    assert child_issues(runtime_contract_diagnostics(capabilities)) == []


def check():
    from mino_nexus import models
    from mino_nexus.core import database
    from mino_nexus.models.pool_account import PoolAccount
    from mino_nexus.models.web_auth_snapshot import WebAuthSnapshot
    from mino_nexus.resources import web_auth_snapshots as auth
    from mino_nexus.resources.credential_safety import sanitize_credentials
    database.Base.metadata.create_all(database.engine)
    database._ready = True  # Empty fixture only; never run production bootstrap.
    lease = {"run_id": "fixture", "case_id": "C1", "sn": "webfixture", "slot": "default",
             "expires_at": "2099-01-01T00:00:00+00:00"}
    with database.session_scope() as db:
        db.add(PoolAccount(id="account-a", project_id="project-a", app_ids=["app-a"], env="test",
                           username="fixture-user", lease=lease))
    ctx = SimpleNamespace(project_id="project-a", app_id="app-a", env_profile="test",
                          env_surface="web-test", target_package="https://example.test/")
    s = auth.scope(ctx, {"id": "account-a"})
    credential = {"storage_state": {"cookies": [{"name": "session", "value": "fixture-cookie-secret",
        "domain": "example.test", "path": "/", "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax"}],
        "origins": [{"origin": s["origin"], "localStorage": [{"name": "auth", "value": "fixture-local-secret"}]}]},
        "session_storage": {s["origin"]: {"auth": "fixture-session-secret"}},
        "identity_field": "username", "identity_value": "fixture-user"}
    assert auth.get(s, expected_lease=lease)["state"] == "missing"
    first = auth.save(s, credential, 3600, expected_lease=lease)
    loaded = auth.get(s, expected_lease=lease)
    assert loaded["credential"] == credential and loaded["revision"] == first["revision"]
    with database.session_scope() as db:
        row = db.get(WebAuthSnapshot, s["scope_id"])
        assert row.credential.startswith("enc:v1:")
        assert "fixture-cookie-secret" not in row.credential
    # New scope on the same account cannot borrow another environment/surface/origin.
    for field, different in (("env_surface", "web-other"), ("target_package", "https://other.test/")):
        other = copy.copy(ctx)
        setattr(other, field, different)
        assert auth.get(auth.scope(other, {"id": "account-a"}), expected_lease=lease)["state"] == "missing"
    second = auth.save(s, credential, 3600, expected_lease=lease)
    assert not auth.invalidate(s, first["revision"], expected_lease=lease)["invalidated"]
    assert auth.get(s, expected_lease=lease)["state"] == "available"
    assert auth.invalidate(s, second["revision"], expected_lease=lease)["invalidated"]
    assert auth.get(s, expected_lease=lease)["state"] == "invalidated"
    auth.save(s, credential, 60, expected_lease=lease)
    with database.session_scope() as db:
        db.query(WebAuthSnapshot).filter_by(scope_id=s["scope_id"]).update({"expires_at": int(time.time()) - 1})
    assert auth.get(s, expected_lease=lease)["state"] == "expired"
    assert "credential" not in json.dumps(auth.statuses("project-a", "app-a", ["account-a"]))
    assert "fixture-session-secret" not in json.dumps(sanitize_credentials({"credential": credential}))
    wrong = copy.deepcopy(credential)
    wrong["identity_value"] = "another-user"
    rejected(lambda: auth.save(s, wrong, 60, expected_lease=lease), "web_auth_identity_mismatch")
    wrong = copy.deepcopy(credential)
    wrong["session_storage"]["https://foreign.test"] = {"auth": "wrong"}
    rejected(lambda: auth.save(s, wrong, 60, expected_lease=lease), "web_auth_snapshot_invalid")
    wrong = copy.deepcopy(credential)
    wrong["storage_state"]["cookies"][0]["domain"] = "foreign.test"
    rejected(lambda: auth.save(s, wrong, 60, expected_lease=lease), "web_auth_snapshot_invalid")
    wrong = copy.deepcopy(credential)
    wrong["session_storage"][s["origin"]]["large"] = "x" * auth.MAX_BYTES
    rejected(lambda: auth.save(s, wrong, 60, expected_lease=lease), "web_auth_snapshot_invalid")
    rejected(lambda: auth.get(s, expected_lease={**lease, "run_id": "foreign"}), "resource_account_missing")
    rejected(lambda: auth.save(s, credential, 0, expected_lease=lease), "web_auth_ttl_invalid")
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(lambda _: auth.save(s, credential, 60, expected_lease=lease), range(2)))
    assert len({row["revision"] for row in outputs}) == 2
    assert auth.get(s, expected_lease=lease)["revision"] in {row["revision"] for row in outputs}
    with database.session_scope() as db:
        db.query(PoolAccount).filter_by(id="account-a").update({"lease": {**lease, "app_id": "foreign-app"}})
    rejected(lambda: auth.get(s, expected_lease=lease), "web_auth_account_scope_mismatch")
    # Released or rebound accounts cannot read, save or invalidate credentials.
    with database.session_scope() as db:
        db.query(PoolAccount).filter_by(id="account-a").update({"lease": {}})
    rejected(lambda: auth.get(s, expected_lease=lease), "resource_account_missing")
    rejected(lambda: auth.invalidate(s, second["revision"], expected_lease=lease), "resource_account_missing")
    from mino_nexus.loop import script_callback
    from mino_nexus.resources.accounts import lease as account_lease
    admission = SimpleNamespace(ctx=ctx, platform="web", run_id="fixture", case_id="C1", sn="webfixture")
    assert {"web_auth_get", "web_auth_save", "web_auth_invalidate"} <= script_callback.HANDLED
    old = account_lease.leased_account_for_slot
    try:
        account_lease.leased_account_for_slot = lambda *a: None
        rejected(lambda: script_callback._web_auth(admission, "web_auth_get", {"account_id": "account-a"}), "web_auth_account_not_leased")
    finally:
        account_lease.leased_account_for_slot = old
    check_runtime_contract_diagnostics()
    print("PASS: encrypted snapshots, scope isolation, exact identity, TTL, revision-safe invalidation, lease rejection, redaction")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="mino-web-auth-contract-") as root:
        os.environ["MINO_NEXUS_MODE"] = "development"
        os.environ["MINO_NEXUS_DEV_DATA_DIR"] = root
        os.environ["MINO_NEXUS_DATA_DIR"] = root
        check()
