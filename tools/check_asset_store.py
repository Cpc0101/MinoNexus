#!/usr/bin/env python3
"""Isolated asset-catalog smoke. No object storage, credentials, or business database."""
import hashlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    with tempfile.TemporaryDirectory(prefix="mino-asset-store-") as temporary:
        os.environ["MINO_NEXUS_MODE"] = "development"
        os.environ["MINO_NEXUS_DEV_DATA_DIR"] = temporary
        import mino_nexus.models
        from mino_nexus.core import database as database
        from mino_nexus.core.schema_contract import initialize_current_schema
        from mino_nexus.core.schema_migrations import ASSET_TABLES
        from mino_nexus.models.project import Project, App
        from mino_nexus.models.agent_task import AgentTask
        from mino_nexus.models.asset_storage import AssetStorage
        from mino_nexus.resources import asset_store as store
        from mino_nexus.resources import asset_preparation as prep
        from mino_nexus.contract.ownership import execution_user
        from sqlalchemy import inspect
        metadata = database.Base.metadata
        metadata.create_all(database.engine, tables=[t for n, t in metadata.tables.items() if n not in ASSET_TABLES])
        with database.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA user_version=15")
            connection.exec_driver_sql("INSERT INTO projects(id,name,created_by) VALUES('PRJ001','Hi3d','owner')")
        initialize_current_schema(database.engine, metadata)
        assert set(ASSET_TABLES) <= set(inspect(database.engine).get_table_names())
        with database.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA user_version").scalar() == 15
        database._ready = True
        sess = {"user_id": "owner"}
        with database.session_scope() as db:
            assert db.get(Project, "PRJ001").name == "Hi3d"
            db.add(App(id="APP003", project_id="PRJ001", name="hi3d_overseas"))
            db.add(AgentTask(owner_user_id="owner", task_id="task-one", project_id="PRJ001",
                            state={"status": "running"}, updated_at="2026-11-27T00:00:00Z"))
        config = {"storage_id": "local", "quota_bytes": 5000, "max_file_bytes": 3000, "max_preparations_per_task": 2}
        store.configure("PRJ001", sess, config)
        with database.session_scope() as db:
            row = db.get(AssetStorage, ("PRJ001", "local"))
            assert row.secret_key == "" and row.endpoint == "scout-library"
        public = store.storages("PRJ001", sess)
        assert public["location"] == "scout_library" and "access_key" not in str(public) and "endpoint" not in str(public)
        payload = b"asset-smoke-content"
        value = {"asset_id": "portrait", "version": 1, "kind": "portrait", "title": "portrait", "filename": "portrait.png",
                 "sha256": hashlib.sha256(payload).hexdigest(), "md5": hashlib.md5(payload, usedforsecurity=False).hexdigest(),
                 "size_bytes": len(payload), "attributes": {"height": 15}, "source": {"task_id": "task-one", "node_id": "scout-a"}}
        first = store.begin("PRJ001", sess, value)
        assert first["asset"]["state"] == "ready" and first["location"] == "scout_library" and "transfer" not in first
        assert first["asset"]["library_key"] == "library/" + value["sha256"]
        assert first["asset"]["holder_node_id"] == "scout-a"
        assert "http" not in str(first)
        assert store.storages("PRJ001", sess)["used_bytes"] == len(payload)
        def rejected(fn, code):
            try:
                fn()
            except store.AssetError as exc:
                assert exc.code == code, (exc.code, code)
            else:
                raise AssertionError(code)
        rejected(lambda: store.begin("PRJ001", sess, {**value, "attributes": {"height": 20}}), "asset_version_immutable")
        rejected(lambda: store.begin("PRJ001", sess, {**value, "asset_id": "oversize", "size_bytes": 4000, "sha256": "a" * 64, "md5": "b" * 32}), "asset_file_too_large")
        rejected(lambda: store.begin("PRJ001", sess, {**value, "asset_id": "secret", "attributes": {"token": "not-allowed"}}), "asset_metadata_invalid")
        rejected(lambda: store.configure("PRJ001", sess, {**config, "endpoint": "https://account.r2.cloudflarestorage.com"}), "asset_storage_external_not_supported")
        assert store.begin("PRJ001", sess, value)["deduplicated"] is True
        assert store.complete("PRJ001", sess, "portrait", 1)["location"] == "scout_library"
        assert store.ticket("PRJ001", sess, "portrait", 1)["location"] == "scout_library"
        assert "transfer" not in store.ticket("PRJ001", sess, "portrait", 1)
        assert store.holder_node_id("PRJ001", "portrait", 1) == "scout-a"
        assert store.list_assets("PRJ001", sess, attributes={"height": 15})["total"] == 1
        assert store.list_assets("PRJ001", sess, attributes={"height": 16})["total"] == 0
        payload2 = b"local-asset"
        value2 = {**value, "asset_id": "doll", "sha256": hashlib.sha256(payload2).hexdigest(), "md5": hashlib.md5(payload2, usedforsecurity=False).hexdigest(), "size_bytes": len(payload2), "source": {"task_id": "task-one", "node_id": "scout-a"}}
        second = store.begin("PRJ001", sess, value2)
        assert second["deduplicated"] is False and second["asset"]["state"] == "ready" and "transfer" not in second
        try:
            store.list_assets("PRJ001", {"user_id": "other"})
        except KeyError:
            pass
        else:
            raise AssertionError("cross-account access")
        demand = {"app_id": "APP003", "environment": "test", "kind": "portrait", "attributes": {"height": 15}}
        claimed = prep.claim("PRJ001", sess, {"task_id": "task-one", "demand": demand, "recipe_ref": "prepare_portrait.py"})
        assert claimed["claimed"] is True
        assert prep.claim("PRJ001", sess, {"task_id": "task-one", "demand": demand, "recipe_ref": "another.py"})["action"] == "wait"
        prep.finish("PRJ001", sess, claimed["preparation_id"], {"state": "ready", "asset_ref": {"asset_id": "portrait", "version": 1}, "evidence": {"task_id": "task-one"}})
        assert prep.claim("PRJ001", sess, {"task_id": "task-one", "demand": demand, "recipe_ref": "another.py"})["action"] == "reuse"
        demand2 = {**demand, "attributes": {"height": 20}}
        failed = prep.claim("PRJ001", sess, {"task_id": "task-one", "demand": demand2, "recipe_ref": "prepare.py"})
        prep.finish("PRJ001", sess, failed["preparation_id"], {"state": "failed", "evidence": {"reason": "generation_rejected"}})
        rejected(lambda: prep.claim("PRJ001", sess, {"task_id": "task-one", "demand": demand2, "recipe_ref": "prepare.py", "retry": True, "retry_evidence": {"result_known": True}}), "asset_preparation_budget_exhausted")
        from mino_nexus.assistant import assistant_tools
        from mino_nexus.contract.agent_question_policy import internal_question
        assert internal_question("plugin") and internal_question("session_id、attempt_id") and not internal_question("project_id")
        spec = {"id": "sample", "title": "sample", "params_schema": {"properties": {"project_id": {}, "app_id": {}}}, "param_resolvers": {"project_id": "project.by_name", "app_id": "app.by_name"}}
        with execution_user("owner"):
            args, ask, _, _ = assistant_tools.resolve_args(spec, {"project_id": "APP003"}, user_id="owner")
            assert not ask and args["project_id"] == "PRJ001" and args["app_id"] == "APP003"
            args, ask, _, _ = assistant_tools.resolve_args(spec, {"project_id": "default", "app_id": "APP003"}, user_id="owner")
            assert not ask and args["project_id"] == "PRJ001"
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from mino_nexus.api.rAssets import router
        from mino_nexus.api.deps import current_session
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[current_session] = lambda: sess
        with TestClient(app) as client:
            listed = client.get("/project/PRJ001/assets")
            assert listed.status_code == 200 and listed.json()["data"]["total"] == 2
            assert client.get("/project/PRJ001/assets/storage").json()["data"]["can_configure"] is True
            downloaded = client.post("/project/PRJ001/assets/portrait/1/download-ticket")
            assert downloaded.status_code == 200 and downloaded.json()["data"]["location"] == "scout_library"
            assert "transfer" not in downloaded.json()["data"] and "http" not in downloaded.text
            assert client.post("/project/PRJ001/assets/portrait/1/upload-ticket").json()["data"]["deduplicated"] is True
            assert client.get("/project/PRJ001/assets/preparations").json()["data"]["items"]
            assert client.put("/project/PRJ001/assets/storage", json={**config, "file": "not-accepted"}).status_code == 422
            assert client.put("/project/PRJ001/assets/storage", json={**config, "endpoint": "https://storage.example.com"}).status_code == 422
        # Generic internal parameters do not become operator questions.
        cli_spec = {"id": "cli.query", "route": "POST /runtime/cli/query", "risk": "read", "surfaces": ["mcp"],
                    "params_schema": {"properties": {"plugin": {}, "command": {}}, "required": ["plugin", "command"]}}
        with execution_user("owner"):
            result = assistant_tools._run_tool_owned(cli_spec, {"command": ["help"]}, user_id="owner", surface="mcp")
            assert not result.ask and result.data["reason_code"] == "agent_tool_arguments_required"
        # No file content has been written to Nexus's data root.
        assert not list(Path(temporary).rglob("*.blob")) and not list(Path(temporary).rglob("*.png"))
        print("PASS: additive migration, local catalog, quota, immutable versions, holder node, permissions, preparation dedup/budget, typed reference repair")


if __name__ == "__main__":
    main()
