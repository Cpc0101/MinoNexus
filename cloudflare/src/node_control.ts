/**
 * Node remote control — match mino_nexus.api.rRuntime plugin/command/tool-task paths.
 */

import type { Env } from "./env";
import { getScoutContract, listDevices } from "./db";
import { merged, scoutExecute } from "./rpc";

function envelope(data: unknown, msg = "", status = 200): Response {
  const body: Record<string, unknown> = { code: status === 202 ? 200 : status, ok: status < 400, data };
  if (msg) body.msg = msg;
  return Response.json(body, { status: status === 202 ? 202 : status < 400 ? 200 : status });
}

function fail(status: number, msg: string): Response {
  return Response.json({ code: status, ok: false, msg }, { status });
}

async function requireOwnedLive(
  env: Env,
  nodeId: string,
  userId: string,
): Promise<{ error?: Response }> {
  const scout = await getScoutContract(env.DB, nodeId);
  if (!scout) return { error: fail(404, "没有这个节点") };
  if (scout.owner_user_id && scout.owner_user_id !== userId) {
    return { error: fail(404, "没有这个节点") };
  }
  if (!scout.online) return { error: fail(409, "节点离线，无法下发") };
  return {};
}

async function relayPlugin(
  env: Env,
  nodeId: string,
  capability_id: string,
  params: Record<string, unknown>,
  timeout_ms: number,
): Promise<Response> {
  const result = await scoutExecute(env, {
    scout_id: nodeId,
    capability_id,
    params,
    timeout_ms,
  });
  if (result.http === 409) return fail(409, "节点离线，无法下发");
  if (result.http === 504) return fail(504, "节点未应答");
  if (!result.ok) {
    return fail(502, result.error || result.summary || "节点拒绝了插件操作");
  }
  const raw = merged(result);
  if (Array.isArray(raw.plugins)) {
    // Persist refreshed plugin snapshot from Scout reply.
    await env.DB.prepare(
      `UPDATE scouts SET plugins_json=?, plugins_ready=1 WHERE scout_id=?`,
    )
      .bind(JSON.stringify(raw.plugins), nodeId)
      .run()
      .catch(() => undefined);
  }
  const hidden = ["secret", "password", "webhook", "bot_token", "app_secret", "plugin_secret"];
  const publicData: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(raw)) {
    if (key === "plugins" || key === "command") continue;
    if (hidden.some((flag) => key.includes(flag))) continue;
    publicData[key] = value;
  }
  return envelope({
    node_id: nodeId,
    kind: String(params.class || params.kind || ""),
    plugin_id: String(params.id || ""),
    status: result.status,
    summary: result.summary || "",
    configured: Boolean(raw.configured),
    data: publicData,
  });
}

function publicTask(row: {
  task_id: string;
  operation_id: string;
  node_id: string;
  tool_id: string;
  action: string;
  state: string;
  snapshot_json: string | null;
  created_at_ms: number;
  updated_at_ms: number;
}) {
  let snapshot: Record<string, unknown> = {};
  if (row.snapshot_json) {
    try {
      snapshot = JSON.parse(row.snapshot_json) as Record<string, unknown>;
    } catch {
      snapshot = {};
    }
  }
  const active = ["pending", "running", "unknown"].includes(row.state);
  return {
    ...snapshot,
    task_id: row.task_id,
    operation_id: row.operation_id,
    node_id: row.node_id,
    tool_id: row.tool_id,
    action: row.action,
    state: row.state,
    active,
    created_at: Math.floor(row.created_at_ms / 1000),
    updated_at: Math.floor(row.updated_at_ms / 1000),
  };
}

