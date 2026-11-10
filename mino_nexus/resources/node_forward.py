"""通用能力转发：寻址 → 补选路 → 下发 → 等回执。

**为什么单独一层**：`api/rForward.py`（HTTP）和 `assistant/assistant_tools.py`
（MCP / IM 工具）都要转发，但层号只允许向下依赖。转发核心放这里（resources），
两边都调它，不再各自拼一遍 `Execute`。

**为什么不能用进程内 ASGI 互调**：`NodeConnection.request()` 用
`asyncio.get_running_loop()` 建 future，并把它放在 `self._waiters` 里等主循环上的
RESULT 来 `set_result`。如果调用方在自己新建的事件循环里跑嵌套 ASGI，那个 future
属于另一个 loop，主循环收到 RESULT 时**唤不醒**等待者 —— 结果就是每次都空等到
`timeout_sec` 才拿到（实测 5ms 的结果等 50s/90s/150s）。所以：

- 异步调用方（HTTP 路由，本身就在主循环上）用 `forward_async()`；
- 同步调用方（MCP 的 `to_thread` worker）用 `forward_sync()`，它把协程送到主循环，
  和 `node_plugin_call` 同一条路径。

Nexus 在这里**不做能力白名单**：`capability_id` 与 `params` 原样交给目标 Scout，
由它判断认不认识。Nexus 只负责定位、补选路、取证和透传。
"""
from __future__ import annotations

import inspect
import time
from typing import Any

from mino_nexus.core import protocol as P
from mino_nexus.core.async_util import run_sync as _run_sync
from mino_nexus.core.log import SLog
from mino_nexus.resources.node_registry import get_registry

TAG = "Forward"

# 离线兜底只用这套码，不再散落「节点离线 / 设备离线 / 节点未应答」多种措辞。
OFFLINE_DEVICE = "device_offline"
OFFLINE_NODE = "node_offline"
OFFLINE_UNKNOWN = "target_unresolved"
MESSAGE = {
    OFFLINE_DEVICE: "设备离线：这台设备当前没有连到任何活跃节点",
    OFFLINE_NODE: "节点离线：设备所属节点已断开或心跳超时",
    OFFLINE_UNKNOWN: "找不到目标：请给出 sn 或 node_id",
}

# 装包/卸载的成败由 adb / devicectl / 安装器给出终态，不依赖图形桌面。
# 桌面未就绪（锁屏、没授录屏权限）不该连带挡掉「装个包」。
PACKAGE_CAPS = frozenset({"install", "install_apk", "install_app", "uninstall_app"})


class ForwardError(Exception):
    """转发失败。`code` 直接给调用方，`status` 映射 HTTP 状态码。"""

    def __init__(self, code: str, message: str = "", *, status: int = 409,
                 detail: dict[str, Any] | None = None):
        super().__init__(message or MESSAGE.get(code, code))
        self.code = code
        self.message = message or MESSAGE.get(code, code)
        self.status = status
        self.detail = dict(detail or {})


def is_package_cap(capability_id: str) -> bool:
    return str(capability_id or "").strip() in PACKAGE_CAPS


def resolve_target(sn: str, node_id: str):
    """→ (node, sn)。设备优先按 sn 找；只给 node_id 时直接落到该节点。"""
    reg = get_registry()
    if sn:
        node, reason = reg.resolve(sn)
        if node is None:
            raise ForwardError(OFFLINE_DEVICE if "未登记" in reason else OFFLINE_NODE, reason)
        return node, sn
    if node_id:
        node = reg.get_node(node_id)
        if node is None or not node.alive or node.send is None:
            raise ForwardError(OFFLINE_NODE, f"节点 {node_id} 不在线")
        return node, ""
    raise ForwardError(OFFLINE_UNKNOWN)


def require_owner(node: Any, user_id: str) -> None:
    from mino_nexus.resources.access_scope import node_owner_id

    if node_owner_id(node.node_id) != user_id:
        raise ForwardError("target_unresolved", "没有这个节点", status=404)


