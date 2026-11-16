import { requireProjectOwner } from "./project_access";
/** Account pool and atomic leases. Business facets rank accounts; never prove prep. */
import { requireOwnedProject } from "./script_repo";
import { getProjectEnv, updateProjectEnv } from "./project_env";

type Clause = { facet: string; op: string; value?: unknown };
type Account = Record<string, unknown> & {
  id: string; project_id: string; owner_user_id: string; env: string;
  facets_json: string; lease_run_id: string; lease_session_id: string; locked: number;
};
const BAD_HEALTH = new Set(["dirty", "quarantine", "bad", "banned", "disabled", "invalid"]);
const TEXT_FIELDS = ["env", "display_name", "phone", "email", "username", "password", "otp", "kind", "profile_id", "note"];
const PHONE_SEQ_START = "17000000000";
const GMAIL_ALIAS_START = "10000";
type LoginKind = "phone" | "email";
type LeaseScope = {
  owner: string; project: string; env: string; run: string; session: string;
  app_id?: string; env_surface?: string; case_id?: string; sn?: string;
};
type LoginSecrets = {
  login: { mode: string; kind: LoginKind };
  otp: { mode: string; fixed: string; from_allowlist: string[]; subject_contains: string; poll_interval_ms: number; max_wait_ms: number };
};

export class AccountError extends Error {
  constructor(readonly code: string, message: string, readonly http = 422) { super(message); }
}
function facets(row: Account): Record<string, unknown> {
  try { return JSON.parse(row.facets_json || "{}"); } catch { return {}; }
}
function publicAccount(row: Account, secrets = false): Record<string, unknown> {
  const out: Record<string, unknown> = { ...row, account_id: row.id, facets: facets(row), locked: Boolean(row.locked),
    account_ident: row.email || row.phone || row.username || row.display_name,
    lease: row.lease_run_id ? { run_id: row.lease_run_id, session_id: row.lease_session_id } : {} };
  for (const key of ["facets_json", "owner_user_id", "lease_run_id", "lease_session_id", "lease_id", "lease_expires_at_ms"]) delete out[key];
  if (!secrets) { out.password_configured = Boolean(row.password); out.otp_configured = Boolean(row.otp); delete out.password; delete out.otp; }
  return out;
}
async function projectGate(db: D1Database, owner: string, project: string): Promise<void> {
  if (!await requireOwnedProject(db, project, owner)) throw new AccountError("project_not_found", "没有这个项目", 404);
}
export async function listAccounts(db: D1Database, owner: string, project: string, env = ""): Promise<Record<string, unknown>> {
  owner = await requireProjectOwner(db, project, owner);
  await projectGate(db, owner, project);
  const rows = await db.prepare("SELECT * FROM pool_accounts WHERE owner_user_id=? AND project_id=? AND (?='' OR env=?) ORDER BY created_at,id")
    .bind(owner, project, env, env).all<Account>();
  const accounts = (rows.results || []).map(r => publicAccount(r));
  const environments = [...new Set((rows.results || []).map(r => r.env))].map(key => ({ key, label: key }));
  return { accounts, environments, default_profile: String(((await getProjectEnv(db, owner, project)).env as Record<string, unknown>)?.default_profile || "test") };
}
export type ImportMode = "skip" | "merge" | "overwrite_credentials";

function phoneKey(value: unknown): string { return String(value || "").replace(/\D+/g, ""); }
function accountMatchKey(row: Record<string, unknown>): string {
  const id = String(row.account_id || row.id || "").trim();
  if (id) return `id:${id}`;
  const phone = phoneKey(row.phone);
  if (phone.length >= 11) return `phone:${phone}`;
  const email = String(row.email || "").trim().toLowerCase();
  if (email.includes("@")) return `email:${email}`;
  const username = String(row.username || "").trim().toLowerCase();
  return username.length >= 2 ? `user:${username}` : "";
}
function plainObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
function hasIdentity(row: Record<string, unknown>): boolean {
  return ["display_name", "phone", "email", "username"].some(key => String(row[key] || "").trim());
}
function mergedPayload(incoming: Record<string, unknown>, old: Account, mode: ImportMode): Record<string, unknown> {
  const payload: Record<string, unknown> = { ...old, facets: { ...facets(old), ...(plainObject(incoming.facets) ? incoming.facets : {}) } };
  for (const key of TEXT_FIELDS) if (incoming[key] !== undefined && String(incoming[key] ?? "").trim()) payload[key] = incoming[key];
  if (incoming.locked !== undefined) payload.locked = Boolean(incoming.locked);
  // Both merge modes preserve stored credentials when the row leaves them blank.
  // overwrite_credentials additionally refreshes all non-empty profile fields.
  if (mode === "merge") {
    for (const key of ["display_name", "phone", "email", "username", "note", "env"]) {
      if (incoming[key] !== undefined && String(incoming[key] ?? "").trim()) payload[key] = incoming[key];
    }
  }
  return payload;
}

