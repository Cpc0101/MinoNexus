/**
 * Run ledger on D1 — roughly mino_nexus.casework.run_store (task json shape) plus
 * script admissions for Scout script.callback validation.
 */

export const RUN_LIVE = new Set(["queued", "running", "cancel_requested", "unknown"]);
const CASE_TERMINAL = new Set(["pass", "fail", "blocked", "declined", "skipped", "cancelled"]);
const MAX_EVENTS = 300;

export type CaseEvent = Record<string, unknown>;

export type CaseRow = {
  case_id: string;
  status: string;
  summary: string;
  error: string;
  reason_code: string;
  failure_stage: string;
  elapsed_ms: number;
  sn: string;
  session_id: string;
  script_path: string;
  execution_started: string;
  started_at: string | null;
  finished_at: string | null;
  events: CaseEvent[];
  attempt_id?: string;
  record_source?: string;
  record_node_id?: string;
  detail_availability?: string;
  status_sync_at?: string;
  summary_version?: number;
  last_event_seq?: number;
  sealed?: boolean;
  assertions?: Record<string, number>;
  source_coverage?: Record<string, unknown>;
  source_hash?: string;
  resource_isolated?: boolean;
};

export type RunDoc = {
  run_id: string;
  task_id: string;
  client_request_id: string;
  owner_user_id: string;
  project_id: string;
  app_id: string;
  app_name: string;
  app_key: string;
  scout_id: string;
  sn: string;
  platform: string;
  env_profile: string;
  env_surface: string;
  revision: string;
  repo_url: string;
  repo_branch: string;
  status: string;
  error: string;
  reason_code: string;
  started_at: string;
  finished_at: string | null;
  cases: CaseRow[];
  report_url?: string;
  cancel_requested?: boolean;
  resource_isolated?: boolean;
  lease_id?: string;
  lease_generation?: number;
  request_fingerprint?: string;
};

export type Admission = {
  run_id: string;
  session_id: string;
  case_id: string;
  sn: string;
  scout_id: string;
  project_id: string;
  app_id: string;
  owner_user_id: string;
  platform: string;
  live: boolean;
};

export function isLive(status: string): boolean {
  return RUN_LIVE.has(String(status || ""));
}

export function newCaseRow(case_id: string, sn: string, session_id: string): CaseRow {
  return {
    case_id,
    status: "pending",
    summary: "",
    error: "",
    reason_code: "",
    failure_stage: "",
    elapsed_ms: 0,
    sn,
    session_id,
    script_path: "",
    execution_started: "no",
    started_at: null,
    finished_at: null,
    events: [],
  };
}

export function appendCaseEvent(row: CaseRow, event: CaseEvent): void {
  if (row.events.length >= MAX_EVENTS) return;
  row.events.push({ ts: new Date().toISOString(), ...event });
}

export async function putRun(db: D1Database, doc: RunDoc): Promise<void> {
  const now = Date.now();
  await db
    .prepare(
      `INSERT INTO app_regression_runs
         (run_id, owner_user_id, project_id, app_id, status, payload_json, created_at_ms, updated_at_ms)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(run_id) DO UPDATE SET
         status=excluded.status,
         payload_json=excluded.payload_json,
         updated_at_ms=excluded.updated_at_ms`,
    )
    .bind(
      doc.run_id,
      doc.owner_user_id,
      doc.project_id,
      doc.app_id,
      doc.status,
      JSON.stringify(doc),
      now,
      now,
    )
    .run();
}

export async function getRun(db: D1Database, runId: string): Promise<RunDoc | null> {
  const id = String(runId || "").trim();
  if (!id) return null;
  const row = await db
    .prepare(`SELECT payload_json FROM app_regression_runs WHERE run_id = ?`)
    .bind(id)
    .first<{ payload_json: string }>();
  if (!row) return null;
  try {
    return JSON.parse(String(row.payload_json)) as RunDoc;
  } catch {
    return null;
  }
}

