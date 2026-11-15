/**
 * Confirmation cards for create / update / delete.
 * The client must accept text/event-stream. We send elicitation/create on that
 * stream and wait for the client's JSON-RPC response on a later POST.
 */

import type { Env } from "./env";

export type ToolResult = {
  isError: boolean;
  content: Array<{ type: string; text: string }>;
};

type JsonRpcId = string | number | null;

type Field = {
  name: string;
  title: string;
  description?: string;
  required?: boolean;
  default: string;
  maxLength?: number;
};

type Prepared =
  | { kind: "none" }
  | { kind: "error"; result: ToolResult }
  | {
      kind: "form";
      message: string;
      editable: boolean;
      fields: Field[];
      apply: (content: Record<string, unknown>) => { ok: true; args: Record<string, unknown> } | { ok: false; message: string };
    };

const CONFIRM_TOOLS = new Set([
  "project_create",
  "project_delete",
  "app_create",
  "app_delete",
  "scout_install_token",
  "scout_uninstall",
  "requirement_save",
  "project_script_repo_set",
  "repo_apps_sync",
  "resource_env_set",
  "resource_account_create",
  "resource_account_update",
  "resource_account_delete",
  "resource_account_import",
]);

const WAIT_MS = 120_000;
const POLL_MS = 400;

export function acceptsEventStream(request: Request): boolean {
  return (request.headers.get("accept") || "").includes("text/event-stream");
}

export function toolNeedsConfirm(name: string): boolean {
  return CONFIRM_TOOLS.has(name);
}

function text(value: unknown): string {
  return String(value ?? "").trim();
}

function field(name: string, title: string, value: unknown, extra: Partial<Field> = {}): Field {
  return { name, title, default: text(value), ...extra };
}

function choiceSchema() {
  return {
    type: "object",
    properties: {
      decision: {
        type: "string",
        title: "请选择",
        enum: ["确认执行"],
      },
    },
    required: ["decision"],
  };
}

function picked(content: Record<string, unknown>, name: string, fallback: unknown): string {
  if (!Object.prototype.hasOwnProperty.call(content, name) || content[name] == null) return text(fallback);
  return text(content[name]);
}

function contentOf(content: Record<string, unknown>, name: string): string {
  return text(content[name]);
}

function invalid(message: string): { ok: false; message: string } {
  return { ok: false, message };
}

export function cancelled(summary: string): ToolResult {
  return {
    isError: true,
    content: [{ type: "text", text: JSON.stringify({ ok: false, summary, data: { action: "cancel" } }) }],
  };
}

