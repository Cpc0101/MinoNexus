/**
 * Workbench-compatible HTTP surface on the Cloudflare hub.
 */

import type { Env } from "./env";
import { listDevices, listScouts } from "./db";
import {
  bearer,
  createProject,
  destroySession,
  listProjects,
  loginUser,
  registerUser,
  sessionUser,
} from "./auth";
import { getInstallStatus, issueInstallToken, scoutReleaseMeta } from "./install";
import { deleteAIProvider, listAIProviders, saveAIProvider } from "./settings";

function envelope(data: unknown, msg = ""): Response {
  const body: Record<string, unknown> = { code: 200, ok: true, data };
  if (msg) body.msg = msg;
  return Response.json(body);
}

function fail(status: number, msg: string): Response {
  return Response.json({ code: status, ok: false, msg }, { status });
}

function authStatusPayload(user: Awaited<ReturnType<typeof sessionUser>>, token = "") {
  if (!user) {
    return {
      logged_in: false,
      needs_setup: false,
      mail_configured: false,
      user_id: "",
      email: "",
      name: "",
      username: "",
      role: "",
      ws_token: "",
    };
  }
  return {
    logged_in: true,
    needs_setup: false,
    mail_configured: false,
    ...user,
    token,
    ws_token: token,
    capabilities: {
      manage_users: user.role === "admin",
      write_mail: false,
      write_packs: false,
      write_keys: true,
      write_plugins: false,
      run_cases: false,
      install_scout: true,
      scope: ["scout_online", "mcp", "device_sync", "workbench_auth"],
      hub: "cloudflare",
    },
  };
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
      owner_user_id: s.owner_user_id || undefined,
      last_heartbeat: s.last_seen_ms
        ? new Date(Number(s.last_seen_ms)).toISOString()
        : undefined,
      last_seen_ago_sec: s.last_seen_ms
        ? Math.max(0, Math.floor((Date.now() - Number(s.last_seen_ms)) / 1000))
        : undefined,
      device_count: mine.length,
      devices: mine,
      plugins: null,
      plugins_ready: null,
    };
  });
}

async function requireUser(request: Request, env: Env) {
  const token = bearer(request);
  const user = await sessionUser(env.DB, token);
  if (!user) return { error: fail(401, "未登录") as Response, user: null as null, token: "" };
  return { error: null as null, user, token };
}

function randomId(prefix: string): string {
  const bytes = new Uint8Array(8);
  crypto.getRandomValues(bytes);
  return `${prefix}_${[...bytes].map((b) => b.toString(16).padStart(2, "0")).join("")}`;
}

async function readJson(request: Request): Promise<Record<string, unknown>> {
  try {
    return (await request.json()) as Record<string, unknown>;
  } catch {
    return {};
  }
}

/** Soft empty shapes so Workbench pages load instead of hard-failing. */
function softGet(pathname: string): unknown {
  if (pathname.endsWith("/summary") || pathname.includes("/metrics")) {
    return { total: 0, running: 0, failed: 0, succeeded: 0, items: [], count: 0 };
  }
  if (pathname.includes("/costs")) {
    return {
      summary: { attempts: 0, prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, known_estimated_cost_usd: 0, unknown_usage_attempts: 0, unknown_cost_attempts: 0, cost_complete: true },
      items: [],
      group_count: 0,
      has_more: false,
      currency: "USD",
      notice: "",
    };
  }
  if (pathname.includes("/models")) {
    return { models: [], providers: [], sdk_version: "", notice: "Cloudflare hub 不提供 SDK 模型目录" };
  }
  if (pathname.includes("/jobs/health") || pathname.endsWith("/status")) {
    return { ok: true, jobs: [], running: 0 };
  }
  if (
    pathname.includes("/list") ||
    pathname.endsWith("/nodes") ||
    pathname.endsWith("/devices") ||
    pathname.endsWith("/cases") ||
    pathname.endsWith("/tasks") ||
    pathname.endsWith("/runs") ||
    pathname.endsWith("/sessions") ||
    pathname.endsWith("/accounts") ||
    pathname.endsWith("/plugins") ||
    pathname.endsWith("/packs") ||
    pathname.endsWith("/skills") ||
    pathname.endsWith("/roles") ||
    pathname.endsWith("/tools") ||
    pathname.endsWith("/turns") ||
    pathname.endsWith("/subscriptions") ||
    pathname.endsWith("/identities") ||
    pathname.endsWith("/requests") ||
    pathname.endsWith("/mcp-tokens") ||
    pathname.endsWith("/oauth-grants") ||
    pathname.endsWith("/flow-blocks") ||
    pathname.endsWith("/catalog") ||
    pathname.endsWith("/entries") ||
    pathname.endsWith("/controls") ||
    pathname.endsWith("/requirements") ||
    pathname.includes("/case-modules") ||
    pathname.includes("/import/sources")
  ) {
    return { items: [], nodes: [], devices: [], list: [], count: 0, users: [], sessions: [], providers: [] };
  }
  return {};
}