def route_for(node: Any, sn: str, platform: str, capability_id: str = "") -> tuple[str, str]:
    """→ (platform, executor)。给不出 executor 时返回空串，由 Scout 自判。

    传 `capability_id` 是为了让**装包**在桌面未就绪时仍然有路可走：装包不需要
    图形会话、不需要截图、不需要独占租约，只要求 install 通道可用。
    """
    from mino_nexus.contract.executors import executors_for_platform

    plat = str(platform or "").strip().lower()
    dev = None
    if node is not None and sn and getattr(node, "devices", None):
        dev = node.devices.get(sn)
    if not plat:
        plat = str(getattr(dev, "platform", "") or "").strip().lower()
    if not plat and str(sn or "").startswith("web"):
        plat = "web"
    if not plat:
        return "", ""
    family = [ex for ex in executors_for_platform(plat) if ex != "remote"]
    if not family:
        return plat, ""
    available = set(node.available_executors()) if node is not None else set()
    chosen = next((ex for ex in family if ex in available), "")
    if not chosen and is_package_cap(capability_id) and plat in {"mac", "win"}:
        # 桌面没就绪时节点仍声明 install 通道；这里不因桌面状态丢掉装包。
        chosen = plat
    if not chosen:
        # 老节点只报历史 id `desktop`；Scout 入站会把它归一成 mac / win。
        chosen = "desktop" if "desktop" in available else family[0]
    return plat, chosen


def device_hint(node: Any, sn: str, platform: str) -> dict[str, Any]:
    """Scout 原生通道要 `session_key`；adb 要 serial。缺了它 Scout 会判身份冲突。"""
    dev = None
    if node is not None and sn and getattr(node, "devices", None):
        dev = node.devices.get(sn)
    hint: dict[str, Any] = {}
    plat = str(platform or "").strip().lower()
    if plat in {"mac", "win"}:
        hint["session_key"] = str(getattr(dev, "session_key", "") or "console")
    if plat == "ios":
        hint["udid"] = sn
    if plat == "android":
        serial = ""
        if dev is not None and isinstance(getattr(dev, "channels", None), dict):
            serial = str((dev.channels or {}).get("serial") or "")
        hint["adb_serial"] = serial or sn
    return hint


def _evidence_open(session_id: str, capability_id: str, sn: str, node_id: str) -> Any:
    """轻量取证：一次调用 = 一个 session。取证失败不影响本次转发。"""
    try:
        from mino_nexus.core.ids import prefixed
        from mino_nexus.records.eventlog.writer import open_session

        return open_session(
            session_id=session_id or prefixed("fwd"),
            run_id="", case_id="", app_id="", provider_id="", sn=sn,
            sop_id="forward-call",
            extra={"capability_id": capability_id, "node_id": node_id, "forward": True},
        )
    except Exception as exc:
        SLog.w(TAG, f"取证 session 打开失败 cap={capability_id}: {exc!r}")
        return None


def _record(writer: Any, kind: str, payload: dict[str, Any]) -> None:
    if writer is None:
        return
    try:
        writer.append(kind, payload)
    except Exception as exc:
        SLog.w(TAG, f"写信事件失败 kind={kind}: {exc!r}")


def build_execute(
    *,
    capability_id: str,
    params: dict[str, Any] | None,
    sn: str,
    node: Any,
    platform: str = "",
    timeout_sec: float = 60.0,
    executor_order: list[str] | None = None,
    selected_impl: dict[str, Any] | None = None,
) -> P.Execute:
    """按设备的真实平台补齐 executor_order / device_hint，再拼 `Execute`。"""
    plat, executor = route_for(node, sn, platform, capability_id)
    order = list(executor_order or [])
    impl = dict(selected_impl or {})
    if not order and executor:
        order = [executor]
    if order and not impl:
        impl = {"executor": order[0]}
    return P.Execute(
        run_id="",
        step_idx=-1,
        sn=sn,
        capability_id=capability_id,
        params=dict(params or {}),
        executor_order=order,
        selected_impl=impl,
        device_hint=device_hint(node, sn, plat),
        timeout_sec=float(timeout_sec),
        device_id=sn,
        platform=plat or str(platform or ""),
    )


