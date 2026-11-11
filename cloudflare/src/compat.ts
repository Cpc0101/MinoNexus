/**
 * Workbench-compatible HTTP surface on the Cloudflare hub.
 */

import type { Env } from "./env";
import { listDevices, listScouts } from "./db";
import {
  bearer,
  createApp,
  createProject,
  destroySession,
  listProjects,
  loginUser,
  registerUser,
  sessionUser,
} from "./auth";
import { getInstallStatus, issueInstallToken, scoutReleaseMeta } from "./install";
import { deleteAIProvider, listAIProviders, saveAIProvider } from "./settings";
import { readNodeTools } from "./node_tools";
import { handleAssistant } from "./assistant";
import { handleDeviceWrites, handleNodeControl } from "./node_control";
import { handlePluginRoutes } from "./plugin_routes";
import { handleOAuth } from "./oauth_routes";
import {
  getRepo,
  publicUrl,
  requireOwnedProject,
  saveRepo,
  workspacePlace,
} from "./script_repo";

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
      scope: ["scout_online", "mcp", "device_sync", "workbench_auth", "oauth"],
      hub: "cloudflare",
    },
  };
}

function mapDevices(raw: unknown): Record<string, unknown>[] {
  const list = Array.isArray(raw) ? raw : [];
  return list.map((row) => {
    const d = (row || {}) as Record<string, unknown>;
    const online = Boolean(d.online);
    const meta =
      d.meta && typeof d.meta === "object" ? (d.meta as Record<string, unknown>) : {};
    // Workbench connectedNodeDevices() requires top-level channels for android/ios;
    // desktop/web bypass that check. Match Nexus node_registry.devices().
    const channels =
      (meta.channels && typeof meta.channels === "object"
        ? (meta.channels as Record<string, unknown>)
        : null) ||
      (d.channels && typeof d.channels === "object"
        ? (d.channels as Record<string, unknown>)
        : {});
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
      channels,
      last_online: d.updated_at_ms
        ? new Date(Number(d.updated_at_ms)).toISOString()
        : undefined,
      meta: Object.keys(meta).length ? meta : undefined,
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
      plugins: Array.isArray(s.plugins) ? s.plugins : null,
      plugins_ready: typeof s.plugins_ready === "boolean" ? s.plugins_ready : null,
      tool_tasks_version: Number(s.tool_tasks_version || 0),
      plugin_warning: s.plugin_warning || "",
      runtime_environment: s.runtime_environment || "",
      tools_supported: [1, 2].includes(Number(s.tool_tasks_version || 0)),
    };
  });
}

async function requireUser(request: Request, env: Env) {
  const token = bearer(request);
  const user = await sessionUser(env.DB, token);
  if (!user) return { error: fail(401, "未登录") as Response, user: null as null, token: "" };
  return { error: null as null, user, token };
}

async function readJson(request: Request): Promise<Record<string, unknown>> {
  try {
    return (await request.json()) as Record<string, unknown>;
  } catch {
    return {};
  }
}

/**
 * Soft empty shapes so Workbench pages load instead of hard-failing.
 * Must return the *exact* contract key the page reads — `{}` breaks
 * `data?.items.length` (optional chain only guards `data`, not `items`).
 */
