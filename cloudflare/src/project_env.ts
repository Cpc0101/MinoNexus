import { requireProjectOwner } from "./project_access";
/** Project environment configuration persisted in owner-scoped D1 settings. */
import { TaskError } from "./task_scheduler";

type Json = Record<string, unknown>;
type Project = { project_id: string; name: string };
const OBJECT_FIELDS = ["profiles", "channel_secrets", "channel_gmail_alias", "channel_phone_seq", "gmail_inbox"];
const ARRAY_FIELDS = ["environments", "channels", "pipeline", "login_policies"];
const KEY = (project: string) => `project.env:${project}`;
function object(value: unknown): value is Json { return Boolean(value) && typeof value === "object" && !Array.isArray(value); }
function clone<T>(value: T): T { return JSON.parse(JSON.stringify(value)) as T; }
function defaults(): Json {
  return { default_profile: "test", environments: [{ key: "test", label: "测试" }],
    channels: [{ id: "android", kind: "app", platform: "android", alias: "", app_identifier: "", third_party: false,
      label: "安卓", field: "package", placeholder: "com.example.app" }], pipeline: ["test"], profiles: { test: { android: { package: "" } } },
    gmail_inbox: { address: "" }, channel_secrets: {}, channel_gmail_alias: {}, channel_phone_seq: {}, login_policies: [] };
}
function withDefaults(raw: Json): Json {
  const base = defaults(), env = { ...base, ...clone(raw) };
  for (const key of OBJECT_FIELDS) env[key] = { ...(base[key] as Json), ...(object(env[key]) ? env[key] as Json : {}) };
  for (const key of ARRAY_FIELDS) if (!Array.isArray(env[key])) env[key] = clone(base[key]);
  env.environments = (env.environments as Json[]).map(row => object(row) && !object(row.secrets)
    ? { ...row, secrets: { login: { mode: "auto", kind: "phone" }, otp: { mode: "auto", fixed: "", from_allowlist: [], subject_contains: "", poll_interval_ms: 200, max_wait_ms: 90000 } } }
    : row);
  return env;
}
async function projectGate(db: D1Database, owner: string, id: string): Promise<Project> {
  const project = await db.prepare("SELECT project_id,name FROM projects WHERE project_id=? AND owner_user_id=?").bind(id, owner).first<Project>();
  if (!project) throw new TaskError("project_not_found", "没有这个项目", 404);
  return project;
}
async function stored(db: D1Database, owner: string, id: string): Promise<{ raw: string | null; env: Json }> {
  const row = await db.prepare("SELECT value_json FROM user_settings WHERE owner_user_id=? AND key=?").bind(owner, KEY(id)).first<{ value_json: string }>();
  if (!row) return { raw: null, env: defaults() };
  try { const env = JSON.parse(row.value_json); if (object(env)) return { raw: row.value_json, env: withDefaults(env) }; } catch { /* report corrupt persistence */ }
  throw new TaskError("project_env_invalid", "已保存的环境配置无法读取", 500);
}
function response(project: Project, env: Json): Json {
  return { project_id: project.project_id, project_name: project.name, env,
    profile_labels: Object.fromEntries((env.environments as Json[]).map(row => [String(row.key), String(row.label || row.key)])) };
}
export async function getProjectEnv(db: D1Database, owner: string, id: string): Promise<Json> {
  owner = await requireProjectOwner(db, id, owner);
  const project = await projectGate(db, owner, id);
  return response(project, (await stored(db, owner, id)).env);
}
function validate(previous: Json, input: unknown): Json {
  if (!object(input) || new TextEncoder().encode(JSON.stringify(input)).byteLength > 524288)
    throw new TaskError("project_env_invalid", "环境配置必须是小于 512 KB 的对象", 422);
  if ("test_accounts" in input) throw new TaskError("account_api_required", "测试账号请通过账号池接口保存", 422);
  for (const key of OBJECT_FIELDS) if (input[key] !== undefined && !object(input[key])) throw new TaskError("project_env_invalid", `${key} 必须是对象`, 422);
  for (const key of ARRAY_FIELDS) if (input[key] !== undefined && !Array.isArray(input[key])) throw new TaskError("project_env_invalid", `${key} 必须是列表`, 422);
  const next = clone(previous);
  for (const key of [...OBJECT_FIELDS, ...ARRAY_FIELDS, "default_profile"]) if (input[key] !== undefined) next[key] = input[key];
  const environments = next.environments as unknown[], channels = next.channels as unknown[];
  if (!environments.length || environments.length > 50 || !channels.length || channels.length > 100)
    throw new TaskError("project_env_invalid", "环境或平台配置数量无效", 422);
  const keys = new Set<string>(), ids = new Set<string>();
  for (const row of environments) {
    if (!object(row) || !/^[a-zA-Z0-9_-]{1,64}$/.test(String(row.key || "")) || keys.has(String(row.key))) throw new TaskError("project_env_invalid", "环境标识无效或重复", 422);
    keys.add(String(row.key));
  }
  for (const row of channels) {
    if (!object(row) || !/^[a-zA-Z0-9_.-]{1,64}$/.test(String(row.id || "")) || ids.has(String(row.id)) || !["app", "web", "server"].includes(String(row.kind)) || !/^[a-zA-Z_][a-zA-Z0-9_]*$/.test(String(row.field || "")))
      throw new TaskError("project_env_invalid", "平台标识、类型或配置字段无效", 422);
    if (row.kind === "app" && !["android", "ios", "win", "mac"].includes(String(row.platform)) || row.kind === "web" && row.platform !== "web") throw new TaskError("project_env_invalid", "平台类型无效", 422);
    ids.add(String(row.id));
  }
  if (!keys.has(String(next.default_profile || ""))) throw new TaskError("project_env_invalid", "默认环境不存在", 422);
  if ((next.pipeline as unknown[]).some(key => !keys.has(String(key)))) throw new TaskError("project_env_invalid", "执行顺序引用了不存在的环境", 422);
  // A labels-only environment edit must preserve its previously saved secrets.
  const old = new Map((previous.environments as Json[]).map(row => [String(row.key), row]));
  next.environments = (environments as Json[]).map(row => ({ ...row, ...(!("secrets" in row) && old.get(String(row.key))?.secrets ? { secrets: old.get(String(row.key))!.secrets } : {}) }));
  if (new TextEncoder().encode(JSON.stringify(next)).byteLength > 524288) throw new TaskError("project_env_invalid", "环境配置不能超过 512 KB", 422);
  return next;
}