export async function importAccounts(
  db: D1Database,
  owner: string,
  project: string,
  input: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  owner = await requireProjectOwner(db, project, owner);
  await projectGate(db, owner, project);
  const rawRows = input.rows;
  if (!Array.isArray(rawRows) || !rawRows.length) throw new AccountError("account_import_empty", "rows 必须是非空账号列表");
  if (rawRows.length > 500) throw new AccountError("account_import_too_large", "一次最多导入 500 个账号");
  const mode = String(input.on_duplicate || "merge") as ImportMode;
  if (!["skip", "merge", "overwrite_credentials"].includes(mode)) throw new AccountError("account_import_mode_invalid", "on_duplicate 只能是 skip、merge 或 overwrite_credentials");
  const preview = input.preview === true;
  const envDoc = await getProjectEnv(db, owner, project);
  const envRows = Array.isArray((envDoc.env as Record<string, unknown>)?.environments) ? (envDoc.env as Record<string, unknown>).environments as unknown[] : [];
  const envKeys = new Set(envRows.filter(plainObject).map(row => String(row.key || "")).filter(Boolean));
  const defaultEnv = String(input.default_env || (envDoc.env as Record<string, unknown>)?.default_profile || "test").trim() || "test";
  const existingRows = await db.prepare("SELECT * FROM pool_accounts WHERE owner_user_id=? AND project_id=? ORDER BY created_at,id").bind(owner, project).all<Account>();
  const existing = new Map<string, Account>();
  for (const row of existingRows.results || []) {
    const key = accountMatchKey({ account_id: row.id, phone: row.phone, email: row.email, username: row.username });
    if (key) existing.set(key, row);
  }
  const created: string[] = [], updated: string[] = [], skipped: Array<Record<string, unknown>> = [], errors: Array<Record<string, unknown>> = [], actions: Array<Record<string, unknown>> = [];
  for (let index = 0; index < rawRows.length; index++) {
    const raw = rawRows[index];
    if (!plainObject(raw)) { errors.push({ row: index + 1, error: "账号行必须是对象" }); continue; }
    const row: Record<string, unknown> = { ...raw, env: String(raw.env || defaultEnv).trim() || defaultEnv };
    if (!hasIdentity(row)) { errors.push({ row: index + 1, error: "至少填写展示名、手机、邮箱或用户名之一" }); continue; }
    if (envKeys.size && !envKeys.has(String(row.env))) { errors.push({ row: index + 1, error: `环境「${row.env}」不在项目环境配置中` }); continue; }
    const key = accountMatchKey(row), hit = key ? existing.get(key) : undefined;
    if (!hit) {
      actions.push({ row: index + 1, action: "create", incoming: row });
      if (preview) { created.push(""); continue; }
      try {
        const saved = await saveAccount(db, owner, project, row);
        const id = String(saved.account_id || "");
        created.push(id);
        const savedKey = accountMatchKey(saved);
        if (savedKey) existing.set(savedKey, await db.prepare("SELECT * FROM pool_accounts WHERE id=? AND project_id=? AND owner_user_id=?").bind(id, project, owner).first<Account>() as Account);
      } catch (error) {
        if (error instanceof AccountError) errors.push({ row: index + 1, error: error.message });
        else throw error;
      }
      continue;
    }
    if (mode === "skip") { skipped.push({ row: index + 1, reason: "已存在（跳过）", account_id: hit.id }); actions.push({ row: index + 1, action: "skip", account_id: hit.id, incoming: row }); continue; }
    if (hit.lease_run_id) { skipped.push({ row: index + 1, reason: "账号跑批占用中，已跳过", account_id: hit.id }); actions.push({ row: index + 1, action: "skip", account_id: hit.id, incoming: row }); continue; }
    if (mode === "overwrite_credentials" && Number(hit.locked || 0)) { skipped.push({ row: index + 1, reason: "账号已手动锁定，已跳过", account_id: hit.id }); actions.push({ row: index + 1, action: "skip", account_id: hit.id, incoming: row }); continue; }
    actions.push({ row: index + 1, action: "update", account_id: hit.id, incoming: row });
    if (preview) { updated.push(hit.id); continue; }
    try {
      const saved = await saveAccount(db, owner, project, mergedPayload(row, hit, mode), hit.id);
      updated.push(String(saved.account_id || hit.id));
      existing.set(key, await db.prepare("SELECT * FROM pool_accounts WHERE id=? AND project_id=? AND owner_user_id=?").bind(hit.id, project, owner).first<Account>() as Account);
    } catch (error) {
      if (error instanceof AccountError) errors.push({ row: index + 1, error: error.message, account_id: hit.id });
      else throw error;
    }
  }
  return {
    ok: true, committed: !preview, preview, on_duplicate: mode, default_env: defaultEnv,
    created, updated, skipped, errors,
    stats: { created: created.length, updated: updated.length, skipped: skipped.length, errors: errors.length },
    preview_items: actions.slice(0, 80), preview_truncated: actions.length > 80,
  };
}

