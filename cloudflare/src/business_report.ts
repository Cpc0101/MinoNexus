/** Bounded business projection; detailed events remain on Scout. */
export function normalizeTestReport(raw: unknown): Record<string, unknown> {
  const object = (v: unknown): Record<string, unknown> => v && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : {};
  const row = object(raw);
  const attrs = ['', 'automation', 'source_mismatch', 'suspected_product', 'blocked', 'unknown'];
  if (row.version !== 1 || !['authoring', 'verification', 'regression'].includes(String(row.run_purpose)) || !Array.isArray(row.items) || row.items.length > 100)
    throw new Error('record_report_invalid');
  const counts = (value: unknown, keys: string[]) => {
    const data = object(value);
    if (keys.some(k => !Number.isSafeInteger(data[k]) || Number(data[k]) < 0)) throw new Error('record_report_invalid');
    return Object.fromEntries(keys.map(k => [k, data[k]]));
  };
  const fields: Record<string, number> = { source_ref: 120, title: 300, expected: 800, actual: 800, checker: 80, artifact_id: 120, reason_code: 120 };
  const items = row.items.map(value => {
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
  return { version: 1, run_purpose: row.run_purpose, items, items_truncated: row.items_truncated === true,
    counts: businessCounts,
    attribution: attrs.includes(String(row.attribution)) ? row.attribution : 'unknown', reason_code: String(row.reason_code || '').slice(0, 120),
    model_usage: { ...modelCounts,
      total_tokens: Number.isSafeInteger(usage.total_tokens) && Number(usage.total_tokens) >= 0 ? usage.total_tokens : null,
      usage_complete: usage.usage_complete === true },
    ...(row.failure && typeof row.failure === 'object' ? { failure: { source_ref: String(failure.source_ref || '').slice(0, 120),
      title: String(failure.title || '').slice(0, 300), actual: String(failure.actual || '').slice(0, 800),
      event_seq: Number.isSafeInteger(failure.event_seq) && Number(failure.event_seq) >= 0 ? failure.event_seq : 0 } } : {}) };
}
