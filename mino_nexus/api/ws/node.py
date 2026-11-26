"""Scout 节点接入：`WS /node`。

与上游 MiniOrangeServer 的关系：上游 `server/websocket/device_manager.py`（1,623 行）
同时干三件事 —— UI observers 广播、ClawNode 连接管理、adb 通道分叉。拆分后：
  - UI observers 广播        → `mino_nexus/api/ws/observers.py`
  - ClawNode 连接管理        → **MinoScout** 的 `clawnode/`
  - **Scout 节点接入 → 本文件**（新写）

这一层只做搬运：收帧 → 交 NodeRegistry / 唤醒等待者；发帧 → 等应答。
选路与业务在 `loop/router_proxy.py`。
"""
from __future__ import annotations

import asyncio
import json
import time
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import WebSocket, WebSocketDisconnect

from mino_nexus.core import protocol as P
from mino_nexus.core.wire_identity import assign_execute_identity
from mino_nexus.core.log import SLog
from mino_nexus.resources.node_registry import get_registry

TAG = "NodeWS"

from mino_nexus.core.version import VERSION as NEXUS_VERSION
HEARTBEAT_INTERVAL_SEC = 15
INBOUND_CALLBACK_TIMEOUT_SEC = 30.0
CONNECTION_DRAIN_TIMEOUT_SEC = 5.0

# 协议 §1
MAX_FRAME_BYTES = 32 * 1024 * 1024

_seq = 0


def _msg_id() -> str:
    global _seq
    _seq += 1
    return f"{int(time.time() * 1000):013d}{_seq:06d}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


