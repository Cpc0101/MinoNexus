"""通用调用转发：agent → Nexus（寻址 + 鉴权 + 取证）→ Scout（执行并回执）。

Nexus 在这里**不做能力白名单**：`capability_id` 与 `params` 原样交给目标 Scout，
由 Scout 判断自己认不认识。Nexus 只负责四件事：

1. 定位：`sn` 或 `node_id` → 那台持有设备的活节点；
2. 兜底：设备/节点不在线时返回统一的离线错误，不让调用方猜；
3. 取证：落一条轻量 session，记 `tool/call` 与 `tool/result`（CLAUDE.md §2.3）；
4. 透传：把 Scout 的 `Result` 按原样回给调用方，不改业务结论。

入站方向（Scout→Nexus）不走这里：IM 入站是 Scout 主动推送，见
`resources/node_registry.py` 的事件处理表。
"""
from __future__ import annotations

import inspect
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from mino_nexus.api.deps import current_session
from mino_nexus.core import protocol as P
from mino_nexus.core.http_util import ok
from mino_nexus.core.log import SLog
from mino_nexus.resources.node_registry import get_registry

router = APIRouter(prefix="/forward", tags=["Forward"])

TAG = "Forward"

# 离线兜底只用这套码，不再散落「节点离线 / 设备离线 / 节点未应答」多种措辞。
OFFLINE_DEVICE = "device_offline"
OFFLINE_NODE = "node_offline"
OFFLINE_UNKNOWN = "target_unresolved"
MESSAGE = {
    OFFLINE_DEVICE: "设备离线：这台设备当前没有连到任何在线节点",
    OFFLINE_NODE: "节点离线：设备所属节点已断开或心跳超时",
    OFFLINE_UNKNOWN: "找不到目标：请给出 sn 或 node_id",
}


class ForwardBody(BaseModel):
    """一次透传。`capability_id` 与 `params` 由调用方决定，Nexus 不解释。"""

    model_config = ConfigDict(extra="forbid")

    capability_id: str = Field(min_length=1, max_length=128)
    params: dict[str, Any] = Field(default_factory=dict)
    sn: str = Field(default="", max_length=256)
    node_id: str = Field(default="", max_length=128)
    platform: str = Field(default="", max_length=32)
    timeout_sec: float = Field(default=60.0, ge=1.0, le=1800.0)
    # 设备动作需要恰好一个 executor；给了就原样带给 Scout，不给由 Nexus 按设备补。
    executor_order: list[str] = Field(default_factory=list)
    selected_impl: dict[str, Any] = Field(default_factory=dict)
    session_id: str = Field(default="", max_length=128)


def _offline(code: str, detail: str = "") -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"code": code, "message": detail or MESSAGE[code], "retryable": True},
    )


def _resolve_target(body: ForwardBody):
    """→ (node, sn)。设备优先按 sn 找；只给 node_id 时直接落到该节点。"""
    reg = get_registry()
    if body.sn:
        node, reason = reg.resolve(body.sn)
        if node is None:
            code = OFFLINE_DEVICE if "未登记" in reason else OFFLINE_NODE
            raise _offline(code, reason)
        return node, body.sn
    if body.node_id:
        node = reg.get_node(body.node_id)
        if node is None or not node.alive or node.send is None:
            raise _offline(OFFLINE_NODE, f"节点 {body.node_id} 不在线")
        return node, ""
    raise _offline(OFFLINE_UNKNOWN)


def _evidence(body: ForwardBody, sn: str, node_id: str) -> Any:
    """轻量取证：一次调用 = 一个 session。取证失败不影响本次转发。"""
    try:
        from mino_nexus.core.ids import prefixed
        from mino_nexus.records.eventlog.writer import open_session

        return open_session(
            session_id=body.session_id or prefixed("fwd"),
            run_id="",
            case_id="",
            app_id="",
            provider_id="",
            sn=sn,
            sop_id="forward-call",
            extra={"capability_id": body.capability_id, "node_id": node_id, "forward": True},
        )
    except Exception as exc:  # 取证是旁路，不能因为它拒绝执行
        SLog.w(TAG, f"取证 session 打开失败 cap={body.capability_id}: {exc!r}")
        return None


def _record(writer: Any, kind: str, payload: dict[str, Any]) -> None:
    if writer is None:
        return
    try:
        writer.append(kind, payload)
    except Exception as exc:
        SLog.w(TAG, f"写信事件失败 kind={kind}: {exc!r}")


