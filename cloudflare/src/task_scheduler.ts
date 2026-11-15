/** Durable dispatch and result indexes. Never waits for a test to finish. */
import { hubStub, type Env } from "./env";
import { listDevices } from "./db";
import { scoutExecute, merged, type ScoutRpcResult } from "./rpc";
import { releaseAccounts } from "./account_pool";
import { aggregateStatus, getRun, patchRun, registerAdmission, clearAdmission, type RunDoc } from "./run_store";

export type Dispatch = {
  attempt_id: string; run_id: string; case_id: string; session_id: string; scout_id: string; owner_user_id: string;
  state: string; params_json: string; hint_json: string; result_json: string; sync_version: number;
  attempts: number; next_attempt_ms: number; accepted_at_ms: number; updated_at_ms: number; created_at_ms: number;
};
type DeviceLease = { resource_key: string; scout_id: string; sn: string; slot: number; run_id: string; lease_id: string; generation: number; state: string; expires_at_ms: number };
export type Rpc = (args: { scout_id: string; capability_id: string; params?: Record<string, unknown>; timeout_ms?: number; execute?: Record<string, unknown> }) => Promise<ScoutRpcResult>;
const TERMINAL = new Set(["pass", "fail", "blocked", "declined", "skipped", "cancelled"]);

