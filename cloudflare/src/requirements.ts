/** Project requirements with explicit application scope and document links. */
const REQ_ID = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/;
export type RequirementRow = {
  id: string; title: string; source_url: string; cases_url: string;
  app_id: string; app_ids: string[]; project_id: string;
};
export type RequirementInput = {
  project_id: string; requirement_id: string; new_requirement_id?: string;
  title?: string; source_url?: string; cases_url?: string; app_id?: string; app_ids?: unknown;
};
export function requirementIdOk(value: string): boolean { return REQ_ID.test(value); }
export function sourceUrlOk(value: string): boolean {
  return !!value && value.length <= 2000 && /^https?:\/\//.test(value) && !/[\s\u0000-\u001f]/.test(value);
}
function publicRow(row: Record<string, any>): RequirementRow {
  const app_ids = JSON.parse(row.app_ids_json || "[]") as string[];
  return { id: row.requirement_id, title: row.title, source_url: row.source_url || "",
    cases_url: row.cases_url || "", app_ids, app_id: app_ids.length === 1 ? app_ids[0] : "", project_id: row.project_id };
}
export async function listRequirements(db: D1Database, projectId: string): Promise<RequirementRow[]> {
  const { results } = await db.prepare("SELECT * FROM project_requirements WHERE project_id = ? ORDER BY requirement_id").bind(projectId).all();
  return (results || []).map(publicRow);
}
async function validated(db: D1Database, input: RequirementInput, old?: RequirementRow): Promise<RequirementRow> {
  const id = String(input.new_requirement_id ?? input.requirement_id).trim();
  if (!requirementIdOk(id)) throw new Error("需求 ID 须以字母开头，只含字母、数字、下划线或连字符，最长 64 位");
  const title = String(input.title ?? old?.title ?? "").trim();
  if (!title || title.length > 200 || /[\u0000-\u001f]/.test(title)) throw new Error("请输入 1 到 200 字的需求名称");
  const source_url = String(input.source_url ?? old?.source_url ?? "").trim();
  const cases_url = String(input.cases_url ?? old?.cases_url ?? "").trim();
  for (const link of [source_url, cases_url]) if (link && !sourceUrlOk(link)) throw new Error("文档链接须是 http 或 https 地址");
  if (input.app_ids !== undefined && input.app_id !== undefined) throw new Error("app_ids 与 app_id 不能同时填写");
  const raw = input.app_ids ?? (input.app_id !== undefined ? (input.app_id ? [input.app_id] : []) : old?.app_ids ?? []);
  if (!Array.isArray(raw) || raw.length > 100 || raw.some(v => typeof v !== "string" || !v.trim())) throw new Error("app_ids 须为应用 ID 列表");
  const app_ids = [...new Set(raw.map(v => v.trim()))] as string[];
  for (const app of app_ids) {
    if (!await db.prepare("SELECT app_id FROM apps WHERE project_id = ? AND app_id = ?").bind(input.project_id, app).first()) throw new Error("应用不属于当前项目");
  }
  return { id, title, source_url, cases_url, app_ids, app_id: app_ids.length === 1 ? app_ids[0] : "", project_id: input.project_id };
}
export async function saveRequirement(db: D1Database, input: RequirementInput): Promise<RequirementRow> {
  // Existing MCP clients can explicitly rename through save without creating a duplicate.
  if (input.new_requirement_id !== undefined) return updateRequirement(db, input);
  const old = (await listRequirements(db, input.project_id)).find(row => row.id === input.requirement_id);
  const row = await validated(db, input, old);
  const now = new Date().toISOString();
  await db.prepare(`INSERT INTO project_requirements (project_id, requirement_id, app_id, app_ids_json, title, source_url, cases_url, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(project_id, requirement_id) DO UPDATE SET
    app_id=excluded.app_id, app_ids_json=excluded.app_ids_json, title=excluded.title, source_url=excluded.source_url, cases_url=excluded.cases_url, updated_at=excluded.updated_at`)
    .bind(row.project_id, row.id, row.app_id, JSON.stringify(row.app_ids), row.title, row.source_url, row.cases_url, now, now).run();
  return row;
}
export async function updateRequirement(db: D1Database, input: RequirementInput): Promise<RequirementRow> {
  const old = await db.prepare("SELECT * FROM project_requirements WHERE project_id = ? AND requirement_id = ?").bind(input.project_id, input.requirement_id).first();
  if (!old) throw new Error("需求不存在");
  const row = await validated(db, input, publicRow(old));
  if (row.id !== input.requirement_id && await db.prepare("SELECT 1 FROM project_requirements WHERE project_id = ? AND requirement_id = ?").bind(row.project_id, row.id).first()) throw new Error("需求 ID 已在本项目使用");
  const result = await db.prepare(`UPDATE project_requirements SET requirement_id=?, app_id=?, app_ids_json=?, title=?, source_url=?, cases_url=?, updated_at=?
    WHERE project_id=? AND requirement_id=? AND updated_at=?`)
    .bind(row.id, row.app_id, JSON.stringify(row.app_ids), row.title, row.source_url, row.cases_url, new Date().toISOString(), row.project_id, input.requirement_id, old.updated_at).run();
  if (!result.meta.changes) throw new Error("需求已被修改，请重新读取后重试");
  return row;
}