export async function saveAccount(db: D1Database, owner: string, project: string, input: Record<string, unknown>, accountId = ""): Promise<Record<string, unknown>> {
  owner = await requireProjectOwner(db, project, owner);
  await projectGate(db, owner, project);
  const id = accountId || `acc_${crypto.randomUUID().replaceAll("-", "")}`;
  const old = accountId ? await db.prepare("SELECT * FROM pool_accounts WHERE id=? AND project_id=? AND owner_user_id=?").bind(id, project, owner).first<Account>() : null;
  if (accountId && !old) throw new AccountError("account_not_found", "没有这个账号", 404);
  if (old?.lease_run_id) throw new AccountError("account_busy", "账号仍被任务占用，结束任务后修改", 409);
  const next: Record<string, unknown> = { env: "test", kind: "mixed", ...(old || {}) };
  for (const key of TEXT_FIELDS) if (input[key] !== undefined) next[key] = String(input[key] ?? "").trim().slice(0, key === "note" ? 2000 : 1000);
  if (!["display_name", "phone", "email", "username"].some(k => String(next[k] || "").trim())) throw new AccountError("account_ident_missing", "账号至少需要展示名、手机号、邮箱或用户名");
  if (!String(next.env || "").trim()) throw new AccountError("account_env_missing", "账号必须指定环境");
  const nextFacets = input.facets && typeof input.facets === "object" && !Array.isArray(input.facets)
    ? input.facets as Record<string, unknown> : old ? facets(old) : {};
  if (Object.keys(nextFacets).length > 100 || Object.values(nextFacets).some(v => typeof v === "object")) throw new AccountError("account_facets_invalid", "账号标签必须是简单字段");
  const locked = input.locked === undefined ? Number(old?.locked || 0) : input.locked ? 1 : 0;
  const now = Date.now();
  const columns = ["id", "project_id", "owner_user_id", ...TEXT_FIELDS, "locked", "facets_json", "created_at", "updated_at"];
  const values = [id, project, owner, ...TEXT_FIELDS.map(k => String(next[k] || "")), locked, JSON.stringify(nextFacets), Number(old?.created_at || now), now];
  const changed = await db.prepare(`INSERT INTO pool_accounts (${columns.join(",")}) VALUES (${columns.map(() => "?").join(",")})
    ON CONFLICT(id) DO UPDATE SET ${[...TEXT_FIELDS, "locked", "facets_json", "updated_at"].map(k => `${k}=excluded.${k}`).join(",")}
    WHERE pool_accounts.owner_user_id=excluded.owner_user_id AND pool_accounts.project_id=excluded.project_id AND pool_accounts.lease_run_id=''`)
    .bind(...values).run();
  if (!changed.meta.changes) throw new AccountError("account_busy", "账号状态已变化，请刷新后重试", 409);
  const saved = await db.prepare("SELECT * FROM pool_accounts WHERE id=?").bind(id).first<Account>();
  return publicAccount(saved!);
}
export async function deleteAccount(db: D1Database, owner: string, project: string, id: string): Promise<void> {
  owner = await requireProjectOwner(db, project, owner);
  await projectGate(db, owner, project);
  const row = await db.prepare("SELECT * FROM pool_accounts WHERE id=? AND project_id=? AND owner_user_id=?").bind(id, project, owner).first<Account>();
  if (!row) throw new AccountError("account_not_found", "没有这个账号", 404);
  const result = await db.prepare("DELETE FROM pool_accounts WHERE id=? AND project_id=? AND owner_user_id=? AND lease_run_id=''").bind(id, project, owner).run();
  if (!result.meta.changes) throw new AccountError("account_busy", "账号仍被任务占用", 409);
}
function clauses(raw: unknown): Clause[] {
  if (raw == null) return [];
  if (!Array.isArray(raw) || raw.length > 100) throw new AccountError("account_requirements_invalid", "all/prefer 必须为条件列表");
  return raw.map(item => {
    if (!item || typeof item !== "object") throw new AccountError("account_requirements_invalid", "账号条件格式不正确");
    const row = item as Record<string, unknown>, facet = String(row.facet || "").trim(), op = String(row.op || "eq");
    if (!facet || !["eq", "ne", "in", "not_in", "exists"].includes(op)) throw new AccountError("account_requirements_invalid", "账号条件字段或操作符不正确");
    return { facet, op, value: row.value };
  });
}
function matches(value: unknown, clause: Clause): boolean {
  const known = value !== undefined && value !== null && value !== "" && value !== "unknown";
  if (clause.op === "exists") return known === (clause.value !== false);
  if (!known) return false;
  const same = String(value) === String(clause.value);
  if (clause.op === "eq") return same;
  if (clause.op === "ne") return !same;
  const included = Array.isArray(clause.value) && clause.value.some(v => String(v) === String(value));
  return clause.op === "in" ? included : !included;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.map(item => String(item || "").trim()).filter(Boolean).slice(0, 20) : [];
}
function loginSecrets(value: unknown): LoginSecrets {
  const row = plainObject(value) ? value : {};
  const login = plainObject(row.login) ? row.login : {};
  const otp = plainObject(row.otp) ? row.otp : {};
  const kind = String(login.kind || "phone").trim().toLowerCase() === "email" ? "email" : "phone";
  const mode = ["auto", "fixed", "gmail", "hitl"].includes(String(otp.mode || "")) ? String(otp.mode || "auto") : "auto";
  return {
    login: { mode: String(login.mode || "auto"), kind },
    otp: {
      mode,
      fixed: String(otp.fixed || "").trim().slice(0, 32),
      from_allowlist: stringList(otp.from_allowlist),
      subject_contains: String(otp.subject_contains || "").trim().slice(0, 120),
      poll_interval_ms: Math.max(200, Math.min(10_000, Number(otp.poll_interval_ms || 200))),
      max_wait_ms: Math.max(5_000, Math.min(180_000, Number(otp.max_wait_ms || 90_000))),
    },
  };
}
function matchingPolicy(env: Record<string, unknown>, appId: string): Record<string, unknown> | null {
  const policies = Array.isArray(env.login_policies) ? env.login_policies : [];
  for (const item of policies) {
    if (!plainObject(item)) continue;
    const apply = String(item.apply || "all");
    if (apply !== "selected" || stringList(item.app_ids).includes(appId)) return item;
  }
  return null;
}
function effectiveLoginSecrets(env: Record<string, unknown>, scope: LeaseScope): LoginSecrets {
  const policy = matchingPolicy(env, String(scope.app_id || ""));
  if (policy && plainObject(policy.secrets) && plainObject(policy.secrets[scope.env])) return loginSecrets(policy.secrets[scope.env]);
  const channel = plainObject(env.channel_secrets) && plainObject(env.channel_secrets[String(scope.env_surface || "")])
    ? env.channel_secrets[String(scope.env_surface || "")] : {};
  const channelEnv = plainObject(channel) && plainObject(channel[scope.env]) ? channel[scope.env] as Record<string, unknown> : null;
  if (channelEnv && channelEnv.inherit !== true) return loginSecrets(channelEnv);
  const environments = Array.isArray(env.environments) ? env.environments : [];
  const environment = environments.find(item => plainObject(item) && String(item.key || "") === scope.env);
  return loginSecrets(plainObject(environment) && plainObject(environment.secrets) ? environment.secrets : undefined);
}
function seqValue(value: unknown): { start: string; next: string } {
  const row = plainObject(value) ? value : {};
  const start = String(row.start || "").trim().slice(0, 32), next = String(row.next || start).trim().slice(0, 32);
  return { start, next };
}
function sequenceSlot(env: Record<string, unknown>, scope: LeaseScope, key: "phone_seq" | "gmail_alias"): { start: string; next: string } {
  const policy = matchingPolicy(env, String(scope.app_id || ""));
  const policyRows = policy && plainObject(policy[key]) ? policy[key] : {};
  if (plainObject(policyRows) && plainObject(policyRows[scope.env])) {
    const slot = seqValue(policyRows[scope.env]);
    if (slot.start || slot.next) return slot;
  }
  const fallbackKey = key === "phone_seq" ? "channel_phone_seq" : "channel_gmail_alias";
  const channelRows = plainObject(env[fallbackKey]) ? env[fallbackKey] as Record<string, unknown> : {};
  const perEnv = plainObject(channelRows[String(scope.env_surface || "")]) ? channelRows[String(scope.env_surface || "")] as Record<string, unknown> : {};
  return seqValue(perEnv[scope.env]);
}
function writeSequenceSlot(env: Record<string, unknown>, scope: LeaseScope, key: "phone_seq" | "gmail_alias", slot: { start: string; next: string }): void {
  const policy = matchingPolicy(env, String(scope.app_id || ""));
  if (policy) {
    policy[key] = { ...(plainObject(policy[key]) ? policy[key] as Record<string, unknown> : {}), [scope.env]: slot };
    return;
  }
  const fallbackKey = key === "phone_seq" ? "channel_phone_seq" : "channel_gmail_alias";
  const map = plainObject(env[fallbackKey]) ? env[fallbackKey] as Record<string, unknown> : {};
  const surface = String(scope.env_surface || "");
  const perEnv = plainObject(map[surface]) ? map[surface] as Record<string, unknown> : {};
  perEnv[scope.env] = slot;
  map[surface] = perEnv;
  env[fallbackKey] = map;
}
function nextSequence(slot: { start: string; next: string }, fallback: string): bigint {
  const start = slot.start || fallback, next = slot.next || start, value = BigInt(next || start || fallback);
  return value < BigInt(start || fallback) ? BigInt(start || fallback) : value;
}
function gmailAlias(base: string, tag: string): string {
  const [local, domain] = String(base || "").trim().toLowerCase().split("@");
  if (!local || !domain || !tag) return "";
  return `${local.replace(/\+.*$/, "")}+${tag}@${domain}`;
}
function gmailInbox(env: Record<string, unknown>): string {
  return plainObject(env.gmail_inbox) ? String(env.gmail_inbox.address || "").trim() : "";
}
function loginPolicyPayload(env: Record<string, unknown>, scope: LeaseScope): Record<string, unknown> {
  const policy = matchingPolicy(env, String(scope.app_id || ""));
  const secrets = effectiveLoginSecrets(env, scope);
  const inbox = gmailInbox(env);
  return {
    policy_id: String(policy?.id || ""), login_kind: secrets.login.kind,
    otp_mode: secrets.otp.mode, otp_fixed: secrets.otp.fixed,
    otp_from_allowlist: secrets.otp.from_allowlist, otp_subject_contains: secrets.otp.subject_contains,
    otp_poll_interval_ms: secrets.otp.poll_interval_ms, otp_max_wait_ms: secrets.otp.max_wait_ms,
    otp_inbox_address: secrets.login.kind === "email" ? inbox : "",
  };
}
function accountValues(row: Account): Record<string, unknown> {
  return { ...facets(row), env: row.env, id: row.id, account_id: row.id, owner_user_id: row.owner_user_id,
    project_id: row.project_id, phone: row.phone, email: row.email, username: row.username };
}
function accountFitsLoginKind(row: Account, kind: LoginKind): boolean {
  return kind === "email" ? Boolean(String(row.email || "").trim()) : Boolean(String(row.phone || "").trim());
}
async function stableAccountId(scope: LeaseScope): Promise<string> {
  const identity = [scope.owner, scope.project, scope.app_id || "", scope.run, scope.case_id || "",
    scope.sn || "", scope.env_surface || "", scope.env].join("\n");
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(identity)));
  return "acc" + [...digest].map(byte => byte.toString(16).padStart(2, "0")).join("").slice(0, 24);
}
async function leaseRow(db: D1Database, scope: LeaseScope, row: Account, loginPolicy: Record<string, unknown>): Promise<Record<string, unknown>> {
  const leaseId = String(row.lease_id || crypto.randomUUID());
  const result = await db.prepare("UPDATE pool_accounts SET lease_run_id=?,lease_session_id=?,lease_id=?,lease_expires_at_ms=?,updated_at=? WHERE id=? AND owner_user_id=? AND project_id=? AND env=? AND locked=0 AND facets_json=? AND (lease_run_id='' OR (lease_run_id=? AND lease_session_id=?))")
    .bind(scope.run, scope.session, leaseId, Date.now() + 120_000, Date.now(), row.id, scope.owner, scope.project, scope.env, row.facets_json, scope.run, scope.session).run();
  if (!result.meta.changes) return {};
  return { ...publicAccount(row, true), id: row.id, login_policy: loginPolicy, lease_id: leaseId };
}
async function findStableAccount(db: D1Database, scope: LeaseScope, id: string): Promise<Account | null> {
  return db.prepare("SELECT * FROM pool_accounts WHERE id=? AND owner_user_id=? AND project_id=? AND env=?")
    .bind(id, scope.owner, scope.project, scope.env).first<Account>();
}
async function provisionAccount(db: D1Database, scope: LeaseScope, requirements: Record<string, unknown>, envDoc: Record<string, unknown>, all: Clause[]): Promise<Record<string, unknown> | null> {
  const secrets = effectiveLoginSecrets(envDoc, scope);
  const inbox = gmailInbox(envDoc);
  if (secrets.login.kind === "email" && !["gmail", "auto"].includes(secrets.otp.mode)) return null;
  if (secrets.login.kind === "email" && !inbox) return null;
  const id = await stableAccountId(scope);
  const existing = await findStableAccount(db, scope, id);
  if (existing) {
    const values = accountValues(existing);
    if (!accountFitsLoginKind(existing, secrets.login.kind) || !all.every(clause => matches(values[clause.facet], clause))) return null;
    return leaseRow(db, scope, existing, loginPolicyPayload(envDoc, scope));
  }
  const identityRows = await db.prepare("SELECT phone,email FROM pool_accounts WHERE owner_user_id=? AND project_id=? AND env=?")
    .bind(scope.owner, scope.project, scope.env).all<{ phone: string; email: string }>();
  const phones = new Set((identityRows.results || []).map(row => phoneKey(row.phone)).filter(Boolean));
  const emails = new Set((identityRows.results || []).map(row => String(row.email || "").trim().toLowerCase()).filter(Boolean));
  let selected = { phone: "", email: "", tag: "" };
  const updatedEnv = await updateProjectEnv(db, scope.owner, scope.project, current => {
    const key = secrets.login.kind === "phone" ? "phone_seq" : "gmail_alias";
    const fallback = secrets.login.kind === "phone" ? PHONE_SEQ_START : GMAIL_ALIAS_START;
    const slot = sequenceSlot(current, scope, key);
    let number = nextSequence(slot, fallback);
    let phone = "", email = "", tag = "";
    for (let attempt = 0; attempt < 1000; attempt++, number++) {
      if (secrets.login.kind === "phone") {
        phone = number.toString();
        if (phone.length !== 11) throw new AccountError("phone_seq_number_out_of_range", "手机号序列必须生成 11 位手机号");
        if (!phones.has(phone)) break;
      } else {
        tag = number.toString();
        email = gmailAlias(inbox, tag);
        if (email && !emails.has(email)) break;
      }
    }
    if (!phone && !email) throw new AccountError("account_seq_collision_limit", "连续 1000 个候选账号都已存在");
    selected = { phone, email, tag };
    writeSequenceSlot(current, scope, key, { start: slot.start || fallback, next: (number + 1n).toString() });
    return current;
  });
  if (!all.every(clause => matches({ ...planFacets(), env: scope.env, id, account_id: id, owner_user_id: scope.owner,
    project_id: scope.project, phone: selected.phone, email: selected.email, username: "" }[clause.facet], clause))) return null;
  const now = Date.now(), facetsJson = JSON.stringify(planFacets());
  await db.prepare(`INSERT OR IGNORE INTO pool_accounts
    (id,project_id,owner_user_id,env,display_name,phone,email,username,password,otp,kind,profile_id,note,locked,facets_json,created_at,updated_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`)
    .bind(id, scope.project, scope.owner, scope.env, (selected.email || selected.phone).slice(0, 80), selected.phone, selected.email,
      "", "", "", "mixed", "", `auto ${secrets.login.kind} ${scope.env_surface || ""} ${selected.tag || selected.phone}`.trim(),
      0, facetsJson, now, now).run();
  const created = await findStableAccount(db, scope, id);
  if (!created) throw new AccountError("account_provision_failed", "新账号已分配序列但未能写入号池");
  return leaseRow(db, scope, created, loginPolicyPayload(updatedEnv, scope));
}
function planFacets(): Record<string, unknown> {
  return { lifecycle: "unregistered", session: "logged_out", health: "available" };
}

