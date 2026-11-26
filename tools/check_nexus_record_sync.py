#!/usr/bin/env python3
"""Disposable SQLite and fake-socket regression; no services or target UI."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def check_records(root):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from mino_nexus.core.database import Base
    from mino_nexus.models.scout_record import ScoutRecord
    from mino_nexus.models.run import AppRegressionRun
    from mino_nexus.resources import scout_record_store as records
    from mino_nexus.loop import scout_record_tasks as tasks, script_callback as callback
    from mino_nexus.platform.identity import execution_user
    from mino_nexus.core import protocol as P

    engine = create_engine(f"sqlite:///{root}/fixture.sqlite")
    Base.metadata.create_all(engine, tables=[ScoutRecord.__table__, AppRegressionRun.__table__])
    factory = sessionmaker(bind=engine)

    @contextmanager
    def scope():
        with factory.begin() as db:
            yield db

    payload = dict(task_id="fixture-run", run_id="fixture-run", session_id="fixture-session",
                   attempt_id="fixture-session", case_id="fixture-case", owner_user_id="fixture-owner",
                   sn="fixture-web", platform="web", script_path="fixture.py")
    incoming = {**payload, "node_id": "fixture-node", "summary_version": 748, "status": "running",
                "state": "running", "sealed": False, "execution_started": "yes"}
    with patch.object(records, "session_scope", scope), execution_user("fixture-owner"):
        row = records.register(node_id="fixture-node", payload=payload, context={})
        row = records.receive("fixture-node", incoming)
        assert row["summary_version"] == 748 and row["status"] == "running"
        terminal = {**incoming, "summary_version": 833, "status": "fail", "state": "completed", "sealed": True,
                    "execution_started": "unknown", "resource_isolated": True, "summary": "fixture failure"}
        row = records.receive("fixture-node", terminal)
        assert row["status"] == "fail" and not records.release_evidence_confirmed(row)
        assert records.receive("fixture-node", incoming)["summary_version"] == 833
        assert records.receive("fixture-node", terminal) == row
        for changes, error in (({"owner_user_id": "foreign"}, "record_owner_mismatch"),
                               ({"node_id": "foreign"}, "record_owner_mismatch"),
                               ({"sealed": False}, "record_terminal_unsealed"),
                               ({"summary_version": True}, "record_version_invalid"),
                               ({"summary_version": 834, "status": "running"}, "record_terminal_conflict")):
            try:
                records.receive("fixture-node", {**terminal, **changes})
            except (ValueError, PermissionError) as exc:
                assert str(exc) == error
            else:
                raise AssertionError(f"Accepted invalid summary: {changes}")
        with patch.object(tasks, "normalize_incoming", side_effect=lambda r, value, owner: value), \
             patch("mino_nexus.resources.access_scope.node_owner_id", return_value="fixture-owner"), \
             patch.object(tasks, "project", side_effect=RuntimeError("fixture projector unavailable")):
            req = P.Execute(run_id="fixture-run", step_idx=-1, sn="fixture-web", capability_id="record.sync", params=terminal)
            ack = callback.handle("fixture-node", req)
            assert ack.status == P.EventStatus.PASS and ack.data["ack_version"] == 833
        from mino_nexus.casework import run_store
        doc = {"run_id": "fixture-run", "owner_user_id": "fixture-owner", "status": "running",
               "cases": [{"case_id": "fixture-case", "sn": "fixture-web", "status": "running", "summary_version": 748}]}
        def get(_):
            return deepcopy(doc)
        def put(value):
            doc.clear()
            doc.update(deepcopy(value))
            return deepcopy(doc)
        def finish(_, **kwargs):
            assert kwargs["release"] is False and kwargs["terminal_records_confirmed"] is True
            doc.update(status="unverified", finished_at="fixture")
            return deepcopy(doc)
        with patch.object(run_store, "get", side_effect=get), patch.object(run_store, "put", side_effect=put), \
             patch.object(run_store, "finish", side_effect=finish), patch.object(records, "settle_resources", wraps=records.settle_resources) as release, \
             patch("mino_nexus.loop.scout_resource_resolution.settle_terminal_task") as settle, \
             patch("mino_nexus.loop.observe.persist_run_finish.persist_run_finish"), \
             patch("mino_nexus.loop.observe.agent_stream.emit_testing_task"):
            # The stale caller snapshot must project the committed terminal version.
            assert tasks.project({**row, "status": "running", "summary_version": 748, "summary": incoming})
            case = doc["cases"][0]
            assert case["status"] == "fail" and case["summary_version"] == 833 and case["resource_isolated"] is True
            assert doc["write_safety"]["scout:fixture-session"]["state"] == "isolated"
            assert release.call_count == 1 and settle.call_count == 1
            assert not records.release_evidence_confirmed(row), "Projection must not fabricate stop evidence"
        with scope() as db:
            db.add(AppRegressionRun(run_id="fixture-run", owner_user_id="fixture-owner", status="running", payload={}))
            for i in range(5):
                db.add(ScoutRecord(session_id=f"scan-{i}", attempt_id=f"scan-{i}", task_id="fixture-run",
                    owner_user_id="fixture-owner", node_id="fixture-node", case_id="fixture-case", sn="fixture-web",
                    status="fail", summary_version=1, payload=payload, context={}, summary=terminal, updated_at=f"z{i}"))
        records._TERMINAL_SCAN_CURSORS.pop("projection", None)
        pages = [records.terminal_records_for_projection(limit=2) for _ in range(3)]
        assert len({r["session_id"] for page in pages for r in page}) == 6, "Recovery scan starved later rows"
        assert records.terminal_records_for_projection(limit=2)[0]["session_id"] == "fixture-session"
        closed = deepcopy(doc)
        closed["finished_at"] = "2026-11-26T10:00:00Z"
        with scope() as db:
            run = db.get(AppRegressionRun, "fixture-run")
            run.status, run.finished_at, run.payload = closed["status"], closed["finished_at"], closed
        upgraded = {**row["summary"], "summary_version": 834, "detail_availability": "available"}
        row = records.receive("fixture-node", upgraded)
        records._TERMINAL_SCAN_CURSORS.pop("projection", None)
        assert records.terminal_records_for_projection()[0]["summary_version"] == 834
        from mino_nexus.core import database
        def persisted(_):
            with factory() as db:
                return deepcopy(db.get(AppRegressionRun, "fixture-run").payload)
        with patch.object(database, "SessionLocal", factory), patch.object(database, "ensure_db"), \
             patch.object(run_store, "get", side_effect=persisted), \
             patch.object(run_store, "put", side_effect=AssertionError("Closed refresh used frozen put")), \
             patch.object(run_store, "finish", side_effect=AssertionError("Closed refresh reopened task")), \
             patch("mino_nexus.loop.scout_resource_resolution.settle_terminal_task"):
            assert tasks.project(row)
            refreshed = persisted("fixture-run")
            assert refreshed["cases"][0]["summary_version"] == 834
            assert refreshed["cases"][0]["detail_availability"] == "available"
            assert refreshed["status"] == closed["status"] and refreshed["finished_at"] == closed["finished_at"]
            assert refreshed["write_safety"] == closed["write_safety"]
            assert tasks.project(row) and persisted("fixture-run") == refreshed
            assert not records.terminal_records_for_projection(), "Closed projection watermark did not converge"
            try:
                run_store.refresh_scout_case({**row, "attempt_id": "foreign"}, **refreshed["cases"][0])
            except (PermissionError, ValueError):
                pass
            else:
                raise AssertionError("Closed refresh accepted a different attempt")
        run_store._LIVE.pop("fixture-run", None)
    engine.dispose()
    print("PASS: 748→833 sealed failure, replay/identity/conflict guards, ACK despite projector failure, latest projection, isolation, paged repair")


async def check_socket():
    from mino_nexus.api.ws.node import NodeConnection
    from mino_nexus.core import protocol as P
    from mino_nexus.loop import script_callback as callback
    from mino_nexus.resources.node_registry import NodeRegistry
    sent = asyncio.Queue()
    closed = []
    class Socket:
        async def send_text(self, value):
            await sent.put(P.loads(json.loads(value)))
        async def close(self, **kwargs):
            closed.append(kwargs)
    conn = NodeConnection(Socket())
    conn.node_id = "fixture-node"
    live = SimpleNamespace(send=conn.request, owner_user_id="fixture-owner",
                           scout_process_generation="fixture-process", node_connection_generation="fixture-connection")
    registry = NodeRegistry()
    registry._nodes["fixture-node"] = live
    registry.disconnect = lambda *a, **kw: []
    loop = asyncio.get_running_loop()
    def handle(node, req):
        rpc = P.Execute(run_id=req.run_id, step_idx=2, sn=req.sn, capability_id="fixture-read", params={})
        result = asyncio.run_coroutine_threadsafe(conn.request(P.MsgType.EXECUTE, rpc, timeout=1), loop).result(2)
        assert result and result.status == P.EventStatus.PASS, "Receive pump blocked by callback RPC"
        return P.Result(run_id=req.run_id, step_idx=req.step_idx, status=P.EventStatus.PASS)
    def frame(env):
        return json.dumps(P.dumps(env))
    request = P.Envelope(type=P.MsgType.EXECUTE, msg_id="fixture-callback", ts="", payload=P.Execute(
        run_id="fixture-run", step_idx=-1, sn="fixture-web", capability_id="case.account", params={}))
    with patch("mino_nexus.api.ws.node.get_registry", return_value=registry), patch.object(callback, "handle", side_effect=handle):
        await conn.on_text(frame(request))
        nested = await asyncio.wait_for(sent.get(), 2)
        await conn.on_text(frame(P.Envelope(type=P.MsgType.RESULT, msg_id="fixture-result", ts="", reply_to=nested.msg_id,
            payload=P.Result(run_id="fixture-run", step_idx=2, status=P.EventStatus.PASS))))
        ack = await asyncio.wait_for(sent.get(), 2)
        assert ack.reply_to == "fixture-callback" and ack.payload.status == P.EventStatus.PASS
        await asyncio.gather(*conn._inbound_tasks)
        conn._inbound_tasks.update(range(64))
        await conn.on_text(frame(request))
        assert (await sent.get()).payload.error == "callback_overloaded"
        conn._inbound_tasks.clear()
        live.send = object()
        await conn.on_text(frame(request))
        assert conn._closed and closed[-1]["code"] == 1008
        await conn.aclose()
    # The replaced socket must not disconnect the newly registered session.
    reg = NodeRegistry()
    replacement = SimpleNamespace(send=object())
    reg._nodes["fixture-node"] = replacement
    assert reg.disconnect("fixture-node", send=conn.request) == []
    assert reg.get_node("fixture-node") is replacement
    print("PASS: callback nested RPC, bounded queue, stale-frame rejection, old socket close preserves replacement")


def check_callback_and_contract():
    from mino_nexus.loop import script_callback as callback
    from mino_nexus.core import protocol as P
    from mino_nexus.assistant.agent_contract import AGENT_INSTRUCTIONS, agent_contract
    from mino_nexus.assistant.agent_guide import agent_guide
    from mino_nexus.assistant.agent_task_runtime import tracked_mcp
    from mino_nexus.api.rMcp import _task_schema, _handle, _tools_payload
    req = P.Execute(run_id="fixture-run", step_idx=-1, sn="fixture-web", capability_id="case.account",
                    params={"session_id": "fixture-session", "password": "DO-NOT-LOG", "otp": "DO-NOT-LOG"})
    with patch.object(callback, "_require", side_effect=ValueError("account_slot_scope_mismatch")), patch.object(callback.SLog, "w") as log:
        result = callback.handle("fixture-node", req)
        assert result.error == "account_slot_scope_mismatch"
        assert "fixture-session" in log.call_args.args[1] and "DO-NOT-LOG" not in log.call_args.args[1]
    from mino_nexus.casework import run_store
    from mino_nexus.resources import scout_record_store, node_registry
    from mino_nexus.platform.identity import current_user_id
    admission = callback.ScriptAdmission(run_id="fixture-run", session_id="fixture-session", case_id="fixture-case",
        sn="fixture-web", node_id="fixture-node", project_id="fixture-project", app_id="fixture-app",
        provider_id="", owner_user_id="fixture-owner", platform="web", case={}, ctx=None, writer=None)
    callback.register(admission)
    def owned_task(_):
        assert current_user_id() == "fixture-owner", "Callback did not bind durable owner"
        return {"status": "running"}
    try:
        with patch.object(run_store, "get", side_effect=owned_task), \
             patch.object(scout_record_store, "for_node", return_value=None), \
             patch.object(node_registry, "get_registry", return_value=SimpleNamespace(resolve=lambda _: (
                 SimpleNamespace(node_id="fixture-node", owner_user_id="fixture-owner"), ""))), \
             patch("mino_nexus.resources.access_scope.is_physical_device", return_value=False):
            assert callback._require("fixture-node", {"run_id": "fixture-run", "session_id": "fixture-session"}) is admission
    finally:
        callback.unregister("fixture-run", "fixture-session")
    for name in ("agent_guide", "project_list", "task_get", "session_events", "resource_env_get"):
        with patch("mino_nexus.assistant.assistant_tools.tool_by_function_name", return_value={"risk": "read"}), \
             patch("mino_nexus.assistant.agent_task_runtime.tasks.create", side_effect=AssertionError("Read created board")):
            call = tracked_mcp(lambda uid, tool, args: {"content": [{"type": "text", "text": '{"ok":true}'}]})
            assert not call("fixture-owner", name, {}).get("isError")
    call = tracked_mcp(lambda *a: (_ for _ in ()).throw(AssertionError("Guard bypassed")))
    assert call("fixture-owner", "case_run", {})["isError"]
    assert "agent_task_id" not in _task_schema({"id": "task.get"}).get("required", [])
    assert "agent_task_id" in _task_schema({"id": "case.run"})["required"]
    initialized = _handle("fixture-owner", {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert initialized["result"]["instructions"] == AGENT_INSTRUCTIONS
    tools = {tool["name"]: tool for tool in _tools_payload()}
    assert "不触发测试工作流" in tools["agent_guide"]["description"]
    assert "agent_task_id" not in tools["task_get"]["inputSchema"].get("required", [])
    assert "agent_task_id" in tools["case_run"]["inputSchema"]["required"]
    guide = agent_guide()
    assert guide["workflow_trigger"]["readonly_diagnosis_requires_board"] is False
    assert "coordinating/main thread" not in AGENT_INSTRUCTIONS
    assert "协调主线程" not in json.dumps(guide, ensure_ascii=False)
    assert "协调主线程" not in tools["agent_guide"]["description"]
    for excluded in agent_contract()["workflow_trigger"]["excluded"]:
        assert excluded.lower() in AGENT_INSTRUCTIONS.lower()
    print("PASS: account error code and credential-free identity log, read-only no board, explicit test guard, narrowed public instructions")


def main():
    with tempfile.TemporaryDirectory(prefix="nexus-sync-check-") as root:
        os.environ["MINO_NEXUS_DATA_DIR"] = root
        os.environ["MINO_NEXUS_DEV_DATA_DIR"] = root
        check_records(root)
        asyncio.run(check_socket())
        check_callback_and_contract()


if __name__ == "__main__":
    main()
