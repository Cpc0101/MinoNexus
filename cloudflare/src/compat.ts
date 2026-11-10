/**
 * Workbench / main-compatible HTTP surface for the thin CF hub.
 * Mirrors MCP scout_list + device_list behind the same paths Workbench already calls.
 */

import type { Env } from "./env";
import { hubStub } from "./env";

function envelope(data: unknown, msg = ""): Response {
  const body: Record<string, unknown> = { code: 200, ok: true, data };
  if (msg) body.msg = msg;
  return Response.json(body);
}

function bearer(request: Request): string {
  const raw = request.headers.get("Authorization") || "";
  if (raw.toLowerCase().startsWith("bearer ")) return raw.slice(7).trim();
  return "";
}

function authed(request: Request, env: Env): boolean {
  const token = bearer(request);
  return Boolean(token && token === env.MCP_BEARER_TOKEN);
}

async function hubJson(env: Env, path: string): Promise<unknown> {
  const res = await hubStub(env).fetch(new Request(`https://hub${path}`));
  return res.json();
}

function mapDevices(raw: unknown): Record<string, unknown>[] {
  const list = Array.isArray(raw) ? raw : [];
  return list.map((row) => {
    const d = (row || {}) as Record<string, unknown>;
    const online = Boolean(d.online);
    return {
      sn: String(d.device_id || ""),
      device_id: d.device_id,
      scout_id: d.scout_id,
      node_id: d.scout_id,
      current_scout_id: d.scout_id,
      platform: d.platform || undefined,
      type: d.platform || undefined,
      model: d.label || d.device_id,
      status: online ? "online" : "offline",
      ready: Boolean(d.ready),
      node_alive: online,
      executable: online && Boolean(d.ready),
      last_online: d.updated_at_ms
        ? new Date(Number(d.updated_at_ms)).toISOString()
        : undefined,
      meta: d.meta || undefined,
    };
  });
}

function mapScouts(rawScouts: unknown, devices: Record<string, unknown>[]): Record<string, unknown>[] {
  const list = Array.isArray(rawScouts) ? rawScouts : [];
  return list.map((row) => {
    const s = (row || {}) as Record<string, unknown>;
    const scoutId = String(s.scout_id || "");
    const online = Boolean(s.online);
    const host = (s.host && typeof s.host === "object" ? s.host : {}) as Record<string, unknown>;
    const mine = devices.filter((d) => String(d.scout_id || d.node_id || "") === scoutId);
    return {
      node_id: scoutId,
      scout_id: scoutId,
      online,
      alive: online,
      status: online ? "online" : "offline",
      scout_version: s.scout_version || undefined,
      hostname: host.hostname || host.name || scoutId,
      platform: host.os || host.platform || undefined,
      arch: host.arch || undefined,
      host,
      last_heartbeat: s.last_seen_ms
        ? new Date(Number(s.last_seen_ms)).toISOString()
        : undefined,
      last_seen_ago_sec: s.last_seen_ms
        ? Math.max(0, Math.floor((Date.now() - Number(s.last_seen_ms)) / 1000))
        : undefined,
      device_count: mine.length,
      devices: mine,
      owner_user_id: "cf-free",
      owner_name: "cloudflare",
    };
  });
}

function guestUser(env: Env): Record<string, unknown> {
  return {
    logged_in: true,
    user_id: "cf-free",
    username: "cloudflare",
    name: "Cloudflare Free",
    email: "cf-free@minocc.top",
    role: "admin",
    token: env.MCP_BEARER_TOKEN,
    ws_token: env.MCP_BEARER_TOKEN,
    capabilities: {
      scope: ["scout_online", "mcp", "device_sync"],
      hub: "cloudflare-free",
    },
  };
}

export async function handleCompat(
  request: Request,
  env: Env,
  pathname: string,
): Promise<Response | null> {
  if (pathname === "/auth/status" && request.method === "GET") {
    if (!authed(request, env)) {
      return envelope({ logged_in: false });
    }
    return envelope(guestUser(env));
  }

  if (pathname === "/auth/login" && request.method === "POST") {
    let body: Record<string, unknown> = {};
    try {
      body = (await request.json()) as Record<string, unknown>;
    } catch {
      body = {};
    }
    const password = String(body.password || "");
    if (password !== env.MCP_BEARER_TOKEN) {
      return Response.json(
        { code: 401, ok: false, msg: "账号或密码错误（Cloudflare 免费 hub 请用 MCP_BEARER_TOKEN 作密码）" },
        { status: 401 },
      );
    }
    return envelope(guestUser(env));
  }

  if (pathname === "/auth/logout" && request.method === "POST") {
    return envelope({ logged_in: false });
  }

  if (pathname === "/auth/register" && request.method === "POST") {
    return Response.json(
      { code: 400, ok: false, msg: "Cloudflare 免费 hub 不开放注册，请用 MCP_BEARER_TOKEN 登录" },
      { status: 400 },
    );
  }

  if (pathname === "/me/bootstrap" && request.method === "GET") {
    if (!authed(request, env)) {
      return new Response(JSON.stringify({ code: 401, ok: false, msg: "未登录" }), {
        status: 401,
        headers: { "content-type": "application/json" },
      });
    }
    return envelope({
      user: guestUser(env),
      scope: ["scout_online", "mcp", "device_sync"],
      hub: "cloudflare-free",
    });
  }

  if (pathname === "/sys/server_info" && request.method === "GET") {
    return envelope({
      service: "mino-nexus-cloudflare",
      version: "0.0.1",
      nexus_version: "0.0.1-cloudflare",
      scope: ["scout_online", "mcp", "device_sync"],
    });
  }

  // MCP scout_list ↔ Workbench /runtime/nodes
  if (pathname === "/runtime/nodes" && request.method === "GET") {
    if (!authed(request, env)) {
      return new Response(JSON.stringify({ code: 401, ok: false, msg: "未登录" }), {
        status: 401,
        headers: { "content-type": "application/json" },
      });
    }
    const [scouts, devicesRaw] = await Promise.all([
      hubJson(env, "/internal/scouts"),
      hubJson(env, "/internal/devices?online_only=0"),
    ]);
    const devices = mapDevices(devicesRaw);
    const nodes = mapScouts(scouts, devices);
    return envelope({ nodes, count: nodes.length });
  }

  // MCP device_list ↔ Workbench /device/list
  if (pathname === "/device/list" && request.method === "GET") {
    if (!authed(request, env)) {
      return new Response(JSON.stringify({ code: 401, ok: false, msg: "未登录" }), {
        status: 401,
        headers: { "content-type": "application/json" },
      });
    }
    const url = new URL(request.url);
    const onlineOnly =
      url.searchParams.get("online_only") === "1" ||
      url.searchParams.get("only_online") === "true";
    const devicesRaw = await hubJson(
      env,
      `/internal/devices?online_only=${onlineOnly ? "1" : "0"}`,
    );
    const devices = mapDevices(devicesRaw);
    return envelope({ devices, list: devices, items: devices, count: devices.length });
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
  });
}
