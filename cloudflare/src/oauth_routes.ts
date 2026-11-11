/**
 * Port of mino_nexus.api.rOAuth — discovery + register/authorize/token + consent APIs.
 */

import type { Env } from "./env";
import { bearer, sessionUser } from "./auth";
import {
  OAuthPage,
  OAuthRedirect,
  beginAuthorization,
  canonicalResource,
  consentTarget,
  decide,
  exchange,
  listGrants,
  metadata,
  publicOrigin,
  registerClient,
  requestView,
  resourceMetadata,
  revokeGrant,
} from "./mcp_oauth";

function originOf(request: Request): string {
  return publicOrigin(new URL(request.url).origin);
}

function remoteOf(request: Request): string {
  return request.headers.get("cf-connecting-ip") || "unknown";
}

function oauthError(exc: OAuthPage): Response {
  return Response.json(
    { error: exc.error, error_description: exc.description },
    { status: exc.status, headers: { "Cache-Control": "no-store" } },
  );
}

function page(title: string, message: string, status: number): Response {
  const body = `<!doctype html>
<meta charset="utf-8">
<title>${escapeHtml(title)}</title>
<body style="font:16px/1.5 sans-serif;margin:48px;max-width:32rem">
<h1 style="font-size:20px">${escapeHtml(title)}</h1>
<p>${escapeHtml(message)}</p>
`;
  return new Response(body, {
    status,
    headers: { "content-type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
  });
}

function escapeHtml(value: string): string {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function envelope(data: unknown): Response {
  return Response.json({ code: 200, ok: true, data });
}

function fail(status: number, msg: string): Response {
  return Response.json({ code: status, ok: false, msg }, { status });
}

async function requireSession(request: Request, env: Env) {
  const user = await sessionUser(env.DB, bearer(request));
  if (!user) return { error: fail(401, "未登录") as Response, user: null as null };
  return { error: null as null, user };
}

export async function handleOAuth(
  request: Request,
  env: Env,
  pathname: string,
): Promise<Response | null> {
  const method = request.method;
  const origin = originOf(request);

  if (
    (pathname === "/.well-known/oauth-protected-resource" ||
      pathname === "/.well-known/oauth-protected-resource/mcp") &&
    method === "GET"
  ) {
    return Response.json(resourceMetadata(origin));
  }

  if (
    (pathname === "/.well-known/oauth-authorization-server" ||
      pathname === "/.well-known/openid-configuration") &&
    method === "GET"
  ) {
    return Response.json(metadata(origin));
  }

  if (pathname === "/oauth/register" && method === "POST") {
    let body: Record<string, unknown>;
    try {
      body = (await request.json()) as Record<string, unknown>;
    } catch {
      return oauthError(new OAuthPage("invalid_client_metadata", "登记内容不是 JSON"));
    }
    try {
      const out = await registerClient(env.DB, body, remoteOf(request));
      return Response.json(out, { status: 201 });
    } catch (exc) {
      if (exc instanceof OAuthPage) return oauthError(exc);
      throw exc;
    }
  }

  if (pathname === "/oauth/authorize" && method === "GET") {
    const url = new URL(request.url);
    const params: Record<string, string> = {};
    for (const key of [
      "response_type",
      "client_id",
      "redirect_uri",
      "code_challenge",
      "code_challenge_method",
      "state",
      "scope",
      "resource",
    ]) {
      params[key] = url.searchParams.get(key) || "";
    }
    try {
      const txn = await beginAuthorization(
        env.DB,
        params,
        canonicalResource(origin),
        remoteOf(request),
      );
      const target = consentTarget(origin) + "/oauth/consent?txn=" + encodeURIComponent(txn);
      return Response.redirect(target, 302);
    } catch (exc) {
      if (exc instanceof OAuthRedirect) {
        return Response.redirect(exc.location, 302);
      }
      if (exc instanceof OAuthPage) {
        return page("无法开始授权", exc.description, exc.status);
      }
      throw exc;
    }
  }

  {
    const m = pathname.match(/^\/oauth\/requests\/([^/]+)$/);
    if (m && method === "GET") {
      const gate = await requireSession(request, env);
      if (gate.error) return gate.error;
      const view = await requestView(env.DB, decodeURIComponent(m[1]));
      if (!Object.keys(view).length) {
        return Response.json(
          { detail: "这次授权已经失效，回到客户端重新连接" },
          { status: 404 },
        );
      }
      return envelope(view);
    }
  }

  {
    const m = pathname.match(/^\/oauth\/requests\/([^/]+)\/(approve|deny)$/);
    if (m && method === "POST") {
      const gate = await requireSession(request, env);
      if (gate.error) return gate.error;
      try {
        const location = await decide(
          env.DB,
          decodeURIComponent(m[1]),
          gate.user!.user_id,
          m[2] === "approve",
        );
        return envelope({ redirect_to: location });
      } catch (exc) {
        if (exc instanceof OAuthPage) {
          return Response.json({ detail: exc.description }, { status: exc.status });
        }
        throw exc;
      }
    }
  }

  if (pathname === "/oauth/token" && method === "POST") {
    const contentType = request.headers.get("content-type") || "";
    let payload: Record<string, string> = {};
    try {
      if (contentType.includes("application/json")) {
        const raw = (await request.json()) as Record<string, unknown>;
        for (const [k, v] of Object.entries(raw || {})) {
          payload[k] = v == null ? "" : String(v);
        }
      } else {
        const form = await request.formData();
        for (const [k, v] of form.entries()) {
          payload[k] = String(v);
        }
      }
    } catch {
      return oauthError(new OAuthPage("invalid_request", "令牌请求无法解析"));
    }
    try {
      const issued = await exchange(
        env.DB,
        payload,
        canonicalResource(origin),
        remoteOf(request),
      );
      return Response.json(issued, { headers: { "Cache-Control": "no-store" } });
    } catch (exc) {
      if (exc instanceof OAuthPage) {
        const response = oauthError(exc);
        if (exc.status === 401) {
          response.headers.set("WWW-Authenticate", 'Bearer error="invalid_client"');
        }
        return response;
      }
      throw exc;
    }
  }

  if (pathname === "/assistant/oauth-grants" && method === "GET") {
    const gate = await requireSession(request, env);
    if (gate.error) return gate.error;
    return envelope({ items: await listGrants(env.DB, gate.user!.user_id) });
  }

  {
    const m = pathname.match(/^\/assistant\/oauth-grants\/([^/]+)$/);
    if (m && method === "DELETE") {
      const gate = await requireSession(request, env);
      if (gate.error) return gate.error;
      const ok = await revokeGrant(env.DB, gate.user!.user_id, decodeURIComponent(m[1]));
      if (!ok) return fail(404, "没有这条授权");
      return envelope({ family_id: decodeURIComponent(m[1]) });
    }
  }

  return null;
}
