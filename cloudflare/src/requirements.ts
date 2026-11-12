/** Requirement catalog: id, title, and one source link. Case bodies stay in the doc. */

const REQ_ID = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/;

export type RequirementRow = {
  id: string;
  title: string;
  source_url: string;
  app_id: string;
  project_id: string;
};

export function requirementIdOk(value: string): boolean {
  return REQ_ID.test(value);
}

export function sourceUrlOk(value: string): boolean {
  const text = String(value || "").trim();
  if (!text || text.length > 2000 || !(text.startsWith("http://") || text.startsWith("https://"))) {
    return false;
  }
  return !/[\s\u0000-\u001f]/.test(text);
}

export async function listRequirements(db: D1Database, projectId: string): Promise<RequirementRow[]> {
  const { results } = await db
    .prepare(
      `SELECT project_id, requirement_id, app_id, title, source_url
       FROM project_requirements WHERE project_id = ? ORDER BY requirement_id`,
    )
    .bind(projectId)
    .all<{
      project_id: string;
      requirement_id: string;
      app_id: string;
      title: string;
      source_url: string;
    }>();
  return (results || []).map((row) => ({
    id: row.requirement_id,
    title: row.title,
    source_url: row.source_url || "",
    app_id: row.app_id || "",
    project_id: row.project_id,
  }));
}

export async function saveRequirement(
  db: D1Database,
  input: { project_id: string; app_id: string; requirement_id: string; title: string; source_url?: string },
): Promise<RequirementRow> {
  const project_id = String(input.project_id || "").trim();
  const requirement_id = String(input.requirement_id || "").trim();
  const title = String(input.title || "").trim();
  const source_url = String(input.source_url || "").trim();
  const app_id = String(input.app_id || "").trim();
  if (!project_id) throw new Error("缺少 project_id");
  if (!requirementIdOk(requirement_id)) {
    throw new Error("需求 ID 须以字母开头，只含字母、数字、下划线或连字符，最长 64 位");
  }
  if (!title || title.length > 200) throw new Error("请输入 1 到 200 字的需求名称");
  if (source_url && !sourceUrlOk(source_url)) throw new Error("用例链接须是 http 或 https 地址");
  const now = new Date().toISOString();
  await db
    .prepare(
      `INSERT INTO project_requirements
         (project_id, requirement_id, app_id, title, source_url, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(project_id, requirement_id) DO UPDATE SET
         app_id=excluded.app_id,
         title=excluded.title,
         source_url=excluded.source_url,
         updated_at=excluded.updated_at`,
    )
    .bind(project_id, requirement_id, app_id, title, source_url, now, now)
    .run();
  return { id: requirement_id, title, source_url, app_id, project_id };
}
