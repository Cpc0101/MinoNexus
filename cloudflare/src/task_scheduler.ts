import { ACCESS_SQL } from "./project_access";
import { normalizeTestReport } from "./business_report";
/** Durable dispatch and result indexes. Never waits for a test to finish. */
import { hubStub, type Env } from "./env";
import { listDevices } from "./db";
import { scoutExecute, merged, type ScoutRpcResult } from "./rpc";
import { releaseAccounts } from "./account_pool";
import { aggregateStatus, getRun, patchRun, registerAdmission, type RunDoc } from "./run_store";

export type Dispatch = {
  attempt_id: string; run_id: string; case_id: string; session_id: string; scout_id: string; owner_user_id: string;
  state: string; params_json: string; hint_json: string; result_json: string; sync_version: number;
  attempts: number; next_attempt_ms: number; accepted_at_ms: number; updated_at_ms: number; created_at_ms: number;
};
type DeviceLease = { resource_key: string; scout_id: string; sn: string; slot: number; run_id: string; lease_id: string; generation: number; state: string; expires_at_ms: number };
export type Rpc = (args: { scout_id: string; capability_id: string; params?: Record<string, unknown>; timeout_ms?: number; execute?: Record<string, unknown> }) => Promise<ScoutRpcResult>;
const TERMINAL = new Set(["pass", "fail", "blocked", "declined", "skipped", "cancelled"]);

