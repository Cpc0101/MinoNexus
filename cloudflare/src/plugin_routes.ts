/**
 * Aggregate MCP/Workbench routes from mino_nexus.api.rRuntime:
 * GET  /runtime/plugins/status
 * POST /runtime/channel-programs/install
 * POST /runtime/plugins/install
 */

import type { Env } from "./env";
import { listScouts } from "./db";
import { readNodeTools } from "./node_tools";
import { handleNodeControl } from "./node_control";

const CHANNEL_PROGRAMS = new Set(["chromium", "adb.keyboard", "appium"]);
const INSTALL_PLUGINS = new Set(["cli.feishu", "cli.meego"]);

function envelope(data: unknown, msg = "", status = 200): Response {
  const body: Record<string, unknown> = {
    code: status === 202 ? 200 : status,
    ok: status < 400,
    data,
  };
  if (msg) body.msg = msg;
  return Response.json(body, {
    status: status === 202 ? 202 : status < 400 ? 200 : status,
  });
}

function fail(status: number, msg: string): Response {
  return Response.json({ code: status, ok: false, msg }, { status });
}

function compactComponent(raw: unknown): Record<string, unknown> {
  if (!raw || typeof raw !== "object") return {};
  const src = raw as Record<string, unknown>;
  const keys = [
    "tool_id",
    "label",
    "purpose",
    "state",
    "detail",
    "installable",
    "updatable",
    "removable",
    "version",
    "plugin_class",
    "plugin_id",
  ];
  const out: Record<string, unknown> = {};
  for (const key of keys) {
    if (key in src) out[key] = src[key];
  }
  return out;
}

function compactTask(raw: unknown): Record<string, unknown> {
  if (!raw || typeof raw !== "object") return {};
  const src = raw as Record<string, unknown>;
  const keys = [
    "task_id",
    "operation_id",
    "tool_id",
    "action",
    "state",
    "active",
    "stage",
    "label",
    "percent",
    "error",
    "created_at",
    "updated_at",
  ];
  const out: Record<string, unknown> = {};
  for (const key of keys) {
    if (key in src) out[key] = src[key];
  }
  return out;
}

function pluginStatusRow(
  row: Record<string, unknown>,
  tools: Record<string, unknown> | null,
  toolsError: string,
): Record<string, unknown> {
  const pluginToolIds = new Set(["cli.feishu", "cli.meego"]);
  const components = ((tools?.items as unknown[]) || [])
    .map(compactComponent)
    .filter((item) => pluginToolIds.has(String(item.tool_id || "")));
  const byPlugin = new Map<string, Record<string, unknown>>();
  for (const item of components) {
    if (item.plugin_class && item.plugin_id) {
      byPlugin.set(`${item.plugin_class}:${item.plugin_id}`, item);
    }
  }
  const rawPlugins = Array.isArray(row.plugins) ? row.plugins : null;
  const plugins: Record<string, unknown>[] = [];
  for (const raw of rawPlugins || []) {
    if (!raw || typeof raw !== "object") continue;
    const p = raw as Record<string, unknown>;
    const kind = String(p.class || "");
    const pluginId = String(p.id || "");
    if (!kind || !pluginId) continue;
    const component = byPlugin.get(`${kind}:${pluginId}`) || {};
    const installed = Boolean(p.installed);
    plugins.push({
      class: kind,
      id: pluginId,
      label: String(p.label || pluginId),
      installed,
      configured: Boolean(p.configured),
      import_blocked: Boolean(p.import_blocked),
      installable: component.installable,
      updatable: component.updatable,
      removable: component.removable,
      install_state: component.state || (installed ? "installed" : "missing"),
      version: component.version,
      detail: component.detail || (installed ? "" : "需要安装后才能保存配置"),
      status: p.status && typeof p.status === "object" ? p.status : undefined,
    });
  }
  return {
    node_id: String(row.node_id || row.scout_id || ""),
    hostname: String(
      (row.host && typeof row.host === "object"
        ? (row.host as Record<string, unknown>).hostname
        : "") ||
        row.hostname ||
        "",
    ),
    online: Boolean(row.online),
    plugins_known: rawPlugins !== null,
    plugins_ready: row.plugins_ready,
    plugin_warning: String(row.plugin_warning || ""),
    plugin_job: {},
    plugins,
    components,
    tasks: ((tools?.tasks as unknown[]) || [])
      .map(compactTask)
      .filter((task) => pluginToolIds.has(String(task.tool_id || ""))),
    tools_checked: tools !== null,
    tools_supported: Boolean(tools?.supported),
    tools_contract_version: Number(tools?.contract_version || 0),
    tools_stale: Boolean(tools?.stale),
    tools_error: toolsError,
  };
}

