"""MCP 远程 HTTP 的 OAuth 2.1 授权服务器。

Nexus 同时是资源服务器和授权服务器。访问令牌的主人就是 Workbench 用户，
工具、模型密钥和审计沿用这个 user_id。刷新令牌每次使用都轮换；
被替换过的刷新令牌再次出现时，整组授权作废。
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import secrets
import socket
import time
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from mino_nexus.core.database import session_scope

SCOPE = "mcp"
ACCESS_PREFIX = "mino_oat_"
REFRESH_PREFIX = "mino_ort_"
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
CODE_TTL = 60
REQUEST_TTL = 600
CLIENT_PREFIX = "mino_client_"
_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}")
_CHALLENGE = re.compile(r"[A-Za-z0-9\-_]{43,128}")
_HITS: dict[str, list[float]] = {}
_CIMD: dict[str, tuple[float, dict[str, Any]]] = {}
_CIMD_TTL = 600.0


class OAuthPage(Exception):
    """错误只能显示在 Nexus 自己的页面上，不能跳到未核验的回调。"""

    def __init__(self, error: str, description: str, status: int = 400):
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status


class OAuthRedirect(Exception):
    def __init__(self, location: str):
        super().__init__(location)
        self.location = location


def _now() -> int:
    return int(time.time())


def _hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def public_origin(base_url: str) -> str:
    return str(base_url or "").rstrip("/")


def canonical_resource(base_url: str) -> str:
    return public_origin(base_url) + "/mcp"


def _norm_resource(value: str) -> str:
    parts = urlsplit(str(value or "").strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return ""
    if parts.username or parts.password or parts.query or parts.fragment:
        return ""
    host = parts.hostname.lower()
    port = parts.port
    if (parts.scheme == "https" and port == 443) or (parts.scheme == "http" and port == 80):
        port = None
    path = (parts.path or "").rstrip("/")
    netloc = host if port is None else f"{host}:{port}"
    return f"{parts.scheme.lower()}://{netloc}{path}"


def same_resource(got: str, expected: str) -> bool:
    left = _norm_resource(got)
    right = _norm_resource(expected)
    return bool(left) and left == right


def _limit(bucket: str, limit: int, window: float) -> None:
    now = time.monotonic()
    rows = [item for item in _HITS.get(bucket, []) if now - item < window]
    if len(rows) >= limit:
        raise OAuthPage("too_many_requests", "请求过多，稍后再试", 429)
    rows.append(now)
    _HITS[bucket] = rows[-limit:]


def _clean_name(value: str) -> str:
    name = " ".join(str(value or "").split())
    if not name or len(name) > 80:
        raise OAuthPage("invalid_client_metadata", "客户端名称无效")
    return name


def _loopback(hostname: str) -> bool:
    host = str(hostname or "").lower().strip("[]")
    return host in {"localhost", "127.0.0.1", "::1"}


def _public_ip(value: str) -> bool:
    try:
        return bool(ipaddress.ip_address(value).is_global)
    except ValueError:
        return False


def validate_redirect(uri: str) -> str:
    raw = str(uri or "").strip()
    parts = urlsplit(raw)
    if not raw or len(raw) > 500 or parts.fragment or parts.username or parts.password:
        raise OAuthPage("invalid_request", "回调地址无效")
    host = (parts.hostname or "").lower()
    if parts.scheme == "https" and host:
        return raw
    if parts.scheme == "http" and _loopback(host):
        return raw
    raise OAuthPage("invalid_request", "回调地址必须是 https，或这台电脑上的 http")


def _global_host(hostname: str) -> None:
    host = str(hostname or "").lower().strip("[]")
    if not host or host in {"localhost"} or host.endswith((".localhost", ".local", ".internal")):
        raise OAuthPage("invalid_client", "客户端登记地址不可用")
    try:
        if not ipaddress.ip_address(host).is_global:
            raise OAuthPage("invalid_client", "客户端登记地址不可用")
        return
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OAuthPage("invalid_client", "读不到客户端登记") from exc
    addresses = [item[4][0] for item in infos]
    if not addresses or any(not _public_ip(item) for item in addresses):
        raise OAuthPage("invalid_client", "客户端登记地址不可用")


def _fetch_cimd(url: str) -> dict[str, Any]:
    cached = _CIMD.get(url)
    if cached and cached[0] > time.monotonic():
        return dict(cached[1])
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or not parts.path or parts.path == "/" or parts.fragment or parts.username:
        raise OAuthPage("invalid_client", "客户端编号必须是带路径的 https 地址")
    _global_host(parts.hostname)
    try:
        with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:
            response = client.get(url, headers={"Accept": "application/json", "User-Agent": "MinoNexus"})
    except httpx.HTTPError as exc:
        raise OAuthPage("invalid_client", "读不到客户端登记") from exc
    if response.status_code != 200 or len(response.content) > 65536:
        raise OAuthPage("invalid_client", "读不到客户端登记")
    try:
        document = response.json()
    except json.JSONDecodeError as exc:
        raise OAuthPage("invalid_client", "客户端登记不是 JSON") from exc
    if not isinstance(document, dict) or document.get("client_id") != url:
        raise OAuthPage("invalid_client", "客户端登记与地址不一致")
    method = str(document.get("token_endpoint_auth_method") or "none")
    if method != "none":
        raise OAuthPage("invalid_client", "目前只接受不带客户端密钥的登记")
    _CIMD[url] = (time.monotonic() + _CIMD_TTL, document)
    return dict(document)


def _client_record(client_id: str) -> dict[str, Any]:
    raw = str(client_id or "").strip()
    if not raw or len(raw) > 500:
        raise OAuthPage("invalid_client", "缺少客户端")
    if raw.startswith("https://"):
        document = _fetch_cimd(raw)
        redirects = document.get("redirect_uris")
        if not isinstance(redirects, list):
            raise OAuthPage("invalid_client", "客户端登记缺少回调地址")
        name = " ".join(str(document.get("client_name") or "").split())
        if not name or len(name) > 80:
            raise OAuthPage("invalid_client", "客户端登记缺少名称")
        return {
            "client_id": raw,
            "client_name": name,
            "redirect_uris": [str(item) for item in redirects if isinstance(item, str) and item],
            "auth_method": "none",
            "secret_hash": "",
        }
    from mino_nexus.models.mcp_oauth import OauthClient

    with session_scope() as db:
        row = db.get(OauthClient, raw)
        if row is None or row.source != "dcr":
            raise OAuthPage("invalid_client", "客户端不存在")
        try:
            redirects = json.loads(row.redirect_uris or "[]")
        except json.JSONDecodeError as exc:
            raise OAuthPage("invalid_client", "客户端登记已损坏") from exc
        return {
            "client_id": row.client_id,
            "client_name": row.client_name,
            "redirect_uris": list(redirects),
            "auth_method": row.auth_method,
            "secret_hash": row.secret_hash,
        }


def _match_redirect(client: dict[str, Any], redirect_uri: str) -> str:
    uri = validate_redirect(redirect_uri)
    allowed = {str(item) for item in client["redirect_uris"]}
    if uri not in allowed:
        raise OAuthPage("invalid_request", "回调地址没有登记")
    return uri


def error_redirect(redirect_uri: str, error: str, description: str, state: str = "") -> str:
    query = {"error": error, "error_description": description}
    if state:
        query["state"] = state
    separator = "&" if "?" in redirect_uri else "?"
    return redirect_uri + separator + urlencode(query)


def metadata(base_url: str) -> dict[str, Any]:
    origin = public_origin(base_url)
    return {
        "issuer": origin,
        "authorization_endpoint": origin + "/oauth/authorize",
        "token_endpoint": origin + "/oauth/token",
        "registration_endpoint": origin + "/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "scopes_supported": [SCOPE],
        "client_id_metadata_document_supported": True,
    }


def resource_metadata(base_url: str) -> dict[str, Any]:
    origin = public_origin(base_url)
    return {
        "resource": canonical_resource(origin),
        "authorization_servers": [origin],
        "scopes_supported": [SCOPE],
        "bearer_methods_supported": ["header"],
        "resource_name": "Mino",
    }


def _purge() -> None:
    from mino_nexus.models.mcp_oauth import OauthCode, OauthRequest, OauthToken

    now = _now()
    with session_scope() as db:
        db.query(OauthRequest).filter(OauthRequest.expires_at <= now).delete(synchronize_session=False)
        db.query(OauthCode).filter(OauthCode.expires_at <= now).delete(synchronize_session=False)
        db.query(OauthToken).filter(OauthToken.expires_at <= now - 86400).delete(synchronize_session=False)


def register_client(body: dict[str, Any], remote: str) -> dict[str, Any]:
    _limit(f"register:{remote}", 20, 3600)
    _purge()
    if not isinstance(body, dict):
        raise OAuthPage("invalid_client_metadata", "登记内容无效")
    name = _clean_name(str(body.get("client_name") or "MCP 客户端"))
    redirects = body.get("redirect_uris")
    if not isinstance(redirects, list) or not redirects or len(redirects) > 8:
        raise OAuthPage("invalid_client_metadata", "需要 1 到 8 个回调地址")
    cleaned = [validate_redirect(str(item)) for item in redirects]
    method = str(body.get("token_endpoint_auth_method") or "none")
    if method not in {"none", "client_secret_post"}:
        raise OAuthPage("invalid_client_metadata", "不支持这种客户端认证方式")
    grants = body.get("grant_types") or ["authorization_code", "refresh_token"]
    responses = body.get("response_types") or ["code"]
    if not isinstance(grants, list) or "authorization_code" not in grants:
        raise OAuthPage("invalid_client_metadata", "需要 authorization_code")
    if not isinstance(responses, list) or "code" not in responses:
        raise OAuthPage("invalid_client_metadata", "需要 response_type=code")
    from mino_nexus.models.mcp_oauth import OauthClient

    client_id = CLIENT_PREFIX + secrets.token_urlsafe(18)
    secret = secrets.token_urlsafe(32) if method == "client_secret_post" else ""
    with session_scope() as db:
        db.add(OauthClient(
            client_id=client_id,
            client_name=name,
            redirect_uris=json.dumps(cleaned, ensure_ascii=False),
            auth_method=method,
            secret_hash=_hash(secret) if secret else "",
            source="dcr",
            created_at=_now(),
        ))
    out = {
        "client_id": client_id,
        "client_name": name,
        "redirect_uris": cleaned,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": method,
        "client_id_issued_at": _now(),
    }
    if secret:
        out["client_secret"] = secret
        out["client_secret_expires_at"] = 0
    return out


def begin_authorization(params: dict[str, str], resource_expected: str, remote: str) -> str:
    _limit(f"authorize:{remote}", 30, 60)
    _purge()
    client_id = str(params.get("client_id") or "")
    state = str(params.get("state") or "")
    if len(state) > 512 or any(ord(ch) < 32 for ch in state):
        raise OAuthPage("invalid_request", "state 无效")
    client = _client_record(client_id)
    redirect_uri = _match_redirect(client, str(params.get("redirect_uri") or ""))

    def fail(error: str, description: str) -> None:
        raise OAuthRedirect(error_redirect(redirect_uri, error, description, state))

    if str(params.get("response_type") or "") != "code":
        fail("unsupported_response_type", "只支持授权码")
    if not state:
        fail("invalid_request", "缺少 state")
    if str(params.get("code_challenge_method") or "") != "S256" or not _CHALLENGE.fullmatch(str(params.get("code_challenge") or "")):
        fail("invalid_request", "需要 S256 PKCE")
    scope = str(params.get("scope") or SCOPE).strip()
    if scope != SCOPE:
        fail("invalid_scope", "只提供 mcp")
    resource = str(params.get("resource") or "")
    if not same_resource(resource, resource_expected):
        fail("invalid_target", "resource 必须是这个 MCP 地址")
    from mino_nexus.models.mcp_oauth import OauthRequest

    txn = secrets.token_urlsafe(32)
    now = _now()
    with session_scope() as db:
        db.add(OauthRequest(
            txn=txn,
            client_id=client["client_id"],
            client_name=client["client_name"],
            redirect_uri=redirect_uri,
            code_challenge=str(params.get("code_challenge") or ""),
            resource=_norm_resource(resource),
            scope=SCOPE,
            state=state,
            expires_at=now + REQUEST_TTL,
            created_at=now,
        ))
    return txn


def consent_target(base_url: str) -> str:
    """同意页放在用户已经登录的 Workbench 上。开发时这是另一个端口。"""
    from mino_nexus.core.browser_origins import browser_origins

    base = public_origin(base_url)
    base_parts = urlsplit(base)
    origins = browser_origins()
    for origin in origins:
        parts = urlsplit(origin)
        if parts.hostname == base_parts.hostname and parts.port != base_parts.port:
            return origin
    for origin in origins:
        if urlsplit(origin).port != base_parts.port:
            return origin
    return base


def request_view(txn: str) -> dict[str, Any]:
    from mino_nexus.models.mcp_oauth import OauthRequest

    with session_scope() as db:
        row = db.get(OauthRequest, str(txn or ""))
        if row is None or int(row.expires_at or 0) <= _now():
            return {}
        host = (urlsplit(row.redirect_uri).hostname or "").lower()
        return {
            "client_name": row.client_name,
            "redirect_uri": row.redirect_uri,
            "localhost": _loopback(host),
            "scope": row.scope,
        }


def _pkce(verifier: str, challenge: str) -> bool:
    if not _VERIFIER.fullmatch(verifier or "") or not _CHALLENGE.fullmatch(challenge or ""):
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(computed, challenge)


def _add_tokens(db, user_id: str, client_id: str, client_name: str, resource: str, family_id: str = "", family_created: int = 0) -> dict[str, Any]:
    from mino_nexus.models.mcp_oauth import OauthToken

    now = _now()
    family_id = family_id or secrets.token_urlsafe(18)
    family_created = family_created or now
    access = ACCESS_PREFIX + secrets.token_urlsafe(32)
    refresh = REFRESH_PREFIX + secrets.token_urlsafe(32)
    db.add(OauthToken(
        family_id=family_id, user_id=user_id, client_id=client_id, client_name=client_name,
        kind="access", token_hash=_hash(access), prefix=access[:16], resource=resource, scope=SCOPE,
        expires_at=now + ACCESS_TTL, family_created=family_created, created_at=now,
    ))
    db.add(OauthToken(
        family_id=family_id, user_id=user_id, client_id=client_id, client_name=client_name,
        kind="refresh", token_hash=_hash(refresh), prefix=refresh[:16], resource=resource, scope=SCOPE,
        expires_at=now + REFRESH_TTL, family_created=family_created, created_at=now,
    ))
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL,
        "refresh_token": refresh,
        "scope": SCOPE,
    }


def decide(txn: str, user_id: str, allow: bool) -> str:
    from mino_nexus.models.mcp_oauth import OauthCode, OauthRequest

    uid = str(user_id or "").strip()
    if not uid:
        raise OAuthPage("access_denied", "请先登录", 401)
    with session_scope() as db:
        row = db.get(OauthRequest, str(txn or ""))
        if row is None or int(row.expires_at or 0) <= _now():
            raise OAuthPage("invalid_request", "这次授权已经失效，回到客户端重新连接", 404)
        redirect_uri = row.redirect_uri
        state = row.state
        if not allow:
            db.delete(row)
            return error_redirect(redirect_uri, "access_denied", "已拒绝", state)
        code = secrets.token_urlsafe(32)
        db.add(OauthCode(
            code_hash=_hash(code), user_id=uid, client_id=row.client_id, client_name=row.client_name,
            redirect_uri=redirect_uri, code_challenge=row.code_challenge, resource=row.resource,
            scope=row.scope, state=state, expires_at=_now() + CODE_TTL, used=False,
        ))
        db.delete(row)
    return redirect_uri + ("&" if "?" in redirect_uri else "?") + urlencode({"code": code, "state": state})


def _check_secret(client: dict[str, Any], presented: str) -> None:
    method = client["auth_method"]
    secret = str(presented or "")
    if method == "none":
        if secret:
            raise OAuthPage("invalid_client", "这个客户端不使用密钥", 401)
        return
    if not secret or not secrets.compare_digest(_hash(secret), str(client["secret_hash"] or "")):
        raise OAuthPage("invalid_client", "客户端密钥不正确", 401)


def _revoke_family(db, family_id: str) -> None:
    from mino_nexus.models.mcp_oauth import OauthToken

    db.query(OauthToken).filter(OauthToken.family_id == family_id).update(
        {"revoked": True}, synchronize_session=False)


def exchange(form: dict[str, str], resource_expected: str, remote: str) -> dict[str, Any]:
    _limit(f"token:{remote}", 60, 60)
    grant = str(form.get("grant_type") or "")
    client = _client_record(str(form.get("client_id") or ""))
    _check_secret(client, str(form.get("client_secret") or ""))
    resource = str(form.get("resource") or "")
    if not same_resource(resource, resource_expected):
        raise OAuthPage("invalid_target", "resource 必须是这个 MCP 地址")
    if grant == "authorization_code":
        return _exchange_code(form, client, _norm_resource(resource))
    if grant == "refresh_token":
        return _exchange_refresh(form, client, _norm_resource(resource))
    raise OAuthPage("unsupported_grant_type", "不支持这种授权方式")


def _exchange_code(form: dict[str, str], client: dict[str, Any], resource: str) -> dict[str, Any]:
    from mino_nexus.models.mcp_oauth import OauthCode

    code = str(form.get("code") or "")
    redirect_uri = str(form.get("redirect_uri") or "")
    verifier = str(form.get("code_verifier") or "")
    if not code or not redirect_uri or not verifier:
        raise OAuthPage("invalid_request", "授权码、回调和 code_verifier 都要带上")
    failure = ""
    tokens: dict[str, Any] = {}
    with session_scope() as db:
        updated = db.query(OauthCode).filter(
            OauthCode.code_hash == _hash(code),
            OauthCode.used.is_(False),
            OauthCode.expires_at > _now(),
        ).update({"used": True}, synchronize_session=False)
        row = db.query(OauthCode).filter(OauthCode.code_hash == _hash(code)).one_or_none()
        if updated != 1 or row is None:
            failure = "授权码无效或已使用"
        elif row.client_id != client["client_id"] or row.redirect_uri != redirect_uri or row.resource != resource:
            failure = "授权码与这次请求不一致"
        elif not _pkce(verifier, row.code_challenge):
            failure = "PKCE 校验失败"
        else:
            tokens = _add_tokens(db, row.user_id, row.client_id, row.client_name, resource)
    if failure:
        raise OAuthPage("invalid_grant", failure)
    return tokens


def _exchange_refresh(form: dict[str, str], client: dict[str, Any], resource: str) -> dict[str, Any]:
    from mino_nexus.models.mcp_oauth import OauthToken

    presented = str(form.get("refresh_token") or "")
    scope = str(form.get("scope") or SCOPE).strip()
    if scope != SCOPE:
        raise OAuthPage("invalid_scope", "不能扩大权限")
    if not presented.startswith(REFRESH_PREFIX):
        raise OAuthPage("invalid_grant", "刷新令牌无效")
    digest = _hash(presented)
    failure = ""
    tokens: dict[str, Any] = {}
    with session_scope() as db:
        updated = db.query(OauthToken).filter(
            OauthToken.token_hash == digest,
            OauthToken.kind == "refresh",
            OauthToken.replaced.is_(False),
            OauthToken.revoked.is_(False),
            OauthToken.expires_at > _now(),
        ).update({"replaced": True}, synchronize_session=False)
        row = db.query(OauthToken).filter(OauthToken.token_hash == digest).one_or_none()
        if updated != 1 or row is None:
            if row is not None and row.replaced and not row.revoked:
                _revoke_family(db, row.family_id)
            failure = "刷新令牌无效或已轮换"
        elif row.client_id != client["client_id"] or row.resource != resource:
            _revoke_family(db, row.family_id)
            failure = "刷新令牌与这个客户端不一致"
        else:
            tokens = _add_tokens(
                db, row.user_id, row.client_id, row.client_name, resource,
                row.family_id, int(row.family_created or row.created_at or _now()),
            )
    if failure:
        raise OAuthPage("invalid_grant", failure)
    return tokens


def resolve_access_token(token: str, resource_expected: str) -> str:
    """令牌有效时返回 user_id。前缀不符返回空串，调用方继续识别其他凭证。过期或被吊销抛 OAuthPage。"""
    raw = str(token or "")
    if not raw.startswith(ACCESS_PREFIX):
        return ""
    from mino_nexus.models.mcp_oauth import OauthToken

    with session_scope() as db:
        row = db.query(OauthToken).filter(OauthToken.token_hash == _hash(raw), OauthToken.kind == "access").one_or_none()
        if row is None or row.revoked or int(row.expires_at or 0) <= _now() or not same_resource(row.resource, resource_expected):
            raise OAuthPage("invalid_token", "访问令牌无效或已过期", 401)
        row.last_used_at = _now()
        return row.user_id


def list_grants(user_id: str) -> list[dict[str, Any]]:
    from mino_nexus.models.mcp_oauth import OauthToken

    with session_scope() as db:
        rows = db.query(OauthToken).filter(
            OauthToken.user_id == user_id, OauthToken.kind == "refresh", OauthToken.replaced.is_(False),
        ).order_by(OauthToken.id.desc()).all()
        families = [row.family_id for row in rows]
        used: dict[str, int] = {}
        if families:
            for item in db.query(OauthToken).filter(OauthToken.family_id.in_(families)).all():
                used[item.family_id] = max(used.get(item.family_id, 0), int(item.last_used_at or 0))
        return [
            {
                "family_id": row.family_id,
                "client_name": row.client_name,
                "prefix": row.prefix,
                "created_at": int(row.family_created or row.created_at or 0),
                "last_used_at": used.get(row.family_id, 0),
                "revoked": bool(row.revoked),
            }
            for row in rows
        ]


def revoke_grant(user_id: str, family_id: str) -> bool:
    from mino_nexus.models.mcp_oauth import OauthToken

    with session_scope() as db:
        rows = db.query(OauthToken).filter(
            OauthToken.user_id == user_id, OauthToken.family_id == str(family_id or ""),
        ).all()
        if not rows:
            return False
        for row in rows:
            row.revoked = True
        return True