async def forward_async(
    *,
    capability_id: str,
    params: dict[str, Any] | None = None,
    sn: str = "",
    node_id: str = "",
    platform: str = "",
    timeout_sec: float = 60.0,
    executor_order: list[str] | None = None,
    selected_impl: dict[str, Any] | None = None,
    user_id: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """转发一次能力调用。**必须在主事件循环上调用。**

    成功返回 `{capability_id, node_id, sn, elapsed_ms, data, extra, summary}`；
    失败抛 `ForwardError`（离线 / 未授权 / Scout 拒绝）。
    """
    cap = str(capability_id or "").strip()
    if not cap:
        raise ForwardError("target_unresolved", "capability_id 不能为空", status=400)
    node, sn = resolve_target(str(sn or "").strip(), str(node_id or "").strip())
    require_owner(node, user_id)
    payload = build_execute(
        capability_id=cap, params=params, sn=sn, node=node, platform=platform,
        timeout_sec=timeout_sec, executor_order=executor_order, selected_impl=selected_impl,
    )
    writer = _evidence_open(session_id, cap, sn, node.node_id)
    started = time.time()
    _record(writer, "tool/call", {
        "capability_id": cap, "sn": sn, "node_id": node.node_id,
        "params_keys": sorted((params or {}).keys()),
    })
    try:
        from mino_nexus.core.wire_identity import assign_execute_identity

        assign_execute_identity(payload)
    except Exception:
        pass
    result = node.send(P.MsgType.EXECUTE, payload, timeout=float(timeout_sec) + 30.0)
    if inspect.isawaitable(result):
        result = await result
    elapsed = int((time.time() - started) * 1000)
    if result is None:
        _record(writer, "tool/result", {"capability_id": cap, "status": "timeout", "elapsed_ms": elapsed})
        raise ForwardError(OFFLINE_NODE, f"节点 {node.node_id} 未应答 {cap}")
    status = getattr(getattr(result, "status", None), "value", None) or getattr(result, "status", "")
    status_text = str(status or "").lower()
    summary = str(getattr(result, "summary", "") or "")
    error = str(getattr(result, "error", "") or "")
    ok = status_text in ("pass", "passed", "ok", "success")
    _record(writer, "tool/result", {
        "capability_id": cap, "status": status_text,
        "summary": summary[:300], "error": error[:300], "elapsed_ms": elapsed,
    })
    if writer is not None:
        try:
            writer.close(status="pass" if ok else "fail",
                         summary=(summary or status_text)[:240], step_count=1)
        except Exception as exc:
            SLog.w(TAG, f"关闭取证 session 失败: {exc!r}")
    data = dict(getattr(result, "data", None) or {})
    extra = dict(getattr(result, "extra", None) or {})
    if not ok:
        # 原样透出 Scout 的判定，不把「Scout 拒绝」改写成「Nexus 出错」。
        raise ForwardError(error or "scout_rejected", summary or error or "Scout 拒绝了这次调用",
                           status=502, detail={
                               "capability_id": cap, "node_id": node.node_id,
                               "executor_used": str(getattr(result, "executor_used", "") or ""),
                               "data": data, "extra": extra,
                           })
    return {"capability_id": cap, "node_id": node.node_id, "sn": sn,
            "elapsed_ms": elapsed, "summary": summary, "data": data, "extra": extra}


def forward_sync(*, user_id: str = "", **kwargs: Any) -> dict[str, Any]:
    """同步壳。调用方线程没有事件循环时走主循环（MCP 的 to_thread worker 就是这种）。"""
    from mino_nexus.platform.identity import execution_user

    try:
        import asyncio

        asyncio.get_running_loop()
    except RuntimeError:
        with execution_user(user_id):
            return _run_sync(forward_async(user_id=user_id, **kwargs))
    raise ForwardError("target_unresolved", "当前线程已有事件循环：请直接用 forward_async", status=500)


def resolve_async(*, sn: str = "", node_id: str = "", user_id: str = "") -> dict[str, Any]:
    """只查目标落在哪个节点、是否在线，不执行。"""
    node, sn = resolve_target(str(sn or "").strip(), str(node_id or "").strip())
    require_owner(node, user_id)
    return {"sn": sn, "node_id": node.node_id, "online": bool(node.alive),
            "platform": str(getattr(node, "host", {}) or {}).get("os")
                        or str(getattr(node.devices.get(sn) if sn else None, "platform", "") or "")}


def resolve_sync(*, user_id: str = "", sn: str = "", node_id: str = "") -> dict[str, Any]:
    try:
        import asyncio

        asyncio.get_running_loop()
    except RuntimeError:
        from mino_nexus.platform.identity import execution_user

        with execution_user(user_id):
            return _run_sync(resolve_async(user_id=user_id, sn=sn, node_id=node_id))
    raise ForwardError("target_unresolved", "当前线程已有事件循环：请直接用 resolve_async", status=500)
