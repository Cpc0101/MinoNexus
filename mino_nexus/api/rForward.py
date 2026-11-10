"""通用调用转发：agent → Nexus（寻址 + 鉴权 + 取证）→ Scout（执行并回执）。

Nexus 在这里**不做能力白名单**：`capability_id` 与 `params` 原样交给目标 Scout，
由 Scout 判断自己认不认识。转发核心在 `resources/node_forward.py` —— 这里只做
HTTP 适配：请求体校验、状态码映射、响应包装。

入站方向（Scout→Nexus）不走这里：IM 入站是 Scout 主动推送，见
`resources/node_registry.py` 的事件处理表。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from mino_nexus.api.deps import current_session
from mino_nexus.core.http_util import ok
from mino_nexus.resources import node_forward

router = APIRouter(prefix="/forward", tags=["Forward"])


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


class RouterBody(BaseModel):
    """反问 Nexus「这条 cap 现在能发给谁」。只做寻址，不执行。"""

    model_config = ConfigDict(extra="forbid")

    sn: str = Field(default="", max_length=256)
    node_id: str = Field(default="", max_length=128)


def _raise(exc: node_forward.ForwardError) -> None:
    detail: dict[str, Any] = {
        "code": exc.code, "message": exc.message, **exc.detail,
    }
    if exc.status in (409,):
        detail["retryable"] = True
    raise HTTPException(status_code=exc.status, detail=detail)


@router.post("/call")
async def forward_call(body: ForwardBody, sess: dict = Depends(current_session)):
    """把一次 capability 调用原样转给持有目标的 Scout，并回传它的结果。

    不做能力白名单：`capability_id` 由调用方给，Scout 不认识时它会回 FAIL，
    这里按 502 透出原始错误码，不改写成 Nexus 的错误。
    """
    uid = str(sess.get("user_id") or "")
    if not uid:
        raise HTTPException(status_code=401, detail="请先登录")
    try:
        # 直接在请求自己的主事件循环上 await：Scout 的 RESULT 由同一条循环唤醒。
        result = await node_forward.forward_async(
            capability_id=body.capability_id,
            params=body.params,
            sn=body.sn,
            node_id=body.node_id,
            platform=body.platform,
            timeout_sec=body.timeout_sec,
            executor_order=body.executor_order,
            selected_impl=body.selected_impl,
            user_id=uid,
            session_id=body.session_id,
        )
    except node_forward.ForwardError as exc:
        _raise(exc)
    return ok(result, msg=result.get("summary") or "已完成")


@router.post("/resolve")
async def forward_resolve(body: RouterBody, sess: dict = Depends(current_session)):
    uid = str(sess.get("user_id") or "")
    if not uid:
        raise HTTPException(status_code=401, detail="请先登录")
    try:
        return ok(await node_forward.resolve_async(sn=body.sn, node_id=body.node_id, user_id=uid))
    except node_forward.ForwardError as exc:
        _raise(exc)