export async function handleNodeControl(
  request: Request,
  env: Env,
  pathname: string,
  method: string,
  userId: string,
): Promise<Response | null> {
  const body = method === "GET" || method === "DELETE"
    ? {}
    : await request.json().catch(() => ({})) as Record<string, unknown>;

  // GET logs via node.log_tail
  {
    const m = pathname.match(/^\/runtime\/nodes\/([^/]+)\/logs$/);
    if (m && method === "GET") {
      const nodeId = decodeURIComponent(m[1]);
      const gate = await requireOwnedLive(env, nodeId, userId);
      if (gate.error) return gate.error;
      const url = new URL(request.url);
      const lines = Math.min(Math.max(Number(url.searchParams.get("lines") || 200), 1), 2000);
      const result = await scoutExecute(env, {
        scout_id: nodeId,
        capability_id: "node.log_tail",
        params: { lines },
        timeout_ms: 35_000,
      });
      if (result.http === 409) return fail(409, "节点离线，无法拉日志");
      if (result.http === 504) return fail(504, "节点未应答");
      if (!result.ok) return fail(502, result.error || result.summary || "拉日志失败");
      const raw = merged(result);
      const logs = raw.logs && typeof raw.logs === "object" ? raw.logs : raw;
      return envelope({
        node_id: nodeId,
        logs,
        lines: Array.isArray((logs as { lines?: unknown }).lines)
          ? (logs as { lines: string[] }).lines
          : [],
        text: String((logs as { text?: string }).text || result.summary || ""),
        summary: result.summary || "",
      });
    }
  }

  // POST command
  {
    const m = pathname.match(/^\/runtime\/nodes\/([^/]+)\/command$/);
    if (m && method === "POST") {
      const nodeId = decodeURIComponent(m[1]);
      const cmd = String(body.command || "").trim().toLowerCase();
      if (cmd === "update") return fail(409, "请使用固定版本更新接口，并提供 operation_id 和 build_id");
      if (cmd === "start") return fail(400, "离线专机无法远程启动");
      if (!["stop", "restart", "sleep", "wake"].includes(cmd)) {
        return fail(400, "不支持的节点指令");
      }
      const gate = await requireOwnedLive(env, nodeId, userId);
      if (gate.error) return gate.error;
      const result = await scoutExecute(env, {
        scout_id: nodeId,
        capability_id: `node.${cmd}`,
        params: { command: cmd, reason: String(body.reason || "workbench") },
        timeout_ms: 50_000,
      });
      if (result.http === 409) return fail(409, "节点离线，无法下发");
      if (result.http === 504) return fail(504, "节点未应答");
      return envelope({
        node_id: nodeId,
        command: cmd,
        status: result.status,
        summary: result.summary || "",
        error: result.error || "",
        extra: result.extra,
      });
    }
  }

  // POST update (Scout fixed version)
  {
    const m = pathname.match(/^\/runtime\/nodes\/([^/]+)\/update$/);
    if (m && method === "POST") {
      const nodeId = decodeURIComponent(m[1]);
      const gate = await requireOwnedLive(env, nodeId, userId);
      if (gate.error) return gate.error;
      const operation_id = String(body.operation_id || "").trim();
      const build_id = String(body.build_id || "").trim();
      const release_tag = String(body.release_tag || "").trim();
      if (!operation_id || !build_id || !release_tag) {
        return fail(422, "必须指定 operation_id / build_id / release_tag");
      }
      if (build_id.toLowerCase() === "latest" || release_tag.toLowerCase() === "latest") {
        return fail(422, "必须指定固定 release tag/build_id");
      }
      const result = await scoutExecute(env, {
        scout_id: nodeId,
        capability_id: "node.update",
        params: {
          operation_id,
          build_id,
          release_tag,
          action: String(body.action || "update"),
          rollback_of: String(body.rollback_of || ""),
          reason: String(body.reason || "workbench"),
        },
        timeout_ms: 630_000,
      });
      if (result.http === 409) return fail(409, "节点离线，无法下发");
      if (result.http === 504) {
        return fail(
          504,
          "节点未应答（更新可能仍在下载/安装，或进程已退出未重连）",
        );
      }
      return envelope({
        node_id: nodeId,
        operation_id,
        status: result.status,
        summary: result.summary || "",
        error: result.error || "",
        ...merged(result),
      });
    }
  }

  // plugins/config|install|remove|call
  {
    const m = pathname.match(/^\/runtime\/nodes\/([^/]+)\/plugins\/(config|install|remove|call)$/);
    if (m && method === "POST") {
      const nodeId = decodeURIComponent(m[1]);
      const action = m[2];
      const gate = await requireOwnedLive(env, nodeId, userId);
      if (gate.error) return gate.error;
      if (action === "call") {
        const cap = String(body.capability_id || "").trim();
        if (!cap) return fail(400, "capability_id 不能为空");
        return relayPlugin(
          env,
          nodeId,
          cap,
          (body.params && typeof body.params === "object"
            ? (body.params as Record<string, unknown>)
            : {}),
          cap === "plugin.cli.feishu" ? 90_000 : 50_000,
        );
      }
      const kind = String(body.kind || "").trim();
      const plugin_id = String(body.plugin_id || "").trim();
      if (!kind || !plugin_id) return fail(400, "缺少插件分类或 id");
      if (action === "install" && !["cli", "mcp", "im"].includes(kind)) {
        return fail(400, "只有 CLI、MCP 和对话渠道（LangBot）需要安装");
      }
      if (action === "config") {
        const values: Record<string, string> = {};
        if (body.values && typeof body.values === "object") {
          for (const [k, v] of Object.entries(body.values as Record<string, unknown>)) {
            if (String(k).trim()) values[String(k)] = String(v);
          }
        }
        const cleared = Array.isArray(body.clear)
          ? body.clear.map((x) => String(x).trim()).filter(Boolean)
          : [];
        return relayPlugin(
          env,
          nodeId,
          "node.plugin_config",
          { class: kind, id: plugin_id, values, clear: cleared },
          60_000,
        );
      }
      if (action === "install") {
        return relayPlugin(
          env,
          nodeId,
          "node.plugin_install",
          { class: kind, id: plugin_id },
          630_000,
        );
      }
      return relayPlugin(
        env,
        nodeId,
        "node.plugin_remove",
        { class: kind, id: plugin_id },
        180_000,
      );
    }
  }

  // configuration export/import
  {
    const m = pathname.match(/^\/runtime\/nodes\/([^/]+)\/configuration\/(export|import)$/);
    if (m && method === "POST") {
      const nodeId = decodeURIComponent(m[1]);
      const gate = await requireOwnedLive(env, nodeId, userId);
      if (gate.error) return gate.error;
      const scout = await getScoutContract(env.DB, nodeId);
      if (scout?.plugins_ready !== true) {
        return fail(409, "节点尚未就绪，无法迁移配置");
      }
      if (m[2] === "export") {
        const result = await scoutExecute(env, {
          scout_id: nodeId,
          capability_id: "node.config_export",
          params: {},
          timeout_ms: 90_000,
        });
        if (!result.ok) return fail(502, result.error || "Scout 未返回配置文件，请升级 Scout");
        const data = merged(result);
        if (typeof data.archive !== "string") {
          return fail(502, "Scout 未返回配置文件，请升级 Scout");
        }
        return envelope(data);
      }
      if (!body.overwrite) return fail(400, "请明确确认覆盖目标节点配置");
      const result = await scoutExecute(env, {
        scout_id: nodeId,
        capability_id: "node.config_import",
        params: { archive: body.archive },
        timeout_ms: 90_000,
      });
      if (!result.ok) return fail(502, result.error || result.summary || "导入失败");
      return envelope(merged(result));
    }
  }

  // claim
  {
    const m = pathname.match(/^\/runtime\/nodes\/([^/]+)\/claim$/);
    if (m && method === "POST") {
      const nodeId = decodeURIComponent(m[1]);
      const scout = await getScoutContract(env.DB, nodeId);
      if (!scout) return fail(404, "没有这个节点");
      await env.DB.prepare(
        `UPDATE scouts SET owner_user_id=? WHERE scout_id=?`,
      )
        .bind(userId, nodeId)
        .run();
      return envelope({ node_id: nodeId, owner_user_id: userId }, "已认领");
    }
  }

  // tool-tasks submit
  {
    const m = pathname.match(/^\/runtime\/nodes\/([^/]+)\/tool-tasks$/);
    if (m && method === "POST") {
      const nodeId = decodeURIComponent(m[1]);
      const operation_id = String(body.operation_id || "").trim();
      const tool_id = String(body.tool_id || "").trim();
      const action = String(body.action || "install").trim();
      if (!/^[A-Za-z0-9_-]{8,100}$/.test(operation_id)) return fail(422, "operation_id invalid");
      if (!["cli.feishu", "cli.meego", "chromium", "adb.keyboard", "appium"].includes(tool_id)) {
        return fail(422, "unsupported_tool_action");
      }
      if (!["install", "update"].includes(action)) return fail(422, "unsupported_tool_action");

      const scout = await getScoutContract(env.DB, nodeId);
      if (!scout || (scout.owner_user_id && scout.owner_user_id !== userId)) {
        return fail(404, "没有这个节点");
      }
      if (!scout.online) return fail(409, "节点离线，不能安装");
      if (![1, 2].includes(scout.tool_tasks_version) || scout.plugins_ready !== true) {
        return fail(409, "节点尚未上报下载中心合同，请手动升级 Scout 或等待账号确认");
      }

      const existing = await env.DB.prepare(
        `SELECT task_id, owner_user_id, operation_id, node_id, tool_id, action, state, snapshot_json, created_at_ms, updated_at_ms
         FROM node_tool_tasks WHERE owner_user_id=? AND operation_id=?`,
      )
        .bind(userId, operation_id)
        .first<{
          task_id: string;
          owner_user_id: string;
          operation_id: string;
          node_id: string;
          tool_id: string;
          action: string;
          state: string;
          snapshot_json: string | null;
          created_at_ms: number;
          updated_at_ms: number;
        }>();

      let taskId = existing?.task_id || "";
      if (existing) {
        if (
          existing.node_id !== nodeId ||
          existing.tool_id !== tool_id ||
          existing.action !== action
        ) {
          return fail(409, "operation_parameters_conflict");
        }
        if (["succeeded", "failed", "interrupted"].includes(existing.state)) {
          return envelope(publicTask(existing), "", 202);
        }
      } else {
        taskId = crypto.randomUUID().replaceAll("-", "");
        const now = Date.now();
        const slot = `${userId}:${nodeId}:${tool_id}`;
        const busy = await env.DB.prepare(
          `SELECT task_id FROM node_tool_tasks WHERE active_slot=? AND state IN ('pending','running','unknown')`,
        )
          .bind(slot)
          .first();
        if (busy) return fail(409, "tool_busy");
        await env.DB.prepare(
          `INSERT INTO node_tool_tasks
           (task_id, owner_user_id, operation_id, node_id, tool_id, action, state, active_slot, snapshot_json, created_at_ms, updated_at_ms)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, '{}', ?, ?)`,
        )
          .bind(taskId, userId, operation_id, nodeId, tool_id, action, slot, now, now)
          .run();
      }

      const result = await scoutExecute(env, {
        scout_id: nodeId,
        capability_id: "node.tool_task_submit",
        params: { task_id: taskId, tool_id, action },
        timeout_ms: 22_000,
      });
      const now = Date.now();
      if (result.http === 504 || result.http === 409 || !result.ok) {
        const snap = {
          error:
            result.error ||
            (result.http === 504
              ? "节点应答未确认，只查询原任务，不自动重装"
              : "node_rejected"),
        };
        await env.DB.prepare(
          `UPDATE node_tool_tasks SET state=?, snapshot_json=?, updated_at_ms=?, active_slot=NULL WHERE task_id=?`,
        )
          .bind(result.http === 504 ? "unknown" : "failed", JSON.stringify(snap), now, taskId)
          .run();
        const row = await env.DB.prepare(
          `SELECT task_id, owner_user_id, operation_id, node_id, tool_id, action, state, snapshot_json, created_at_ms, updated_at_ms
           FROM node_tool_tasks WHERE task_id=?`,
        )
          .bind(taskId)
          .first<Parameters<typeof publicTask>[0]>();
        return envelope(row ? publicTask(row) : { task_id: taskId, state: "unknown" }, "", 202);
      }

      const task = (merged(result).task || {}) as Record<string, unknown>;
      const state = String(task.state || "running");
      const snap = {
        stage: task.stage,
        label: task.label,
        percent: task.percent,
        bytes_received: task.bytes_received,
        bytes_total: task.bytes_total,
        error: task.error,
      };
      const active = ["pending", "running", "unknown"].includes(state);
      await env.DB.prepare(
        `UPDATE node_tool_tasks SET state=?, snapshot_json=?, updated_at_ms=?, active_slot=? WHERE task_id=?`,
      )
        .bind(
          state,
          JSON.stringify(snap),
          now,
          active ? `${userId}:${nodeId}:${tool_id}` : null,
          taskId,
        )
        .run();
      const row = await env.DB.prepare(
        `SELECT task_id, owner_user_id, operation_id, node_id, tool_id, action, state, snapshot_json, created_at_ms, updated_at_ms
         FROM node_tool_tasks WHERE task_id=?`,
      )
        .bind(taskId)
        .first<Parameters<typeof publicTask>[0]>();
      return envelope(row ? publicTask(row) : { task_id: taskId, state }, "", 202);
    }
  }

  // tool-tasks get
  {
    const m = pathname.match(/^\/runtime\/nodes\/([^/]+)\/tool-tasks\/([^/]+)$/);
    if (m && method === "GET") {
      const nodeId = decodeURIComponent(m[1]);
      const taskId = decodeURIComponent(m[2]);
      const row = await env.DB.prepare(
        `SELECT task_id, owner_user_id, operation_id, node_id, tool_id, action, state, snapshot_json, created_at_ms, updated_at_ms
         FROM node_tool_tasks WHERE task_id=? AND owner_user_id=? AND node_id=?`,
      )
        .bind(taskId, userId, nodeId)
        .first<Parameters<typeof publicTask>[0] & { owner_user_id: string }>();
      if (!row) return fail(404, "没有这个安装任务");
      if (["pending", "running", "unknown"].includes(row.state)) {
        const result = await scoutExecute(env, {
          scout_id: nodeId,
          capability_id: "node.tool_task_get",
          params: { task_id: taskId },
          timeout_ms: 12_000,
        });
        if (result.ok) {
          const task = (merged(result).task || null) as Record<string, unknown> | null;
          if (task) {
            const state = String(task.state || row.state);
            const snap = {
              stage: task.stage,
              label: task.label,
              percent: task.percent,
              bytes_received: task.bytes_received,
              bytes_total: task.bytes_total,
              error: task.error,
            };
            const active = ["pending", "running", "unknown"].includes(state);
            await env.DB.prepare(
              `UPDATE node_tool_tasks SET state=?, snapshot_json=?, updated_at_ms=?, active_slot=? WHERE task_id=?`,
            )
              .bind(
                state,
                JSON.stringify(snap),
                Date.now(),
                active ? `${userId}:${nodeId}:${row.tool_id}` : null,
                taskId,
              )
              .run();
            const updated = await env.DB.prepare(
              `SELECT task_id, owner_user_id, operation_id, node_id, tool_id, action, state, snapshot_json, created_at_ms, updated_at_ms
               FROM node_tool_tasks WHERE task_id=?`,
            )
              .bind(taskId)
              .first<Parameters<typeof publicTask>[0]>();
            if (updated) return envelope(publicTask(updated));
          }
        }
      }
      return envelope(publicTask(row));
    }
  }

  return null;
}