export async function prepareConfirm(
  env: Env,
  userId: string,
  name: string,
  args: Record<string, unknown>,
): Promise<Prepared> {
  if (!CONFIRM_TOOLS.has(name)) return { kind: "none" };

  if (name === "app_delete") {
    const appId = text(args.app_id);
    if (!appId) return { kind: "error", result: cancelled("缺少 app_id") };
    const row = await env.DB.prepare(
      `SELECT app_id, project_id, name FROM apps WHERE app_id = ? AND owner_user_id = ?`,
    )
      .bind(appId, userId)
      .first<{ app_id: string; project_id: string; name: string }>();
    if (!row) return { kind: "error", result: cancelled("没有这个应用") };
    return {
      kind: "form",
      editable: false,
      message: `删除应用「${row.name}」（${row.app_id}），项目 ${row.project_id}。只删除这条应用记录。`,
      fields: [],
      apply: () => ({ ok: true, args }),
    };
  }

  if (name === "project_delete") {
    const projectId = text(args.project_id);
    if (!projectId) return { kind: "error", result: cancelled("缺少 project_id") };
    const row = await env.DB.prepare(
      `SELECT project_id, name FROM projects WHERE project_id = ? AND owner_user_id = ?`,
    )
      .bind(projectId, userId)
      .first<{ project_id: string; name: string }>();
    if (!row) return { kind: "error", result: cancelled("没有这个项目") };
    const apps = await env.DB.prepare(
      `SELECT name FROM apps WHERE project_id = ? AND owner_user_id = ?`,
    )
      .bind(projectId, userId)
      .all<{ name: string }>();
    const names = (apps.results || []).map((app) => app.name).filter(Boolean);
    const appsText = names.length ? `，一并删除应用 ${names.join("、")}` : "";
    return {
      kind: "form",
      editable: false,
      message: `删除项目「${row.name}」（${row.project_id}）${appsText}。`,
      fields: [],
      apply: () => ({ ok: true, args }),
    };
  }

  if (name === "resource_env_set") {
    const projectId = text(args.project_id);
    if (!projectId) return { kind: "error", result: cancelled("缺少项目") };
    const row = await env.DB.prepare("SELECT project_id, name FROM projects WHERE project_id = ? AND owner_user_id = ?")
      .bind(projectId, userId)
      .first<{ project_id: string; name: string }>();
    if (!row) return { kind: "error", result: cancelled("没有这个项目") };
    const input = args.env && typeof args.env === "object" && !Array.isArray(args.env) ? args.env as Record<string, unknown> : {};
    const policies = Array.isArray(input.login_policies) ? input.login_policies.length : undefined;
    const policyHint = policies === undefined ? "" : `，登录策略 ${policies} 条`;
    return {
      kind: "form",
      editable: false,
      message: `保存项目「${row.name}」（${row.project_id}）的 Nexus 环境配置${policyHint}。`,
      fields: [],
      apply: () => ({ ok: true, args }),
    };
  }

  if (name === "resource_account_create") {
    const projectId = text(args.project_id);
    if (!projectId) return { kind: "error", result: cancelled("缺少项目") };
    const row = await env.DB.prepare("SELECT project_id, name FROM projects WHERE project_id = ? AND owner_user_id = ?")
      .bind(projectId, userId)
      .first<{ project_id: string; name: string }>();
    if (!row) return { kind: "error", result: cancelled("没有这个项目") };
    const identity = text(args.email || args.phone || args.username || args.display_name);
    return {
      kind: "form",
      editable: false,
      message: `向项目「${row.name}」（${row.project_id}）的账号池新增账号${identity ? `「${identity}」` : ""}。`,
      fields: [],
      apply: () => ({ ok: true, args }),
    };
  }

  if (name === "resource_account_update" || name === "resource_account_delete") {
    const projectId = text(args.project_id);
    const accountId = text(args.account_id);
    if (!projectId) return { kind: "error", result: cancelled("缺少项目") };
    if (!accountId) return { kind: "error", result: cancelled("缺少账号") };
    const row = await env.DB.prepare(
      `SELECT a.id, a.display_name, a.phone, a.email, a.username, p.name AS project_name
       FROM pool_accounts a JOIN projects p ON p.project_id = a.project_id
       WHERE a.id = ? AND a.project_id = ? AND a.owner_user_id = ? AND p.owner_user_id = ?`,
    )
      .bind(accountId, projectId, userId, userId)
      .first<{ id: string; display_name: string; phone: string; email: string; username: string; project_name: string }>();
    if (!row) return { kind: "error", result: cancelled("没有这个账号") };
    const identity = row.email || row.phone || row.username || row.display_name || row.id;
    return {
      kind: "form",
      editable: false,
      message: `${name === "resource_account_delete" ? "删除" : "修改"}项目「${row.project_name}」中的账号「${identity}」（${row.id}）。`,
      fields: [],
      apply: () => ({ ok: true, args }),
    };
  }

  if (name === "resource_account_import") {
    const projectId = text(args.project_id);
    if (!projectId) return { kind: "error", result: cancelled("缺少项目") };
    const row = await env.DB.prepare("SELECT project_id, name FROM projects WHERE project_id = ? AND owner_user_id = ?")
      .bind(projectId, userId)
      .first<{ project_id: string; name: string }>();
    if (!row) return { kind: "error", result: cancelled("没有这个项目") };
    const count = Array.isArray(args.rows) ? args.rows.length : 0;
    return {
      kind: "form",
      editable: false,
      message: `${args.preview === true ? "预览" : "导入"}项目「${row.name}」（${row.project_id}）的 ${count} 个测试账号。`,
      fields: [],
      apply: () => ({ ok: true, args }),
    };
  }

  if (name === "scout_install_token") {
    return {
      kind: "form",
      editable: false,
      message: "将为当前账号签发 15 分钟的 Scout 安装命令。确认后才能在本机安装。",
      fields: [],
      apply: () => ({ ok: true, args }),
    };
  }

  if (name === "scout_uninstall") {
    const nodeId = text(args.node_id);
    if (!nodeId) return { kind: "error", result: cancelled("缺少 node_id") };
    const row = await env.DB.prepare(
      `SELECT scout_id, host_json FROM scouts WHERE scout_id = ? AND owner_user_id = ?`,
    )
      .bind(nodeId, userId)
      .first<{ scout_id: string; host_json: string | null }>();
    if (!row) return { kind: "error", result: cancelled("没有这个节点") };
    let hostname = row.scout_id;
    if (row.host_json) {
      try {
        hostname = String((JSON.parse(row.host_json) as { hostname?: string }).hostname || hostname);
      } catch {
        hostname = row.scout_id;
      }
    }
    return {
      kind: "form",
      editable: false,
      message: `卸载 Scout「${hostname}」（${row.scout_id}），并删除它在当前账号下的绑定。`,
      fields: [],
      apply: () => ({ ok: true, args }),
    };
  }

  if (name === "project_create") {
    const fields = [
      field("name", "项目名称", args.name, { required: true, maxLength: 200 }),
      field("description", "说明", args.description, { maxLength: 2000 }),
    ];
    return {
      kind: "form",
      editable: true,
      message: "确认新建项目。名称和说明如下，选择「确认执行」。",
      fields,
      apply: (content) => {
        const projectName = picked(content, "name", args.name);
        const description = picked(content, "description", args.description);
        if (!projectName || projectName.length > 200) return invalid("请填写 1 到 200 字的项目名称");
        if (description.length > 2000) return invalid("说明最长 2000 字");
        return { ok: true, args: { ...args, name: projectName, description } };
      },
    };
  }

  if (name === "app_create") {
    const project_id = text(args.project_id);
    if (!project_id) return { kind: "error", result: cancelled("缺少项目") };
    return {
      kind: "form",
      editable: true,
      message: `确认新建应用。项目 ${project_id}。名称、平台和说明如下，选择「确认执行」。`,
      fields: [
        field("name", "应用名称", args.name, { required: true, maxLength: 200 }),
        field("platforms", "平台", args.platforms, {
          required: true,
          description: "逗号分隔：android、ios、web、mac、win",
        }),
        field("description", "说明", args.description, { maxLength: 2000 }),
      ],
      apply: (content) => {
        const appName = picked(content, "name", args.name);
        const platforms = picked(content, "platforms", args.platforms);
        const description = picked(content, "description", args.description);
        if (!appName || appName.length > 200) return invalid("请填写 1 到 200 字的应用名称");
        if (!platforms) return invalid("请填写平台");
        return { ok: true, args: { ...args, project_id, name: appName, platforms, description } };
      },
    };
  }

  if (name === "requirement_save") {
    const project_id = text(args.project_id);
    if (!project_id) return { kind: "error", result: cancelled("缺少项目") };
    return {
      kind: "form",
      editable: true,
      message: `确认保存需求。项目 ${project_id}。内容如下，选择「确认执行」。编号同时是脚本目录 tests/<编号>/。`,
      fields: [
        field("app_id", "应用", args.app_id),
        field("requirement_id", "需求编号", args.requirement_id, { required: true }),
        field("title", "名称", args.title, { required: true, maxLength: 200 }),
        field("source_url", "链接", args.source_url),
      ],
      apply: (content) => ({
        ok: true,
        args: {
          ...args,
          project_id,
          app_id: picked(content, "app_id", args.app_id),
          requirement_id: picked(content, "requirement_id", args.requirement_id),
          title: picked(content, "title", args.title),
          source_url: picked(content, "source_url", args.source_url),
        },
      }),
    };
  }

  if (name === "project_script_repo_set") {
    const project_id = text(args.project_id);
    if (!project_id) return { kind: "error", result: cancelled("缺少项目") };
    return {
      kind: "form",
      editable: true,
      message: `确认保存脚本仓库。项目 ${project_id}。地址和分支如下，选择「确认执行」。不要在地址里带令牌。`,
      fields: [
        field("url", "Git 地址", args.url, { required: true }),
        field("branch", "分支", args.branch),
      ],
      apply: (content) => {
        const url = picked(content, "url", args.url);
        if (!url) return invalid("请填写 Git 地址");
        return { ok: true, args: { ...args, project_id, url, branch: picked(content, "branch", args.branch) } };
      },
    };
  }

  const apps = args.apps && typeof args.apps === "object" ? args.apps : {};
  const remove = Array.isArray(args.remove) ? args.remove.map((item) => text(item)).filter(Boolean) : [];
  const project_id = text(args.project_id);
  if (!project_id) return { kind: "error", result: cancelled("缺少项目") };
  return {
    kind: "form",
    editable: true,
    message: `确认同步脚本仓库的应用清单。项目 ${project_id}。内容如下，选择「确认执行」。`,
    fields: [
      field("revision", "提交", args.revision),
      field("branch", "分支", args.branch),
      field("apps_json", "应用 JSON", JSON.stringify(apps), {
        description: "本次新增或修改的应用。身份字段只有 package、bundle_id、url。",
      }),
      field("remove_text", "删除的应用 key", remove.join(",")),
    ],
    apply: (content) => {
      let parsed: unknown = apps;
      const raw = Object.prototype.hasOwnProperty.call(content, "apps_json") ? contentOf(content, "apps_json") : "";
      if (raw) {
        try {
          parsed = JSON.parse(raw);
        } catch {
          return invalid("应用 JSON 无法解析");
        }
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return invalid("应用 JSON 必须是对象");
      }
      const removeKeys = (Object.prototype.hasOwnProperty.call(content, "remove_text")
        ? contentOf(content, "remove_text")
        : remove.join(","))
        .split(/[\s,]+/)
        .map((item) => item.trim())
        .filter(Boolean);
      return {
        ok: true,
        args: {
          ...args,
          project_id,
          revision: picked(content, "revision", args.revision),
          branch: picked(content, "branch", args.branch),
          apps: parsed,
          remove: removeKeys,
        },
      };
    },
  };
}