export async function leaseAccount(db: D1Database, scope: LeaseScope, requirements: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
  scope = { ...scope, owner: await requireProjectOwner(db, scope.project, scope.owner) };
  const all = clauses(requirements.all), prefer = clauses(requirements.prefer);
  if (!all.length) all.push({ facet: "health", op: "eq", value: "available" });
  if (requirements.env && requirements.env !== scope.env) throw new AccountError("account_env_mismatch", "账号环境必须与本次任务一致");
  const envDoc = ((await getProjectEnv(db, scope.owner, scope.project)).env || {}) as Record<string, unknown>;
  const secrets = effectiveLoginSecrets(envDoc, scope);
  const loginPolicy = loginPolicyPayload(envDoc, scope);
  const rows = await db.prepare("SELECT * FROM pool_accounts WHERE owner_user_id=? AND project_id=? AND env=? AND locked=0 AND (lease_run_id='' OR (lease_run_id=? AND lease_session_id=?))")
    .bind(scope.owner, scope.project, scope.env, scope.run, scope.session).all<Account>();
  const candidates = (rows.results || []).map(row => {
    const values = accountValues(row);
    const safe = !BAD_HEALTH.has(String(values.health || "")) && accountFitsLoginKind(row, secrets.login.kind) && all.every(c => matches(values[c.facet], c));
    const score = prefer.filter(c => matches(values[c.facet], c)).length;
    return { row, score: score + (row.lease_run_id === scope.run ? 1000 : 0), safe };
  }).filter(c => c.safe).sort((a, b) => b.score - a.score || a.row.id.localeCompare(b.row.id));
  for (const { row } of candidates) {
    const leased = await leaseRow(db, scope, row, loginPolicy);
    if (Object.keys(leased).length) return leased;
  }
  const provisioned = await provisionAccount(db, scope, requirements, envDoc, all);
  if (provisioned) return provisioned;
  throw new AccountError("resource_account_missing", "当前项目与环境没有健康且空闲的可用账号");
}
export async function releaseAccounts(db: D1Database, run: string, session = ""): Promise<void> {
  await db.prepare("UPDATE pool_accounts SET lease_run_id='',lease_session_id='',lease_id='',lease_expires_at_ms=0,updated_at=? WHERE lease_run_id=? AND (?='' OR lease_session_id=?)")
    .bind(Date.now(), run, session, session).run();
}