def _fill_route(body: ForwardBody, node: Any, sn: str) -> None:
    """Scout 的 router 要求 `executor_order` 恰好一个元素。

    `/forward/call` 是通用透传，调用方常常只给 `capability_id` + `sn`。这里按设备
    平台补齐唯一通道，避免把「没填 executor」记成 Scout 的选路失败。

    平台优先取调用方显式给的，其次取设备清单里登记的，最后按 sn 前缀判断。
    """
    from mino_nexus.contract.executors import executors_for_platform

    if body.executor_order:
        return
    platform = str(body.platform or "").strip().lower()
    dev = None
    if node is not None and sn:
        dev = getattr(node, "devices", {}).get(sn) if getattr(node, "devices", None) else None
    if not platform:
        platform = str(getattr(dev, "platform", "") or "").strip().lower()
    if not platform and sn.startswith("web"):
        platform = "web"
    if not platform:
        return
    family = [ex for ex in executors_for_platform(platform) if ex != "remote"]
    if not family:
        return
    available = set(node.available_executors()) if node is not None else set()
    chosen = next((ex for ex in family if ex in available), "")
    if not chosen:
        # 老节点只报历史 id `desktop`；Scout 入站会把它归一成 mac / win。
        chosen = next((ex for ex in ("desktop",) if ex in available), family[0])
    body.platform = body.platform or platform
    body.executor_order = [chosen]
    if not body.selected_impl:
        body.selected_impl = {"executor": chosen}


@router.post("/call")
async def forward_call(body: ForwardBody, sess: dict = Depends(current_session)):
    """把一次 capability 调用原样转给持有目标的 Scout，并回传它的结果。

    不做能力白名单：`capability_id` 由调用方给，Scout 不认识时它会回 FAIL，
    这里按 502 透出原始错误码，不改写成 Nexus 的错误。
    """
    uid = str(sess.get("user_id") or "")
    if not uid:
        raise HTTPException(status_code=401, detail="请先登录")

    node, sn = _resolve_target(body)
    from mino_nexus.resources.access_scope import node_owner_id

    if node_owner_id(node.node_id) != uid:
        raise HTTPException(status_code=404, detail="没有这个节点")
    _fill_route(body, node, sn)

    writer = _evidence(body, sn, node.node_id)
    started = time.time()
    _record(writer, "tool/call", {
        "capability_id": body.capability_id, "sn": sn, "node_id": node.node_id,
        "params_keys": sorted(body.params.keys()),
    })

    payload = P.Execute(
        run_id="",
        step_idx=-1,
        sn=sn,
        capability_id=body.capability_id,
        params=dict(body.params or {}),
        executor_order=list(body.executor_order or []),
        selected_impl=dict(body.selected_impl or {}),
        timeout_sec=float(body.timeout_sec),
        device_id=sn,
        platform=str(body.platform or ""),
    )
    try:
        from mino_nexus.core.wire_identity import assign_execute_identity

        assign_execute_identity(payload)
    except Exception:
        pass

    result = node.send(P.MsgType.EXECUTE, payload, timeout=float(body.timeout_sec) + 30.0)
    if inspect.isawaitable(result):
        result = await result
    elapsed = int((time.time() - started) * 1000)
    if result is None:
        _record(writer, "tool/result", {"capability_id": body.capability_id, "status": "timeout", "elapsed_ms": elapsed})
        raise _offline(OFFLINE_NODE, f"节点 {node.node_id} 未应答 {body.capability_id}")

    status = getattr(getattr(result, "status", None), "value", None) or getattr(result, "status", "")
    status_text = str(status or "").lower()
    summary = str(getattr(result, "summary", "") or "")
    error = str(getattr(result, "error", "") or "")
    _record(writer, "tool/result", {
        "capability_id": body.capability_id, "status": status_text,
        "summary": summary[:300], "error": error[:300], "elapsed_ms": elapsed,
    })
    if writer is not None:
        try:
            writer.close(status="pass" if status_text in ("pass", "passed", "ok", "success") else "fail",
                         summary=(summary or status_text)[:240], step_count=1)
        except Exception as exc:
            SLog.w(TAG, f"关闭取证 session 失败: {exc!r}")

    data = dict(getattr(result, "data", None) or {})
    extra = dict(getattr(result, "extra", None) or {})
    if status_text not in ("pass", "passed", "ok", "success"):
        # 原样透出 Scout 的判定，不把「Scout 拒绝」改写成「Nexus 出错」。
        raise HTTPException(status_code=502, detail={
            "code": error or "scout_rejected",
            "message": summary or error or "Scout 拒绝了这次调用",
            "capability_id": body.capability_id,
            "node_id": node.node_id,
            "executor_used": str(getattr(result, "executor_used", "") or ""),
            "data": data,
            "extra": extra,
        })
    return ok({"capability_id": body.capability_id, "node_id": node.node_id, "sn": sn,
               "elapsed_ms": elapsed, "data": data, "extra": extra}, msg=summary or "已完成")


class RouterBody(BaseModel):
    """反问 Nexus「这条 cap 现在能发给谁」。只做寻址，不执行。"""

    model_config = ConfigDict(extra="forbid")

    sn: str = Field(default="", max_length=256)
    node_id: str = Field(default="", max_length=128)


@router.post("/resolve")
def forward_resolve(body: RouterBody, sess: dict = Depends(current_session)):
    uid = str(sess.get("user_id") or "")
    from mino_nexus.resources.access_scope import node_owner_id

    if not uid:
        raise HTTPException(status_code=401, detail="请先登录")
    node, sn = _resolve_target(body)
    if node_owner_id(node.node_id) != uid:
        raise HTTPException(status_code=404, detail="没有这个节点")
    return ok({"sn": sn, "node_id": node.node_id, "online": bool(node.alive),
               "platform": str(getattr(node, "host", {}).get("os") or "")})