export async function handleCompat(
  request: Request,
  env: Env,
  pathname: string,
): Promise<Response | null> {
  const method = request.method;

  if (pathname === "/auth/status" && method === "GET") {
    const token = bearer(request);
    const user = await sessionUser(env.DB, token);
    return envelope(authStatusPayload(user, user ? token : ""));
  }

  if (pathname === "/auth/login" && method === "POST") {
    const body = await readJson(request);
    try {
      const result = await loginUser(
        env.DB,
        String(body.email || body.username || ""),
        String(body.password || ""),
      );
      return envelope(authStatusPayload(result.user, result.token));
    } catch (e) {
      return fail(401, e instanceof Error ? e.message : "账号或密码错误");
    }
  }

  if (pathname === "/auth/register" && method === "POST") {
    const body = await readJson(request);
    try {
      const result = await registerUser(env.DB, String(body.email || ""), String(body.password || ""));
      return envelope(authStatusPayload(result.user, result.token), "账号已创建");
    } catch (e) {
      return fail(400, e instanceof Error ? e.message : "注册失败");
    }
  }

  if (pathname === "/auth/logout" && method === "POST") {
    await destroySession(env.DB, bearer(request));
    return envelope({ logged_in: false });
  }

  if (pathname === "/me/bootstrap" && method === "GET") {
    const gate = await requireUser(request, env);
    if (gate.error) return gate.error;
    return envelope({
      ...authStatusPayload(gate.user, gate.token),
      client: request.headers.get("X-Mino-Client") || "",
      hub: "cloudflare",
      scope: ["scout_online", "mcp", "device_sync", "workbench_auth"],
    });
  }

  if (pathname === "/sys/server_info" && method === "GET") {
    return envelope({
      service: "mino-nexus-cloudflare",
      version: "0.0.1",
      nexus_version: "0.0.1-cloudflare",
      scope: ["scout_online", "mcp", "device_sync", "workbench_auth"],
      workbench: true,
      http_url: "https://nexus.minocc.top",
      node_ws_url: "wss://nexus.minocc.top/node",
    });
  }

  if (pathname === "/sys/runtime" && method === "GET") {
    const scouts = (await listScouts(env.DB)) as Array<{ online?: boolean }>;
    const alive = scouts.filter((s) => s.online).length;
    return envelope({
      electron: { online: false },
      embeddedServer: { running: true },
      endpoints: [{ name: "nexus.minocc.top", url: "https://nexus.minocc.top", online: true }],
      isLocalGateway: false,
      lan_host: "nexus.minocc.top",
      http_url: "https://nexus.minocc.top",
      node_ws_url: "wss://nexus.minocc.top/node",
      node: {
        role: "nexus",
        connected: true,
        is_master: true,
        node_count: scouts.length,
        nodes_alive: alive,
      },
      nodes: [],
      node_count: scouts.length,
      nodes_alive: alive,
      nexus_version: "0.0.1-cloudflare",
      jobs: { ok: true },
    });
  }

  if (pathname === "/runtime/nodes" && method === "GET") {
    const gate = await requireUser(request, env);
    if (gate.error) return gate.error;
    const [scouts, devicesRaw] = await Promise.all([
      listScouts(env.DB),
      listDevices(env.DB, false),
    ]);
    const devices = mapDevices(devicesRaw);
    const nodes = mapScouts(scouts, devices);
    return envelope({ nodes, count: nodes.length });
  }

  if (pathname === "/runtime/nodes/install-token" && method === "POST") {
    const gate = await requireUser(request, env);
    if (gate.error) return gate.error;
    const data = await issueInstallToken(env.DB, gate.user!.user_id, "https://nexus.minocc.top");
    return envelope(data);
  }

  {
    const m = pathname.match(/^\/runtime\/nodes\/installations\/([^/]+)$/);
    if (m && method === "GET") {
      const gate = await requireUser(request, env);
      if (gate.error) return gate.error;
      const status = await getInstallStatus(env.DB, decodeURIComponent(m[1]), gate.user!.user_id);
      if (!status) return fail(404, "没有本次安装记录");
      return envelope(status);
    }
  }

  if (pathname === "/device/list" && method === "GET") {
    const gate = await requireUser(request, env);
    if (gate.error) return gate.error;
    const url = new URL(request.url);
    const onlineOnly =
      url.searchParams.get("online_only") === "1" ||
      url.searchParams.get("only_online") === "true";
    const devices = mapDevices(await listDevices(env.DB, onlineOnly));
    return envelope({ devices, list: devices, items: devices, count: devices.length });
  }

  if (pathname === "/project/list" && method === "GET") {
    const gate = await requireUser(request, env);
    if (gate.error) return gate.error;
    return envelope(await listProjects(env.DB, gate.user!.user_id));
  }

  if (pathname === "/project/create" && method === "POST") {
    const gate = await requireUser(request, env);
    if (gate.error) return gate.error;
    const body = await readJson(request);
    return envelope(
      await createProject(env.DB, gate.user!.user_id, String(body.name || "")),
      "已创建",
    );
  }

  if (pathname === "/project/app/create" && method === "POST") {
    const gate = await requireUser(request, env);
    if (gate.error) return gate.error;
    const body = await readJson(request);
    const app_id = randomId("app");
    const now = Date.now();
    const project_id = String(body.project_id || "");
    const name = String(body.name || "未命名应用");
    await env.DB.prepare(
      `INSERT INTO apps (app_id, project_id, owner_user_id, name, platform, meta_json, created_at_ms)
       VALUES (?, ?, ?, ?, ?, NULL, ?)`,
    )
      .bind(app_id, project_id, gate.user!.user_id, name, String(body.platform || ""), now)
      .run();
    return envelope({ app_id, id: app_id, project_id, name, platform: body.platform || "" }, "已创建");
  }

  {
    const m = pathname.match(/^\/project\/([^/]+)\/env$/);
    if (m && method === "GET") {
      const gate = await requireUser(request, env);
      if (gate.error) return gate.error;
      return envelope({ env: {} });
    }
    if (m && method === "PUT") {
      const gate = await requireUser(request, env);
      if (gate.error) return gate.error;
      return envelope({ ok: true }, "已保存");
    }
  }

  {
    const m = pathname.match(/^\/project\/([^/]+)\/(cases|case-modules|accounts|repo-apps|script-repo|requirements|controls|device-app-sessions|resource-allocation-logs)$/);
    if (m && method === "GET") {
      const gate = await requireUser(request, env);
      if (gate.error) return gate.error;
      if (m[2] === "script-repo") return envelope({ url: "", branch: "" });
      if (m[2] === "repo-apps") return envelope({ apps: [] });
      if (m[2] === "case-modules") return envelope({ items: [], total: 0 });
      return envelope([]);
    }
  }

  if (pathname === "/settings/ai/providers" && method === "GET") {
    const gate = await requireUser(request, env);
    if (gate.error) return gate.error;
    return envelope(await listAIProviders(env.DB, gate.user!.user_id));
  }

  {
    const m = pathname.match(/^\/settings\/ai\/providers\/([^/]+)$/);
    if (m && method === "PUT") {
      const gate = await requireUser(request, env);
      if (gate.error) return gate.error;
      const body = await readJson(request);
      const data = await saveAIProvider(env.DB, gate.user!.user_id, decodeURIComponent(m[1]), body);
      return envelope(data, "已保存");
    }
    if (m && method === "DELETE") {
      const gate = await requireUser(request, env);
      if (gate.error) return gate.error;
      await deleteAIProvider(env.DB, gate.user!.user_id, decodeURIComponent(m[1]));
      return envelope({ deleted: true }, "已删除");
    }
  }

  {
    const m = pathname.match(/^\/settings\/ai\/providers\/([^/]+)\/validate$/);
    if (m && method === "POST") {
      const gate = await requireUser(request, env);
      if (gate.error) return gate.error;
      return fail(501, "Cloudflare hub 暂不支持在线验证模型 Key");
    }
  }

  if (pathname === "/releases/scout/meta" && method === "GET") {
    const gate = await requireUser(request, env);
    if (gate.error) return gate.error;
    const url = new URL(request.url);
    return envelope(
      await scoutReleaseMeta(url.searchParams.get("os") || "", url.searchParams.get("arch") || ""),
    );
  }

  // Case-runner reads: empty. Writes that need Scout dispatch: 501.
  if (pathname.startsWith("/case-runner/")) {
    const gate = await requireUser(request, env);
    if (gate.error) return gate.error;
    if (method === "GET") {
      if (pathname === "/case-runner/devices") {
        return envelope(mapDevices(await listDevices(env.DB, false)));
      }
      if (pathname === "/case-runner/tasks/summary") {
        return envelope({ total: 0, running: 0, failed: 0, succeeded: 0 });
      }
      return envelope(softGet(pathname));
    }
    return fail(501, "Cloudflare hub 暂不支持跑批 / case_run");
  }

  {
    const toolsMatch = pathname.match(/^\/runtime\/nodes\/([^/]+)\/tools$/);
    if (toolsMatch && method === "GET") {
      const gate = await requireUser(request, env);
      if (gate.error) return gate.error;
      const nodeId = decodeURIComponent(toolsMatch[1]);
      const scouts = (await listScouts(env.DB)) as Array<{ scout_id?: string; online?: boolean }>;
      const online = scouts.some((s) => s.scout_id === nodeId && s.online);
      // Shape must match Workbench NodeTools; missing `tasks` crashes downloads pane.
      return envelope({
        node_id: nodeId,
        online,
        supported: false,
        contract_version: 0,
        stale: true,
        reported_at: Math.floor(Date.now() / 1000),
        items: [],
        tasks: [],
      });
    }
  }

  {
    const m = pathname.match(/^\/runtime\/nodes\/([^/]+)\/update-status$/);
    if (m && method === "GET") {
      const gate = await requireUser(request, env);
      if (gate.error) return gate.error;
      return envelope({});
    }
  }

  {
    const m = pathname.match(/^\/runtime\/nodes\/([^/]+)\/logs$/);
    if (m && method === "GET") {
      const gate = await requireUser(request, env);
      if (gate.error) return gate.error;
      return envelope({ lines: [], text: "" });
    }
  }

  // Node plugin / command paths need live Scout RPC — not on this hub yet.
  if (
    pathname.match(/^\/runtime\/nodes\/[^/]+\/(command|update|plugins|tools|tool-tasks|logs|update-status|claim)/) &&
    method !== "GET"
  ) {
    const gate = await requireUser(request, env);
    if (gate.error) return gate.error;
    return fail(501, "节点远程控制尚未迁移到 Cloudflare hub");
  }

  if (pathname.match(/^\/runtime\/nodes\/[^/]+/) && method === "GET") {
    const gate = await requireUser(request, env);
    if (gate.error) return gate.error;
    return envelope(softGet(pathname));
  }

  // Remaining Workbench surfaces: authenticated soft responses.
  if (
    pathname.startsWith("/settings/") ||
    pathname.startsWith("/assistant/") ||
    pathname.startsWith("/project/") ||
    pathname.startsWith("/packs") ||
    pathname.startsWith("/flow-blocks") ||
    pathname.startsWith("/nav-fsm/") ||
    pathname.startsWith("/device/") ||
    pathname.startsWith("/oauth/") ||
    pathname.startsWith("/releases/")
  ) {
    const gate = await requireUser(request, env);
    if (gate.error) return gate.error;
    if (method === "GET") return envelope(softGet(pathname));
    if (method === "DELETE") return envelope({ deleted: true });
    // Accept no-op writes so forms don't hard-crash; persist only where implemented above.
    return envelope({ ok: true }, "Cloudflare hub 已受理（部分能力为占位）");
  }

  return null;
}

export async function enhancedHealth(env: Env): Promise<Response> {
  const scouts = (await listScouts(env.DB)) as Array<{ online?: boolean }>;
  const alive = Array.isArray(scouts) ? scouts.filter((s) => s.online).length : 0;
  return Response.json({
    ok: true,
    service: "mino-nexus-cloudflare",
    version: "0.0.1",
    nexus_version: "0.0.1-cloudflare",
    status: "ok",
    nodes: Array.isArray(scouts) ? scouts.length : 0,
    nodes_alive: alive,
    scope: ["scout_online", "mcp", "device_sync", "workbench_auth"],
    workbench: true,
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
