/** Frozen business inputs and explicit Agent-owned task lineage. */
const secretKeys = new Set(["password", "passwd", "otp", "otp_code", "token", "api_key", "authorization", "cookie", "cookies", "access_token", "refresh_token", "secret"]);
export function runtimeData(value: unknown, maxBytes = 16384): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("runtime_data_invalid");
  function visit(item: unknown, depth: number): unknown {
    if (depth > 8) throw new Error("runtime_data_too_deep");
    if (Array.isArray(item)) return item.map(v => visit(v, depth + 1));
    if (item !== null && typeof item === "object") return Object.fromEntries(Object.keys(item).sort().map(key => {
      if (!key || [...key].length > 128 || secretKeys.has(key.toLowerCase()) || key.startsWith("__")) throw new Error("runtime_data_key_invalid");
      return [key, visit((item as Record<string, unknown>)[key], depth + 1)];
    }));
    if (item === null || typeof item === "string" || typeof item === "boolean" || typeof item === "number" && Number.isFinite(item)) return item;
    throw new Error("runtime_data_value_invalid");
  }
  const data = visit(value, 0) as Record<string, unknown>;
  if (new TextEncoder().encode(JSON.stringify(data)).length > maxBytes) throw new Error("runtime_data_too_large");
  return data;
}
export function caseInputs(value: unknown): Record<string, Record<string, unknown>> {
  const data = runtimeData(value ?? {}, 65536);
  return Object.fromEntries(Object.entries(data).map(([key, inputs]) => [key, runtimeData(inputs)]));
}
export function sourceContext(value: unknown): Record<string, unknown> {
  const data = runtimeData(value ?? {});
  if (!Object.keys(data).length) return {};
  if (Object.keys(data).some(k => !["parent_task_id", "purpose", "data_need"].includes(k)) || !["prepare", "resume"].includes(String(data.purpose)) || typeof data.parent_task_id !== "string" || !data.parent_task_id || [...data.parent_task_id].length > 255 || typeof (data.data_need ?? "") !== "string" || [...String(data.data_need ?? "")].length > 2000) throw new Error("source_context_invalid");
  return data;
}

export function dataSummary(value: Record<string, unknown>): Record<string, unknown> {
  const outputs = runtimeData(value.data_outputs === undefined ? {} : value.data_outputs), missing = value.data_missing === undefined ? [] : value.data_missing;
  if (!Array.isArray(missing) || missing.length > 20) throw new Error("data_missing_invalid");
  const portable = runtimeData({missing});
  for (const raw of missing) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error("data_missing_invalid");
    const need = raw as Record<string, unknown>;
    if (Object.keys(need).some(k => !["name", "description", "kind", "attributes"].includes(k)) || typeof need.name !== "string" || !need.name || typeof (need.description ?? "") !== "string" || typeof (need.kind ?? "") !== "string" || !(need.attributes === undefined ? {} : need.attributes) || typeof (need.attributes === undefined ? {} : need.attributes) !== "object" || Array.isArray(need.attributes)) throw new Error("data_missing_invalid");
  }
  return {data_outputs: outputs, data_missing: portable.missing};
}
