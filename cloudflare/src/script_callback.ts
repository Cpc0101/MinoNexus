/**
 * Scout → Nexus EXECUTE during script.run (capability script.callback).
 * Match mino_nexus.loop.script_callback: admission check, then note/step/fail/lease;
 * account stays unavailable until an account pool exists on the hub.
 */

import type { Env } from "./env";
import {
  appendCaseEvent,
  getAdmission,
  getRun,
  isLive,
  patchCase,
  type Admission,
  type CaseEvent,
} from "./run_store";

const ALIASES: Record<string, string> = {
  "case.account": "account",
  account: "account",
  "case.note": "note",
  note: "note",
  "case.step": "step",
  step: "step",
  "case.fail": "fail",
  fail: "fail",
  "script.step": "evidence",
  "script.session": "evidence",
  "script.lease": "lease",
  lease: "lease",
};

const RESERVED = new Set(["run_id", "session_id", "text", "image_base64", "image_mime", "event"]);

class CallbackError extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

function clip(value: unknown, max: number): string {
  return String(value ?? "").slice(0, max);
}

function scalars(source: Record<string, unknown>, max = 20): CaseEvent {
  const out: CaseEvent = {};
  for (const [key, value] of Object.entries(source)) {
    if (RESERVED.has(key) || Object.keys(out).length >= max) continue;
    if (typeof value === "string") out[key] = clip(value, 200);
    else if (typeof value === "number" || typeof value === "boolean") out[key] = value;
  }
  return out;
}

function reply(
  run_id: string,
  step_idx: number,
  status: "pass" | "fail",
  summary: string,
  error = "",
  data?: Record<string, unknown>,
): Record<string, unknown> {
  const out: Record<string, unknown> = {
    run_id,
    step_idx,
    status,
    summary: clip(summary, 300),
    error,
    executor_used: "nexus",
    source: "nexus",
    elapsed_ms: 0,
  };
  if (data && Object.keys(data).length) out.data = data;
  return out;
}

async function requireAdmission(
  env: Env,
  scout_id: string,
  params: Record<string, unknown>,
): Promise<Admission> {
  const run_id = String(params.run_id || "").trim();
  const session_id = String(params.session_id || "").trim();
  if (!run_id || !session_id) throw new CallbackError("admission_missing", "这次运行没有准入");
  const adm = await getAdmission(env.DB, run_id);
  if (!adm || !adm.live) throw new CallbackError("admission_missing", "这次运行没有准入");
  if (session_id !== adm.session_id) {
    throw new CallbackError("session_mismatch", "session_id 与这次运行不一致");
  }
  if (scout_id !== adm.scout_id) {
    throw new CallbackError("scout_mismatch", "调用方不是这台设备所在节点");
  }
  const run = await getRun(env.DB, run_id);
  if (!run || !isLive(run.status)) throw new CallbackError("run_finished", "这次运行已经结束");
  return adm;
}

async function record(
  env: Env,
  adm: Admission,
  params: Record<string, unknown>,
  event: CaseEvent,
  after?: (row: { summary: string; error: string }) => void,
): Promise<void> {
  const case_id = adm.case_id || String(params.case_id || "");
  await patchCase(env.DB, adm.run_id, case_id, (row) => {
    appendCaseEvent(row, event);
    after?.(row);
  });
}

async function dispatch(
  env: Env,
  adm: Admission,
  op: string,
  params: Record<string, unknown>,
): Promise<{ summary: string; data?: Record<string, unknown> }> {
  if (op === "evidence") {
    const nested =
      params.event && typeof params.event === "object"
        ? (params.event as Record<string, unknown>)
        : params;
    await record(env, adm, params, {
      kind: "evidence",
      capability_id: clip(nested.capability_id || "script", 80),
      status: clip(nested.status || "pass", 40),
      summary: clip(nested.summary, 800),
      error: clip(nested.error, 800),
    });
    return { summary: "已写入动作" };
  }

  if (op === "lease") {
    const case_id = String(params.case_id || "").trim();
    if (case_id && case_id !== adm.case_id) {
      throw new CallbackError("case_mismatch", "case_id 与这次运行不一致");
    }
    return {
      summary: "这次运行仍占用该节点和设备",
      data: { occupied: true, sn: adm.sn, node_id: adm.scout_id },
    };
  }

  if (op === "account") {
    throw new CallbackError(
      "account_pool_missing",
      "Cloudflare hub 尚未配置账号池，无法分配账号；请在脚本仓使用固定测试账号或等待号池迁移",
    );
  }

  if (op === "note") {
    const text = clip(params.text, 500);
    await record(env, adm, params, {
      kind: "note",
      status: "pass",
      ...scalars(params),
      text,
    });
    return { summary: text.slice(0, 300) };
  }

  if (op === "step") {
    const action = String(params.action || "");
    if (action !== "enter" && action !== "leave") {
      throw new CallbackError("script_step_missing", "步骤动作无效");
    }
    const title = typeof params.title === "string" ? params.title.trim().slice(0, 300) : "";
    const number = params.number == null ? null : Number(params.number);
    if (!title && (number === null || !Number.isFinite(number))) {
      throw new CallbackError("script_step_missing", "步骤编号无效");
    }
    await record(env, adm, params, {
      kind: "step",
      action,
      role: clip(params.role, 20),
      ...(title ? { title } : { number }),
      ...(action === "leave" ? { status: params.failed ? "fail" : "pass" } : {}),
    });
    return { summary: title || String(number) };
  }

  if (op === "fail") {
    const message = clip(params.message || "脚本判定失败", 300);
    await record(env, adm, params, { kind: "fail", status: "fail", summary: message }, (row) => {
      row.error = message;
    });
    return { summary: message };
  }

  throw new CallbackError("callback_unsupported", "Nexus 不处理这个回调");
}

export async function handleInboundExecute(
  env: Env,
  scout_id: string,
  execute: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const step_idx = Number(execute.step_idx ?? -1);
  const params: Record<string, unknown> =
    execute.params && typeof execute.params === "object"
      ? { ...(execute.params as Record<string, unknown>) }
      : {};
  if (!String(params.run_id || "").trim() && execute.run_id) params.run_id = String(execute.run_id);

  let cap = String(execute.capability_id || "");
  if (cap === "script.callback") cap = String(params.op || "");
  const run_id = String(params.run_id || execute.run_id || "");

  if (cap.startsWith("node.")) {
    return reply(run_id, step_idx, "pass", "已收到");
  }
  const op = ALIASES[cap];
  if (!op) {
    return reply(
      run_id,
      step_idx,
      "fail",
      `Nexus 不处理回调 ${clip(cap, 60) || "(空)"}`,
      "callback_unsupported",
    );
  }

  try {
    const adm = await requireAdmission(env, scout_id, params);
    const out = await dispatch(env, adm, op, params);
    return reply(run_id, step_idx, "pass", out.summary || "ok", "", out.data);
  } catch (err) {
    if (err instanceof CallbackError) {
      return reply(run_id, step_idx, "fail", err.message, err.code);
    }
    const msg = err instanceof Error ? err.message : String(err);
    return reply(run_id, step_idx, "fail", msg || "callback_rejected", "callback_error");
  }
}