export async function findRunByRequest(
  db: D1Database,
  ownerUserId: string,
  clientRequestId: string,
): Promise<RunDoc | null> {
  const rid = String(clientRequestId || "").trim();
  if (!rid) return null;
  const row = await db
    .prepare(
      `SELECT payload_json FROM app_regression_runs
       WHERE owner_user_id = ? AND json_extract(payload_json, '$.client_request_id') = ?
       ORDER BY created_at_ms DESC LIMIT 1`,
    )
    .bind(ownerUserId, rid)
    .first<{ payload_json: string }>();
  if (!row) return null;
  try {
    return JSON.parse(String(row.payload_json)) as RunDoc;
  } catch {
    return null;
  }
}

/** Read-modify-write. Runner only patches at case boundaries, callbacks inside a case. */
export async function patchRun(
  db: D1Database,
  runId: string,
  mutate: (doc: RunDoc) => void,
): Promise<RunDoc | null> {
  // Compare-and-swap prevents a result callback racing cancel or another case
  // update from overwriting the entire task index.
  for (let tries = 0; tries < 12; tries++) {
    const row = await db.prepare("SELECT payload_json FROM app_regression_runs WHERE run_id=?").bind(runId).first<{ payload_json: string }>();
    if (!row) return null;
    const doc = JSON.parse(row.payload_json) as RunDoc;
    mutate(doc);
    const changed = await db.prepare("UPDATE app_regression_runs SET status=?,payload_json=?,updated_at_ms=? WHERE run_id=? AND payload_json=?")
      .bind(doc.status, JSON.stringify(doc), Date.now(), runId, row.payload_json).run();
    if (changed.meta.changes) return doc;
  }
  throw new Error("task_index_conflict");
}

export async function patchCase(
  db: D1Database,
  runId: string,
  caseId: string,
  mutate: (row: CaseRow) => void,
): Promise<RunDoc | null> {
  return patchRun(db, runId, (doc) => {
    const row = doc.cases.find((c) => c.case_id === caseId);
    if (row) mutate(row);
  });
}

export function aggregateStatus(cases: CaseRow[]): string {
  if (!cases.length) return "fail";
  const has = (...names: string[]) => cases.some((c) => names.includes(c.status));
  if (cases.every((c) => c.status === "pass")) return "pass";
  if (has("fail", "declined", "skipped")) return "fail";
  if (has("blocked")) return "blocked";
  if (has("cancelled")) return "cancelled";
  return "fail";
}

export async function finishRun(
  db: D1Database,
  runId: string,
  error = "",
  reasonCode = "",
): Promise<RunDoc | null> {
  return patchRun(db, runId, (doc) => {
    if (!isLive(doc.status)) return;
    for (const row of doc.cases) {
      if (row.status === "pending" || row.status === "running") {
        row.status = error ? "fail" : "cancelled";
        row.summary = row.summary || error || "任务已结束，本条未执行";
        row.reason_code = row.reason_code || reasonCode || "batch_aborted";
        row.finished_at = row.finished_at || new Date().toISOString();
      }
    }
    doc.status = aggregateStatus(doc.cases);
    doc.error = error || doc.error;
    doc.reason_code = reasonCode || doc.reason_code;
    doc.finished_at = new Date().toISOString();
  });
}

export function toTaskJson(doc: RunDoc, includeCases = true): Record<string, unknown> {
  const cases = doc.cases || [];
  const count = (...names: string[]) => cases.filter((c) => names.includes(c.status)).length;
  const total = cases.length;
  const completed = cases.filter((c) => CASE_TERMINAL.has(c.status)).length;
  const passed = count("pass");
  const failed = count("fail");
  const judged = passed + failed;
  const current = cases.find((c) => c.status === "running");
  const task: Record<string, unknown> = {
    task_id: doc.run_id,
    run_id: doc.run_id,
    client_request_id: doc.client_request_id || "",
    app_id: doc.app_id,
    app_name: doc.app_name || "",
    app_key: doc.app_key || "",
    project_id: doc.project_id,
    run_type: "manual",
    sn: doc.sn,
    sns: doc.sn ? [doc.sn] : [],
    scout_id: doc.scout_id,
    node_id: doc.scout_id,
    platform: doc.platform,
    env_profile: doc.env_profile || "",
    env_surface: doc.env_surface || "",
    status: doc.status,
    total,
    requested_total: total,
    completed,
    passed,
    failed,
    blocked: count("blocked"),
    declined: count("declined"),
    cancelled: count("cancelled"),
    skipped: count("skipped"),
    revision: doc.revision || "",
    repo_url: doc.repo_url || "",
    repo_branch: doc.repo_branch || "",
    progress: total ? Math.min(100, Math.round((completed / total) * 100)) : 0,
    pass_rate: judged ? Math.round((passed / judged) * 100) : 0,
    error: doc.error || "",
    reason_code: doc.reason_code || "",
    resource_isolated: Boolean(doc.resource_isolated),
    cancel_requested: Boolean(doc.cancel_requested),
    report_url: doc.report_url || `https://workbench.minocc.top/testing/projects/${encodeURIComponent(doc.project_id)}/apps/${encodeURIComponent(doc.app_id)}/tasks/${encodeURIComponent(doc.run_id)}`,
    started_at: doc.started_at || "",
    finished_at: doc.finished_at || null,
    busy: isLive(doc.status),
    current_case_id: current?.case_id || "",
    engine: "script",
    execution_mode: "script",
  };
  if (includeCases) {
    task.cases = cases.map((c) => ({ ...c, title: c.case_id }));
  } else {
    task.case_ids = cases.map((c) => c.case_id);
  }
  return task;
}

