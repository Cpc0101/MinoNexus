import type { Env } from "./env";
import { getRun } from "./run_store";
import { getSessionDispatch, TaskError } from "./task_scheduler";
import { scoutExecute, merged } from "./rpc";

export async function resolveDebugOrigin(env: Env, owner: string, app: string, profile: string, revision: string, reference: unknown) {
  if (!reference || typeof reference !== "object" || Array.isArray(reference)) throw new TaskError("debug_origin_reference_invalid", "缺少调试检查点引用", 422);
  const ref = reference as Record<string, unknown>;
  if (Object.keys(ref).sort().join(",") !== "checkpoint_id,session_id" || ![ref.session_id, ref.checkpoint_id].every(v => typeof v === "string" && v.length > 0)) throw new TaskError("debug_origin_reference_invalid", "调试来源只接收 session_id/checkpoint_id", 422);
  if (!/^[0-9a-fA-F]{40}$/.test(revision)) throw new TaskError("debug_origin_fixed_commit_required", "调试转正式执行须固定完整 commit SHA", 422);
  const job = await getSessionDispatch(env.DB, owner, String(ref.session_id));
  const parent = job ? await getRun(env.DB, job.run_id) : null;
  if (!job || !parent || parent.owner_user_id !== owner || parent.app_id !== app || parent.env_profile !== profile || parent.execution_kind !== "debug") throw new TaskError("debug_origin_scope_mismatch", "调试来源须属于同一账号、应用和环境", 422);
  const summary = JSON.parse(job.result_json || "{}");
  if (!summary.sealed || summary.execution_stopped !== true || summary.resource_isolated) throw new TaskError("debug_origin_stop_required", "须先确认调试停止与资源结算", 409);
  const reply = await scoutExecute(env, {scout_id: job.scout_id, capability_id: "debug.checkpoint_get", timeout_ms: 15000,
    params: {task_id: job.run_id, attempt_id: job.attempt_id, session_id: job.session_id, owner_user_id: owner, checkpoint_id: ref.checkpoint_id}});
  const receipt = merged(reply);
  if (reply.http !== 200 || reply.status !== "pass") throw new TaskError(String(receipt.reason_code || "debug_checkpoint_unavailable"), reply.summary || "检查点暂不可读", 409);
  if (receipt.stage !== "path_validated") throw new TaskError("debug_origin_unverified", "检查点尚未完成路径验证", 422);
  return Object.fromEntries(["session_id", "checkpoint_id", "script_path", "workspace_hash", "script_hash", "stage"].map(key => [key, receipt[key]]));
}
