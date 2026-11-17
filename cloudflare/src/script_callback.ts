/** Scout owns detailed records. Nexus handles summary sync and resource calls. */

import type { Env } from "./env";
import { syncRecord, TaskError } from "./task_scheduler";
import { leaseAccount, AccountError } from "./account_pool";
import { recordAccountDataRequest, validateDataNeed } from "./account_data";
import {
  getAdmission,
  getRun,
  isLive,
  type Admission,
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
  if (run.cancel_requested) throw new CallbackError("task_cancelled", "任务已请求取消");
  return adm;
}


async function dispatch(
  env: Env,
  adm: Admission,
  op: string,
  params: Record<string, unknown>,
): Promise<{ summary: string; data?: Record<string, unknown> }> {
  if (["evidence", "note", "step", "fail"].includes(op)) {
    throw new CallbackError("record_api_required", "详细证据必须先保存在 Scout 本机，通过 record.sync 同步摘要");
  }

  if (op === "lease") {
    const case_id = String(params.case_id || "").trim();
    if (case_id && case_id !== adm.case_id) {
      throw new CallbackError("case_mismatch", "case_id 与这次运行不一致");
    }
    const run = await getRun(env.DB, adm.run_id);
    if (run?.cancel_requested) throw new CallbackError("task_cancelled", "任务已请求取消");
    const lease = await env.DB.prepare("SELECT lease_id,generation FROM script_device_leases WHERE run_id=? AND scout_id=? AND sn=? AND state='held'")
      .bind(adm.run_id, adm.scout_id, adm.sn).first<{ lease_id: string; generation: number }>();
    if (!lease) throw new CallbackError("resource_isolated", "设备租约失效或状态尚未核实");
    await env.DB.prepare("UPDATE script_device_leases SET expires_at_ms=?,updated_at_ms=? WHERE run_id=? AND lease_id=?").bind(Date.now() + 120_000, Date.now(), adm.run_id, lease.lease_id).run();
    return {
      summary: "这次运行仍占用该节点和设备",
      data: { occupied: true, sn: adm.sn, node_id: adm.scout_id, lease_id: lease.lease_id, lease_generation: lease.generation },
    };
  }

  if (op === "account") {
    const dataNeed = validateDataNeed(params.data_need ?? "");
    const run = await getRun(env.DB, adm.run_id);
    const requirements = params.requirements && typeof params.requirements === "object" ? params.requirements as Record<string, unknown> : {};
    requirements.data_requirements = params.data_requirements;
    const data = await leaseAccount(env.DB, {
        owner: adm.owner_user_id,
        project: adm.project_id,
        env: run?.env_profile || "test",
        run: adm.run_id,
        session: adm.session_id,
        app_id: adm.app_id,
        env_surface: run?.env_surface || "",
        case_id: adm.case_id,
        sn: adm.sn,
      }, requirements);
    await recordAccountDataRequest(env.DB, adm.owner_user_id, adm.project_id, String(data.id || data.account_id || ""), adm.run_id, adm.session_id, adm.app_id, adm.case_id, dataNeed);
    return { summary: "已租用本次测试账号", data };
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

  if (cap === "record.sync") {
    try { return reply(run_id, step_idx, "pass", "记录摘要已保存", "", await syncRecord(env, scout_id, params)); }
    catch (error) { return reply(run_id, step_idx, "fail", error instanceof Error ? error.message : "摘要保存失败", error instanceof TaskError ? error.code : "record_sync_failed"); }
  }

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
    if (err instanceof CallbackError || err instanceof AccountError) {
      return reply(run_id, step_idx, "fail", err.message, err.code);
    }
    const msg = err instanceof Error ? err.message : String(err);
    return reply(run_id, step_idx, "fail", msg || "callback_rejected", "callback_error");
  }
}