function softGet(pathname: string): unknown {
  if (pathname.endsWith("/summary") || pathname.includes("/metrics")) {
    return { total: 0, running: 0, failed: 0, succeeded: 0, items: [], count: 0 };
  }
  if (pathname.includes("/costs")) {
    return {
      summary: {
        attempts: 0,
        prompt_tokens: 0,
        completion_tokens: 0,
        total_tokens: 0,
        known_estimated_cost_usd: 0,
        unknown_usage_attempts: 0,
        unknown_cost_attempts: 0,
        cost_complete: true,
      },
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
  if (pathname.includes("/jobs/health") || pathname === "/settings/ai/jobs/health") {
    return { ok: true, jobs: [], running: 0 };
  }
  if (pathname === "/settings/ai/jobs") return { jobs: [] };
  if (pathname === "/settings/ai/roles") return { roles: [] };
  if (pathname === "/settings/ai/skills") return { items: [], skills: [] };
  if (pathname === "/settings/ai/stack") return { items: [], layers: [] };
  if (pathname === "/settings/plugins" || pathname.startsWith("/settings/plugins/")) {
    return { plugins: [], items: [] };
  }
  if (pathname === "/packs" || pathname.startsWith("/packs/")) return { items: [], counts: {} };
  if (pathname === "/flow-blocks" || pathname.startsWith("/flow-blocks/")) {
    return { items: [] };
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
    pathname.endsWith("/skills") ||
    pathname.endsWith("/roles") ||
    pathname.endsWith("/tools") ||
    pathname.endsWith("/turns") ||
    pathname.endsWith("/subscriptions") ||
    pathname.endsWith("/identities") ||
    pathname.endsWith("/requests") ||
    pathname.endsWith("/entries") ||
    pathname.endsWith("/controls") ||
    pathname.endsWith("/requirements") ||
    pathname.includes("/case-modules") ||
    pathname.includes("/import/sources")
  ) {
    return { items: [], count: 0, total: 0 };
  }
  return { items: [] };
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
    const projects = await listProjects(env.DB, gate.user!.user_id);
    // Workbench unwrapList looks for `projects` / `items`; also keep array-compatible.
    return envelope({ projects, items: projects, count: projects.length });
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
    const project_id = String(body.project_id || "");
    if (!project_id) return fail(400, "project_id required");
    try {
      const app = await createApp(
        env.DB,
        gate.user!.user_id,
        project_id,
        String(body.name || ""),
        String(body.platforms || body.platform || ""),
      );
      return envelope(app, "已创建");
    } catch (e) {
      return fail(400, e instanceof Error ? e.message : "创建失败");
    }
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

  // Match mino_nexus.api.rProjectCases get/put script-repo
  {
    const m = pathname.match(/^\/project\/([^/]+)\/script-repo$/);
    if (m) {
      const gate = await requireUser(request, env);
      if (gate.error) return gate.error;
      const projectId = decodeURIComponent(m[1]);
      if (!(await requireOwnedProject(env.DB, projectId, gate.user!.user_id))) {
        return fail(404, "没有这个项目");
      }
      if (method === "GET") {
        const row = await getRepo(env.DB, projectId);
        return envelope({
          url: publicUrl(row.url),
          branch: row.branch,
          updated_at: row.updated_at,
          workspace: workspacePlace(),
        });
      }
      if (method === "PUT") {
        const body = await readJson(request);
        try {
          const saved = await saveRepo(
            env.DB,
            projectId,
            String(body.url || ""),
            String(body.branch || ""),
          );
          const place = workspacePlace();
          // Hub does not clone; Scout pulls. empty error → Workbench toast「脚本仓库已保存」.
          return envelope(
            {
              url: saved.public_url,
              branch: saved.branch,
              updated_at: saved.updated_at,
              workspace: place,
            },
            "脚本仓库已保存",
          );
        } catch (e) {
          const msg = e instanceof Error ? e.message : "保存失败";
          if (msg === "script_repo_url_invalid") {
            return fail(400, msg);
          }
          return fail(400, msg);
        }
      }
    }
  }

  {
    const m = pathname.match(/^\/project\/([^/]+)\/(cases|case-modules|accounts|repo-apps|requirements|controls|device-app-sessions|resource-allocation-logs)$/);
    if (m && method === "GET") {
      const gate = await requireUser(request, env);
      if (gate.error) return gate.error;
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
      const id = decodeURIComponent(m[1]);
      const listed = await listAIProviders(env.DB, gate.user!.user_id);
      const providers = (listed as { providers?: Array<Record<string, unknown>> }).providers || [];
      const row = providers.find((p) => String(p.id) === id);
      if (!row) return fail(404, "没有这个服务商");
      if (!row.configured) return fail(422, "请先保存 API Key");
      // Persist a local validation mark — live upstream probe stays optional.
      await saveAIProvider(env.DB, gate.user!.user_id, id, {
        validation: { ok: true, checked_at: Date.now(), source: "saved_key" },
      });
      return envelope({ ok: true, provider_id: id }, "已确认密钥已保存");
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
      return envelope(await readNodeTools(env, nodeId, gate.user!.user_id));
    }
  }

  {
    const m = pathname.match(/^\/runtime\/nodes\/([^/]+)\/update-status$/);
    if (m && method === "GET") {
      const gate = await requireUser(request, env);
      if (gate.error) return gate.error;
      const nodeId = decodeURIComponent(m[1]);
      const scouts = (await listScouts(env.DB)) as Array<{
        scout_id?: string;
        online?: boolean;
        scout_version?: string;
      }>;
      const scout = scouts.find((s) => s.scout_id === nodeId);
      return envelope({
        node_id: nodeId,
        online: Boolean(scout?.online),
        running_version: scout?.online ? scout.scout_version || null : null,
        operation: null,
      });
    }
  }

  // Aggregate plugin / channel-program routes (mcp0 rRuntime).
  if (
    pathname === "/runtime/plugins/status" ||
    pathname === "/runtime/channel-programs/install" ||
    pathname === "/runtime/plugins/install"
  ) {
    const gate = await requireUser(request, env);
    if (gate.error) return gate.error;
    const handled = await handlePluginRoutes(
      request,
      env,
      pathname,
      method,
      gate.user!.user_id,
    );
    if (handled) return handled;
  }

  // Node remote control / plugins / tool-tasks / logs / claim — live Scout EXECUTE.
  if (pathname.startsWith("/runtime/nodes/")) {
    const gate = await requireUser(request, env);
    if (gate.error) return gate.error;
    const handled = await handleNodeControl(
      request,
      env,
      pathname,
      method,
      gate.user!.user_id,
    );
    if (handled) return handled;
    if (method === "GET") return envelope(softGet(pathname));
    return fail(501, "该节点接口尚未迁移到 Cloudflare hub");
  }

  // Assistant surfaces — exact Workbench contracts (never bare {}).
  if (pathname.startsWith("/assistant/")) {
    const gate = await requireUser(request, env);
    if (gate.error) return gate.error;
    const handled = await handleAssistant(request, env, pathname, gate.user!.user_id);
    if (handled) return handled;
  }

  // Device password / IME via Scout.
  if (pathname.startsWith("/device/")) {
    const gate = await requireUser(request, env);
    if (gate.error) return gate.error;
    const handled = await handleDeviceWrites(
      request,
      env,
      pathname,
      method,
      gate.user!.user_id,
    );
    if (handled) return handled;
    if (method === "GET") return envelope(softGet(pathname));
    return fail(501, "该设备接口尚未迁移到 Cloudflare hub");
  }

  // OAuth discovery + consent APIs (mcp0 rOAuth).
  {
    const oauth = await handleOAuth(request, env, pathname);
    if (oauth) return oauth;
  }

  // Remaining Workbench GETs: empty contract shapes so list pages render.
  // Unimplemented writes must fail — never pretend success.
  if (
    pathname.startsWith("/settings/") ||
    pathname.startsWith("/project/") ||
    pathname.startsWith("/packs") ||
    pathname.startsWith("/flow-blocks") ||
    pathname.startsWith("/nav-fsm/") ||
    pathname.startsWith("/releases/")
  ) {
    const gate = await requireUser(request, env);
    if (gate.error) return gate.error;
    if (method === "GET") return envelope(softGet(pathname));
    return fail(501, "该接口尚未迁移到 Cloudflare hub");
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
