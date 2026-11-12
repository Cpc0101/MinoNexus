/**
 * MCP endpoint — auth and tool table match mino_nexus.api.rMcp + prompts/assistant/tools.
 * Tools dispatch to the same HTTP routes as mcp0.0.1 assistant tool YAML `route` fields.
 */

import type { Env } from "./env";
import { listProjects, createApp } from "./auth";
import { listDevices, listScouts } from "./db";
import {
  ACCESS_PREFIX,
  OAuthPage,
  canonicalResource,
  publicOrigin,
  resolveAccessToken,
} from "./mcp_oauth";
import { resolveMcpToken } from "./mcp_tokens";
import { handlePluginRoutes } from "./plugin_routes";
import { startRun } from "./case_runner";
import { getRepoApps, saveRepoApps } from "./repo_apps";
import { getRun, toTaskJson } from "./run_store";
import { requireOwnedProject, saveRepo } from "./script_repo";

type JsonRpcId = string | number | null;

type JsonRpcRequest = {
  jsonrpc?: string;
  id?: JsonRpcId;
  method?: string;
  params?: Record<string, unknown>;
};

const PROTOCOL = "2025-03-26";

/** Tool specs copied from mino_nexus/prompts/assistant/tools/*.yaml (mcp surface). */
const TOOLS = [
  {
    name: "agent_guide",
    title: "给其他 agent 的使用说明",
    description:
      "外部 agent 使用 Mino 时先调用。返回工具怎么分组调用，以及脚本、Git、设备分别由谁做。",
    inputSchema: { type: "object", properties: {} },
    annotations: {
      title: "给其他 agent 的使用说明",
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: "project_list",
    title: "项目与应用列表",
    description: "列出所有项目和其下的应用。",
    inputSchema: { type: "object", properties: {} },
    annotations: {
      title: "项目与应用列表",
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: "scout_list",
    title: "查询 Scout 节点",
    description: "列出当前账号可见的 Scout 节点，含 node_id、在线状态、上报版本。",
    inputSchema: { type: "object", additionalProperties: false, properties: {} },
    annotations: {
      title: "查询 Scout 节点",
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: "device_list",
    title: "兼容设备列表",
    description: "实时查询设备。status 为 online/offline。",
    inputSchema: {
      type: "object",
      properties: {
        only_online: { type: "boolean" },
        project_id: { type: "string" },
        app_id: { type: "string" },
        env_surface: { type: "string" },
      },
    },
    annotations: {
      title: "兼容设备列表",
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: "app_create",
    title: "创建应用",
    description: "在项目下创建一个应用，返回它的 id。",
    inputSchema: {
      type: "object",
      properties: {
        project_id: { type: "string" },
        name: { type: "string" },
        description: { type: "string" },
        platforms: {
          type: "string",
          description: "逗号分隔，取值 android / ios / web / mac / win",
        },
      },
      required: ["project_id", "name", "platforms"],
    },
    annotations: {
      title: "创建应用",
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: false,
      openWorldHint: false,
    },
  },
  {
    name: "plugin_status",
    title: "查看插件安装状态",
    description:
      "查看本人 Scout 节点上的插件是否已安装、是否已配置，以及下载中心里的安装/更新能力。",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      properties: { node_id: { type: "string" } },
      required: [],
    },
    annotations: {
      title: "查看插件安装状态",
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: "node_channel_program",
    title: "安装或更新渠道程序",
    description:
      "把安装或更新渠道程序转成该用户当前在线节点的下载中心请求。允许 chromium、adb.keyboard、appium。",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      properties: {
        node_id: { type: "string", minLength: 1, maxLength: 128 },
        tool_id: { type: "string", enum: ["chromium", "adb.keyboard", "appium"] },
        action: { type: "string", enum: ["install", "update"], default: "install" },
        operation_id: { type: "string", maxLength: 100 },
      },
      required: ["node_id", "tool_id"],
    },
    annotations: {
      title: "安装或更新渠道程序",
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: false,
      openWorldHint: false,
    },
  },
  {
    name: "plugin_install",
    title: "安装或更新插件",
    description:
      "在本人 Scout 节点上提交插件安装或更新任务。仅支持 cli.feishu、cli.meego。",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      properties: {
        node_id: { type: "string", minLength: 1, maxLength: 128 },
        plugin: { type: "string", enum: ["cli.feishu", "cli.meego"] },
        action: { type: "string", enum: ["install", "update"], default: "install" },
        operation_id: { type: "string", maxLength: 100 },
        wait_ms: { type: "integer", minimum: 0, maximum: 60000, default: 0 },
      },
      required: ["node_id", "plugin"],
    },
    annotations: {
      title: "安装或更新插件",
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: false,
      openWorldHint: false,
    },
  },
  {
    name: "case_list",
    title: "项目需求列表",
    description:
      "列出项目已登记的需求：id、名称、渠道链接。这里没有用例正文。编写脚本前读链接，跑的时候用 case_run 的 script_targets。",
    inputSchema: {
      type: "object",
      properties: {
        project_id: { type: "string" },
      },
      required: ["project_id"],
    },
    annotations: {
      title: "项目用例列表",
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: "case_run",
    title: "跑用例",
    description:
      "在指定设备上执行脚本，返回任务 id。优先传 script_targets：[{requirement_id, case_ids}]。case_ids 为空则执行 tests/<需求ID>/ 下的全部脚本；指定了用例 ID 时路径是 tests/<需求ID>/<用例ID>.py。revision 填已 push 的 commit，不传则 Scout 取分支最新。",
    inputSchema: {
      type: "object",
      properties: {
        client_request_id: { type: "string", maxLength: 160 },
        project_id: { type: "string" },
        app_id: { type: "string" },
        env_surface: { type: "string" },
        env_profile: { type: "string" },
        sn: { type: "string" },
        case_ids: { type: "array", items: { type: "string" } },
        script_targets: {
          type: "array",
          items: {
            type: "object",
            properties: {
              requirement_id: { type: "string" },
              case_ids: { type: "array", items: { type: "string" } },
            },
            required: ["requirement_id"],
          },
        },
        revision: { type: "string" },
      },
      required: ["app_id"],
    },
    annotations: {
      title: "跑用例",
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: false,
      openWorldHint: false,
    },
  },
  {
    name: "requirement_list",
    title: "项目需求列表",
    description: "列出项目已登记的需求：id、名称、渠道链接。与 case_list 相同。",
    inputSchema: {
      type: "object",
      properties: { project_id: { type: "string" } },
      required: ["project_id"],
    },
    annotations: {
      title: "项目需求列表",
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: "requirement_save",
    title: "登记需求",
    description: "保存需求 ID、名称和一条渠道链接。ID 同时是脚本目录 tests/<需求ID>/。",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      properties: {
        project_id: { type: "string" },
        app_id: { type: "string" },
        requirement_id: { type: "string" },
        title: { type: "string" },
        source_url: { type: "string" },
      },
      required: ["project_id", "requirement_id", "title"],
    },
    annotations: {
      title: "登记需求",
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: "project_script_repo_set",
    title: "设置项目脚本仓库",
    description:
      "保存项目的脚本仓库地址和分支。Nexus 只记地址，不拉取仓库，不保存 Git 令牌；Scout 在本机用自己的 git 凭据拉取。",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      properties: {
        project_id: { type: "string", minLength: 1 },
        url: { type: "string", description: "http/https/ssh/git 地址；不要带令牌" },
        branch: { type: "string" },
      },
      required: ["project_id", "url"],
    },
    annotations: {
      title: "设置项目脚本仓库",
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: "repo_apps_sync",
    title: "同步脚本仓库的应用清单",
    description:
      "脚本仓库根目录 config.yaml 的 apps 改动并 push 后必须调用。revision 填 push 后的 commit，branch 填分支。差异更新：apps 只填这次新增或修改的应用（取自 config.yaml，按 key 整项替换；每个应用要有 platform，身份字段只有 package / bundle_id / url，可带 envs；不要传 app_id、不要传 otp）。未带 app_id 时 Nexus 按 key 对齐或自动创建应用并回填。删除的应用 key 填进 remove；没出现的应用保持不变。不能含账号密码或 otp。Nexus 不读仓库，凭此知道项目能测哪些应用。",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      properties: {
        project_id: { type: "string", minLength: 1 },
        revision: { type: "string", description: "push 后的 commit" },
        branch: { type: "string" },
        apps: {
          type: "object",
          description:
            "本次新增或修改的应用（只含 platform 与 package/bundle_id/url/envs；不要 otp；app_id 可省略）",
        },
        remove: {
          type: "array",
          items: { type: "string" },
          description: "本次从 config.yaml 删除的应用 key",
        },
      },
      required: ["project_id"],
    },
    annotations: {
      title: "同步脚本仓库的应用清单",
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: "repo_apps_get",
    title: "查看脚本仓库的应用清单",
    description:
      "读取 Nexus 记录的项目脚本仓库应用清单（含同步的 commit、分支、同步人和时间）。没有同步过则 apps 为空。",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      properties: { project_id: { type: "string", minLength: 1 } },
      required: ["project_id"],
    },
    annotations: {
      title: "查看脚本仓库的应用清单",
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: "task_get",
    title: "任务详情",
    description: "查看一次跑批任务的结果和失败原因。",
    inputSchema: {
      type: "object",
      properties: { task_id: { type: "string" } },
      required: ["task_id"],
    },
    annotations: {
      title: "任务详情",
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
] as const;

function rpcResult(id: JsonRpcId | undefined, result: unknown): Response {
  return Response.json({ jsonrpc: "2.0", id: id ?? null, result });
}

function rpcError(id: JsonRpcId | undefined, code: number, message: string): Response {
  return Response.json({
    jsonrpc: "2.0",
    id: id ?? null,
    error: { code, message },
  });
}

function unauthorized(request: Request, invalid: boolean): Response {
  const origin = publicOrigin(new URL(request.url).origin);
  const metadata = origin + "/.well-known/oauth-protected-resource/mcp";
  let challenge = `Bearer resource_metadata="${metadata}", scope="mcp"`;
  let detail = "需要授权";
  if (invalid) {
    challenge += ', error="invalid_token", error_description="The access token is invalid or expired"';
    detail = "令牌无效或已过期";
  }
  return Response.json(
    { detail },
    {
      status: 401,
      headers: { "WWW-Authenticate": challenge, "Cache-Control": "no-store" },
    },
  );
}

async function authenticate(
  request: Request,
  env: Env,
): Promise<{ userId: string } | Response> {
  const raw = request.headers.get("authorization") || "";
  const token = raw.toLowerCase().startsWith("bearer ") ? raw.slice(7).trim() : "";
  if (!token) return unauthorized(request, false);

  // Keep shared bearer for smoke tests only (tools/list/initialize).
  if (env.MCP_BEARER_TOKEN && token === env.MCP_BEARER_TOKEN) {
    return { userId: "system:probe" };
  }

  if (token.startsWith(ACCESS_PREFIX)) {
    try {
      const uid = await resolveAccessToken(
        env.DB,
        token,
        canonicalResource(new URL(request.url).origin),
      );
      return uid ? { userId: uid } : unauthorized(request, true);
    } catch (exc) {
      if (exc instanceof OAuthPage) return unauthorized(request, true);
      throw exc;
    }
  }

  if (token.startsWith("mino_mcp_")) {
    const uid = await resolveMcpToken(env.DB, token);
    return uid ? { userId: uid } : unauthorized(request, true);
  }

  return unauthorized(request, true);
}

function agentGuidePayload(): Record<string, unknown> {
  // Match mino_nexus.assistant.agent_guide.agent_guide()
  return {
    channel: "Mino MCP。工具名里的点写成下划线，例如 agent.guide 调用为 agent_guide。",
    first_call: "agent_guide",
    boundaries: [
      "脚本文本只由 agent 写在本机脚本仓并通过 Git 推送；MCP 只做查询、同步元数据与触发执行，不写代码。各仓库分工与 Scout 安装见 agent 技能 overview。",
      "改了 config.yaml 的 apps 后必须 push 并调用 repo_apps_sync（只传 platform 与包名/URL，不要传 otp；可不传 app_id，Nexus 按 key 对齐或自动创建应用）；选应用前可先调 repo_apps_get。Nexus 不保存 Git 令牌，也不拉取脚本仓库。",
      "开跑后整份脚本在 Scout 执行。账号、步骤和失败直接写入 Nexus。验证码优先读脚本仓 otp.code，否则要人工提供；模型在 Scout 本机完成。脚本里不要自己连接 adb、WDA、Playwright 或 Appium。",
    ],
    workbench: {
      url: "https://workbench.minocc.top",
      login_failure: "无法连接服务器",
    },
    flows: [
      { name: "确认能操作哪些项目", tools: ["project_list"] },
      {
        name: "编写或修改用例脚本",
        tools: [
          "project_list",
          "case_list",
          "device_list",
          "case_run",
          "task_get",
          "session_events",
          "session_screenshot",
        ],
        then: "使用 write-test-cases。先选一台 executable 设备。",
      },
      {
        name: "按需求链接编写并执行脚本",
        tools: ["case_list", "device_list", "case_run", "task_get"],
        then: "case_list 返回需求 id、名称和链接。从链接读用例，写成 tests/<需求ID>/<用例ID>.py 并 push。开跑前 plugin_status，缺的渠道程序先装完。case_run 传 script_targets。执行与汇报使用 agent 技能 run-test-cases。",
      },
      {
        name: "让节点安装渠道程序",
        tools: ["node_channel_program"],
        then: "只转发给该节点下载中心已有的安装请求。Nexus 不下载安装包，也不接受临时地址。",
      },
      {
        name: "查看一次执行",
        tools: ["task_list", "task_get", "session_list", "session_events", "session_screenshot"],
      },
      {
        name: "看资源够不够",
        tools: ["resource_overview", "resource_device_list", "resource_account_list"],
      },
    ],
  };
}

function mapDevices(raw: unknown[]): Record<string, unknown>[] {
  return raw.map((row) => {
    const d = (row || {}) as Record<string, unknown>;
    const online = Boolean(d.online);
    return {
      sn: String(d.device_id || ""),
      device_id: d.device_id,
      scout_id: d.scout_id,
      node_id: d.scout_id,
      platform: d.platform || undefined,
      status: online ? "online" : "offline",
      ready: Boolean(d.ready),
      executable: online && Boolean(d.ready),
    };
  });
}

function mapScouts(raw: unknown[], userId: string): Record<string, unknown>[] {
  return (raw as Array<Record<string, unknown>>)
    .filter((s) => !s.owner_user_id || s.owner_user_id === userId || userId === "system:probe")
    .map((s) => ({
      node_id: s.scout_id,
      scout_id: s.scout_id,
      online: Boolean(s.online),
      scout_version: s.scout_version || undefined,
      hostname:
        (s.host && typeof s.host === "object"
          ? (s.host as Record<string, unknown>).hostname
          : undefined) || s.scout_id,
      owner_user_id: s.owner_user_id || undefined,
      plugins_ready: s.plugins_ready,
      tool_tasks_version: Number(s.tool_tasks_version || 0),
    }));
}

async function callHttpTool(
  env: Env,
  userId: string,
  method: string,
  path: string,
  body?: Record<string, unknown>,
): Promise<{ ok: boolean; status: number; summary: string; data: unknown }> {
  const url = new URL(path, "https://nexus.minocc.top");
  const req = new Request(url.toString(), {
    method,
    headers: { "content-type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const handled = await handlePluginRoutes(req, env, url.pathname, method, userId);
  if (!handled) {
    return { ok: false, status: 404, summary: "没有这个工具路由", data: {} };
  }
  const json = (await handled.json()) as {
    ok?: boolean;
    code?: number;
    msg?: string;
    data?: unknown;
  };
  return {
    ok: Boolean(json.ok) && handled.status < 400,
    status: Number(json.code || handled.status),
    summary: String(json.msg || ""),
    data: json.data,
  };
}

async function callTool(
  env: Env,
  userId: string,
  name: string,
  args: Record<string, unknown>,
  mutationScope: string,
  waitUntil: (promise: Promise<unknown>) => void,
): Promise<{ isError: boolean; content: Array<{ type: string; text: string }> }> {
  const pack = (ok: boolean, summary: string, data: unknown, status = 0) => {
    const body: Record<string, unknown> = { ok, summary, data };
    if (!ok && status) {
      const code =
        status === 401
          ? "auth_required"
          : status === 403
            ? "forbidden"
            : status === 404
              ? "not_found"
              : status === 409
                ? "conflict"
                : status === 422 || status === 400
                  ? "invalid_argument"
                  : status === 501
                    ? "tool_failed"
                    : "tool_failed";
      body.code = code;
      body.retryable = code === "conflict";
    }
    return {
      isError: !ok,
      content: [{ type: "text", text: JSON.stringify(body) }],
    };
  };

  if (userId === "system:probe") {
    return pack(false, "probe token 仅允许 initialize/ping/tools/list", {}, 403);
  }

  if (name === "agent_guide") {
    return pack(true, "", agentGuidePayload());
  }

  if (name === "project_list") {
    return pack(true, "", await listProjects(env.DB, userId));
  }

  if (name === "scout_list") {
    return pack(true, "", {
      nodes: mapScouts(await listScouts(env.DB), userId),
    });
  }

  if (name === "device_list") {
    const onlyOnline = args.only_online !== false;
    return pack(true, "", mapDevices(await listDevices(env.DB, onlyOnline)));
  }

  if (name === "app_create") {
    try {
      const app = await createApp(
        env.DB,
        userId,
        String(args.project_id || ""),
        String(args.name || ""),
        String(args.platforms || ""),
      );
      return pack(true, "已创建", app);
    } catch (err) {
      return pack(false, err instanceof Error ? err.message : "创建失败", {}, 400);
    }
  }

  if (name === "plugin_status") {
    const qs = args.node_id
      ? `?node_id=${encodeURIComponent(String(args.node_id))}`
      : "";
    const out = await callHttpTool(env, userId, "GET", `/runtime/plugins/status${qs}`);
    return pack(out.ok, out.summary, out.data, out.status);
  }

  if (name === "node_channel_program") {
    const operation_id =
      String(args.operation_id || "").trim() ||
      (mutationScope
        ? [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(mutationScope)))]
            .map((b) => b.toString(16).padStart(2, "0"))
            .join("")
            .slice(0, 32)
        : "");
    const out = await callHttpTool(env, userId, "POST", "/runtime/channel-programs/install", {
      node_id: args.node_id,
      tool_id: args.tool_id,
      action: args.action || "install",
      operation_id,
    });
    return pack(out.ok, out.summary, out.data, out.status);
  }

  if (name === "plugin_install") {
    const operation_id =
      String(args.operation_id || "").trim() ||
      (mutationScope
        ? [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(mutationScope)))]
            .map((b) => b.toString(16).padStart(2, "0"))
            .join("")
            .slice(0, 32)
        : "");
    const out = await callHttpTool(env, userId, "POST", "/runtime/plugins/install", {
      node_id: args.node_id,
      plugin: args.plugin,
      action: args.action || "install",
      operation_id,
      wait_ms: args.wait_ms || 0,
    });
    return pack(out.ok, out.summary, out.data, out.status);
  }

  if (name === "requirement_save") {
    const project_id = String(args.project_id || "").trim();
    if (!project_id) return pack(false, "缺少 project_id", {}, 422);
    if (!(await requireOwnedProject(env.DB, project_id, userId))) {
      return pack(false, "没有这个项目", {}, 404);
    }
    const { saveRequirement } = await import("./requirements");
    try {
      const requirement = await saveRequirement(env.DB, {
        project_id,
        app_id: String(args.app_id || ""),
        requirement_id: String(args.requirement_id || ""),
        title: String(args.title || ""),
        source_url: String(args.source_url || ""),
      });
      return pack(true, "需求已保存", { requirement });
    } catch (err) {
      return pack(false, err instanceof Error ? err.message : "保存失败", {}, 422);
    }
  }

  if (name === "case_list" || name === "requirement_list") {
    const project_id = String(args.project_id || "").trim();
    if (!project_id) return pack(false, "缺少 project_id", {}, 422);
    if (!(await requireOwnedProject(env.DB, project_id, userId))) {
      return pack(false, "没有这个项目", {}, 404);
    }
    const { listRequirements } = await import("./requirements");
    const requirements = await listRequirements(env.DB, project_id);
    return pack(true, "", { requirements, items: requirements, count: requirements.length, total: requirements.length });
  }

  if (name === "project_script_repo_set") {
    const project_id = String(args.project_id || "").trim();
    if (!project_id) return pack(false, "缺少 project_id", {}, 422);
    if (!(await requireOwnedProject(env.DB, project_id, userId))) {
      return pack(false, "没有这个项目", {}, 404);
    }
    try {
      const saved = await saveRepo(
        env.DB,
        project_id,
        String(args.url || ""),
        String(args.branch || ""),
      );
      return pack(true, "脚本仓库已保存", {
        url: saved.public_url,
        branch: saved.branch,
        updated_at: saved.updated_at,
      });
    } catch (err) {
      return pack(false, err instanceof Error ? err.message : "保存失败", {}, 422);
    }
  }

  if (name === "repo_apps_get") {
    const project_id = String(args.project_id || "").trim();
    if (!project_id) return pack(false, "缺少 project_id", {}, 422);
    if (!(await requireOwnedProject(env.DB, project_id, userId))) {
      return pack(false, "没有这个项目", {}, 404);
    }
    return pack(true, "", await getRepoApps(env.DB, project_id));
  }

  if (name === "repo_apps_sync") {
    const project_id = String(args.project_id || "").trim();
    if (!project_id) return pack(false, "缺少 project_id", {}, 422);
    if (!(await requireOwnedProject(env.DB, project_id, userId))) {
      return pack(false, "没有这个项目", {}, 404);
    }
    try {
      const doc = await saveRepoApps(env.DB, {
        project_id,
        revision: args.revision,
        branch: args.branch,
        apps: args.apps,
        remove: args.remove,
        synced_by: `mcp:${userId}`,
        owner_user_id: userId,
      });
      return pack(true, "已同步", doc);
    } catch (err) {
      return pack(false, err instanceof Error ? err.message : "同步失败", {}, 422);
    }
  }

  if (name === "task_get") {
    const task_id = String(args.task_id || "").trim();
    if (!task_id) return pack(false, "缺少 task_id", {}, 422);
    const doc = await getRun(env.DB, task_id);
    if (!doc || doc.owner_user_id !== userId) return pack(false, "没有这个任务", {}, 404);
    return pack(true, "", toTaskJson(doc));
  }

  if (name === "case_run") {
    const out = await startRun(env, {
      userId,
      app_id: String(args.app_id || ""),
      sn: String(args.sn || ""),
      case_ids: args.case_ids,
      script_targets: args.script_targets,
      revision: String(args.revision || ""),
      env_profile: String(args.env_profile || ""),
      env_surface: String(args.env_surface || ""),
      project_id: String(args.project_id || ""),
      client_request_id: String(args.client_request_id || ""),
      waitUntil,
    });
    if (!out.ok) {
      return pack(false, out.msg, { reason_code: out.reason_code }, out.status);
    }
    return pack(true, out.reused ? "沿用已有任务" : "已开始执行", {
      task_id: out.task_id,
      run_id: out.run_id,
      id: out.run_id,
      status: out.status,
      total: out.total,
      sn: out.sn,
    });
  }

  return pack(false, `没有这个工具：${name}`, {});
}

export async function handleMcp(
  request: Request,
  env: Env,
  ctx?: ExecutionContext,
): Promise<Response> {
  if (request.method === "GET") {
    const auth = await authenticate(request, env);
    if (auth instanceof Response) return auth;
    return new Response(null, { status: 405, headers: { Allow: "POST" } });
  }
  if (request.method !== "POST") {
    return new Response("Method Not Allowed", { status: 405 });
  }

  const auth = await authenticate(request, env);
  if (auth instanceof Response) return auth;
  const { userId } = auth;

  const requested = request.headers.get("mcp-protocol-version");
  if (requested && requested !== PROTOCOL) {
    return Response.json({ detail: `仅支持 MCP ${PROTOCOL}` }, { status: 400 });
  }

  let body: JsonRpcRequest | JsonRpcRequest[];
  try {
    body = (await request.json()) as JsonRpcRequest | JsonRpcRequest[];
  } catch {
    return Response.json(
      { jsonrpc: "2.0", id: null, error: { code: -32700, message: "不是合法 JSON" } },
      { status: 400 },
    );
  }

  const items = Array.isArray(body) ? body : [body];
  const out: unknown[] = [];

  for (const msg of items) {
    if (!msg || typeof msg !== "object") {
      out.push({ jsonrpc: "2.0", id: null, error: { code: -32600, message: "无效请求" } });
      continue;
    }
    const method = String(msg.method || "");
    const id = msg.id;
    const probe = userId === "system:probe";

    if (probe && !["initialize", "ping", "tools/list"].includes(method)) {
      out.push({
        jsonrpc: "2.0",
        id: id ?? null,
        error: { code: -32601, message: "probe token 仅允许 initialize/ping/tools/list" },
      });
      continue;
    }

    if (method === "initialize") {
      out.push({
        jsonrpc: "2.0",
        id: id ?? null,
        result: {
          protocolVersion: PROTOCOL,
          capabilities: { tools: {} },
          serverInfo: { name: "mino-nexus", version: "0.0.1-cloudflare" },
        },
      });
      continue;
    }
    if (method === "notifications/initialized") continue;
    if (method === "ping") {
      out.push({ jsonrpc: "2.0", id: id ?? null, result: {} });
      continue;
    }
    if (method === "tools/list") {
      out.push({ jsonrpc: "2.0", id: id ?? null, result: { tools: TOOLS } });
      continue;
    }
    if (method === "tools/call") {
      const name = String(msg.params?.name || "");
      const args = (msg.params?.arguments || {}) as Record<string, unknown>;
      const token = (request.headers.get("authorization") || "").slice(7);
      const scopeBuf = await crypto.subtle.digest(
        "SHA-256",
        new TextEncoder().encode(token + ":" + String(id ?? "")),
      );
      const scope = [...new Uint8Array(scopeBuf)]
        .map((b) => b.toString(16).padStart(2, "0"))
        .join("");
      const waitUntil = (promise: Promise<unknown>) =>
        ctx ? ctx.waitUntil(promise) : void promise.catch(() => null);
      const result = await callTool(env, userId, name, args, `mcp:${scope}:${id}`, waitUntil);
      out.push({ jsonrpc: "2.0", id: id ?? null, result });
      continue;
    }
    out.push({
      jsonrpc: "2.0",
      id: id ?? null,
      error: { code: -32601, message: `不支持的方法 ${method}` },
    });
  }

  if (!out.length) return new Response(null, { status: 202 });
  return Response.json(Array.isArray(body) ? out : out[0]);
}