export function summaryIsolated(summary: Record<string, unknown>): boolean {
  return summary.resource_isolated === true || summary.execution_started === "unknown"
    || summary.state === "interrupted" || summary.reason_code === "write_outcome_unknown"
    || summary.execution_stopped === false || summary.browser_closed === false;
}

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
  const isolated = await db.prepare("SELECT 1 FROM script_device_leases WHERE sn=? AND state IN ('unknown','isolated') LIMIT 1").bind(doc.sn).first();
  throw new TaskError(isolated ? "isolation" : doc.platform === "web" ? "web_parallel_full" : "busy", "设备仍被任务占用或等待状态核实");
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
  return db.prepare(`SELECT d.* FROM script_dispatches d JOIN app_regression_runs r ON r.run_id=d.run_id JOIN projects p ON p.project_id=r.project_id WHERE d.session_id=? AND ${ACCESS_SQL}`).bind(session,owner,owner).first<Dispatch>();
}
function summaryIndex(raw: Record<string, unknown>): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  const text = ["task_id", "run_id", "session_id", "attempt_id", "case_id", "owner_user_id", "node_id", "sn", "platform", "revision", "script_path", "state", "status", "summary", "reason_code", "failure_stage", "execution_started", "scout_process_generation", "started_at", "finished_at", "detail_availability", "source_hash"];
  for (const key of text) if (raw[key] !== undefined) result[key] = raw[key] === null ? null : String(raw[key]).slice(0, key === "summary" ? 1000 : 500);
  for (const key of ["summary_version", "last_event_seq", "elapsed_ms"]) result[key] = Math.max(0, Number(raw[key] || 0));
  result.sealed = raw.sealed === true;
  result.resource_isolated = raw.resource_isolated === true;
  for (const key of ["execution_stopped", "browser_closed"]) if (typeof raw[key] === "boolean") result[key] = raw[key];
  for (const key of ["started_at", "finished_at"]) {
    const value = raw[key];
    if (typeof value === "number") result[key] = value > 0 ? new Date(value < 1e12 ? value * 1000 : value).toISOString() : null;
  }
  if (raw.assertions && typeof raw.assertions === "object") {
    const counts = raw.assertions as Record<string, unknown>;
    const aliases: Record<string, string> = { total: 'assertions', pass: 'passed', fail: 'failed', unknown: 'inconclusive' };
    result.assertions = Object.fromEntries(Object.entries(aliases).flatMap(([key, alias]) => {
      const value = counts[key] ?? counts[alias];
      return Number.isSafeInteger(value) && Number(value) >= 0 ? [[key, value]] : [];
    }));
  }
  if (raw.test_report !== undefined) result.test_report = normalizeTestReport(raw.test_report);
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
    if (job.state === "terminal" && ["state", "reason_code", "execution_started", "resource_isolated", "execution_stopped", "browser_closed", "scout_process_generation"].some(key => previous[key] !== undefined && previous[key] !== index[key]))
      throw new TaskError("record_terminal_conflict", "已封存记录的执行边界不能改变", 409);
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
  const uncertain = summaryIsolated(summary);
  const resolution = await env.DB.prepare("SELECT proof_json FROM script_resource_resolutions WHERE run_id=?").bind(job.run_id).first<{ proof_json: string }>();
  const doc = await patchRun(env.DB, job.run_id, run => {
    if (resolution) run.resource_resolution = JSON.parse(resolution.proof_json);
    const row = run.cases.find(c => c.case_id === job.case_id);
    if (!row || Number(row.summary_version || 0) > Number(summary.summary_version || 0)) return;
    Object.assign(row, { status: terminal ? summary.status : "running", summary: summary.summary || "", reason_code: summary.reason_code || "",
      error: summary.status === "pass" ? "" : summary.summary || "", failure_stage: summary.failure_stage || "",
      elapsed_ms: Number(summary.elapsed_ms || 0), execution_started: summary.execution_started || "unknown",
      started_at: summary.started_at || row.started_at, finished_at: terminal ? summary.finished_at || new Date().toISOString() : null,
      summary_version: Number(summary.summary_version), last_event_seq: Number(summary.last_event_seq || 0), sealed: Boolean(summary.sealed),
      resource_isolated: uncertain && !run.resource_resolution,
      ...(run.resource_resolution ? { resource_resolution: run.resource_resolution } : {}),
      detail_availability: summary.detail_availability || "available", status_sync_at: new Date().toISOString(),
      assertions: summary.assertions || {}, source_coverage: summary.source_coverage, source_hash: summary.source_hash,
      ...(summary.test_report ? { test_report: summary.test_report } : {}), events: [] });
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
  if (!doc) return;
  if (!doc.finished_at && !doc.resource_isolated) await env.DB.prepare("UPDATE script_device_leases SET state='held',expires_at_ms=?,updated_at_ms=? WHERE run_id=? AND state!='released' AND EXISTS (SELECT 1 FROM app_regression_runs WHERE run_id=? AND json_extract(payload_json,'$.finished_at') IS NULL AND COALESCE(json_extract(payload_json,'$.resource_isolated'),0)!=1)").bind(Date.now() + 120_000, Date.now(), job.run_id, job.run_id).run();
  if (doc.resource_isolated) await env.DB.prepare("UPDATE script_device_leases SET state='isolated',updated_at_ms=? WHERE run_id=? AND state!='released' AND EXISTS (SELECT 1 FROM app_regression_runs WHERE run_id=? AND json_extract(payload_json,'$.resource_isolated')=1)").bind(Date.now(), job.run_id, job.run_id).run();
  if (doc?.finished_at) await env.DB.prepare("UPDATE script_dispatches SET state='terminal',updated_at_ms=? WHERE run_id=? AND accepted_at_ms=0 AND attempts=0 AND state!='terminal'").bind(Date.now(), job.run_id).run();
  if (terminal && !uncertain && !doc.finished_at) await releaseAccounts(env.DB, job.run_id, job.session_id);
  if (doc.finished_at && !doc.resource_isolated) await settleRunResources(env.DB, job.run_id);
}

export async function settleRunResources(db: D1Database, runId: string): Promise<void> {
  // All releases share the same transaction and durable eligibility check.
  // A task projection alone cannot retire an accepted/unknown attempt.
  const safe = `EXISTS (SELECT 1 FROM app_regression_runs r WHERE r.run_id=?
    AND json_extract(r.payload_json,'$.finished_at') IS NOT NULL
    AND COALESCE(json_extract(r.payload_json,'$.resource_isolated'),0)!=1)
    AND NOT EXISTS (SELECT 1 FROM script_dispatches d WHERE d.run_id=? AND (
      d.state!='terminal' OR (NOT (d.accepted_at_ms=0 AND d.attempts=0 AND d.sync_version=0) AND (
      COALESCE(json_extract(d.result_json,'$.sealed'),0)!=1
      OR ((json_extract(d.result_json,'$.resource_isolated')=1
        OR json_extract(d.result_json,'$.execution_started')='unknown'
        OR json_extract(d.result_json,'$.state')='interrupted'
        OR json_extract(d.result_json,'$.reason_code')='write_outcome_unknown'
        OR json_extract(d.result_json,'$.execution_stopped')=0
        OR json_extract(d.result_json,'$.browser_closed')=0)
        AND NOT EXISTS (SELECT 1 FROM script_resource_resolutions v WHERE v.run_id=d.run_id))))))`;
  await db.batch([
    db.prepare(`UPDATE script_device_leases SET run_id='',owner_user_id='',lease_id='',state='released',expires_at_ms=0,updated_at_ms=? WHERE run_id=? AND ${safe}`).bind(Date.now(), runId, runId, runId),
    db.prepare(`UPDATE pool_accounts SET lease_run_id='',lease_session_id='',lease_id='',lease_expires_at_ms=0,updated_at=? WHERE lease_run_id=? AND ${safe}`).bind(Math.floor(Date.now()/1000), runId, runId, runId),
    db.prepare(`UPDATE script_admissions SET live=0 WHERE run_id=? AND ${safe}`).bind(runId, runId, runId),
  ]);
}

async function repairSettlements(env: Env): Promise<void> {
  // A crash may have persisted the terminal dispatch but not its run projection.
  // Restore from the durable summary even when Scout is currently offline.
  const summaries = await env.DB.prepare(`SELECT d.* FROM script_dispatches d
    JOIN app_regression_runs r ON r.run_id=d.run_id
    WHERE d.state='terminal' AND d.sync_version>0
    AND (EXISTS (SELECT 1 FROM script_device_leases l WHERE l.run_id=d.run_id)
      OR EXISTS (SELECT 1 FROM pool_accounts a WHERE a.lease_run_id=d.run_id))
    AND EXISTS (SELECT 1 FROM json_each(r.payload_json,'$.cases') c
      WHERE json_extract(c.value,'$.session_id')=d.session_id
      AND COALESCE(json_extract(c.value,'$.summary_version'),0)<d.sync_version)
    ORDER BY d.updated_at_ms LIMIT 16`).all<Dispatch>();
  for (const job of summaries.results || []) await materializeSummary(env, job, JSON.parse(job.result_json));
  const rows = await env.DB.prepare(`SELECT r.run_id FROM app_regression_runs r
    WHERE json_extract(r.payload_json,'$.finished_at') IS NOT NULL
    AND COALESCE(json_extract(r.payload_json,'$.resource_isolated'),0)!=1
    AND NOT EXISTS (SELECT 1 FROM script_dispatches d WHERE d.run_id=r.run_id AND d.state!='terminal')
    AND (EXISTS (SELECT 1 FROM script_device_leases l WHERE l.run_id=r.run_id)
      OR EXISTS (SELECT 1 FROM pool_accounts a WHERE a.lease_run_id=r.run_id)
      OR EXISTS (SELECT 1 FROM script_admissions a WHERE a.run_id=r.run_id AND a.live=1))
    ORDER BY r.updated_at_ms LIMIT 16`).all<{ run_id: string }>();
  for (const row of rows.results || []) await settleRunResources(env.DB, row.run_id);
}

export async function reconcileTaskResources(env: Env, owner: string, runId: string, rpc: Rpc = args => scoutExecute(env, args)): Promise<RunDoc> {
  const stored = await env.DB.prepare("SELECT payload_json FROM app_regression_runs WHERE run_id=? AND owner_user_id=?").bind(runId, owner).first<{ payload_json: string }>();
  if (!stored) throw new TaskError("task_not_found", "没有这个任务", 404);
  const run = JSON.parse(stored.payload_json) as RunDoc;
  if (!run.finished_at) throw new TaskError("task_still_running", "任务仍在运行，不能解除隔离");
  if (!run.resource_isolated || run.resource_resolution) { await settleRunResources(env.DB, runId); return run; }
  if (run.platform !== "web") throw new TaskError("resource_baseline_required", "实体设备需要停止证据与应用基线核验");
  const dispatches = await env.DB.prepare("SELECT * FROM script_dispatches WHERE run_id=? ORDER BY created_at_ms").bind(runId).all<Dispatch>();
  const proofs: Record<string, unknown>[] = [];
  for (const job of dispatches.results || []) {
    if (job.state !== "terminal") throw new TaskError("stop_evidence_required", "仍有未确认停止的执行");
    // Server-sealed, never-delivered cancellations did not create a browser lane.
    if (!job.accepted_at_ms && !job.attempts) continue;
    const out = await rpc({ scout_id: run.scout_id, capability_id: "record.resource_status", timeout_ms: 5000,
      params: { task_id: runId, run_id: runId, session_id: job.session_id, attempt_id: job.attempt_id, owner_user_id: owner } });
    if (!out.ok) throw new TaskError("stop_evidence_unavailable", out.error || "原执行节点的停止证据暂不可用");
    const summary = merged(out), stop = summary.resource_stop as Record<string, unknown> | undefined;
    if (summary.task_id !== runId || summary.run_id !== runId || summary.session_id !== job.session_id
      || summary.attempt_id !== job.attempt_id || summary.owner_user_id !== owner || summary.node_id !== run.scout_id
      || summary.sn !== run.sn || summary.platform !== "web" || summary.sealed !== true
      || summary.summary_version !== job.sync_version || !TERMINAL.has(String(summary.status))
      || stop?.contract !== "script_resource_stop.v1" || stop.stop_confirmed !== true
      || stop.worker_active !== false || stop.browser_closed !== true || !stop.scout_process_generation)
      throw new TaskError("stop_evidence_required", "执行进程与浏览器会话的停止尚未核实");
    proofs.push({ attempt_id: job.attempt_id, summary_version: job.sync_version, resource_stop: stop });
  }
  if (!proofs.length) throw new TaskError("stop_evidence_required", "没有可核验的执行停止证据");
  const resolution = { state: "released", verification_kind: "web_lane_stop", actor: owner,
    resolved_at: new Date().toISOString(), business_result_unchanged: true, proofs };
  const updated: RunDoc = { ...run, resource_isolated: false, reason_code: "", resource_resolution: resolution,
    cases: run.cases.map(c => ({ ...c, resource_isolated: false, resource_resolution: resolution })) };
  const guard = "EXISTS (SELECT 1 FROM script_resource_resolutions WHERE run_id=? AND owner_user_id=? AND proof_json=?)";
  const proofJson = JSON.stringify(resolution);
  const versions = JSON.stringify((dispatches.results || []).map(job => ({ attempt_id: job.attempt_id, version: job.sync_version })));
  const results = await env.DB.batch([
    env.DB.prepare(`INSERT INTO script_resource_resolutions(run_id,owner_user_id,scout_id,sn,lease_id,generation,proof_json,created_at_ms)
      SELECT ?,?,?,?,?,?,?,? WHERE EXISTS (SELECT 1 FROM app_regression_runs WHERE run_id=? AND owner_user_id=? AND payload_json=?)
      AND EXISTS (SELECT 1 FROM script_device_leases WHERE run_id=? AND scout_id=? AND sn=? AND lease_id=? AND generation=? AND state!='released')
      AND NOT EXISTS (SELECT 1 FROM script_dispatches WHERE run_id=? AND state!='terminal')
      AND (SELECT COUNT(*) FROM script_dispatches WHERE run_id=?)=json_array_length(?)
      AND NOT EXISTS (SELECT 1 FROM script_dispatches d WHERE d.run_id=? AND NOT EXISTS (
        SELECT 1 FROM json_each(?) v WHERE json_extract(v.value,'$.attempt_id')=d.attempt_id
          AND json_extract(v.value,'$.version')=d.sync_version))
      ON CONFLICT(run_id) DO NOTHING`).bind(runId, owner, run.scout_id, run.sn, run.lease_id || "", run.lease_generation || 0, proofJson, Date.now(),
        runId, owner, stored.payload_json, runId, run.scout_id, run.sn, run.lease_id || "", run.lease_generation || 0, runId,
        runId, versions, runId, versions),
    env.DB.prepare(`UPDATE app_regression_runs SET payload_json=?,updated_at_ms=? WHERE run_id=? AND owner_user_id=? AND payload_json=? AND ${guard}`).bind(JSON.stringify(updated), Date.now(), runId, owner, stored.payload_json, runId, owner, proofJson),
    env.DB.prepare(`UPDATE script_device_leases SET run_id='',owner_user_id='',lease_id='',state='released',expires_at_ms=0,updated_at_ms=? WHERE run_id=? AND lease_id=? AND generation=? AND ${guard}`).bind(Date.now(), runId, run.lease_id || "", run.lease_generation || 0, runId, owner, proofJson),
    env.DB.prepare(`UPDATE pool_accounts SET lease_run_id='',lease_session_id='',lease_id='',lease_expires_at_ms=0,updated_at=? WHERE lease_run_id=? AND owner_user_id=? AND ${guard}`).bind(Math.floor(Date.now()/1000), runId, owner, runId, owner, proofJson),
    env.DB.prepare(`UPDATE script_admissions SET live=0 WHERE run_id=? AND ${guard}`).bind(runId, runId, owner, proofJson),
  ]);
  if (!results[0].meta.changes) {
    const current = (await getRun(env.DB, runId))!;
    if (current.resource_resolution) return current;
    throw new TaskError("resource_resolution_conflict", "租约或记录已更新，请重新核验");
  }
  return updated;
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
  await repairSettlements(env);
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
    if (!run) continue;
    const identity = { task_id: job.run_id, session_id: job.session_id, attempt_id: job.attempt_id, owner_user_id: job.owner_user_id };
    try {
      await registerAdmission(env.DB, { run_id: job.run_id, session_id: job.session_id, case_id: job.case_id, sn: run.sn, scout_id: job.scout_id,
        project_id: run.project_id, app_id: run.app_id, owner_user_id: run.owner_user_id, platform: run.platform });
      let out: ScoutRpcResult;
      if (run.cancel_requested || run.finished_at || job.state === "cancel_requested") {
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
    const app = await env.DB.prepare(`SELECT a.platform FROM apps a JOIN projects p ON p.project_id=a.project_id WHERE a.app_id=? AND ${ACCESS_SQL}`).bind(args.app_id, owner, owner).first<{ platform: string }>();
    if (!app) throw new TaskError("app_not_found", "没有这个应用", 404);
    platforms = String(app.platform || "").split(",").filter(Boolean);
  }
  const leases = await env.DB.prepare(`SELECT l.*,r.payload_json FROM script_device_leases l
    LEFT JOIN app_regression_runs r ON r.run_id=l.run_id WHERE l.state!='released'`).all<DeviceLease & { payload_json: string | null }>();
  const raw = await listDevices(env.DB, args.only_online !== false);
  return (raw as Array<Record<string, unknown>>).filter(d => owned.has(String(d.scout_id))).map(d => {
    const busy = (leases.results || []).filter(l => l.sn === d.device_id), web = d.platform === "web", full = busy.length >= (web ? 4 : 1);
    const blockers = busy.map(l => {
      let run: RunDoc | null = null;
      try { run = l.payload_json ? JSON.parse(l.payload_json) as RunDoc : null; } catch { /* missing state stays isolated */ }
      const isolated = !run || run.resource_isolated === true || l.state === "unknown" || l.state === "isolated";
      return { task_id: l.run_id, slot: l.slot, isolated, state: isolated ? "isolated" : "running",
        reason_code: isolated ? run?.reason_code || "execution_outcome_unknown" : "",
        finished_at: run?.finished_at || null };
    });
    const isolated = blockers.filter(b => b.isolated).length;
    const compatible = !platforms.length || platforms.includes(String(d.platform));
    const blocked = !d.online ? "node_offline" : !d.ready ? "device_not_ready" : !compatible ? "app_incompatible" : full ? isolated ? "isolation" : web ? "web_parallel_full" : "busy" : "";
    const meta = d.meta && typeof d.meta === "object" ? d.meta as Record<string, unknown> : {};
    return { ...d, sn: d.device_id, node_id: d.scout_id, current_scout_id: d.scout_id, type: d.platform, model: d.label || d.device_id,
      node_alive: Boolean(d.online), channels: meta.channels || {}, status: d.online ? "online" : "offline", executable: !blocked,
      blocking_code: blocked, blocked_reason: blocked, busy_task_id: web ? "" : busy[0]?.run_id || "", web_parallel_full: web && full,
      active_run_count: busy.length, running_run_count: busy.length - isolated, isolated_run_count: isolated,
      ...(web ? { web_parallel_max: 4 } : {}), blocking_tasks: blockers };
  });
}
