/**
 * Workbench assistant surfaces — shapes match mino_nexus.api.rAssistant.
 * Persist prefs / media / group policies / MCP tokens in user_settings.
 */

import type { Env } from "./env";
import { listAIProviders } from "./settings";
import {
  createMcpToken,
  listMcpTokens,
  revokeMcpToken,
} from "./mcp_tokens";
import { listGrants, revokeGrant } from "./mcp_oauth";

async function getJson(
  db: D1Database,
  owner: string,
  key: string,
): Promise<Record<string, unknown>> {
  const row = await db
    .prepare(`SELECT value_json FROM user_settings WHERE owner_user_id = ? AND key = ?`)
    .bind(owner, key)
    .first<{ value_json: string }>();
  if (!row?.value_json) return {};
  try {
    return JSON.parse(row.value_json) as Record<string, unknown>;
  } catch {
    return {};
  }
}

async function putJson(
  db: D1Database,
  owner: string,
  key: string,
  value: Record<string, unknown>,
): Promise<void> {
  await db
    .prepare(
      `INSERT INTO user_settings (owner_user_id, key, value_json, updated_at_ms)
       VALUES (?, ?, ?, ?)
       ON CONFLICT(owner_user_id, key) DO UPDATE SET
         value_json=excluded.value_json,
         updated_at_ms=excluded.updated_at_ms`,
    )
    .bind(owner, key, JSON.stringify(value), Date.now())
    .run();
}

function mcpUrl(): string {
  return "https://nexus.minocc.top/mcp";
}