export async function handleDeviceWrites(
  request: Request,
  env: Env,
  pathname: string,
  method: string,
  userId: string,
): Promise<Response | null> {
  if (method !== "POST") return null;

  if (pathname === "/device/set_password") {
    const body = (await request.json().catch(() => ({}))) as Record<string, unknown>;
    const sn = String(body.sn || "").trim();
    const password = String(body.password || "");
    if (!sn) return fail(400, "请指定设备 sn");
    const devices = (await listDevices(env.DB, false)) as Array<{ device_id?: string; scout_id?: string }>;
    const device = devices.find((d) => d.device_id === sn);
    if (!device) return fail(404, "没有这个设备");
    const scout = await getScoutContract(env.DB, String(device.scout_id || ""));
    if (scout?.owner_user_id && scout.owner_user_id !== userId) {
      return fail(404, "没有这个设备");
    }
    await env.DB.prepare(
      `INSERT INTO device_secrets (device_id, owner_user_id, lock_password, updated_at_ms)
       VALUES (?, ?, ?, ?)
       ON CONFLICT(device_id) DO UPDATE SET
         lock_password=excluded.lock_password,
         owner_user_id=excluded.owner_user_id,
         updated_at_ms=excluded.updated_at_ms`,
    )
      .bind(sn, userId, password, Date.now())
      .run();
    return envelope({ sn, password_configured: Boolean(password) }, "已保存");
  }

  {
    const m = pathname.match(/^\/device\/([^/]+)\/ime\/(adbkeyboard|system)$/);
    if (m) {
      const sn = decodeURIComponent(m[1]);
      const ime = m[2] === "adbkeyboard" ? "adbkeyboard" : "system";
      const devices = (await listDevices(env.DB, false)) as Array<{
        device_id?: string;
        scout_id?: string;
        platform?: string | null;
        online?: boolean;
      }>;
      const device = devices.find((d) => d.device_id === sn);
      if (!device) return fail(404, "没有这个设备");
      const nodeId = String(device.scout_id || "");
      const scout = await getScoutContract(env.DB, nodeId);
      if (scout?.owner_user_id && scout.owner_user_id !== userId) {
        return fail(404, "没有这个设备");
      }
      if (!scout?.online || !device.online) return fail(409, "节点离线，无法下发");
      const plat = String(device.platform || "android").toLowerCase();
      if (plat !== "android" && plat !== "other") {
        return fail(400, `设备 ${sn} 不是 Android（platform=${plat}）`);
      }
      const secret = await env.DB.prepare(
        `SELECT lock_password FROM device_secrets WHERE device_id=?`,
      )
        .bind(sn)
        .first<{ lock_password: string }>();
      const device_hint: Record<string, unknown> = {
        platform: "android",
        adb_serial: sn,
      };
      if (secret?.lock_password) device_hint.password = secret.lock_password;
      const result = await scoutExecute(env, {
        scout_id: nodeId,
        capability_id: "set_input_method",
        params: { ime },
        timeout_ms: 35_000,
        execute: {
          sn,
          device_id: sn,
          platform: "android",
          executor_order: ["adb"],
          selected_impl: { executor: "adb", runs_on: "scout" },
          device_hint,
        },
      });
      if (result.http === 409) return fail(409, "节点离线，无法下发");
      if (result.http === 504) return fail(409, "Scout 未应答");
      const ok = result.ok;
      const data = {
        sn,
        node_id: nodeId,
        status: result.status,
        ok,
        summary: result.summary || "",
        error: result.error || "",
        ...merged(result),
      };
      if (!ok) return fail(502, data.error || data.summary || "切换失败");
      return envelope(
        data,
        ime === "adbkeyboard" ? "已切换为 ADB Keyboard" : "已关闭 ADB Keyboard",
      );
    }
  }

  return null;
}
