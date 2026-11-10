/**
 * Explicit refusals for full Workbench auth/API surface.
 * Cloudflare free hub is MCP + Scout + device sync only — not a login portal.
 */

import type { Env } from "./env";
import { hubStub } from "./env";

function envelope(data: unknown, msg = ""): Response {
  const body: Record<string, unknown> = { code: 200, ok: true, data };
  if (msg) body.msg = msg;
  return Response.json(body);
}

function refused(status: number, msg: string): Response {
  return Response.json({ code: status, ok: false, msg }, { status });
}

async function hubJson(env: Env, path: string): Promise<unknown> {
  const res = await hubStub(env).fetch(new Request(`https://hub${path}`));
  return res.json();
}

const WORKBENCH_MSG =
  "Cloudflare 免费 hub 不提供完整 Workbench。请用 MCP（Authorization: Bearer <MCP_BEARER_TOKEN>）访问 scout/device；完整控制台需完整 Nexus。";

export async function handleCompat(
  request: Request,
  _env: Env,
  pathname: string,
): Promise<Response | null> {
  if (pathname === "/auth/status" && request.method === "GET") {
    return envelope({ logged_in: false });
  }

  if (
    (pathname === "/auth/login" ||
      pathname === "/auth/register" ||
      pathname === "/auth/logout" ||
      pathname === "/me/bootstrap") &&
    (request.method === "GET" || request.method === "POST")
  ) {
    return refused(501, WORKBENCH_MSG);
  }

  // Old Workbench paths: do not fake main auth or leak secrets.
  if (
    pathname === "/runtime/nodes" ||
    pathname === "/device/list" ||
    pathname.startsWith("/runtime/") ||
    pathname.startsWith("/device/") ||
    pathname.startsWith("/case-runner/") ||
    pathname.startsWith("/project/") ||
    pathname.startsWith("/settings/") ||
    pathname.startsWith("/assistant/")
  ) {
    return refused(501, WORKBENCH_MSG);
  }

  if (pathname === "/sys/server_info" && request.method === "GET") {
    return envelope({
      service: "mino-nexus-cloudflare",
      version: "0.0.1",
      nexus_version: "0.0.1-cloudflare",
      scope: ["scout_online", "mcp", "device_sync"],
      workbench: false,
    });
  }

  return null;
}

export async function enhancedHealth(env: Env): Promise<Response> {
  const scouts = (await hubJson(env, "/internal/scouts")) as Array<{ online?: boolean }>;
  const alive = Array.isArray(scouts) ? scouts.filter((s) => s.online).length : 0;
  return Response.json({
    ok: true,
    service: "mino-nexus-cloudflare",
    version: "0.0.1",
    nexus_version: "0.0.1-cloudflare",
    status: "ok",
    nodes: Array.isArray(scouts) ? scouts.length : 0,
    nodes_alive: alive,
    scope: ["scout_online", "mcp", "device_sync"],
    workbench: false,
  });
}

export function requireMcpBearer(request: Request, env: Env): Response | null {
  const raw = request.headers.get("Authorization") || "";
  const token = raw.toLowerCase().startsWith("bearer ") ? raw.slice(7).trim() : "";
  if (!token || token !== env.MCP_BEARER_TOKEN) {
    return new Response("Unauthorized", { status: 401 });
  }
  return null;
}