async function submitTask(
  env: Env,
  userId: string,
  nodeId: string,
  operationId: string,
  toolId: string,
  action: string,
): Promise<{ ok: boolean; status: number; data: Record<string, unknown>; msg: string }> {
  const path = `/runtime/nodes/${encodeURIComponent(nodeId)}/tool-tasks`;
  const req = new Request(`https://nexus.minocc.top${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      operation_id: operationId,
      tool_id: toolId,
      action,
    }),
  });
  const res = await handleNodeControl(req, env, path, "POST", userId);
  if (!res) return { ok: false, status: 500, data: {}, msg: "未处理" };
  const body = (await res.json()) as {
    code?: number;
    ok?: boolean;
    msg?: string;
    data?: Record<string, unknown>;
  };
  return {
    ok: Boolean(body.ok) && res.status < 400,
    status: res.status === 202 ? 202 : Number(body.code || res.status),
    data: (body.data || {}) as Record<string, unknown>,
    msg: String(body.msg || ""),
  };
}

async function refreshTask(
  env: Env,
  userId: string,
  nodeId: string,
  taskId: string,
): Promise<Record<string, unknown> | null> {
  const path = `/runtime/nodes/${encodeURIComponent(nodeId)}/tool-tasks/${encodeURIComponent(taskId)}`;
  const req = new Request(`https://nexus.minocc.top${path}`, { method: "GET" });
  const res = await handleNodeControl(req, env, path, "GET", userId);
  if (!res || res.status >= 400) return null;
  const body = (await res.json()) as { data?: Record<string, unknown> };
  return (body.data || null) as Record<string, unknown> | null;
}

function randomOpId(): string {
  const buf = new Uint8Array(16);
  crypto.getRandomValues(buf);
  return [...buf].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function handlePluginRoutes(
  request: Request,
  env: Env,
  pathname: string,
  method: string,
  userId: string,
): Promise<Response | null> {
  if (pathname === "/runtime/plugins/status" && method === "GET") {
    const url = new URL(request.url);
    const nodeId = String(url.searchParams.get("node_id") || "").trim();
    const scouts = (await listScouts(env.DB)) as Array<Record<string, unknown>>;
    let rows: Array<Record<string, unknown>> = scouts
      .filter((s) => !s.owner_user_id || s.owner_user_id === userId)
      .map((s) => ({
        ...s,
        node_id: s.scout_id,
        online: Boolean(s.online),
      }));
    if (nodeId) {
      rows = rows.filter((r) => String(r.node_id || "") === nodeId);
      if (!rows.length) return fail(404, "没有这个节点");
    }
    const items = [];
    for (const row of rows) {
      const nid = String(row.node_id || "");
      let tools: Record<string, unknown> | null = null;
      let toolsError = "";
      if (nodeId && nid && String(row.owner_user_id || "") === userId) {
        try {
          tools = await readNodeTools(env, nid, userId);
        } catch {
          toolsError = "读取下载中心状态失败";
        }
      }
      items.push(pluginStatusRow(row, tools, toolsError));
    }
    return envelope({ items, count: items.length });
  }

  if (pathname === "/runtime/channel-programs/install" && method === "POST") {
    let body: Record<string, unknown> = {};
    try {
      body = (await request.json()) as Record<string, unknown>;
    } catch {
      body = {};
    }
    const node_id = String(body.node_id || "").trim();
    const tool_id = String(body.tool_id || "").trim();
    const action = String(body.action || "install").trim() || "install";
    if (!CHANNEL_PROGRAMS.has(tool_id)) return fail(422, "unsupported_tool_action");
    let operation_id = String(body.operation_id || "").trim() || randomOpId();
    if (!/^[A-Za-z0-9_-]{8,100}$/.test(operation_id)) {
      return fail(422, "operation_id 必须为 8-100 位字母、数字、下划线或短横线");
    }
    const submitted = await submitTask(env, userId, node_id, operation_id, tool_id, action);
    if (!submitted.ok && submitted.status >= 400) {
      return fail(submitted.status, submitted.msg || "提交失败");
    }
    const task = submitted.data;
    return envelope(
      {
        task_id: String(task.task_id || ""),
        state: String(task.state || ""),
        active: Boolean(task.active),
        tool_id,
        action,
        task,
      },
      "",
      202,
    );
  }

  if (pathname === "/runtime/plugins/install" && method === "POST") {
    let body: Record<string, unknown> = {};
    try {
      body = (await request.json()) as Record<string, unknown>;
    } catch {
      body = {};
    }
    const node_id = String(body.node_id || "").trim();
    const plugin = String(body.plugin || "").trim();
    const action = String(body.action || "install").trim() || "install";
    const wait_ms = Math.min(Math.max(Number(body.wait_ms || 0), 0), 60_000);
    if (!INSTALL_PLUGINS.has(plugin)) return fail(422, "unsupported_tool_action");
    let operation_id = String(body.operation_id || "").trim() || randomOpId();
    if (!/^[A-Za-z0-9_-]{8,100}$/.test(operation_id)) {
      return fail(422, "operation_id 必须为 8-100 位字母、数字、下划线或短横线");
    }
    const submitted = await submitTask(env, userId, node_id, operation_id, plugin, action);
    if (!submitted.ok && submitted.status >= 400) {
      return fail(submitted.status, submitted.msg || "提交失败");
    }
    let task = submitted.data;
    const deadline = Date.now() + wait_ms;
    while (
      wait_ms > 0 &&
      ["pending", "running", "unknown"].includes(String(task.state || "")) &&
      Date.now() < deadline
    ) {
      await new Promise((r) => setTimeout(r, Math.min(1000, deadline - Date.now())));
      const refreshed = await refreshTask(env, userId, node_id, String(task.task_id || ""));
      if (refreshed) task = refreshed;
      else break;
    }
    const state = String(task.state || "");
    let message = action === "install" ? "已提交安装任务" : "已提交更新任务";
    if (state === "succeeded") {
      message = action === "install" ? "插件安装完成" : "插件更新完成";
    } else if (state === "failed") {
      message = "插件任务失败，请查看任务 error";
    } else if (state === "unknown") {
      message = "插件任务结果待确认，请按 task_id 查询";
    }
    return envelope(
      {
        task_id: String(task.task_id || ""),
        state,
        active: Boolean(task.active),
        plugin,
        action,
        task,
      },
      message,
      202,
    );
  }

  return null;
}
