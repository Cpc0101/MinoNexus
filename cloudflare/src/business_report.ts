/** Bounded business projection; detailed events remain on Scout. */
export function normalizeTestReport(raw: unknown): Record<string, unknown> {
  const object = (v: unknown): Record<string, unknown> => v && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : {};
  const row = object(raw);
  const attrs = ['', 'automation', 'source_mismatch', 'suspected_product', 'blocked', 'unknown'];
  if (row.version !== 1 || !['authoring', 'verification', 'regression', 'preparation', 'unclassified'].includes(String(row.run_purpose)) || !Array.isArray(row.items) || row.items.length > 100)
    throw new Error('record_report_invalid');
  const counts = (value: unknown, keys: string[]) => {
    const data = object(value);
    if (keys.some(k => !Number.isSafeInteger(data[k]) || Number(data[k]) < 0)) throw new Error('record_report_invalid');
    return Object.fromEntries(keys.map(k => [k, data[k]]));
  };
  const fields: Record<string, number> = { source_ref: 120, title: 300, expected: 800, actual: 800, checker: 80, artifact_id: 120, reason_code: 120 };
  const items: Record<string, unknown>[] = row.items.map(value => {
    const item = object(value);
    if (!['pass', 'fail', 'unknown', 'not_run'].includes(String(item.status))) throw new Error('record_report_invalid');
    return { ...Object.fromEntries(Object.entries(fields).map(([key, limit]) => [key, String(item[key] || '').slice(0, limit)])),
      status: item.status, attribution: attrs.includes(String(item.attribution)) ? item.attribution : 'unknown',
      event_seq: Number.isSafeInteger(item.event_seq) && Number(item.event_seq) >= 0 ? item.event_seq : 0 };
  });
  const usage = object(row.model_usage);
  const failure = object(row.failure);
  const businessCounts = counts(row.counts, ['total', 'pass', 'fail', 'unknown', 'not_run']);
  const modelCounts = counts(usage, ['calls', 'llm_calls', 'vlm_calls', 'elapsed_ms']);
  if (businessCounts.total !== ['pass', 'fail', 'unknown', 'not_run'].reduce((sum, key) => sum + Number(businessCounts[key]), 0)
    || modelCounts.calls !== Number(modelCounts.llm_calls) + Number(modelCounts.vlm_calls)
    || items.length > Number(businessCounts.total) || row.items_truncated !== true && items.length !== businessCounts.total
    || row.items_truncated !== true && ['pass', 'fail', 'unknown', 'not_run'].some(key => items.filter(item => item.status === key).length !== businessCounts[key]))
    throw new Error('record_report_invalid');
  const optional: Record<string, unknown> = {};
  if (row.context !== undefined) {
    if (!row.context || typeof row.context !== 'object' || Array.isArray(row.context)) throw new Error('record_report_invalid');
    const context = object(row.context);
    optional.context = Object.fromEntries(['task_id', 'session_id', 'attempt_id', 'case_id', 'node_id', 'revision', 'script_path', 'source_hash'].map(key => [key, String(context[key] || '').slice(0, 500)]));
  }
  if (row.auxiliary_assertions !== undefined) {
    const auxiliary = counts(row.auxiliary_assertions, ['total', 'pass', 'fail', 'unknown']);
    if (auxiliary.total !== Number(auxiliary.pass) + Number(auxiliary.fail) + Number(auxiliary.unknown)) throw new Error('record_report_invalid');
    optional.auxiliary_assertions = auxiliary;
  }
  if (items.some(item => !item.source_ref) || new Set(items.map(item => item.source_ref)).size !== items.length) throw new Error('record_report_invalid');
  return { version: 1, run_purpose: row.run_purpose, ...optional, items, items_truncated: row.items_truncated === true,
    counts: businessCounts,
    attribution: attrs.includes(String(row.attribution)) ? row.attribution : 'unknown', reason_code: String(row.reason_code || '').slice(0, 120),
    model_usage: { ...modelCounts,
      total_tokens: Number.isSafeInteger(usage.total_tokens) && Number(usage.total_tokens) >= 0 ? usage.total_tokens : null,
      usage_complete: usage.usage_complete === true },
    ...(row.failure && typeof row.failure === 'object' ? { failure: { source_ref: String(failure.source_ref || '').slice(0, 120),
      title: String(failure.title || '').slice(0, 300), actual: String(failure.actual || '').slice(0, 800),
      event_seq: Number.isSafeInteger(failure.event_seq) && Number(failure.event_seq) >= 0 ? failure.event_seq : 0 } } : {}) };
}


export function validateReportSummary(summary: Record<string, any>, payload: Record<string, any>): void {
  const time = (value: unknown): number | null => {
    if (!value) return null;
    if (typeof value !== 'string' || !/(Z|[+-]\d\d:\d\d)$/.test(value) || !Number.isFinite(Date.parse(value))) throw new Error('record_timestamp_invalid');
    return Date.parse(value);
  };
  const start = time(summary.started_at), end = time(summary.finished_at);
  if (start !== null && end !== null && end < start || ['queued', 'running'].includes(summary.status) && end !== null) throw new Error('record_timestamp_invalid');
  const report = summary.test_report || {};
  if (Object.entries(report.context || {}).some(([key, value]) => value && value !== String(summary[key] || ''))) throw new Error('record_report_identity_mismatch');
  if (report.items?.some((item: Record<string, any>) => item.event_seq > (summary.last_event_seq || 0))) throw new Error('record_report_evidence_mismatch');
  const expectedPurpose = payload.source_context?.purpose === 'prepare' ? 'preparation' : summary.run_purpose;
  if (summary.test_report && expectedPurpose && expectedPurpose !== report.run_purpose) throw new Error('record_report_purpose_mismatch');
  const a = summary.assertions;
  if (a !== undefined && (!a || ['total', 'pass', 'fail', 'unknown'].some(k => !Number.isSafeInteger(a[k]) || a[k] < 0) || a.total !== a.pass + a.fail + a.unknown)) throw new Error('record_assertions_invalid');
  if (summary.status === 'pass' && (!a?.total || a.total !== a.pass || ['fail', 'unknown', 'not_run'].some(k => report.counts?.[k]) || summary.source_coverage?.missing?.length)) throw new Error('record_pass_without_assertions');
}