async function remember(env: Env, userId: string, id: string): Promise<void> {
  await env.DB.prepare(`DELETE FROM mcp_elicitations WHERE created_at_ms < ?`)
    .bind(Date.now() - 5 * 60_000)
    .run();
  await env.DB.prepare(
    `INSERT INTO mcp_elicitations (id, user_id, result_json, created_at_ms) VALUES (?, ?, NULL, ?)`,
  )
    .bind(id, userId, Date.now())
    .run();
}

export async function saveElicitationResult(
  env: Env,
  userId: string,
  id: string,
  result: unknown,
): Promise<boolean> {
  const updated = await env.DB.prepare(
    `UPDATE mcp_elicitations SET result_json = ? WHERE id = ? AND user_id = ? AND result_json IS NULL`,
  )
    .bind(JSON.stringify(result ?? { action: "cancel" }), id, userId)
    .run();
  return Number(updated.meta.changes || 0) > 0;
}

async function waitForResult(env: Env, id: string): Promise<unknown | undefined> {
  const deadline = Date.now() + WAIT_MS;
  while (Date.now() < deadline) {
    const row = await env.DB.prepare(`SELECT result_json FROM mcp_elicitations WHERE id = ?`)
      .bind(id)
      .first<{ result_json: string | null }>();
    if (row?.result_json) return JSON.parse(row.result_json);
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
  }
  return undefined;
}

