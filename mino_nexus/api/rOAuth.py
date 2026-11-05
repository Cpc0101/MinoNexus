"""MCP OAuth 2.1。发现文档和令牌端点给客户端；同意由已登录的 Workbench 用户完成。"""
from __future__ import annotations

import html
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from mino_nexus.api.deps import current_session
from mino_nexus.core.http_util import ok
from mino_nexus.platform import mcp_oauth

router = APIRouter(tags=["OAuth"])


def _origin(request: Request) -> str:
    return mcp_oauth.public_origin(str(request.base_url))


def _remote(request: Request) -> str:
    return (request.client.host if request.client else "") or "unknown"


def _page(title: str, message: str, status: int) -> HTMLResponse:
    safe_title = html.escape(title)
    safe_message = html.escape(message)
    body = f"""<!doctype html>
<meta charset="utf-8">
<title>{safe_title}</title>
<body style="font:16px/1.5 sans-serif;margin:48px;max-width:32rem">
<h1 style="font-size:20px">{safe_title}</h1>
<p>{safe_message}</p>
"""
    return HTMLResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


def _oauth_error(exc: mcp_oauth.OAuthPage) -> JSONResponse:
    return JSONResponse(
        {"error": exc.error, "error_description": exc.description},
        status_code=exc.status,
        headers={"Cache-Control": "no-store"},
    )


def _form(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    return {str(key): "" if value is None else str(value) for key, value in payload.items()}


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource(request: Request) -> dict[str, Any]:
    return mcp_oauth.resource_metadata(_origin(request))


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/openid-configuration")
def authorization_server(request: Request) -> dict[str, Any]:
    return mcp_oauth.metadata(_origin(request))


@router.post("/oauth/register", status_code=201)
async def register(request: Request):
    try:
        body = await request.json()
    except Exception:
        return _oauth_error(mcp_oauth.OAuthPage("invalid_client_metadata", "登记内容不是 JSON"))
    try:
        return mcp_oauth.register_client(body if isinstance(body, dict) else {}, _remote(request))
    except mcp_oauth.OAuthPage as exc:
        return _oauth_error(exc)


@router.get("/oauth/authorize")
def authorize(
    request: Request,
    response_type: str = "",
    client_id: str = "",
    redirect_uri: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "",
    state: str = "",
    scope: str = "",
    resource: str = "",
):
    params = {
        "response_type": response_type,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "state": state,
        "scope": scope,
        "resource": resource,
    }
    try:
        txn = mcp_oauth.begin_authorization(params, mcp_oauth.canonical_resource(_origin(request)), _remote(request))
    except mcp_oauth.OAuthRedirect as exc:
        return RedirectResponse(exc.location, status_code=302, headers={"Cache-Control": "no-store"})
    except mcp_oauth.OAuthPage as exc:
        return _page("无法开始授权", exc.description, exc.status)
    target = mcp_oauth.consent_target(_origin(request)) + "/oauth/consent?txn=" + txn
    return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})


@router.get("/oauth/requests/{txn}")
def show_request(txn: str, _sess: dict = Depends(current_session)):
    view = mcp_oauth.request_view(txn)
    if not view:
        return JSONResponse({"detail": "这次授权已经失效，回到客户端重新连接"}, status_code=404)
    return ok(view)


@router.post("/oauth/requests/{txn}/approve")
def approve(txn: str, sess: dict = Depends(current_session)):
    try:
        location = mcp_oauth.decide(txn, str(sess.get("user_id") or ""), True)
    except mcp_oauth.OAuthPage as exc:
        return JSONResponse({"detail": exc.description}, status_code=exc.status)
    return ok({"redirect_to": location})


@router.post("/oauth/requests/{txn}/deny")
def deny(txn: str, sess: dict = Depends(current_session)):
    try:
        location = mcp_oauth.decide(txn, str(sess.get("user_id") or ""), False)
    except mcp_oauth.OAuthPage as exc:
        return JSONResponse({"detail": exc.description}, status_code=exc.status)
    return ok({"redirect_to": location})


@router.post("/oauth/token")
async def token(request: Request):
    content_type = request.headers.get("content-type", "")
    try:
        if "application/json" in content_type:
            payload = _form(await request.json())
        else:
            form = await request.form()
            payload = {str(key): str(value) for key, value in form.items()}
    except Exception:
        return _oauth_error(mcp_oauth.OAuthPage("invalid_request", "令牌请求无法解析"))
    try:
        issued = mcp_oauth.exchange(payload, mcp_oauth.canonical_resource(_origin(request)), _remote(request))
    except mcp_oauth.OAuthPage as exc:
        response = _oauth_error(exc)
        if exc.status == 401:
            response.headers["WWW-Authenticate"] = 'Bearer error="invalid_client"'
        return response
    return JSONResponse(issued, headers={"Cache-Control": "no-store"})
