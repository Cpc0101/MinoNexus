/** Lightweight references and request descriptions; no automatic data creation. */
import { requireProjectOwner } from "./project_access";
import { AccountError } from "./account_pool";

async function accountGate(db: D1Database, owner: string, project: string, account: string): Promise<void> {
  owner = await requireProjectOwner(db, project, owner);
  const row = await db.prepare("SELECT id FROM pool_accounts WHERE owner_user_id=? AND project_id=? AND id=?")
    .bind(owner, project, account).first();
  if (!row) throw new AccountError("account_not_found", "没有这个账号", 404);
}
export function validateDataNeed(value: unknown): string {
  if (typeof value !== "string" || [...value].length > 2000) throw new AccountError("account_data_invalid", "data_need 必须是最多 2000 字的描述");
  return value.trim();
}
function validateData(input: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, limit] of Object.entries({ data_id: 128, app_id: 128, kind: 64, object_ref: 2048, title: 300, source_task_id: 128 })) {
    const value = input[key] ?? "";
    if (typeof value !== "string" || [...value].length > limit) throw new AccountError("account_data_invalid", `${key} 必须是最多 ${limit} 字的字符串`);
    out[key] = value.trim();
  }
  if (!out.data_id || !out.kind || !out.object_ref) throw new AccountError("account_data_invalid", "缺少 data_id、kind 或 object_ref");
  const attributes = input.attributes ?? {};
  if (!attributes || typeof attributes !== "object" || Array.isArray(attributes)) throw new AccountError("account_data_invalid", "attributes 必须是对象");
  const encoded = JSON.stringify(attributes, (_key, value) => {
    if (typeof value === "number" && !Number.isFinite(value)) throw new AccountError("account_data_invalid", "attributes 不能包含非有限数值");
    return value;
  });
  if (new TextEncoder().encode(encoded).length > 8192) throw new AccountError("account_data_invalid", "attributes 最多 8192 字节");
  out.attributes_json = encoded;
  return out;
}
function publicData(row: Record<string, unknown>): Record<string, unknown> {
  const { attributes_json, ...rest } = row;
  return { ...rest, attributes: JSON.parse(String(attributes_json || "{}")) };
}
export async function listAccountData(db: D1Database, owner: string, project: string, account: string, app = "", kind = "", limit = 50, offset = 0, requestLimit = 20, requestOffset = 0): Promise<Record<string, unknown>> {
  await accountGate(db, owner, project, account);
  const items = await db.prepare("SELECT * FROM pool_account_data WHERE project_id=? AND account_id=? AND (?='' OR app_id=?) AND (?='' OR kind=?) ORDER BY updated_at DESC,data_id LIMIT ? OFFSET ?")
    .bind(project, account, app, app, kind, kind, Math.min(200, Math.max(1, limit)), Math.max(0, offset)).all<Record<string, unknown>>();
  const requests = await db.prepare("SELECT * FROM pool_account_data_requests WHERE project_id=? AND account_id=? AND (?='' OR app_id=?) ORDER BY updated_at DESC,run_id,session_id LIMIT ? OFFSET ?")
    .bind(project, account, app, app, Math.min(200, Math.max(1, requestLimit)), Math.max(0, requestOffset)).all();
  const total = await db.prepare("SELECT COUNT(*) AS total FROM pool_account_data WHERE project_id=? AND account_id=? AND (?='' OR app_id=?) AND (?='' OR kind=?)")
    .bind(project, account, app, app, kind, kind).first<{ total: number }>();
  const requestTotal = await db.prepare("SELECT COUNT(*) AS total FROM pool_account_data_requests WHERE project_id=? AND account_id=? AND (?='' OR app_id=?)")
    .bind(project, account, app, app).first<{ total: number }>();
  return { items: (items.results || []).map(publicData), total: total?.total ?? 0, requests: requests.results || [], requests_total: requestTotal?.total ?? 0, advisory: true };
}
export async function saveAccountData(db: D1Database, owner: string, project: string, account: string, input: Record<string, unknown>): Promise<Record<string, unknown>> {
  await accountGate(db, owner, project, account);
  const row = validateData(input);
  if (row.app_id && !await db.prepare("SELECT app_id FROM apps WHERE app_id=? AND project_id=?").bind(row.app_id, project).first()) throw new AccountError("account_data_invalid", "app_id 不属于当前项目");
  const now = Date.now();
  const result = await db.prepare(`INSERT INTO pool_account_data(project_id,account_id,data_id,app_id,kind,object_ref,title,attributes_json,source_task_id,updated_at)
    SELECT ?,?,?,?,?,?,?,?,?,? WHERE EXISTS(SELECT 1 FROM pool_accounts WHERE project_id=? AND id=?)
    ON CONFLICT(project_id,account_id,data_id) DO UPDATE SET app_id=excluded.app_id,kind=excluded.kind,object_ref=excluded.object_ref,title=excluded.title,attributes_json=excluded.attributes_json,source_task_id=excluded.source_task_id,updated_at=excluded.updated_at`)
    .bind(project, account, row.data_id, row.app_id, row.kind, row.object_ref, row.title, row.attributes_json, row.source_task_id, now, project, account).run();
  if (!result.meta.changes) throw new AccountError("account_not_found", "没有这个账号", 404);
  return publicData({ project_id: project, account_id: account, ...row, updated_at: now });
}
export async function deleteAccountData(db: D1Database, owner: string, project: string, account: string, id: string): Promise<boolean> {
  await accountGate(db, owner, project, account);
  const result = await db.prepare("DELETE FROM pool_account_data WHERE project_id=? AND account_id=? AND data_id=?").bind(project, account, id).run();
  return Boolean(result.meta.changes);
}
export async function recordAccountDataRequest(db: D1Database, owner: string, project: string, account: string, run: string, session: string, app: string, caseId: string, value: unknown): Promise<void> {
  const need = validateDataNeed(value);
  if (!need) return;
  await accountGate(db, owner, project, account);
  await db.prepare(`INSERT INTO pool_account_data_requests(project_id,account_id,run_id,session_id,app_id,case_id,data_need,updated_at)
    SELECT ?,?,?,?,?,?,?,? WHERE EXISTS(SELECT 1 FROM pool_accounts WHERE project_id=? AND id=?)
    ON CONFLICT(project_id,account_id,run_id,session_id) DO UPDATE SET app_id=excluded.app_id,case_id=excluded.case_id,data_need=excluded.data_need,updated_at=excluded.updated_at`)
    .bind(project, account, run, session, app, caseId, need, Date.now(), project, account).run();
}
