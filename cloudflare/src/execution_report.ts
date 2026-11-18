/** Pure task projection. Same contract as contract/execution_report.py. */
type Row = Record<string, any>;
const states = ['passed', 'failed', 'blocked', 'declined', 'skipped', 'cancelled', 'unverified', 'pending'];
const live = ['pending', 'queued', 'running', 'accepted', 'cancel_requested'];
const aliases: Record<string, string> = { product_fail: 'fail', tool_blocked: 'blocked', env_blocked: 'blocked', skip: 'skipped', unknown: 'unverified', untestable: 'unverified', unverifiable: 'unverified', unexecutable: 'unverified' };
export function executionPurpose(c: Row, doc: Row): string {
  if (doc.execution_kind === "debug") return "authoring";
  if (doc.source_context?.purpose === 'prepare') return 'preparation';
  const value = c.run_purpose || c.test_report?.run_purpose;
  return value === 'authoring' ? 'authoring' : !value || ['verification', 'regression'].includes(value) ? 'formal' : 'unclassified';
}
export function executionResult(c: Row): string {
  const status = aliases[c.status] || c.status;
  if (live.includes(status)) return 'pending';
  if (status === 'pass' && c.record_source === 'scout') {
    const a = c.assertions || {}, counts = c.test_report?.counts || {};
    if (c.sealed !== true || !a.total || a.fail || a.unknown || ['fail', 'unknown', 'not_run'].some(k => counts[k])) return 'unverified';
  }
  return status === 'pass' ? 'passed' : status === 'fail' ? 'failed' : states.includes(status) ? status : 'unverified';
}
export function executionBucket(cases: Row[]): Row {
  const out: Row = { total: cases.length, completed: 0, executed: 0, not_run: 0, execution_unknown: 0, ...Object.fromEntries(states.map(k => [k, 0])) };
  for (const c of cases) {
    const state = executionResult(c); out[state]++; if (state !== 'pending') out.completed++;
    if (c.started_at || c.status === 'running' || (c.assertions?.total || 0) > 0) out.executed++;
    else if (['pending', 'queued', 'accepted'].includes(c.status) || c.execution_started === 'no' && (['admission', 'preflight'].includes(c.failure_stage) || ['skipped', 'skip', 'cancelled'].includes(c.status))) out.not_run++;
    else out.execution_unknown++;
  }
  out.judged = out.passed + out.failed;
  out.pass_rate = out.judged ? Math.round(out.passed / out.judged * 100) : null;
  return out;
}
export function executionOutcome(cases: Row[], doc: Row = {}): string {
  const selected = cases.filter(c => executionPurpose(c, doc) === 'formal');
  if (!selected.length) return 'done';
  const c = executionBucket(selected);
  if (c.pending) return 'running';
  for (const [key, status] of [['failed', 'fail'], ['blocked', 'blocked'], ['cancelled', 'cancelled'], ['declined', 'declined']]) if (c[key]) return status;
  if (c.passed === c.total) return 'pass';
  return c.skipped === c.total ? 'skipped' : 'unverified';
}
function timestamp(value: unknown): number | null {
  if (typeof value !== 'string' || !/(Z|[+-]\d\d:\d\d)$/.test(value)) return null;
  const ms = Date.parse(value); return Number.isFinite(ms) ? ms : null;
}
export function taskReport(doc: Row): Row {
  const cases: Row[] = (doc.cases || []).filter((c: unknown) => c && typeof c === 'object' && !Array.isArray(c));
  const groups = Object.fromEntries(['formal', 'authoring', 'preparation', 'unclassified'].map(key => [key, executionBucket(cases.filter(c => executionPurpose(c, doc) === key))]));
  const expectations: Row = { total: 0, pass: 0, fail: 0, unknown: 0, not_run: 0, cases_reported: 0, cases_missing: 0 };
  const assertions: Row = { total: 0, pass: 0, fail: 0, unknown: 0, cases_reported: 0, cases_missing: 0 };
  const auxiliary_assertions = { ...assertions };
  for (const c of cases.filter(c => executionPurpose(c, doc) === 'formal')) for (const [target, raw] of [[expectations, c.test_report?.counts], [assertions, c.assertions], [auxiliary_assertions, c.test_report?.auxiliary_assertions]]) {
    const keys = Object.keys(target).filter(k => !k.startsWith('cases_'));
    const valid = raw && keys.every(k => Number.isSafeInteger(raw[k]) && raw[k] >= 0) && raw.total === keys.filter(k => k !== 'total').reduce((sum, k) => sum + raw[k], 0);
    target[valid ? 'cases_reported' : 'cases_missing']++;
    if (valid) for (const key of keys) target[key] += raw[key];
  }
  const starts = cases.map(c => timestamp(c.started_at)).filter((s): s is number => s !== null);
  const ends = cases.filter(c => c.started_at).map(c => timestamp(c.finished_at)).filter((s): s is number => s !== null);
  let issue = cases.every(c => ['started_at', 'finished_at'].every(k => !c[k] || timestamp(c[k]) !== null)) ? '' : 'timestamp_timezone_missing_or_invalid';
  if (cases.some(c => timestamp(c.finished_at) !== null && timestamp(c.started_at) !== null && timestamp(c.finished_at)! < timestamp(c.started_at)!)) issue = 'timestamp_order_invalid';
  const duration = !issue && !executionBucket(cases).execution_unknown && executionBucket(cases).executed === starts.length && starts.length && ends.length === starts.length && cases.every(c => executionResult(c) !== 'pending') ? Math.round(Math.max(...ends) - Math.min(...starts)) : null;
  return { version: 1, unit: 'case_execution', execution_units: executionBucket(cases), ...groups, expectations, assertions, auxiliary_assertions, result_status: executionOutcome(cases, doc),
    timing: { submitted_at: doc.created_at || doc.started_at || null,
      execution_started_at: starts.length ? cases.find(c => timestamp(c.started_at) === Math.min(...starts))?.started_at || null : null,
      execution_finished_at: duration !== null ? cases.find(c => timestamp(c.finished_at) === Math.max(...ends))?.finished_at || null : null, duration_ms: duration, issue },
    source: { task_id: doc.run_id || doc.task_id || '', parent_task_id: doc.source_context?.parent_task_id || doc.derived_from || '' } };
}
