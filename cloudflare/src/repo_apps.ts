/**
 * Port of mino_nexus.resources.repo_apps — script repo app manifest synced by agents.
 * Scout repo speaks keys (platform / package / url); Nexus speaks app_id.
 */

import { createApp } from "./auth";

const KEY = /^[A-Za-z][A-Za-z0-9_]{0,31}$/;
const APP_ID = /^[A-Za-z0-9_.:\-]{1,120}$/;
const PLATFORMS = ["android", "ios", "web", "mac", "win"];
const IDENTITY = ["package", "bundle_id", "url"] as const;
const SECRET_WORDS = ["password", "passwd", "token", "otp", "secret", "key", "credential"];
const MAX_APPS = 32;

export type RepoApp = {
  platform: string;
  app_id?: string;
  package?: string;
  bundle_id?: string;
  url?: string;
  envs?: Record<string, Record<string, string>>;
};

export type RepoAppsDoc = {
  project_id: string;
  revision: string;
  branch: string;
  apps: Record<string, RepoApp>;
  synced_by: string;
  synced_at: number;
};

type Row = Record<string, unknown>;

function isObject(value: unknown): value is Row {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function noSecrets(label: string, row: Row): void {
  for (const field of Object.keys(row)) {
    const low = field.toLowerCase();
    if (SECRET_WORDS.some((word) => low.includes(word))) {
      throw new Error(
        `应用 ${label}：不允许出现敏感字段 ${field}（otp.code 只留在脚本仓，由 Scout 读取；账号密码由 Nexus 号池管理）`,
      );
    }
  }
}

function identity(label: string, row: Row, into: Row, allowed: Set<string>): void {
  for (const field of Object.keys(row)) {
    if (!allowed.has(field)) {
      throw new Error(
        `应用 ${label}：不支持的字段 ${field}，只允许 platform / app_id / package / bundle_id / url / envs`,
      );
    }
  }
  for (const field of IDENTITY) {
    if (!(field in row)) continue;
    const raw = row[field];
    if (typeof raw !== "string" || !raw.trim()) {
      throw new Error(`应用 ${label}：${field} 必须是非空字符串`);
    }
    const value = raw.trim();
    if (field === "url" && !/^https?:\/\/\S+$/.test(value)) {
      throw new Error(`应用 ${label}：url 必须以 http:// 或 https:// 开头`);
    }
    into[field] = value;
  }
}

export function validateApps(apps: unknown): Record<string, RepoApp> {
  if (!isObject(apps) || !Object.keys(apps).length) throw new Error("apps 必须是非空对象");
  if (Object.keys(apps).length > MAX_APPS) throw new Error(`apps 最多 ${MAX_APPS} 个应用`);
  const out: Record<string, RepoApp> = {};
  for (const [name, row] of Object.entries(apps)) {
    if (!KEY.test(name)) {
      throw new Error(
        `应用 key ${JSON.stringify(name)} 不合法：要求英文字母开头，只含英文字母、数字、下划线，最长 32`,
      );
    }
    if (!isObject(row)) throw new Error(`应用 ${name}：内容必须是对象`);
    noSecrets(name, row);
    const platform = String(row.platform ?? "").trim().toLowerCase();
    if (!PLATFORMS.includes(platform)) {
      throw new Error(`应用 ${name}：platform 必须是 ${[...PLATFORMS].sort().join("/")}`);
    }
    const clean: Row = { platform };
    const appId = row.app_id;
    if (appId !== undefined && appId !== null && String(appId).trim()) {
      if (typeof appId !== "string" || !APP_ID.test(appId.trim())) {
        throw new Error(`应用 ${name}：app_id 不合法`);
      }
      clean.app_id = appId.trim();
    }
    identity(name, row, clean, new Set(["platform", "app_id", "envs", ...IDENTITY]));
    const envs = row.envs;
    if (envs !== undefined && envs !== null) {
      if (!isObject(envs)) throw new Error(`应用 ${name}：envs 必须是对象`);
      const cleanEnvs: Record<string, Row> = {};
      for (const [env, block] of Object.entries(envs)) {
        const label = `${name}.envs.${env}`;
        if (!isObject(block)) throw new Error(`应用 ${label}：内容必须是对象`);
        noSecrets(label, block);
        const picked: Row = {};
        identity(label, block, picked, new Set(IDENTITY));
        cleanEnvs[env] = picked;
      }
      clean.envs = cleanEnvs;
    }
    out[name] = clean as RepoApp;
  }
  return out;
}

export async function getRepoApps(db: D1Database, projectId: string): Promise<RepoAppsDoc> {
  const pid = String(projectId || "").trim();
  const empty: RepoAppsDoc = {
    project_id: pid,
    revision: "",
    branch: "",
    apps: {},
    synced_by: "",
    synced_at: 0,
  };
  if (!pid) return empty;
  const row = await db
    .prepare(
      `SELECT revision, branch, apps_json, synced_by, synced_at FROM project_repo_apps WHERE project_id = ?`,
    )
    .bind(pid)
    .first<{
      revision: string;
      branch: string;
      apps_json: string;
      synced_by: string;
      synced_at: number;
    }>();
  if (!row) return empty;
  let apps: Record<string, RepoApp> = {};
  try {
    const parsed = JSON.parse(String(row.apps_json || "{}"));
    if (isObject(parsed)) apps = parsed as Record<string, RepoApp>;
  } catch {
    apps = {};
  }
  return {
    project_id: pid,
    revision: String(row.revision || ""),
    branch: String(row.branch || ""),
    apps,
    synced_by: String(row.synced_by || ""),
    synced_at: Number(row.synced_at || 0),
  };
}

async function bindAppIds(
  db: D1Database,
  ownerUserId: string,
  projectId: string,
  apps: Record<string, RepoApp>,
  previous: Record<string, RepoApp>,
): Promise<Record<string, RepoApp>> {
  const { results } = await db
    .prepare(`SELECT app_id, name FROM apps WHERE project_id = ?`)
    .bind(projectId)
    .all<{ app_id: string; name: string }>();
  const byId = new Set((results || []).map((a) => a.app_id));
  const byName = new Map<string, string>();
  for (const a of results || []) {
    const name = String(a.name || "").trim();
    if (name && !byName.has(name)) byName.set(name, a.app_id);
  }
  const out: Record<string, RepoApp> = {};
  for (const [key, row] of Object.entries(apps)) {
    const item: RepoApp = { ...row };
    let appId = String(item.app_id || "").trim() || String(previous[key]?.app_id || "").trim();
    if (appId && !byId.has(appId)) {
      throw new Error(`应用 ${key}：app_id=${appId} 不在本项目`);
    }
    if (!appId) {
      const existing = byName.get(key);
      if (existing) {
        appId = existing;
      } else {
        const created = await createApp(db, ownerUserId, projectId, key, item.platform);
        appId = String(created.app_id || "");
        byId.add(appId);
        byName.set(key, appId);
      }
    }
    if (!appId) throw new Error(`应用 ${key}：无法对齐或创建 Nexus 应用`);
    item.app_id = appId;
    out[key] = item;
  }
  return out;
}

/** Diff update: apps replace by key, remove drops keys, the rest stays. */
export async function saveRepoApps(
  db: D1Database,
  args: {
    project_id: string;
    revision?: unknown;
    branch?: unknown;
    apps?: unknown;
    synced_by: string;
    remove?: unknown;
    owner_user_id: string;
  },
): Promise<RepoAppsDoc> {
  const pid = String(args.project_id || "").trim();
  if (args.remove !== undefined && args.remove !== null && !Array.isArray(args.remove)) {
    throw new Error("remove 必须是应用 key 的数组");
  }
  const drop = ((args.remove as unknown[] | undefined) || []).map((key) => String(key));
  const provided = Array.isArray(args.apps)
    ? args.apps.length > 0
    : isObject(args.apps)
      ? Object.keys(args.apps).length > 0
      : Boolean(args.apps);
  const changed = provided ? validateApps(args.apps) : {};
  if (!Object.keys(changed).length && !drop.length) {
    throw new Error("apps 和 remove 不能同时为空");
  }
  const overlap = Object.keys(changed)
    .filter((key) => drop.includes(key))
    .sort();
  if (overlap.length) throw new Error(`应用 ${overlap.join(", ")} 同时出现在 apps 和 remove 里`);

  const current = await getRepoApps(db, pid);
  const previous: Record<string, RepoApp> = {};
  for (const [key, row] of Object.entries(current.apps)) {
    if (!drop.includes(key)) previous[key] = row;
  }
  const merged = { ...previous, ...changed };
  if (!Object.keys(merged).length) throw new Error("同步后项目不能没有任何应用");
  if (Object.keys(merged).length > MAX_APPS) throw new Error(`apps 最多 ${MAX_APPS} 个应用`);
  const cleaned = await bindAppIds(db, args.owner_user_id, pid, merged, previous);

  await db
    .prepare(
      `INSERT INTO project_repo_apps (project_id, revision, branch, apps_json, synced_by, synced_at)
       VALUES (?, ?, ?, ?, ?, ?)
       ON CONFLICT(project_id) DO UPDATE SET
         revision=excluded.revision,
         branch=excluded.branch,
         apps_json=excluded.apps_json,
         synced_by=excluded.synced_by,
         synced_at=excluded.synced_at`,
    )
    .bind(
      pid,
      String(args.revision ?? "").trim().slice(0, 120),
      String(args.branch ?? "").trim().slice(0, 200),
      JSON.stringify(cleaned),
      String(args.synced_by || "").slice(0, 120),
      Math.floor(Date.now() / 1000),
    )
    .run();
  return getRepoApps(db, pid);
}

/** null: project has no manifest (skip check). */
export async function hasApp(
  db: D1Database,
  projectId: string,
  appId: string,
): Promise<boolean | null> {
  const doc = await getRepoApps(db, projectId);
  const rows = Object.values(doc.apps);
  if (!rows.length) return null;
  const wanted = String(appId || "").trim();
  return rows.some((row) => String(row?.app_id || "") === wanted);
}

export async function keyForApp(
  db: D1Database,
  projectId: string,
  appId: string,
): Promise<string> {
  const wanted = String(appId || "").trim();
  if (!wanted) return "";
  const doc = await getRepoApps(db, projectId);
  for (const [key, row] of Object.entries(doc.apps)) {
    if (String(row?.app_id || "") === wanted) return key;
  }
  return "";
}