function confirmMessage(prepared: Extract<Prepared, { kind: "form" }>): string {
  const lines = prepared.fields.filter((item) => item.default).map((item) => `${item.title}：${item.default}`);
  return lines.length ? `${prepared.message}\n${lines.join("\n")}` : prepared.message;
}

function sse(message: unknown): Uint8Array {
  return new TextEncoder().encode(`event: message\ndata: ${JSON.stringify(message)}\n\n`);
}

export async function streamConfirmedCall(
  env: Env,
  userId: string,
  rpcId: JsonRpcId,
  protocol: string,
  prepared: Extract<Prepared, { kind: "form" }>,
  execute: (args: Record<string, unknown>) => Promise<ToolResult>,
): Promise<Response> {
  const elicitId = crypto.randomUUID();
  await remember(env, userId, elicitId);
  const body = new ReadableStream({
    async start(controller) {
      const finish = (result: ToolResult) => {
        controller.enqueue(sse({ jsonrpc: "2.0", id: rpcId ?? null, result }));
        controller.close();
      };
      try {
        controller.enqueue(
          sse({
            jsonrpc: "2.0",
            id: elicitId,
            method: "elicitation/create",
            params: {
              mode: "form",
              message: confirmMessage(prepared),
              requestedSchema: choiceSchema(),
            },
          }),
        );
        const answer = (await waitForResult(env, elicitId)) as { action?: string; content?: Record<string, unknown> } | undefined;
        await env.DB.prepare(`DELETE FROM mcp_elicitations WHERE id = ?`).bind(elicitId).run();
        if (!answer || answer.action !== "accept" || text(answer.content?.decision) !== "确认执行") {
          finish(cancelled(answer?.action === "decline" ? "已拒绝" : "已取消"));
          return;
        }
        const applied = prepared.apply({});
        if (!applied.ok) {
          finish(cancelled(applied.message));
          return;
        }
        finish(await execute(applied.args));
      } catch (error) {
        finish(cancelled(error instanceof Error ? error.message : "确认失败"));
      }
    },
  });
  return new Response(body, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      "MCP-Protocol-Version": protocol,
    },
  });
}
