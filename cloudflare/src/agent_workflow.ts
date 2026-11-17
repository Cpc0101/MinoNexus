/** User-owned investigation budgets. Never executes or admits a Scout case. */
export const STAGES = ["analysis", "authoring", "preparation", "verification"] as const;
const OUTCOMES = ["progress", "blocked", "complete", "failed", "deferred"];
const NOTE = "Agent progress is not a test verdict; verify assertions in the original Scout task.";
type Args = Record<string, unknown>;
type Attempt = { attempt_id: string; unit: string; stage: string; action: string; writes: boolean; blocker: string;
  evidence: string[]; task_id: string; outcome: string; write_outcome: string; note: string; started_at: string; finished_at: string };
type Resolution = { attempt_id: string; task_id: string; evidence: string[]; resolved_at: string };
type State = { workflow_id: string; scope: string; stage_budget: number; total_budget: number; units: string[];
  attempts: Attempt[]; resolutions: Record<string, Resolution>; created_at: string };

function text(value: unknown, name: string, limit = 2000, required = true): string {
  if (typeof value !== "string" || [...value].length > limit || (required && !value.trim())) throw new Error(`Invalid ${name}`);
  return value.trim();
}
function facts(value: unknown): string[] {
  if (!Array.isArray(value) || value.length > 64) throw new Error("Invalid evidence");
  return [...new Set(value.map(v => text(v, "evidence")))].sort();
}
function resolutionFor(state: State, id: string): Resolution | undefined {
  return Object.hasOwn(state.resolutions, id) ? state.resolutions[id] : undefined;
}
function unresolved(state: State) {
  return state.attempts.filter(a => a.write_outcome === "unknown" && !resolutionFor(state, a.attempt_id))
    .map(({unit, attempt_id, task_id}) => ({unit, attempt_id, task_id}));
}
export function workflowSummary(state: State): Args {
  const stages: Record<string, Record<string, number>> = Object.fromEntries(STAGES.map(s => [s,
    Object.fromEntries(["pending", "in_progress", ...OUTCOMES].map(o => [o, 0]))]));
  const independent: string[] = [], unknown = unresolved(state);
  const units = state.units.map(unit => {
    const progress: Record<string, { status: string; attempts: number; remaining: number; task_id: string; blocker: string }> = {};
    for (const stage of STAGES) {
      const history = state.attempts.filter(a => a.unit === unit && a.stage === stage), latest = history.at(-1);
      const status = latest ? latest.outcome || "in_progress" : "pending";
      progress[stage] = { status, attempts: history.length, remaining: Math.max(0, state.stage_budget - history.length),
        task_id: latest?.task_id || "", blocker: latest?.blocker || "" };
      stages[stage][status]++;
    }
    if (progress.verification.status !== "complete" && !Object.values(progress).some(p =>
      ["blocked", "failed", "deferred", "in_progress"].includes(p.status))) independent.push(unit);
    return {unit, stages: progress};
  });
  return {workflow_id: state.workflow_id, scope: state.scope, stages, units, attempts_used: state.attempts.length,
    total_remaining: Math.max(0, state.total_budget - state.attempts.length), unresolved_writes: unknown,
    independent_units: independent.filter(u => !unknown.some(a => a.unit === u)), attempts: structuredClone(state.attempts),
    resolutions: structuredClone(state.resolutions), verification_note: NOTE};
}
export function workflowTransition(old: State | null, operation: string, workflowId: string, args: Args, now: string): [State, Args] {
  workflowId = text(workflowId, "workflow_id", 128);
  let state = old ? structuredClone(old) : null;
  if (operation === "init") {
    const scope = text(args.scope, "scope");
    if (!Array.isArray(args.units) || !args.units.length || args.units.length > 1000) throw new Error("Invalid units");
    const units = [...new Set(args.units.map(u => text(u, "unit", 128)))].sort();
    const stageBudget = args.stage_budget === undefined ? 6 : args.stage_budget;
    const totalBudget = args.total_budget === undefined ? 24 : args.total_budget;
    if (typeof stageBudget !== "number" || typeof totalBudget !== "number" || !Number.isInteger(stageBudget) ||
      !Number.isInteger(totalBudget) || stageBudget < 1 || stageBudget > totalBudget || totalBudget > 10000) throw new Error("Invalid budgets");
    if (state && (state.scope !== scope || state.stage_budget !== stageBudget || state.total_budget !== totalBudget)) throw new Error("Existing workflow scope/budgets differ");
    state ||= {workflow_id: workflowId, scope, stage_budget: stageBudget, total_budget: totalBudget,
      units: [], attempts: [], resolutions: {}, created_at: now};
    state.units = [...new Set([...state.units, ...units])].sort();
    return [state, workflowSummary(state)];
  }
  if (!state) throw new Error("Unknown workflow");
  if (operation === "get") return [state, workflowSummary(state)];
  const attemptId = text(args.attempt_id, "attempt_id", 128), attempt = state.attempts.find(a => a.attempt_id === attemptId);
  let evidence = facts(args.evidence === undefined ? [] : args.evidence);
  if (operation === "start") {
    const unit = text(args.unit, "unit", 128), stage = args.stage;
    const action = text(args.action, "action", 500), blocker = text(args.blocker === undefined ? "" : args.blocker, "blocker", 2000, false);
    const writes = args.writes === undefined ? false : args.writes;
    if (!state.units.includes(unit) || !STAGES.includes(stage as typeof STAGES[number]) || typeof writes !== "boolean") throw new Error("Invalid unit, stage or writes");
    if (attempt) {
      if (attempt.unit !== unit || attempt.stage !== stage || attempt.action !== action || attempt.writes !== writes) throw new Error("Attempt ID already belongs to a different investigation");
      return [state, {allowed: false, reason: "attempt_exists", attempt, next: "read_existing_attempt"}];
    }
    const active = state.attempts.find(a => a.unit === unit && !a.outcome);
    if (active) return [state, {allowed: false, reason: "attempt_in_progress", attempt_id: active.attempt_id, next: "read_existing_attempt"}];
    const unknown = unresolved(state).filter(a => a.unit === unit);
    if (writes && unknown.length) return [state, {allowed: false, reason: "write_outcome_unknown", attempts: unknown, next: "reconcile_existing_task"}];
    const used = state.attempts.length, stageUsed = state.attempts.filter(a => a.unit === unit && a.stage === stage).length;
    if (used >= state.total_budget || stageUsed >= state.stage_budget) return [state, {allowed: false, reason: "budget_exhausted", used, stage_used: stageUsed,
      next: used >= state.total_budget ? "deliver_workflow" : "deliver_phase_and_continue_independent_units"}];
    for (const prior of [...state.attempts].reverse()) {
      if (prior.unit === unit && prior.stage === stage && ["blocked", "failed"].includes(prior.outcome) && prior.action === action &&
        (!blocker || prior.blocker === blocker) && !evidence.some(f => !prior.evidence.includes(f))) return [state,
        {allowed: false, reason: "repeated_blocker_without_evidence", attempt_id: prior.attempt_id, next: "change_approach_or_deliver_phase"}];
    }
    state.attempts.push({attempt_id: attemptId, unit, stage: stage as string, action, writes, blocker, evidence, task_id: "", outcome: "",
      write_outcome: "known", note: "", started_at: now, finished_at: ""});
    return [state, {allowed: true, attempt_id: attemptId, stage_remaining: state.stage_budget - stageUsed - 1,
      total_remaining: state.total_budget - used - 1, next: "perform_declared_investigation"}];
  }
  if (!attempt) throw new Error("Unknown attempt");
  if (operation === "finish") {
    const outcome = args.outcome, writeOutcome = args.write_outcome === undefined ? "known" : args.write_outcome;
    const blocker = text(args.blocker === undefined ? "" : args.blocker, "blocker", 2000, false);
    const taskId = text(args.task_id === undefined ? "" : args.task_id, "task_id", 128, false);
    const note = text(args.note === undefined ? "" : args.note, "note", 2000, false);
    if (!OUTCOMES.includes(outcome as string) || !["known", "unknown"].includes(writeOutcome as string)) throw new Error("Unsupported outcome");
    if (["blocked", "failed"].includes(outcome as string) && !blocker) throw new Error("Blocked/failed investigation must name its concrete blocker");
    evidence = [...new Set([...evidence, ...attempt.evidence])].sort();
    if (outcome === "complete" && (!evidence.length || writeOutcome === "unknown")) throw new Error("Phase completion requires evidence and a known outcome");
    if (writeOutcome === "unknown" && (!attempt.writes || !taskId)) throw new Error("Unknown write outcome requires a writing attempt and its original task ID");
    const values = {outcome: outcome as string, write_outcome: writeOutcome as string, blocker, task_id: taskId, note, evidence};
    if (attempt.outcome) {
      if (Object.entries(values).some(([k, v]) => JSON.stringify(attempt[k as keyof Attempt]) !== JSON.stringify(v))) throw new Error("Finished attempt is immutable");
    } else Object.assign(attempt, values, {finished_at: now});
    return [state, structuredClone(attempt)];
  }
  if (operation === "resolve") {
    const taskId = text(args.task_id, "task_id", 128);
    if (!evidence.length || attempt.write_outcome !== "unknown" || taskId !== attempt.task_id) throw new Error("Reconcile the original task with concrete evidence");
    let resolution = resolutionFor(state, attemptId);
    if (resolution && JSON.stringify(resolution.evidence) !== JSON.stringify(evidence)) throw new Error("Existing reconciliation is immutable");
    resolution ||= {attempt_id: attemptId, task_id: taskId, evidence, resolved_at: now};
    Object.defineProperty(state.resolutions, attemptId, {value: resolution, enumerable: true, configurable: true, writable: true});
    return [state, structuredClone(resolution)];
  }
  throw new Error("Unsupported operation");
}