class NodeConnection:
    """一条 Scout 连接。负责编解码、请求-应答配对、超时。"""

    def __init__(self, ws: Any):
        self.ws = ws
        self.node_id = ""
        self._waiters: dict[str, asyncio.Future] = {}
        self._request_identities: dict[str, dict] = {}
        self._expired_requests: OrderedDict[str, dict] = OrderedDict()
        self._late_tasks: set[asyncio.Task] = set()
        self._reconciling: set[str] = set()
        self._inbound_tasks: set[asyncio.Task] = set()
        self._callback_workers: set[asyncio.Task] = set()
        self._disconnect_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._pending_heartbeat: tuple | None = None
        self._closed = False

    # ---------------- 对外：发请求等应答 ----------------

    async def request(
        self,
        mtype: P.MsgType,
        payload: Any,
        *,
        timeout: float = 30.0,
    ) -> Optional[P.Result]:
        """发一条 N→S 请求并等它的 RESULT。超时返回 None。

        协议 §6：调用方（RouterProxy）负责按同一 `(run_id, step_idx)` 重发一次；
        agent 可重取；V5 正编号写请求禁止网络重发，丢结果按 effect_unknown 终结。
        """
        if self._closed:
            return None
        if mtype == P.MsgType.EXECUTE and isinstance(payload, P.Execute):
            assign_execute_identity(payload)
        env = P.Envelope(type=mtype, msg_id=_msg_id(), ts=_now_iso(), payload=payload)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._waiters[env.msg_id] = fut
        identity = None
        if isinstance(payload, P.Execute):
            node = get_registry().get_node(self.node_id)
            identity = {"run_id": payload.run_id, "step_idx": payload.step_idx,
                "sn": payload.sn, "capability_id": payload.capability_id, "node_id": self.node_id,
                "owner_user_id": getattr(node, "owner_user_id", ""),
                "scout_process_generation": getattr(node, "scout_process_generation", ""),
                "node_connection_generation": getattr(node, "node_connection_generation", ""),
                "expires": time.monotonic() + 900}
            if payload.capability_id == "write_stop_status":
                from copy import deepcopy
                identity["action_ref"] = deepcopy(payload.params.get("action_ref"))
            self._request_identities[env.msg_id] = identity
        try:
            await self.ws.send_text(json.dumps(P.dumps(env), ensure_ascii=False))
        except Exception as exc:
            self._waiters.pop(env.msg_id, None)
            self._request_identities.pop(env.msg_id, None)
            SLog.e(TAG, f"[{self.node_id}] 发送 {mtype.value} 失败: {exc}")
            return None
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._waiters.pop(env.msg_id, None)
            SLog.w(TAG, f"[{self.node_id}] {mtype.value} 等 RESULT 超时 {timeout}s")
            return None
        except asyncio.CancelledError:
            self._waiters.pop(env.msg_id, None)
            SLog.w(TAG, f"[{self.node_id}] {mtype.value} 等待被取消（节点断开）")
            return None
        finally:
            self._request_identities.pop(env.msg_id, None)
            if identity is not None and fut.cancelled():
                self._expired_requests[env.msg_id] = identity
                while self._expired_requests and (len(self._expired_requests) > 512
                        or next(iter(self._expired_requests.values()))["expires"] < time.monotonic()):
                    self._expired_requests.popitem(last=False)

    async def _record_late_result(self, identity, result, receipt_id):
        try:
            from mino_nexus.casework.write_reconciliation import record_late_result
            await asyncio.to_thread(record_late_result, identity, result, receipt_id=receipt_id)
            self._expired_requests.pop(receipt_id, None)
        except Exception as exc:
            SLog.e(TAG, f"[{self.node_id}] 迟到回执持久化失败: {type(exc).__name__}")
        finally:
            self._reconciling.discard(receipt_id)

    # ---------------- 收帧 ----------------

    async def on_text(self, raw: str) -> None:
        try:
            env = P.loads(json.loads(raw))
        except P.UnknownMessageType as exc:
            # 协议 §7：未知 type 记 warn 并忽略，不可关连接
            SLog.w(TAG, f"[{self.node_id}] 未知消息类型 {exc}，忽略（协议 §7）")
            return
        except Exception as exc:
            SLog.e(TAG, f"[{self.node_id}] 解码失败，忽略: {type(exc).__name__}: {exc}")
            return

        if not self.node_id and env.type is not P.MsgType.REGISTER:
            await self.ws.close(code=1008, reason="必须先注册节点")
            self.close()
            return
        if self.node_id and env.type is not P.MsgType.REGISTER:
            live = get_registry().get_node(self.node_id)
            if live is None or live.send != self.request:
                await self.ws.close(code=1008, reason="节点会话已失效")
                self.close()
                return
            # 任意已验证的上行帧都证明连接还活着。Scout 的心跳线程可能被设备
            # 探测拖慢，但 RESULT / 框架事件仍在流动，不能因此判节点离线。
            get_registry().note_seen(self.node_id, send=self.request)

        # RESULT 是应答，交给等待者
        if env.type is P.MsgType.RESULT:
            receipt_id = env.reply_to or ""
            identity = self._request_identities.get(receipt_id) or self._expired_requests.get(receipt_id)
            if identity is not None and (env.payload.run_id, env.payload.step_idx) != (identity["run_id"], identity["step_idx"]):
                SLog.w(TAG, f"[{self.node_id}] RESULT 动作身份不匹配，保留原等待者")
                return
            fut = self._waiters.pop(receipt_id, None)
            if fut is None or fut.done():
                if identity is not None and identity["expires"] >= time.monotonic() and receipt_id not in self._reconciling:
                    self._reconciling.add(receipt_id)
                    task = asyncio.create_task(self._record_late_result(identity, env.payload, receipt_id))
                    self._late_tasks.add(task)
                    task.add_done_callback(self._late_tasks.discard)
                else:
                    SLog.w(TAG, f"[{self.node_id}] 收到无匹配请求的 RESULT（reply_to={env.reply_to}）")
                return
            if not fut.done():
                fut.set_result(env.payload)
            return

        reg = get_registry()
        if env.type is P.MsgType.REGISTER:
            if self.node_id:
                await self.ws.close(code=1008, reason="重新注册必须建立新连接")
                self.close()
                return
            await self._on_register(env)
        elif env.type is P.MsgType.HEARTBEAT:
            if env.payload.node_id != self.node_id:
                return
            binding = self._binding(live)
            self._pending_heartbeat = (env.payload, binding)
            if self._heartbeat_task is None or self._heartbeat_task.done():
                self._heartbeat_task = asyncio.create_task(self._dispatch_heartbeats())
                self._heartbeat_task.add_done_callback(self._worker_done)
            return
        elif env.type is P.MsgType.EXECUTE:
            # Callbacks may issue RPCs back to this socket; RESULT must keep flowing.
            if len(self._inbound_tasks) >= 64 or len(self._callback_workers) >= 64:
                req = env.payload
                await self._reply_result(env, P.Result(
                    run_id=req.run_id, step_idx=req.step_idx, status=P.EventStatus.FAIL,
                    summary="Nexus callback queue is full; retry the same receipt",
                    error="callback_overloaded", executor_used="nexus"))
                return
            binding = self._binding(live)
            task = asyncio.create_task(self._dispatch_inbound(env, binding))
            self._inbound_tasks.add(task)
            task.add_done_callback(self._inbound_tasks.discard)
        else:
            SLog.w(TAG, f"[{self.node_id}] 收到不该由 Nexus 处理的消息 {env.type.value}，忽略")

    def _binding(self, live) -> dict:
        return {"node_id": self.node_id, "send": self.request,
                "owner_user_id": live.owner_user_id,
                "connection_generation": live.node_connection_generation,
                "process_generation": live.scout_process_generation}

    async def _dispatch_heartbeats(self) -> None:
        from mino_nexus.core.callback_scope import bind_callback_scope, require_callback_current
        while self._pending_heartbeat is not None and not self._closed:
            heartbeat, binding = self._pending_heartbeat
            self._pending_heartbeat = None
            revoked = threading.Event()
            deadline = time.monotonic() + INBOUND_CALLBACK_TIMEOUT_SEC
            registry = get_registry()
            def scope():
                return registry.callback_scope(**binding, deadline=deadline,
                    revoked=lambda: self._closed or revoked.is_set())
            def invoke():
                with bind_callback_scope(scope):
                    require_callback_current()
                    registry.heartbeat(heartbeat, send=binding["send"])
            worker = asyncio.create_task(asyncio.to_thread(invoke))
            self._callback_workers.add(worker)
            worker.add_done_callback(self._worker_done)
            try:
                await asyncio.wait_for(asyncio.shield(worker), INBOUND_CALLBACK_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                SLog.w(TAG, f"[{binding['node_id']}] heartbeat persistence pending; receive pump remains active")
                # Keep at most one unfinished heartbeat worker per connection.
                revoked.set()
                try:
                    await asyncio.shield(worker)
                except PermissionError:
                    pass
            except (ValueError, TypeError, AttributeError) as exc:
                from mino_nexus.core.platform_contract import PlatformContractError
                if isinstance(exc, PlatformContractError):
                    code = exc.code
                elif str(exc) in {"executor_snapshot_version_required", "host_os_changed_requires_registration"}:
                    code = str(exc)
                else:
                    code = "heartbeat_contract_invalid"
                if self._scope_matches(binding):
                    SLog.w(TAG, f"[{binding['node_id']}] 心跳被拒绝: {code}")
                    await self.ws.close(code=1008, reason=f"心跳合同无效: {code}")
                    self.close()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                SLog.w(TAG, f"[{binding['node_id']}] heartbeat worker ended: {type(exc).__name__}")
            finally:
                revoked.set()

    def _scope_matches(self, binding) -> bool:
        live = get_registry().get_node(binding["node_id"])
        return bool(not self._closed and live is not None
                    and live.send == binding["send"]
                    and live.owner_user_id == binding["owner_user_id"]
                    and live.node_connection_generation == binding["connection_generation"]
                    and live.scout_process_generation == binding["process_generation"])

    def _worker_done(self, task) -> None:
        self._callback_workers.discard(task)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                SLog.w(TAG, f"[{self.node_id}] callback worker ended: {type(exc).__name__}")

    async def _dispatch_inbound(self, env: P.Envelope, binding: dict) -> None:
        from mino_nexus.core.callback_scope import bind_callback_scope, require_callback_current
        revoked = threading.Event()
        deadline = time.monotonic() + INBOUND_CALLBACK_TIMEOUT_SEC
        registry = get_registry()
        def scope():
            return registry.callback_scope(**binding, deadline=deadline,
                revoked=lambda: self._closed or revoked.is_set())
        def invoke():
            with bind_callback_scope(scope):
                require_callback_current()
                result = self._handle_inbound(env, binding["node_id"])
                require_callback_current()
                return result
        worker = None
        try:
            if not self._scope_matches(binding):
                return
            worker = asyncio.create_task(asyncio.to_thread(invoke))
            self._callback_workers.add(worker)
            worker.add_done_callback(self._worker_done)
            try:
                result = await asyncio.wait_for(asyncio.shield(worker), INBOUND_CALLBACK_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                revoked.set()
                req = env.payload
                result = P.Result(run_id=req.run_id, step_idx=req.step_idx, status=P.EventStatus.FAIL,
                    summary="Callback outcome is unknown; reconcile the same attempt before retrying",
                    error="callback_outcome_unknown", executor_used="nexus")
                SLog.w(TAG, f"[{binding['node_id']}] callback timeout request={env.msg_id}; worker stop unconfirmed")
            if self._scope_matches(binding):
                await self._reply_result(env, result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            SLog.e(TAG, f"[{binding['node_id']}] inbound request={env.msg_id} failed: {type(exc).__name__}")
        finally:
            # Canceling the await does not stop a Python thread or release its resources.
            revoked.set()

    def _handle_inbound(self, env: P.Envelope, node_id: str) -> P.Result:
        req: P.Execute = env.payload
        cap = str(req.capability_id or "")
        from mino_nexus.loop.script_callback import HANDLED, handle as handle_script_callback

        op = str((req.params or {}).get("op") or "") if cap == "script.callback" else ""
        if cap in HANDLED or (cap == "script.callback" and op in HANDLED):
            return handle_script_callback(node_id, req)
        from mino_nexus.resources.node_registry import known_node_events

        event = cap.split(".", 1)[-1] if cap.startswith("node.") else cap
        if event not in known_node_events():
            SLog.w(TAG, f"[{node_id}] 忽略未登记的事件 cap={cap}")
            return P.Result(
                run_id=req.run_id, step_idx=req.step_idx,
                status=P.EventStatus.FAIL, summary=f"Nexus 不认识事件 {cap}",
                error=f"unknown_event:{event}", executor_used="nexus",
            )
        outcome = get_registry().node_event(req, node_id=node_id)
        if not outcome.handled:
            # 收到但没处理必须说清楚，不能回 PASS 让 Scout 以为事件已被消费。
            reason = outcome.reason or f"Nexus 未处理 {cap}"
            SLog.w(TAG, f"[{node_id}] 拒绝 {cap}：{reason}")
            return P.Result(
                run_id=req.run_id, step_idx=req.step_idx,
                status=P.EventStatus.FAIL, summary=reason,
                error=reason, executor_used="nexus",
            )
        if outcome.interrupted:
            from mino_nexus.loop.run_interrupt import interrupt_runs

            event = cap.split(".", 1)[-1]
            interrupt_runs(outcome.interrupted, reason=f"节点 {node_id} {event}")
        return P.Result(
            run_id=req.run_id, step_idx=req.step_idx,
            status=P.EventStatus.PASS, summary=f"acked {cap}",
            executor_used="nexus",
        )

    async def _on_register(self, env: P.Envelope) -> None:
        req: P.Register = env.payload
        reg = get_registry()

        if req.account_isolation_version != 1:
            await self._reply(env, P.Registered(accepted=False, nexus_version=NEXUS_VERSION,
                reason="请升级 Scout：当前客户端未支持账号隔离，旧插件配置不能继承"))
            await self.ws.close(code=1008, reason="Scout 需要升级账号隔离支持")
            self.close()
            return

        from mino_nexus.core.platform_contract import validate_platform_announcement, PlatformContractError
        try:
            validate_platform_announcement(req)
        except (PlatformContractError, TypeError, AttributeError):
            await self._reply(env, P.Registered(accepted=False, nexus_version=NEXUS_VERSION,
                reason="平台或嵌套能力合同无效，请升级 Scout 至平台合同 v1"))
            await self.ws.close(code=1008, reason="平台合同不兼容")
            self.close()
            return

        from dataclasses import replace
        from mino_nexus.resources.runtime_tokens import authenticate_registration

        try:
            identity = await asyncio.to_thread(authenticate_registration, req.token, req.node_id)
        except PermissionError as exc:
            await self._reply(env, P.Registered(
                accepted=False, nexus_version=NEXUS_VERSION,
                reason=str(exc),
            ))
            SLog.w(TAG, f"拒绝注册 node={req.node_id}：token 无效")
            await self.ws.close(code=1008, reason="节点凭证或归属无效")
            self.close()
            return
        owner_user_id = identity["user_id"]
        assigned = identity["node_id"]
        reg_req = replace(req, node_id=assigned, devices=[]) if assigned != req.node_id else req
        self.node_id = assigned
        warnings = list(identity["warnings"])
        warnings.extend(await asyncio.to_thread(reg.register, reg_req, send=self.request, owner_user_id=owner_user_id))

        issued_node_id = ""
        if assigned != req.node_id:
            issued_node_id = assigned

        await self._reply(env, P.Registered(
            accepted=True,
            platform_contract_version=1,
            nexus_version=NEXUS_VERSION,
            session_token=f"sess-{_msg_id()}",
            heartbeat_interval_sec=HEARTBEAT_INTERVAL_SEC,
            warnings=warnings,
            node_id=issued_node_id,
            node_token=identity["node_token"],
            owner_user_id=owner_user_id,
            node_connection_generation=reg.get_node(assigned).node_connection_generation,
        ))

    async def _reply_result(self, req_env: P.Envelope, payload: P.Result) -> None:
        env = P.Envelope(
            type=P.MsgType.RESULT, msg_id=_msg_id(), ts=_now_iso(),
            payload=payload, reply_to=req_env.msg_id,
        )
        if not self._closed:
            await self.ws.send_text(json.dumps(P.dumps(env), ensure_ascii=False))

    async def _reply(self, req_env: P.Envelope, payload: Any) -> None:
        env = P.Envelope(
            type=P.MsgType.REGISTERED, msg_id=_msg_id(), ts=_now_iso(),
            payload=payload, reply_to=req_env.msg_id,
        )
        await self.ws.send_text(json.dumps(P.dumps(env), ensure_ascii=False))

    def _disconnect(self) -> None:
        if self.node_id:
            runs = get_registry().disconnect(self.node_id, send=self.request)
            if runs:
                from mino_nexus.loop.run_interrupt import interrupt_runs
                interrupt_runs(runs, reason=f"节点 {self.node_id} 断开")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pending_heartbeat = None
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
        for task in list(self._inbound_tasks):
            task.cancel()
        for fut in self._waiters.values():
            if not fut.done():
                fut.cancel()
        self._waiters.clear()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._disconnect()
        else:
            self._disconnect_task = loop.create_task(asyncio.to_thread(self._disconnect))
            self._disconnect_task.add_done_callback(self._worker_done)

    async def aclose(self, *, timeout: float = CONNECTION_DRAIN_TIMEOUT_SEC) -> None:
        self.close()
        pending = {*self._inbound_tasks, *self._callback_workers, *self._late_tasks}
        if self._disconnect_task is not None:
            pending.add(self._disconnect_task)
        if self._heartbeat_task is not None:
            pending.add(self._heartbeat_task)
        if pending:
            _, unfinished = await asyncio.wait(pending, timeout=timeout)
            if unfinished:
                SLog.w(TAG, f"[{self.node_id}] close drain pending={len(unfinished)}; worker stop unconfirmed")


# ---------------- FastAPI 路由 ----------------


def register_routes(app: Any) -> None:
    # 注意：WebSocket 必须**模块级** import。本文件用了
    # `from __future__ import annotations`，注解是字符串，FastAPI 会拿模块 globals
    # 去 eval —— 函数内 import 会让它 NameError（踩过一次）。
    @app.websocket("/node")
    async def node_endpoint(ws: WebSocket) -> None:  # pragma: no cover - 需要真连接
        await ws.accept()
        conn = NodeConnection(ws)
        SLog.i(TAG, "Scout 连入，等 REGISTER")
        try:
            while not conn._closed:
                raw = await ws.receive_text()
                await conn.on_text(raw)
        except WebSocketDisconnect:
            SLog.i(TAG, f"[{conn.node_id or '未注册'}] 断开")
        except Exception as exc:
            SLog.e(TAG, f"[{conn.node_id or '未注册'}] 连接异常: {type(exc).__name__}: {exc}")
        finally:
            await conn.aclose()
