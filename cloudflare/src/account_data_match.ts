/** Exact metadata preferences, without availability or capacity certification. */
export type DataNeed = { kind: string; object_ref: string; attributes: Record<string, unknown> };
export function validateDataRequirements(value: unknown): DataNeed[] {
  if (value == null) return [];
  if (!Array.isArray(value) || value.length > 20) throw new Error("data_requirements 必须是最多 20 项的列表");
  const needs = value.map(raw => {
    if (!raw || typeof raw !== "object" || Array.isArray(raw) || Object.keys(raw).some(k => !["kind", "object_ref", "attributes"].includes(k))) throw new Error("数据偏好只允许 kind/object_ref/attributes");
    const row = raw as Record<string, unknown>, kind = row.kind, ref = row.object_ref ?? "", attrs = row.attributes ?? {};
    if (typeof kind !== "string" || !kind.trim() || [...kind].length > 64 || typeof ref !== "string" || [...ref].length > 2048 || !attrs || typeof attrs !== "object" || Array.isArray(attrs)) throw new Error("数据偏好格式无效");
    if (Object.values(attrs).some(v => v !== null && (!["string", "number", "boolean"].includes(typeof v) || typeof v === "number" && !Number.isFinite(v)))) throw new Error("数据偏好的 attributes 只接受简单字段");
    return { kind: kind.trim(), object_ref: ref, attributes: attrs as Record<string, unknown> };
  });
  if (new TextEncoder().encode(JSON.stringify(needs)).length > 8192) throw new Error("数据偏好最多 8192 字节");
  return needs;
}
export function dataMatches(rows: Record<string, unknown>[], needs: DataNeed[]): {data_match_count: number; data_matches: Record<string, unknown>[]} {
  const matched = new Map<string, Record<string, unknown>>();
  let count = 0;
  for (const need of needs) {
    const found = rows.filter(row => row.kind === need.kind && (!need.object_ref || row.object_ref === need.object_ref)
      && Object.entries(need.attributes).every(([k, v]) => Object.hasOwn(row.attributes as object, k) && (row.attributes as Record<string, unknown>)[k] === v));
    if (found.length) { count++; for (const row of found) matched.set(String(row.data_id), row); }
  }
  return {data_match_count: count, data_matches: [...matched.values()].slice(0, 20)};
}
export async function rankAccountData(db: D1Database, project: string, accounts: Record<string, unknown>[], requirements: unknown, app = ""): Promise<Record<string, unknown>[]> {
  const needs = validateDataRequirements(requirements);
  if (!needs.length || !accounts.length) return accounts;
  const rows: Record<string, unknown>[] = [];
  const ids = [...new Set(accounts.map(row => String(row.id || row.account_id)))];
  for (let offset = 0; offset < ids.length; offset += 80) {
    const chunk = ids.slice(offset, offset + 80);
    const page = await db.prepare(`SELECT * FROM pool_account_data WHERE project_id=? AND (?='' OR app_id=?) AND account_id IN (${chunk.map(() => "?").join(",")}) ORDER BY updated_at DESC,data_id`).bind(project, app, app, ...chunk).all<Record<string, unknown>>();
    rows.push(...page.results || []);
  }
  const byAccount = new Map<string, Record<string, unknown>[]>();
  for (const raw of rows) {
    const {attributes_json, ...row} = raw;
    const items = byAccount.get(String(row.account_id)) || [];
    items.push({...row, attributes: JSON.parse(String(attributes_json || "{}"))});
    byAccount.set(String(row.account_id), items);
  }
  return accounts.map(row => ({...row, ...dataMatches(byAccount.get(String(row.id || row.account_id)) || [], needs)})).sort((a, b) => b.data_match_count - a.data_match_count);
}
