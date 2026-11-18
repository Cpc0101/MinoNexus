import { debugCase } from "./debug_cases";
import { ProjectAccessError, canReadRun, ACCESS_SQL } from "./project_access";
/** Shared HTTP business routes for persistent tasks, records and account pools. */
import type { Env } from "./env";
import { listAccountData, saveAccountData, deleteAccountData } from "./account_data";
import { AccountError, listAccounts, saveAccount, deleteAccount } from "./account_pool";
import { getRun, listRuns, toTaskJson } from "./run_store";
import { TaskError, cancelTask, deviceAvailability, reconcileTaskResources } from "./task_scheduler";
import { artifactChunk, artifactResponse, listSessions, readEvents, readRecord, screenshotMetadata } from "./record_proxy";

function ok(data: unknown): Response { return Response.json({ code: 200, ok: true, data }); }
function number(value: string | null, fallback: number): number { const n = Number(value); return value && Number.isFinite(n) ? Math.max(0, Math.floor(n)) : fallback; }
export async function executionRoutes(request: Request, env: Env, owner: string, pathname: string): Promise<Response | null> {
  const url = new URL(request.url), q = url.searchParams, method = request.method;
  try {
    const debug = /^\/case-runner\/debug(?:\/([^/]+)(?:\/(execute|status|cancel|reset|close|checkpoint))?)?$/.exec(pathname);
    if (debug && method === "POST") {
      const args = await request.json() as Record<string, unknown>;
      return ok(await debugCase(env, owner, debug[2] || "start", { ...args, ...(debug[1] ? { session_id: decodeURIComponent(debug[1]) } : {}) }));
    }
    const select = /^\/project\/([^/]+)\/resources\/accounts\/select$/.exec(pathname);
    if (select && method === "POST") {
      const body = await request.json() as Record<string, unknown>;
      const result = await listAccounts(env.DB, owner, decodeURIComponent(select[1]), String(body.env || ""), body.data_requirements, String(body.app_id || ""), true);
      return ok({...result, accounts: (result.accounts as Record<string, unknown>[]).filter(row => row.state === "free")});
    }
    const accountData = /^\/project\/([^/]+)\/resources\/accounts\/([^/]+)\/data(?:\/([^/]+))?$/.exec(pathname);
    if (accountData) {
      const project = decodeURIComponent(accountData[1]), account = decodeURIComponent(accountData[2]);
      const id = accountData[3] ? decodeURIComponent(accountData[3]) : "";
      if (method === "GET" && !id) return ok(await listAccountData(env.DB, owner, project, account, q.get("app_id") || "", q.get("kind") || "", number(q.get("limit"), 50), number(q.get("offset"), 0), number(q.get("request_limit"), 20), number(q.get("request_offset"), 0)));
      if (method === "PUT" && !id) return ok({ item: await saveAccountData(env.DB, owner, project, account, await request.json() as Record<string, unknown>) });
      if (method === "DELETE" && id) return ok({ deleted: await deleteAccountData(env.DB, owner, project, account, id) });
    }
    const accounts = /^\/project\/([^/]+)\/accounts(?:\/([^/]+))?$/.exec(pathname);
    if (accounts) {
      const project = decodeURIComponent(accounts[1]), id = accounts[2] ? decodeURIComponent(accounts[2]) : "";
      if (method === "GET" && !id) return ok(await listAccounts(env.DB, owner, project, q.get("env") || "", [], q.get("app_id") || ""));
      if ((method === "POST" && !id) || (["PUT", "PATCH"].includes(method) && id)) {
        const body = await request.json() as Record<string, unknown>;
        return ok({ account: await saveAccount(env.DB, owner, project, body, id) });
      }
      if (method === "DELETE" && id) { await deleteAccount(env.DB, owner, project, id); return ok({ deleted: true }); }
    }
    if (pathname === "/case-runner/tasks" && method === "GET") return ok(await listRuns(env.DB, owner, { app_id: q.get("app_id") || "", project_id: q.get("project_id") || "", status: q.get("status") || "", limit: number(q.get("limit"), 30), offset: number(q.get("offset"), 0) }));
    if (pathname === "/case-runner/tasks/summary" && method === "GET") {
      const appIds = [...new Set((q.get("app_ids") || "").split(",").map(value => value.trim()).filter(Boolean))].slice(0, 100);
      const appFilter = appIds.length ? " AND s.app_id IN (SELECT value FROM json_each(?))" : "";
      const rows = await env.DB.prepare(`SELECT s.app_id,s.total_count,s.running_count,s.failed_count,s.succeeded_count,
          s.latest_created_at_ms,s.latest_run_id,r.payload_json
        FROM app_regression_run_stats s
        JOIN projects p ON p.project_id=s.project_id
        JOIN app_regression_runs r ON r.run_id=s.latest_run_id
        WHERE ${ACCESS_SQL}${appFilter}
        ORDER BY s.latest_created_at_ms DESC,s.latest_run_id DESC LIMIT 101`).bind(owner, owner, ...(appIds.length ? [JSON.stringify(appIds)] : [])).all<{
          app_id: string; total_count: number; running_count: number; failed_count: number;
          succeeded_count: number; latest_run_id: string; payload_json: string;
        }>();
      const seen = new Set<string>();
      const items: Record<string, unknown>[] = [];
      let total = 0, running = 0, failed = 0, succeeded = 0;
      for (const row of rows.results || []) {
        if (seen.size >= 100) break;
        seen.add(row.app_id);
        total += Number(row.total_count || 0); running += Number(row.running_count || 0);
        failed += Number(row.failed_count || 0); succeeded += Number(row.succeeded_count || 0);
        const latest = toTaskJson(JSON.parse(row.payload_json), false);
        items.push({ app_id: row.app_id, running_count: Number(row.running_count || 0), total_count: Number(row.total_count || 0),
          latest_task_id: latest.task_id, last_task_id: latest.task_id,
          status: latest.status, last_status: latest.status, completed: latest.completed, total: latest.total,
          started_at: latest.started_at, last_started_at: latest.started_at, pass_rate: latest.pass_rate, last_pass_rate: latest.pass_rate,
          statistics: latest.statistics, result_status: latest.result_status });
      }
      return ok({ items, total, running, failed, succeeded, truncated: (rows.results || []).length > 100 });
    }
    if (pathname === "/case-runner/devices" && method === "GET") return ok(await deviceAvailability(env, owner, { app_id: q.get("app_id") || "", only_online: false }));
    const task = /^\/case-runner\/tasks\/([^/]+)(\/cancel|\/resources\/reconcile)?$/.exec(pathname);
    if (task) {
      const id = decodeURIComponent(task[1]);
      if (task[2] === "/cancel" && method === "POST") return ok(toTaskJson(await cancelTask(env, owner, id)));
      if (task[2] === "/resources/reconcile" && method === "POST") return ok(toTaskJson(await reconcileTaskResources(env, owner, id)));
      if (!task[2] && method === "GET") {
        const run = await getRun(env.DB, id);
        if (!run || !await canReadRun(env.DB, owner, run)) throw new TaskError("task_not_found", "没有这个任务", 404);
        return ok(toTaskJson(run));
      }
    }
    if (pathname === "/case-runner/sessions" && method === "GET") return ok(await listSessions(env, owner, { run_id: q.get("run_id") || "", app_id: q.get("app_id") || "", case_id: q.get("case_id") || "", limit: number(q.get("limit"), 30), offset: number(q.get("offset"), 0) }));
    const session = /^\/case-runner\/sessions\/([^/]+)\/(record|events|screenshot|artifacts\/([^/]+)(\/chunk)?)$/.exec(pathname);
    if (session && method === "GET") {
      const sid = decodeURIComponent(session[1]);
      if (session[2] === "record") return ok(await readRecord(env, owner, sid, "record.status"));
      if (session[2] === "events") return ok(await readEvents(env, owner, sid, number(q.get("after_seq") ?? q.get("from_seq"), 0), number(q.get("limit"), 100), number(q.get("max_bytes"), 524288)));
      if (session[2] === "screenshot") return ok(await screenshotMetadata(env, owner, sid, number(q.get("seq"), 0), q.get("artifact_id") || ""));
      if (session[4]) return ok(await artifactChunk(env, owner, sid, decodeURIComponent(session[3]), number(q.get("offset"), 0), number(q.get("length"), 32768)));
      if (session[3]) return artifactResponse(env, owner, sid, decodeURIComponent(session[3]), request.headers.get("Range"));
    }
    return null;
  } catch (error) {
    if (error instanceof ProjectAccessError) return Response.json({ok:false,code:error.http,msg:error.message},{status:error.http});
    if (error instanceof TaskError || error instanceof AccountError) return Response.json({ ok: false, code: error.http, reason_code: error.code, msg: error.message }, { status: error.http });
    throw error;
  }
}