function randomHex(bytes = 24): string {
  const buf = new Uint8Array(bytes);
  crypto.getRandomValues(buf);
  return [...buf].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function handleAssistant(
  request: Request,
  env: Env,
  pathname: string,
  userId: string,
): Promise<Response | null> {
  if (!pathname.startsWith("/assistant/")) return null;
  const method = request.method;
  const envelope = (data: unknown, msg = "") => {
    const body: Record<string, unknown> = { code: 200, ok: true, data };
    if (msg) body.msg = msg;
    return Response.json(body);
  };
  const fail = (status: number, msg: string) =>
    Response.json({ code: status, ok: false, msg }, { status });

  let body: Record<string, unknown> = {};
  if (method !== "GET" && method !== "DELETE") {
    try {
      body = (await request.json()) as Record<string, unknown>;
    } catch {
      body = {};
    }
  }

  if (pathname === "/assistant/status" && method === "GET") {
    const profile = await getJson(env.DB, userId, "assistant.profile");
    const enabled = profile.enabled !== false;
    return envelope({
      enabled,
      online: false,
      mcp_available: enabled,
      mcp_url: mcpUrl(),
      im_nodes: [],
      im_handoff: { enabled: false, target: "" },
    });
  }

  if (pathname === "/assistant/profile" && method === "GET") {
    const profile = await getJson(env.DB, userId, "assistant.profile");
    return envelope({
      user_id: userId,
      persona: String(profile.persona || ""),
      enabled: profile.enabled !== false,
    });
  }

  if (pathname === "/assistant/profile" && method === "PUT") {
    const prev = await getJson(env.DB, userId, "assistant.profile");
    const next = {
      ...prev,
      ...(body.persona !== undefined ? { persona: String(body.persona || "") } : {}),
      ...(body.enabled !== undefined ? { enabled: Boolean(body.enabled) } : {}),
    };
    await putJson(env.DB, userId, "assistant.profile", next);
    return envelope({
      user_id: userId,
      persona: String(next.persona || ""),
      enabled: next.enabled !== false,
    });
  }

  if (pathname === "/assistant/preferences" && method === "GET") {
    const prefs = await getJson(env.DB, userId, "assistant.preferences");
    const out: Record<string, string> = {};
    for (const [k, v] of Object.entries(prefs)) {
      if (typeof v === "string" && v) out[k] = v;
    }
    return envelope(out);
  }

  {
    const m = pathname.match(/^\/assistant\/preferences\/([^/]+)$/);
    if (m && method === "PUT") {
      const key = decodeURIComponent(m[1]);
      if (!["detail", "process", "language"].includes(key)) {
        return fail(422, "unknown_preference");
      }
      const prefs = await getJson(env.DB, userId, "assistant.preferences");
      const value = body.value;
      if (value === null || value === undefined || value === "") {
        delete prefs[key];
      } else {
        prefs[key] = String(value);
      }
      await putJson(env.DB, userId, "assistant.preferences", prefs);
      const out: Record<string, string> = {};
      for (const [k, v] of Object.entries(prefs)) {
        if (typeof v === "string" && v) out[k] = v;
      }
      return envelope(out);
    }
  }

  if (pathname === "/assistant/capabilities" && method === "GET") {
    return envelope({ items: [] });
  }

  if (pathname === "/assistant/group-policies" && method === "GET") {
    const bag = await getJson(env.DB, userId, "assistant.group_policies");
    const items = Array.isArray(bag.items) ? bag.items : [];
    return envelope({ items });
  }

  if (pathname === "/assistant/group-policies" && method === "PUT") {
    const bag = await getJson(env.DB, userId, "assistant.group_policies");
    const items = Array.isArray(bag.items) ? [...bag.items] : [];
    const nextId = Number(bag.next_id || 1);
    const row = {
      id: nextId,
      channel: String(body.channel || ""),
      instance_id: String(body.instance_id || ""),
      tenant: String(body.tenant || ""),
      chat_id: String(body.chat_id || "").trim(),
      enabled: Boolean(body.enabled),
      allow_execute: Boolean(body.allow_execute),
      disclose_results: Boolean(body.disclose_results),
      project_ids: Array.isArray(body.project_ids)
        ? body.project_ids.map((x) => String(x))
        : [],
    };
    if (!row.channel || !row.instance_id || !row.chat_id) {
      return fail(422, "group_policy_incomplete");
    }
    const idx = items.findIndex(
      (item) =>
        item &&
        typeof item === "object" &&
        String((item as { channel?: string }).channel) === row.channel &&
        String((item as { instance_id?: string }).instance_id) === row.instance_id &&
        String((item as { chat_id?: string }).chat_id) === row.chat_id,
    );
    if (idx >= 0) {
      const existing = items[idx] as { id?: number };
      row.id = Number(existing.id || nextId);
      items[idx] = row;
      await putJson(env.DB, userId, "assistant.group_policies", { items, next_id: nextId });
    } else {
      items.push(row);
      await putJson(env.DB, userId, "assistant.group_policies", {
        items,
        next_id: nextId + 1,
      });
    }
    return envelope({ id: row.id });
  }

  if (pathname === "/assistant/media-settings" && method === "GET") {
    const media = await getJson(env.DB, userId, "assistant.media");
    const providers = await listAIProviders(env.DB, userId);
    const list = Array.isArray((providers as { providers?: unknown }).providers)
      ? ((providers as { providers: Array<Record<string, unknown>> }).providers)
      : [];
    return envelope({
      asr_provider_id: String(media.asr_provider_id || ""),
      asr_model: String(media.asr_model || ""),
      providers: list.map((row) => ({
        id: String(row.id || ""),
        name: String(row.name || row.id || ""),
        configured: Boolean(row.configured),
      })),
    });
  }

  if (pathname === "/assistant/media-settings" && method === "PUT") {
    const next = {
      asr_provider_id: String(body.asr_provider_id || ""),
      asr_model: String(body.asr_model || ""),
    };
    await putJson(env.DB, userId, "assistant.media", next);
    return envelope(next, "已保存");
  }

  if (pathname === "/assistant/report-settings" && method === "GET") {
    const report = await getJson(env.DB, userId, "assistant.report");
    const saved = String(report.workbench_url || "");
    const effective = saved || "https://workbench.minocc.top";
    return envelope({
      workbench_url: saved,
      effective_url: effective,
      source: saved ? "configured" : "lan_default",
      available: true,
    });
  }

  if (pathname === "/assistant/report-settings" && method === "PUT") {
    const workbench_url = String(body.workbench_url || "").trim();
    await putJson(env.DB, userId, "assistant.report", { workbench_url });
    return envelope({
      workbench_url,
      effective_url: workbench_url || "https://workbench.minocc.top",
      source: workbench_url ? "configured" : "lan_default",
      available: true,
    }, "已保存");
  }

  if (pathname === "/assistant/mcp-tokens" && method === "GET") {
    return envelope({ items: await listMcpTokens(env.DB, userId) });
  }

  if (pathname === "/assistant/mcp-tokens" && method === "POST") {
    const name = String(body.name || "").trim();
    if (!name) return fail(422, "name required");
    // Match mino_nexus.assistant.store.create_mcp_token (mino_mcp_ + hash).
    return envelope(await createMcpToken(env.DB, userId, name));
  }

  {
    const m = pathname.match(/^\/assistant\/mcp-tokens\/(\d+)$/);
    if (m && method === "DELETE") {
      const id = Number(m[1]);
      if (!(await revokeMcpToken(env.DB, userId, id))) return fail(404, "token 不存在");
      return envelope({ id });
    }
  }

  if (pathname === "/assistant/oauth-grants" && method === "GET") {
    return envelope({ items: await listGrants(env.DB, userId) });
  }

  {
    const m = pathname.match(/^\/assistant\/oauth-grants\/([^/]+)$/);
    if (m && method === "DELETE") {
      const familyId = decodeURIComponent(m[1]);
      if (!(await revokeGrant(env.DB, userId, familyId))) return fail(404, "没有这条授权");
      return envelope({ family_id: familyId });
    }
  }

  if (pathname === "/assistant/identities" && method === "GET") {
    return envelope({ items: [] });
  }

  if (pathname === "/assistant/tools" && method === "GET") {
    return envelope({ items: [] });
  }

  if (pathname === "/assistant/turns" && method === "GET") {
    return envelope({ items: [], total: 0 });
  }

  if (pathname === "/assistant/requests" && method === "GET") {
    return envelope({ items: [], focuses: [] });
  }

  if (pathname === "/assistant/subscriptions" && method === "GET") {
    return envelope({ items: [] });
  }

  if (pathname === "/assistant/agent-guide" && method === "GET") {
    // Match mino_nexus.assistant.agent_guide.agent_guide()
    return envelope({
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
          name: "让 Scout 跑整份脚本",
          tools: ["case_list", "device_list", "case_run", "task_get"],
          then: "case_list 返回需求 id、名称和链接。开跑前 plugin_status，缺的渠道程序先装完。case_run 传 script_targets。执行与汇报使用 agent 技能 run-test-cases。",
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
    });
  }

  // Known-empty GETs keep Workbench list panes from crashing on missing items.
  if (method === "GET") {
    if (pathname.includes("/messages")) return envelope({ items: [], messages: [] });
    if (pathname.includes("/modules")) return envelope({ items: [] });
    return envelope({ items: [], total: 0 });
  }
  return fail(501, "该助手接口尚未迁移到 Cloudflare hub");
}
