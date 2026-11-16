import { ProjectAccessError, canReadRun } from "./project_access";
/** Shared HTTP business routes for persistent tasks, records and account pools. */
import type { Env } from "./env";
import { AccountError, listAccounts, saveAccount, deleteAccount } from "./account_pool";
import { getRun, listRuns, toTaskJson } from "./run_store";
import { TaskError, cancelTask, deviceAvailability } from "./task_scheduler";
import { artifactChunk, artifactResponse, listSessions, readEvents, readRecord, screenshotMetadata } from "./record_proxy";

function ok(data: unknown): Response { return Response.json({ code: 200, ok: true, data }); }
function number(value: string | null, fallback: number): number { const n = Number(value); return value && Number.isFinite(n) ? Math.max(0, Math.floor(n)) : fallback; }
export async function executionRoutes(request: Request, env: Env, owner: string, pathname: string): Promise<Response | null> {
  const url = new URL(request.url), q = url.searchParams, method = request.method;
  try {
    const accounts = /^\/project\/([^/]+)\/accounts(?:\/([^/]+))?$/.exec(pathname);
    if (accounts) {
      const project = decodeURIComponent(accounts[1]), id = accounts[2] ? decodeURIComponent(accounts[2]) : "";
      if (method === "GET" && !id) return ok(await listAccounts(env.DB, owner, project, q.get("env") || ""));
      if ((method === "POST" && !id) || (["PUT", "PATCH"].includes(method) && id)) {
        const body = await request.json() as Record<string, unknown>;
        return ok({ account: await saveAccount(env.DB, owner, project, body, id) });
      }
      if (method === "DELETE" && id) { await deleteAccount(env.DB, owner, project, id); return ok({ deleted: true }); }
    }
    if (pathname === "/case-runner/tasks" && method === "GET") return ok(await listRuns(env.DB, owner, { app_id: q.get("app_id") || "", project_id: q.get("project_id") || "", status: q.get("status") || "", limit: number(q.get("limit"), 30), offset: number(q.get("offset"), 0) }));
    if (pathname === "/case-runner/tasks/summary" && method === "GET") {
      const rows = await env.DB.prepare("SELECT status,COUNT(*) AS count FROM app_regression_runs WHERE owner_user_id=? GROUP BY status").bind(owner).all<{ status: string; count: number }>();
      const counts = Object.fromEntries((rows.results || []).map(r => [r.status, Number(r.count)]));
      return ok({ total: Object.values(counts).reduce((a, b) => a + b, 0), running: (counts.running || 0) + (counts.queued || 0) + (counts.cancel_requested || 0), failed: counts.fail || 0, succeeded: counts.pass || 0 });
    }
    if (pathname === "/case-runner/devices" && method === "GET") return ok(await deviceAvailability(env, owner, { app_id: q.get("app_id") || "", only_online: false }));
    const task = /^\/case-runner\/tasks\/([^/]+)(\/cancel)?$/.exec(pathname);
    if (task) {
      const id = decodeURIComponent(task[1]);
      if (task[2] && method === "POST") return ok(toTaskJson(await cancelTask(env, owner, id)));
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