async function writeProjectEnv(db: D1Database, owner: string, id: string, current: { raw: string | null; env: Json }, next: Json): Promise<Json> {
  owner = await requireProjectOwner(db, id, owner);
  const project = await projectGate(db, owner, id);
  const updated = await db.prepare("INSERT INTO user_settings(owner_user_id,key,value_json,updated_at_ms) SELECT ?,?,?,? WHERE EXISTS (SELECT 1 FROM projects WHERE project_id=? AND owner_user_id=?) ON CONFLICT(owner_user_id,key) DO UPDATE SET value_json=excluded.value_json,updated_at_ms=excluded.updated_at_ms WHERE user_settings.value_json=?")
    .bind(owner, KEY(id), JSON.stringify(next), Date.now(), id, owner, current.raw).run();
  if (!updated.meta.changes) throw new TaskError("project_env_conflict", "环境配置同时被修改，请刷新后重试", 409);
  return response(project, next);
}

export async function saveProjectEnv(db: D1Database, owner: string, id: string, input: unknown): Promise<Json> {
  owner = await requireProjectOwner(db, id, owner);
  const project = await projectGate(db, owner, id);
  for (let retry = 0; retry < 5; retry++) {
    const current = await stored(db, owner, id), next = validate(current.env, input);
    for (const channel of next.channels as Json[]) if (channel.app_id && !await db.prepare("SELECT app_id FROM apps WHERE app_id=? AND project_id=? AND owner_user_id=?").bind(String(channel.app_id), id, owner).first())
      throw new TaskError("app_not_found", "环境平台引用的应用不属于本项目", 422);
    try { return await writeProjectEnv(db, owner, id, current, next); }
    catch (error) { if (!(error instanceof TaskError) || error.code !== "project_env_conflict") throw error; }
    await projectGate(db, owner, id);
  }
  throw new TaskError("project_env_conflict", "环境配置同时被修改，请刷新后重试", 409);
}

/** CAS update used by resource allocation, so sequence increments cannot be lost. */
export async function updateProjectEnv(
  db: D1Database,
  owner: string,
  id: string,
  mutate: (env: Json) => Json,
): Promise<Json> {
  owner = await requireProjectOwner(db, id, owner);
  await projectGate(db, owner, id);
  for (let retry = 0; retry < 8; retry++) {
    const current = await stored(db, owner, id);
    const next = validate(current.env, mutate(clone(current.env)));
    try { return await writeProjectEnv(db, owner, id, current, next); }
    catch (error) { if (!(error instanceof TaskError) || error.code !== "project_env_conflict") throw error; }
  }
  throw new TaskError("project_env_conflict", "环境配置同时被修改，请刷新后重试", 409);
}