export class WorkflowError extends Error {
  constructor(message: string, public http = 422) { super(message); }
}
export async function operateWorkflow(db: D1Database, owner: string, workflowId: string, operation: string, args: Args): Promise<Args> {
  try { owner = text(owner, "owner_user_id", 128); workflowId = text(workflowId, "workflow_id", 128); }
  catch (e) { throw new WorkflowError((e as Error).message); }
  for (let retry = 0; retry < 5; retry++) {
    const row = await db.prepare("SELECT state_json,revision,updated_at FROM agent_workflows WHERE owner_user_id=? AND workflow_id=?")
      .bind(owner, workflowId).first<{state_json: string; revision: number; updated_at: string}>();
    const old: State | null = row ? JSON.parse(row.state_json) : null, now = new Date().toISOString();
    let state: State, result: Args;
    try { [state, result] = workflowTransition(old, operation, workflowId, args, now); }
    catch (e) { throw new WorkflowError((e as Error).message, (e as Error).message === "Unknown workflow" ? 404 : 422); }
    const encoded = JSON.stringify(state);
    if (operation === "get" || encoded === row?.state_json) return {...result, revision: row?.revision, updated_at: row?.updated_at};
    if (new TextEncoder().encode(encoded).length > 2_000_000) throw new WorkflowError("Workflow record limit reached; deliver this scope and start a new workflow");
    const changed = row ? await db.prepare("UPDATE agent_workflows SET state_json=?,revision=revision+1,updated_at=? WHERE owner_user_id=? AND workflow_id=? AND revision=?")
      .bind(encoded, now, owner, workflowId, row.revision).run() : await db.prepare("INSERT OR IGNORE INTO agent_workflows(owner_user_id,workflow_id,state_json,revision,updated_at) VALUES(?,?,?,1,?)")
      .bind(owner, workflowId, encoded, now).run();
    if (changed.meta.changes === 1) return {...result, revision: (row?.revision || 0) + 1, updated_at: now};
  }
  throw new WorkflowError("Workflow changed concurrently; read the existing attempt before retrying", 409);
}