export class TaskError extends Error {
  constructor(readonly code: string, message: string, readonly http = 409) { super(message); }
}
export async function wakeScheduler(env: Env): Promise<void> {
  const result = await hubStub(env).fetch("https://hub/dispatch/wake", { method: "POST" });
  if (!result.ok) throw new Error("dispatch_scheduler_unavailable");
}
async function claimDevice(db: D1Database, doc: RunDoc): Promise<DeviceLease> {
  const max = doc.platform === "web" ? 4 : 1;
  for (let slot = 0; slot < max; slot++) {
    const key = `device:${doc.sn}:${slot}`, now = Date.now(), leaseId = crypto.randomUUID();
    await db.prepare("INSERT OR IGNORE INTO script_device_leases(resource_key,scout_id,sn,slot,updated_at_ms) VALUES(?,?,?,?,?)")
      .bind(key, doc.scout_id, doc.sn, slot, now).run();
    const result = await db.prepare("UPDATE script_device_leases SET scout_id=?,run_id=?,owner_user_id=?,lease_id=?,generation=generation+1,state='held',expires_at_ms=?,updated_at_ms=? WHERE resource_key=? AND state='released'")
      .bind(doc.scout_id, doc.run_id, doc.owner_user_id, leaseId, now + 120_000, now, key).run();
    if (result.meta.changes) return (await db.prepare("SELECT * FROM script_device_leases WHERE resource_key=?").bind(key).first<DeviceLease>())!;
  }
  throw new TaskError(doc.platform === "web" ? "web_parallel_full" : "busy", "设备仍被任务占用或等待状态核实");
}
export async function releaseDevice(db: D1Database, runId: string): Promise<void> {
  await db.prepare("UPDATE script_device_leases SET run_id='',owner_user_id='',lease_id='',state='released',expires_at_ms=0,updated_at_ms=? WHERE run_id=?")
    .bind(Date.now(), runId).run();
}
export async function enqueueRun(env: Env, doc: RunDoc, hint: Record<string, unknown>): Promise<RunDoc> {
  const db = env.DB, now = Date.now();
  const lease = await claimDevice(db, doc);
  doc.lease_id = lease.lease_id; doc.lease_generation = lease.generation;
  let workbench = "https://workbench.minocc.top";
  const setting = await db.prepare("SELECT value_json FROM user_settings WHERE owner_user_id=? AND key='assistant.report'").bind(doc.owner_user_id).first<{ value_json: string }>();
  if (setting) { try { const value = JSON.parse(setting.value_json).workbench_url; if (value && /^https?:\/\/[^\s]+$/.test(value)) workbench = String(value).replace(/\/$/, ""); } catch { /* keep default */ } }
  doc.report_url = `${workbench}/testing/projects/${encodeURIComponent(doc.project_id)}/apps/${encodeURIComponent(doc.app_id)}/tasks/${encodeURIComponent(doc.run_id)}`;
  const statements: D1PreparedStatement[] = [];
  try {
    if (doc.client_request_id) {
      const hash = doc.request_fingerprint || JSON.stringify({ app_id: doc.app_id, sn: doc.sn, revision: doc.revision, cases: doc.cases.map(c => c.case_id), env: doc.env_profile });
      statements.push(db.prepare("INSERT INTO script_request_keys(owner_user_id,request_id,run_id,request_hash,created_at_ms) VALUES(?,?,?,?,?)")
        .bind(doc.owner_user_id, doc.client_request_id, doc.run_id, hash, now));
    }
    for (const [index, row] of doc.cases.entries()) {
      row.attempt_id = row.session_id; row.record_source = "scout"; row.record_node_id = doc.scout_id;
      row.detail_availability = "pending"; row.summary_version = 0; row.last_event_seq = 0; row.events = [];
      const params = { task_id: doc.run_id, run_id: doc.run_id, session_id: row.session_id, attempt_id: row.attempt_id,
        owner_user_id: doc.owner_user_id, case_id: row.case_id, sn: doc.sn, platform: doc.platform,
        repo_url: doc.repo_url, branch: doc.repo_branch, revision: doc.revision, script_path: row.script_path,
        execution_timeout_sec: 600 };
      statements.push(db.prepare("INSERT INTO script_dispatches(attempt_id,run_id,case_id,session_id,scout_id,owner_user_id,params_json,hint_json,updated_at_ms,created_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?)")
        .bind(row.attempt_id, doc.run_id, row.case_id, row.session_id, doc.scout_id, doc.owner_user_id, JSON.stringify(params),
          JSON.stringify({ ...hint, lease_id: lease.lease_id, lease_generation: lease.generation }), now, now + index));
    }
    statements.push(db.prepare("INSERT INTO app_regression_runs(run_id,owner_user_id,project_id,app_id,status,payload_json,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,?,?,?)")
      .bind(doc.run_id, doc.owner_user_id, doc.project_id, doc.app_id, doc.status, JSON.stringify(doc), now, now));
    await db.batch(statements);
  } catch (error) {
    await releaseDevice(db, doc.run_id);
    if (doc.client_request_id) {
      const existing = await db.prepare("SELECT run_id FROM script_request_keys WHERE owner_user_id=? AND request_id=?")
        .bind(doc.owner_user_id, doc.client_request_id).first<{ run_id: string }>();
      const reused = existing ? await getRun(db, existing.run_id) : null;
      if (reused) {
        if (reused.request_fingerprint && reused.request_fingerprint !== doc.request_fingerprint) throw new TaskError("request_conflict", "相同 client_request_id 不能用于不同任务参数");
        return reused;
      }
    }
    throw error;
  }
  // Set the durable alarm before acknowledging the task. Even if this request is
  // interrupted, startup/heartbeat alarms recover the D1 dispatch rows.
  await wakeScheduler(env);
  return doc;
}
export async function getDispatch(db: D1Database, attempt: string): Promise<Dispatch | null> {
  return db.prepare("SELECT * FROM script_dispatches WHERE attempt_id=?").bind(attempt).first<Dispatch>();
}
export async function getSessionDispatch(db: D1Database, owner: string, session: string): Promise<Dispatch | null> {
  return db.prepare("SELECT * FROM script_dispatches WHERE owner_user_id=? AND session_id=?").bind(owner, session).first<Dispatch>();
}
function summaryIndex(raw: Record<string, unknown>): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  const text = ["task_id", "run_id", "session_id", "attempt_id", "case_id", "owner_user_id", "node_id", "sn", "platform", "revision", "script_path", "state", "status", "summary", "reason_code", "failure_stage", "execution_started", "started_at", "finished_at", "detail_availability", "source_hash"];
  for (const key of text) if (raw[key] !== undefined) result[key] = raw[key] === null ? null : String(raw[key]).slice(0, key === "summary" ? 1000 : 500);
  for (const key of ["summary_version", "last_event_seq", "elapsed_ms"]) result[key] = Math.max(0, Number(raw[key] || 0));
  result.sealed = raw.sealed === true;
  result.resource_isolated = raw.resource_isolated === true;
  for (const key of ["started_at", "finished_at"]) {
    const value = raw[key];
    if (typeof value === "number") result[key] = value > 0 ? new Date(value < 1e12 ? value * 1000 : value).toISOString() : null;
  }
  if (raw.assertions && typeof raw.assertions === "object") {
    result.assertions = Object.fromEntries(Object.entries(raw.assertions).filter(([key, value]) => ["assertions", "total", "passed", "failed", "blocked", "inconclusive"].includes(key) && Number.isSafeInteger(value) && Number(value) >= 0));
  }
  if (raw.source_coverage && typeof raw.source_coverage === "object") {
    const source = raw.source_coverage as Record<string, unknown>;
    result.source_coverage = { expected: Math.max(0, Number(source.expected || 0)),
      covered: Array.isArray(source.covered) ? source.covered.filter(v => typeof v === "string").slice(0, 1000).map(v => v.slice(0, 120)) : [],
      missing: Array.isArray(source.missing) ? source.missing.filter(v => typeof v === "string").slice(0, 1000).map(v => v.slice(0, 120)) : [] };
  }
  return result;
}
export async function syncRecord(env: Env, scoutId: string, raw: Record<string, unknown>): Promise<Record<string, unknown>> {
  const attempt = String(raw.attempt_id || raw.session_id || "");
  let job = await getDispatch(env.DB, attempt);
  if (!job) throw new TaskError("record_not_found", "没有这次已准入的执行记录", 404);
  const submitted = JSON.parse(job.params_json) as Record<string, unknown>;
  if (job.scout_id !== scoutId || raw.owner_user_id !== job.owner_user_id || raw.session_id !== job.session_id || raw.attempt_id !== job.attempt_id || raw.run_id !== job.run_id || raw.case_id !== job.case_id || raw.task_id !== job.run_id || raw.node_id !== scoutId || raw.sn !== submitted.sn || raw.platform !== submitted.platform || raw.script_path !== submitted.script_path)
    throw new TaskError("record_owner_mismatch", "记录归属或执行身份不匹配", 403);
  const version = Number(raw.summary_version);
  if (!Number.isSafeInteger(version) || version < 1) throw new TaskError("record_summary_invalid", "记录摘要版本无效", 422);
  const index = summaryIndex(raw), status = String(index.status || "");
  if (!["queued", "running", ...TERMINAL].includes(status) || !["accepted", "running", "completed", "interrupted", "cancel_requested"].includes(String(index.state)))
    throw new TaskError("record_summary_invalid", "记录状态无效", 422);
  const terminal = TERMINAL.has(status);
  if (terminal && !index.sealed) throw new TaskError("record_terminal_unsealed", "终态记录必须封存", 422);
  // Re-read after a lost compare-and-set: concurrent terminal syncs may not
  // overwrite one another with different verdicts. Old deliveries still ACK.
  for (let retry = 0; version > job.sync_version; retry++) {
    if (job.state === "terminal" && !terminal) throw new TaskError("record_state_regression", "已完成的执行不能恢复为运行态", 409);
    const previous = JSON.parse(job.result_json || "{}") as Record<string, unknown>;
    if (job.state === "terminal" && previous.status && previous.status !== status) throw new TaskError("record_terminal_conflict", "已封存记录的结论不能改变", 409);
    if (retry >= 5) throw new TaskError("record_sync_conflict", "记录正在同步，请重试", 409);
    // Cancellation is a command state. A racing running sync must not erase it.
    const changed = await env.DB.prepare("UPDATE script_dispatches SET result_json=?,sync_version=?,state=CASE WHEN ?=1 THEN 'terminal' WHEN state='cancel_requested' THEN state ELSE ? END,accepted_at_ms=CASE WHEN accepted_at_ms=0 THEN ? ELSE accepted_at_ms END,next_attempt_ms=?,updated_at_ms=? WHERE attempt_id=? AND sync_version=?")
      .bind(JSON.stringify(index), version, terminal ? 1 : 0, String(index.state) === "accepted" ? "accepted" : "running", Date.now(), Date.now() + 15_000, Date.now(), attempt, job.sync_version).run();
    job = (await getDispatch(env.DB, attempt))!;
    if (changed.meta.changes) break;
  }
  // Replay after a crash between index and task materialization is harmless.
  const current = (await getDispatch(env.DB, attempt))!;
  const saved = JSON.parse(current.result_json) as Record<string, unknown>;
  await materializeSummary(env, current, saved);
  return { attempt_id: attempt, ack_version: current.sync_version };
}
async function materializeSummary(env: Env, job: Dispatch, summary: Record<string, unknown>): Promise<void> {
  const terminal = Boolean(summary.sealed) && TERMINAL.has(String(summary.status));
  const uncertain = summary.resource_isolated === true || summary.execution_started === "unknown" || summary.state === "interrupted" || summary.reason_code === "write_outcome_unknown";
  const doc = await patchRun(env.DB, job.run_id, run => {
    const row = run.cases.find(c => c.case_id === job.case_id);
    if (!row || Number(row.summary_version || 0) > Number(summary.summary_version || 0)) return;
    Object.assign(row, { status: terminal ? summary.status : "running", summary: summary.summary || "", reason_code: summary.reason_code || "",
      error: summary.status === "pass" ? "" : summary.summary || "", failure_stage: summary.failure_stage || "",
      elapsed_ms: Number(summary.elapsed_ms || 0), execution_started: summary.execution_started || "unknown",
      started_at: summary.started_at || row.started_at, finished_at: terminal ? summary.finished_at || new Date().toISOString() : null,
      summary_version: Number(summary.summary_version), last_event_seq: Number(summary.last_event_seq || 0), sealed: Boolean(summary.sealed),
      resource_isolated: uncertain,
      detail_availability: summary.detail_availability || "available", status_sync_at: new Date().toISOString(),
      assertions: summary.assertions || {}, source_coverage: summary.source_coverage, source_hash: summary.source_hash, events: [] });
    if (summary.revision) run.revision = String(summary.revision);
    // This summary proves the state of the active attempt. Do not retain a
    // temporary disconnect flag after that same attempt has been reconciled.
    run.resource_isolated = run.cases.some(c => c.resource_isolated);
    run.reason_code = run.resource_isolated ? String(summary.reason_code || run.reason_code || "execution_outcome_unknown") : "";
    if (run.cancel_requested) for (const c of run.cases) if (c.status === "pending") { c.status = "cancelled"; c.summary = "任务已取消，本条未执行"; c.execution_started = "no"; }
    if (terminal && uncertain) for (const c of run.cases) if (c.status === "pending") { c.status = "blocked"; c.summary = "上一条执行结果未知，资源保持隔离"; c.reason_code = "resource_isolated"; c.execution_started = "no"; }
    if (run.cases.every(c => TERMINAL.has(c.status))) { run.status = aggregateStatus(run.cases); run.finished_at = new Date().toISOString(); }
    else run.status = run.cancel_requested ? "cancel_requested" : "running";
  });
  if (!doc?.resource_isolated) await env.DB.prepare("UPDATE script_device_leases SET state='held',expires_at_ms=?,updated_at_ms=? WHERE run_id=?").bind(Date.now() + 120_000, Date.now(), job.run_id).run();
  if (doc?.finished_at) await env.DB.prepare("UPDATE script_dispatches SET state='terminal',updated_at_ms=? WHERE run_id=? AND accepted_at_ms=0 AND attempts=0 AND state!='terminal'").bind(Date.now(), job.run_id).run();
  if (terminal && !uncertain) await releaseAccounts(env.DB, job.run_id, job.session_id);
  if (doc?.finished_at && !doc.resource_isolated) { await releaseDevice(env.DB, job.run_id); await releaseAccounts(env.DB, job.run_id); await clearAdmission(env.DB, job.run_id); }
}
export async function cancelTask(env: Env, owner: string, runId: string): Promise<RunDoc> {
  const doc = await getRun(env.DB, runId);
  if (!doc || doc.owner_user_id !== owner) throw new TaskError("task_not_found", "没有这个任务", 404);
  if (doc.finished_at) return doc;
  await patchRun(env.DB, runId, run => { if (!run.finished_at) { run.cancel_requested = true; run.status = "cancel_requested"; } });
  await env.DB.prepare("UPDATE script_dispatches SET state='cancel_requested',next_attempt_ms=0,updated_at_ms=? WHERE run_id=? AND state!='terminal'")
    .bind(Date.now(), runId).run();
  await wakeScheduler(env);
  return (await getRun(env.DB, runId))!;
}
async function markUnknown(env: Env, job: Dispatch, reason: string): Promise<void> {
  const changed = await env.DB.prepare("UPDATE script_dispatches SET state=CASE WHEN state='cancel_requested' THEN state ELSE 'unknown' END,next_attempt_ms=?,updated_at_ms=? WHERE attempt_id=? AND state!='terminal' AND sync_version=?")
    .bind(Date.now() + 15_000, Date.now(), job.attempt_id, job.sync_version).run();
  if (!changed.meta.changes) return;
  await env.DB.prepare("UPDATE script_device_leases SET state='unknown',updated_at_ms=? WHERE run_id=? AND EXISTS (SELECT 1 FROM script_dispatches WHERE attempt_id=? AND sync_version=? AND state IN ('unknown','cancel_requested'))").bind(Date.now(), job.run_id, job.attempt_id, job.sync_version).run();
  await patchRun(env.DB, job.run_id, run => { if (!run.finished_at) {
    const row = run.cases.find(c => c.case_id === job.case_id);
    if (!row || row.sealed || Number(row.summary_version || 0) > job.sync_version) return;
    run.resource_isolated = true; run.reason_code = reason;
    row.resource_isolated = true;
  } });
}
export async function pumpDispatches(env: Env, rpc: Rpc = args => scoutExecute(env, args)): Promise<void> {
  const due = await env.DB.prepare("SELECT d.* FROM script_dispatches d WHERE d.state!='terminal' AND d.next_attempt_ms<=? AND NOT EXISTS (SELECT 1 FROM script_dispatches p WHERE p.run_id=d.run_id AND p.created_at_ms<d.created_at_ms AND p.state!='terminal') ORDER BY d.next_attempt_ms,d.created_at_ms LIMIT 4").bind(Date.now()).all<Dispatch>();
  const seen = new Set<string>();
  for (const job of due.results || []) {
    if (seen.has(job.run_id)) continue;
    seen.add(job.run_id);
    const earlier = await env.DB.prepare("SELECT attempt_id FROM script_dispatches WHERE run_id=? AND created_at_ms<? AND state!='terminal' LIMIT 1").bind(job.run_id, job.created_at_ms).first();
    if (earlier) continue;
    const claimed = await env.DB.prepare("UPDATE script_dispatches SET next_attempt_ms=?,attempts=attempts+1 WHERE attempt_id=? AND next_attempt_ms<=? AND state!='terminal'")
      .bind(Date.now() + 20_000, job.attempt_id, Date.now()).run();
    if (!claimed.meta.changes) continue;
    const run = await getRun(env.DB, job.run_id);
    if (!run || run.finished_at) continue;
    const identity = { task_id: job.run_id, session_id: job.session_id, attempt_id: job.attempt_id, owner_user_id: job.owner_user_id };
    try {
      await registerAdmission(env.DB, { run_id: job.run_id, session_id: job.session_id, case_id: job.case_id, sn: run.sn, scout_id: job.scout_id,
        project_id: run.project_id, app_id: run.app_id, owner_user_id: run.owner_user_id, platform: run.platform });
      let out: ScoutRpcResult;
      if (run.cancel_requested || job.state === "cancel_requested") {
        if (!job.accepted_at_ms && job.attempts === 0) {
          await syncRecord(env, job.scout_id, { ...JSON.parse(job.params_json), ...identity, node_id: job.scout_id, state: "completed", status: "cancelled", summary: "任务取消，本条未开始", summary_version: Math.max(1, job.sync_version + 1), sealed: true, execution_started: "no", detail_availability: "missing" });
          continue;
        }
        out = await rpc({ scout_id: job.scout_id, capability_id: "record.cancel", params: identity, timeout_ms: 5000 });
      } else if (job.accepted_at_ms || job.state === "unknown" || job.state === "accepted" || job.state === "running") {
        out = await rpc({ scout_id: job.scout_id, capability_id: "record.status", params: identity, timeout_ms: 5000 });
        if (out.error === "record_not_found" && !job.accepted_at_ms) {
          // An ACK may have been lost. Identical attempt delivery is durable-idempotent.
          out = await submitJob(env, rpc, job, run);
        }
      } else out = await submitJob(env, rpc, job, run);
      if (!out.ok) { await markUnknown(env, job, out.error || "record_unavailable"); continue; }
      const summary = merged(out);
      if (summary.attempt_id && summary.summary_version) {
        await syncRecord(env, job.scout_id, summary);
      } else await markUnknown(env, job, "record_summary_missing");
    } catch { await markUnknown(env, job, "dispatch_retry_pending"); }
  }
}
async function submitJob(env: Env, rpc: Rpc, job: Dispatch, run: RunDoc): Promise<ScoutRpcResult> {
  const changed = await env.DB.prepare("UPDATE script_dispatches SET state='submitting',updated_at_ms=? WHERE attempt_id=? AND state!='cancel_requested' AND state!='terminal'")
    .bind(Date.now(), job.attempt_id).run();
  if (!changed.meta.changes) {
    const current = (await getDispatch(env.DB, job.attempt_id))!;
    if (current.sync_version) return { http: 200, ok: true, status: "pass", error: "", extra: {}, data: JSON.parse(current.result_json) };
    if (current.state === "cancel_requested" && !job.accepted_at_ms && !job.attempts) return { http: 200, ok: true, status: "pass", error: "", extra: {}, data: {
      ...JSON.parse(job.params_json), node_id: job.scout_id,
      state: "completed", status: "cancelled", summary: "任务取消，本条未开始", summary_version: 1, sealed: true, execution_started: "no", detail_availability: "missing" } };
    throw new TaskError("task_cancelled", "任务已请求取消");
  }
  return rpc({ scout_id: job.scout_id, capability_id: "script.submit", timeout_ms: 5000,
    params: JSON.parse(job.params_json), execute: { run_id: run.run_id, sn: run.sn, platform: run.platform, device_hint: JSON.parse(job.hint_json) } });
}
export async function deviceAvailability(env: Env, owner: string, args: { only_online?: boolean; app_id?: string } = {}): Promise<Record<string, unknown>[]> {
  const scouts = await env.DB.prepare("SELECT scout_id FROM scouts WHERE owner_user_id=?").bind(owner).all<{ scout_id: string }>();
  const owned = new Set((scouts.results || []).map(s => s.scout_id));
  let platforms: string[] = [];
  if (args.app_id) {
    const app = await env.DB.prepare("SELECT platform FROM apps WHERE app_id=? AND owner_user_id=?").bind(args.app_id, owner).first<{ platform: string }>();
    if (!app) throw new TaskError("app_not_found", "没有这个应用", 404);
    platforms = String(app.platform || "").split(",").filter(Boolean);
  }
  const leases = await env.DB.prepare("SELECT * FROM script_device_leases WHERE state!='released'").all<DeviceLease>();
  const raw = await listDevices(env.DB, args.only_online !== false);
  return (raw as Array<Record<string, unknown>>).filter(d => owned.has(String(d.scout_id))).map(d => {
    const busy = (leases.results || []).filter(l => l.sn === d.device_id), web = d.platform === "web", full = busy.length >= (web ? 4 : 1);
    const compatible = !platforms.length || platforms.includes(String(d.platform));
    const blocked = !d.online ? "node_offline" : !d.ready ? "device_not_ready" : !compatible ? "app_incompatible" : full ? busy.some(b => b.state === "unknown") ? "isolation" : web ? "web_parallel_full" : "busy" : "";
    const meta = d.meta && typeof d.meta === "object" ? d.meta as Record<string, unknown> : {};
    return { ...d, sn: d.device_id, node_id: d.scout_id, current_scout_id: d.scout_id, type: d.platform, model: d.label || d.device_id,
      node_alive: Boolean(d.online), channels: meta.channels || {}, status: d.online ? "online" : "offline", executable: !blocked,
      blocking_code: blocked, blocked_reason: blocked, busy_task_id: web ? "" : busy[0]?.run_id || "", web_parallel_full: web && full };
  });
}