export async function registerAdmission(db: D1Database, row: Omit<Admission, "live">): Promise<void> {
  await db
    .prepare(
      `INSERT INTO script_admissions
         (run_id, session_id, case_id, sn, scout_id, project_id, app_id, owner_user_id, platform, live, created_at_ms)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
       ON CONFLICT(run_id) DO UPDATE SET
         session_id=excluded.session_id,
         case_id=excluded.case_id,
         sn=excluded.sn,
         scout_id=excluded.scout_id,
         project_id=excluded.project_id,
         app_id=excluded.app_id,
         owner_user_id=excluded.owner_user_id,
         platform=excluded.platform,
         live=1,
         created_at_ms=excluded.created_at_ms`,
    )
    .bind(
      row.run_id,
      row.session_id,
      row.case_id,
      row.sn,
      row.scout_id,
      row.project_id,
      row.app_id,
      row.owner_user_id,
      row.platform,
      Date.now(),
    )
    .run();
}

export async function getAdmission(db: D1Database, runId: string): Promise<Admission | null> {
  const id = String(runId || "").trim();
  if (!id) return null;
  const row = await db
    .prepare(
      `SELECT run_id, session_id, case_id, sn, scout_id, project_id, app_id, owner_user_id, platform, live
       FROM script_admissions WHERE run_id = ?`,
    )
    .bind(id)
    .first<Omit<Admission, "live"> & { live: number }>();
  if (!row) return null;
  return { ...row, live: Number(row.live) === 1 };
}

export async function suspendAdmission(db: D1Database, runId: string): Promise<void> {
  await db.prepare(`UPDATE script_admissions SET live = 0 WHERE run_id = ?`).bind(runId).run();
}

export async function clearAdmission(db: D1Database, runId: string): Promise<void> {
  await db.prepare(`DELETE FROM script_admissions WHERE run_id = ?`).bind(runId).run();
}

export async function listRuns(db: D1Database, owner: string, args: { app_id?: string; project_id?: string; status?: string; limit?: number; offset?: number } = {}): Promise<Record<string, unknown>> {
  const limit = Math.max(1, Math.min(100, Number(args.limit || 30))), offset = Math.max(0, Number(args.offset || 0));
  const where = "owner_user_id=? AND (?='' OR app_id=?) AND (?='' OR project_id=?) AND (?='' OR status=?)";
  const values = [owner, args.app_id || "", args.app_id || "", args.project_id || "", args.project_id || "", args.status || "", args.status || ""];
  const [page, count] = await Promise.all([
    db.prepare(`SELECT payload_json FROM app_regression_runs WHERE ${where} ORDER BY created_at_ms DESC,run_id DESC LIMIT ? OFFSET ?`).bind(...values, limit, offset).all<{ payload_json: string }>(),
    db.prepare(`SELECT COUNT(*) AS total FROM app_regression_runs WHERE ${where}`).bind(...values).first<{ total: number }>(),
  ]);
  const items = (page.results || []).map(row => toTaskJson(JSON.parse(row.payload_json) as RunDoc));
  return { items, tasks: items, total: Number(count?.total || 0), limit, offset };
}
