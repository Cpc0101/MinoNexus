/** Debugging shares durable admission/evidence, never the formal checkout lifecycle. */
import type { Env } from "./env";
import { startRun } from "./case_runner";
import { getRun, toTaskJson, type RunDoc } from "./run_store";
import { getSessionDispatch, TaskError, cancelTask } from "./task_scheduler";
import { merged, scoutExecute } from "./rpc";

export async function debugCase(env: Env, owner: string, operation: string, args: Record<string, unknown>) {
  if (operation === "start") {
    for (const key of ["app_id", "sn", "node_id", "app_key", "workspace", "client_request_id"])
      if (typeof args[key] !== "string" || !(args[key] as string).trim()) throw new TaskError("debug_argument_missing", `缺少 ${key}`, 422);
    if (["repo_url", "revision", "branch"].some(key => key in args)) throw new TaskError("debug_workspace_only", "调试直接使用编写工作区，不接收 Git 版本", 422);
    const result = await startRun(env, { userId: owner, app_id: String(args.app_id), sn: String(args.sn),
      project_id: args.project_id as string | undefined, env_profile: args.env_profile as string | undefined,
      env_surface: args.env_surface as string | undefined, client_request_id: String(args.client_request_id),
      debug: { workspace: String(args.workspace), node_id: String(args.node_id), app_key: String(args.app_key) }, waitUntil: () => {} });
    if (!result.ok) throw new TaskError(result.reason_code, result.msg, result.status);
    const run = (await getRun(env.DB, result.task_id))!;
    return { ...result, session_id: run.cases[0].session_id, execution_kind: "debug", workspace: run.workspace };
  }
  if (!["execute", "status", "cancel", "reset", "close", "checkpoint", "checkpoint_get"].includes(operation)) throw new TaskError("debug_operation_invalid", "无效调试操作", 422);
  const job = await getSessionDispatch(env.DB, owner, String(args.session_id || ""));
  if (!job || job.owner_user_id !== owner || JSON.parse(job.params_json).execution_kind !== "debug")
    throw new TaskError("debug_session_not_found", "没有这个调试会话", 404);
  if (operation === "close") {
    const run = await cancelTask(env, owner, job.run_id);
    return { task_id: job.run_id, session_id: job.session_id, status: run.status, stop_confirmed: Boolean(run.finished_at) && !run.resource_isolated };
  }
  const params = { ...args, task_id: job.run_id, session_id: job.session_id, attempt_id: job.attempt_id, owner_user_id: owner };
  const reply = await scoutExecute(env, { scout_id: job.scout_id, capability_id: `debug.${operation}`, params, timeout_ms: 15000,
    execute: { run_id: job.run_id, sn: JSON.parse(job.params_json).sn, platform: JSON.parse(job.params_json).platform } });
  const data = merged(reply);
  if (operation === "status" && job.sync_version === 0 && data.reason_code === "record_not_found")
    return { task_id: job.run_id, session_id: job.session_id, state: "starting", status: "queued" };
  if (reply.http !== 200 || reply.status !== "pass") throw new TaskError(String(data.reason_code || "debug_node_unavailable"), reply.summary || "调试节点暂不可用", 409);
  if (operation === "status") {
    const rows = await env.DB.prepare("SELECT payload_json FROM app_regression_runs WHERE debug_origin_session_id=? AND owner_user_id=? ORDER BY created_at_ms DESC LIMIT 200").bind(job.session_id, owner).all<{payload_json: string}>();
    const count = await env.DB.prepare("SELECT COUNT(*) AS total FROM app_regression_runs WHERE debug_origin_session_id=? AND owner_user_id=?").bind(job.session_id, owner).first<{total: number}>();
    data.formal_runs = {total: count?.total || 0, truncated: (count?.total || 0) > (rows.results || []).length, items: (rows.results || []).map(row => toTaskJson(JSON.parse(row.payload_json) as RunDoc))};
  }
  return { ...data, task_id: job.run_id, session_id: job.session_id };
}

export const DEBUG_TOOLS = [
  { name: "debug_case_checkpoint", operation: "checkpoint", title: "记录用例编写阶段", description: "记录 draft/implemented/path_validated 与源码版本。完整实现须通过静态检查；路径验证须引用同版本同 script_path 的成功执行请求。不能据此认定正式通过。", required: ["session_id", "client_request_id", "script_path", "stage"], properties: {
    session_id: { type: "string" }, client_request_id: { type: "string" }, script_path: { type: "string" }, stage: { type: "string", enum: ["draft", "implemented", "path_validated"] }, evidence_request_id: { type: "string" }, workspace_hash: { type: "string" }, timeout_sec: { type: "number", minimum: 1, maximum: 3600 } } },
  { name: "debug_case", operation: "start", title: "启动本机调试会话", description: "直接绑定本机 Scout 编写工作区；不拉仓，不要求 commit。持续持有设备，使用 debug_case_execute 分段观察和调试。远端节点仅用于正式 case_run。", required: ["app_id", "sn", "node_id", "app_key", "workspace", "client_request_id"], properties: {
    app_id: { type: "string" }, project_id: { type: "string" }, sn: { type: "string" }, node_id: { type: "string", description: "本机 mino-scout status 返回的节点 ID" }, app_key: { type: "string" }, workspace: { type: "string", description: "本机已有编写工作区的绝对路径" }, client_request_id: { type: "string" }, env_profile: { type: "string" }, env_surface: { type: "string" } } },
  { name: "debug_case_execute", operation: "execute", title: "执行调试片段", description: "同一现场执行 code 或工作区 script_path，二选一。支持 run(case) 或直接使用 case/app。刷新 pages/flows，不重复前后置、不释放设备。请求 ID 幂等；用 debug_case_status 查询，再读 session_events。", required: ["session_id", "client_request_id"], properties: {
    session_id: { type: "string" }, client_request_id: { type: "string" }, code: { type: "string" }, script_path: { type: "string" }, workspace_hash: { type: "string" }, timeout_sec: { type: "number", minimum: 1, maximum: 3600 } } },
  { name: "debug_case_status", operation: "status", title: "查询调试状态", description: "查询会话和片段；调试完成不等于测试通过。", required: ["session_id"], properties: { session_id: { type: "string" }, client_request_id: { type: "string" } } },
  { name: "debug_case_cancel", operation: "cancel", title: "取消当前片段", description: "取消指定请求，尽量保留现场；无法确认动作停止时保持资源隔离。", required: ["session_id", "client_request_id"], properties: { session_id: { type: "string" }, client_request_id: { type: "string" } } },
  { name: "debug_case_reset", operation: "reset", title: "重置调试现场", description: "关闭并启动绑定应用，清除片段变量，保留租约和证据；未知写结果时拒绝重置。", required: ["session_id", "client_request_id"], properties: { session_id: { type: "string" }, client_request_id: { type: "string" }, timeout_sec: { type: "number", minimum: 1, maximum: 3600 } } },
  { name: "debug_case_close", operation: "close", title: "结束调试会话", description: "持久记录结束请求；Scout 确认停止后才释放资源。", required: ["session_id"], properties: { session_id: { type: "string" } } },
].map(({ operation, required, properties, ...tool }) => ({ ...tool, operation,
  inputSchema: { type: "object", additionalProperties: false, required, properties },
  annotations: { readOnlyHint: operation === "status", destructiveHint: false, idempotentHint: true, openWorldHint: false } }));
