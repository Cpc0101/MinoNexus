#!/usr/bin/env python3
"""Offline callback lifetime/heartbeat/lock-order regression with disposable SQLite."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def exercise(root):
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from mino_nexus.core import database, protocol as P
    from mino_nexus.api.ws import node as ws
    from mino_nexus.resources.node_registry import NodeRegistry, NodeSession
    from mino_nexus.loop import script_callback as callback

    engine = create_engine(f"sqlite:///{root}/lifecycle.sqlite", connect_args={"check_same_thread": False, "timeout": 3})
    factory = sessionmaker(bind=engine)
    with engine.begin() as db:
        db.execute(text("CREATE TABLE markers (id INTEGER PRIMARY KEY)"))

    def count():
        with engine.connect() as db:
            return db.execute(text("SELECT count(*) FROM markers")).scalar()

    class Socket:
        def __init__(self):
            self.sent = asyncio.Queue()
        async def send_text(self, raw):
            await self.sent.put(P.loads(json.loads(raw)))
        async def close(self, **kwargs):
            pass

    def fixture():
        socket = Socket()
        conn = ws.NodeConnection(socket)
        conn.node_id = "fixture-node"
        registry = NodeRegistry()
        live = NodeSession(node_id=conn.node_id, platform="mac", host={"os": "mac"},
                           send=conn.request, owner_user_id="owner", node_connection_generation="epoch",
                           scout_process_generation="process")
        registry._nodes[conn.node_id] = live
        registry._callback_lock(conn.node_id)
        registry.disconnect = lambda *a, **kw: []
        admission = callback.ScriptAdmission(run_id="run", session_id="session", case_id="case", sn="sn",
            node_id=conn.node_id, project_id="project", app_id="app", provider_id="", owner_user_id="owner",
            platform="web", case={}, ctx=None, writer=None)
        env = P.Envelope(type=P.MsgType.EXECUTE, msg_id="callback", ts="", payload=P.Execute(
            run_id="run", step_idx=-1, sn="sn", capability_id="case.account", params={"session_id": "session"}))
        return socket, conn, registry, live, admission, env

    async def feed(conn, env):
        await asyncio.wait_for(conn.on_text(json.dumps(P.dumps(env))), .5)

    async def event(signal):
        assert await asyncio.wait_for(asyncio.to_thread(signal.wait, 2), 3), "Worker did not reach barrier"

    with patch.object(database, "SessionLocal", factory), patch.object(database, "ensure_db"):
        # Hold a real write transaction and ask this same socket for a RESULT.
        socket, conn, registry, live, admission, env = fixture()
        loop = asyncio.get_running_loop()
        started, release, registry_held = threading.Event(), threading.Event(), threading.Event()
        def slow_call(*args):
            with database.session_scope() as db:
                db.execute(text("INSERT INTO markers VALUES (1)"))
                started.set()
                rpc = P.Execute(run_id="run", step_idx=2, sn="sn", capability_id="fixture-read")
                result = asyncio.run_coroutine_threadsafe(conn.request(P.MsgType.EXECUTE, rpc, timeout=2), loop).result(3)
                assert result and result.status == P.EventStatus.PASS
                assert release.wait(2)
            return {"_summary": "fixture", "id": "credential-must-not-leak"}
        def competing_writer():
            # Mirrors an unrelated registry writer waiting for the SQLite writer.
            with registry._lock:
                registry_held.set()
                with factory.begin() as db:
                    db.execute(text("INSERT INTO markers VALUES (2)"))
        with patch.object(ws, "get_registry", return_value=registry), \
             patch.object(callback, "_require", return_value=admission), patch.object(callback, "_call", side_effect=slow_call), \
             patch("mino_nexus.core.platform_contract.validate_platform_announcement"), \
             patch.object(registry, "_update_binding_epoch"), patch.object(live, "persist_snapshot") as persisted:
            await feed(conn, env)
            await event(started)
            rpc = await asyncio.wait_for(socket.sent.get(), 2)
            with ThreadPoolExecutor(max_workers=1) as pool:
                other = pool.submit(competing_writer)
                await event(registry_held)
                for i in range(20):
                    hb = P.Heartbeat(node_id=conn.node_id, uptime_sec=i, busy=bool(i % 2),
                        executors=[], executor_snapshot_version=1, node_connection_generation="epoch")
                    await feed(conn, P.Envelope(type=P.MsgType.HEARTBEAT, msg_id=f"hb{i}", ts="", payload=hb))
                assert persisted.call_count == 0, "Heartbeat acquired the transaction fence too early"
                assert len(conn._callback_workers) <= 2, "Heartbeats created unbounded workers"
                await feed(conn, P.Envelope(type=P.MsgType.RESULT, msg_id="result", ts="", reply_to=rpc.msg_id,
                    payload=P.Result(run_id="run", step_idx=2, status=P.EventStatus.PASS)))
                release.set()
                ack = await asyncio.wait_for(socket.sent.get(), 2)
                assert ack.reply_to == "callback" and ack.payload.status == P.EventStatus.PASS
                await asyncio.wait_for(conn._heartbeat_task, 2)
                assert persisted.call_count <= 2 and live.busy is True, "Latest coalesced heartbeat was lost"
                assert other.result(timeout=2) is None, "Registry/DB lock cycle"
            await conn.aclose()
            assert not conn._callback_workers
        assert count() == 2
        print("PASS: real slow SQLite callback + blocked registry writer + 20 heartbeats + nested RESULT; bounded coalescing; no lock cycle")

        # Already-running thread cannot be stopped by canceling its async await.
        socket, conn, registry, live, admission, env = fixture()
        started, release = threading.Event(), threading.Event()
        def pending_call(*args):
            with database.session_scope() as db:
                db.execute(text("INSERT INTO markers VALUES (3)"))
                started.set()
                assert release.wait(2)
            return {"id": "credential-must-not-leak"}
        with patch.object(ws, "get_registry", return_value=registry), \
             patch.object(callback, "_require", return_value=admission), patch.object(callback, "_call", side_effect=pending_call), \
             patch.object(ws, "INBOUND_CALLBACK_TIMEOUT_SEC", .03):
            await feed(conn, env)
            await event(started)
            receipt = await asyncio.wait_for(socket.sent.get(), 2)
            assert receipt.payload.error == "callback_outcome_unknown" and not receipt.payload.data
            await conn.aclose(timeout=.01)
            assert conn._callback_workers, "Await cancellation incorrectly claimed thread stopped"
            release.set()
            await conn.aclose(timeout=2)
            assert not conn._callback_workers and socket.sent.empty(), "Late credential reply after close"
        assert count() == 2, "Expired callback transaction committed instead of rolling back"
        print("PASS: callback timeout reports unknown; bounded close drain; running thread retained; rollback and no late credential reply")

        # A thread waiting for the admission lock must recheck the transport epoch.
        for field, changed in (("node_connection_generation", "new-epoch"), ("owner_user_id", "new-owner"),
                               ("scout_process_generation", "new-process"), ("closed", True)):
            socket, conn, registry, live, admission, env = fixture()
            required = threading.Event()
            def require(*args):
                required.set()
                return admission
            admission.lock.acquire()
            with patch.object(ws, "get_registry", return_value=registry), \
                 patch.object(callback, "_require", side_effect=require), patch.object(callback, "_call") as mutate:
                await feed(conn, env)
                await event(required)
                if field == "closed":
                    conn.close()
                else:
                    with registry._callback_lock(conn.node_id):
                        setattr(live, field, changed)
                admission.lock.release()
                await asyncio.wait_for(asyncio.gather(*list(conn._inbound_tasks), return_exceptions=True), 2)
                await conn.aclose(timeout=2)
                assert mutate.call_count == 0 and socket.sent.empty(), f"Stale {field} entered mutation/replied"
        print("PASS: admission-lock recheck rejects changed connection epoch, credential owner, process epoch and close")

        # Node events must revalidate after waiting for the registry lock too.
        from mino_nexus.core.callback_scope import bind_callback_scope
        socket, conn, registry, live, admission, env = fixture()
        binding = conn._binding(live)
        entered = threading.Event()
        def retired_event():
            with bind_callback_scope(lambda: registry.callback_scope(**binding,
                    deadline=float("inf"), revoked=lambda: False)):
                entered.set()
                try:
                    registry.node_event(env.payload, node_id=conn.node_id)
                except PermissionError:
                    return "rejected"
                raise AssertionError("Retired node event mutated the new session")
        with patch.object(live, "persist_snapshot") as persisted:
            registry._lock.acquire()
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(retired_event)
                assert entered.wait(2)
                live.node_connection_generation = "replacement"
                registry._lock.release()
                assert pending.result(timeout=2) == "rejected"
            assert persisted.call_count == 0
        # Timed-out worker threads still consume admission capacity.
        with patch.object(ws, "get_registry", return_value=registry):
            conn._callback_workers.update(object() for _ in range(64))
            await feed(conn, env)
            reply = await asyncio.wait_for(socket.sent.get(), 2)
            assert reply.payload.error == "callback_overloaded"
            conn._callback_workers.clear()
            await conn.aclose()
        print("PASS: node-event post-lock epoch fence; unfinished workers retain bounded queue capacity")
    engine.dispose()


def main():
    with tempfile.TemporaryDirectory(prefix="nexus-lifecycle-check-") as root:
        os.environ["MINO_NEXUS_DATA_DIR"] = root
        os.environ["MINO_NEXUS_DEV_DATA_DIR"] = root
        asyncio.run(exercise(root))


if __name__ == "__main__":
    main()
