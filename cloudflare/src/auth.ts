/** User accounts + session tokens on D1 (Workbench login). */

const SESSION_TTL_MS = 30 * 24 * 60 * 60 * 1000;
const PBKDF2_ITERS = 100_000;

export type UserRow = {
  user_id: string;
  email: string;
  username: string;
  name: string;
  role: string;
  password_salt: string;
  password_hash: string;
};

function enc() {
  return new TextEncoder();
}

function toHex(buf: ArrayBuffer | Uint8Array): string {
  const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function fromHex(hex: string): Uint8Array {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) {
    out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  return out;
}

function randomHex(bytes = 24): string {
  const buf = new Uint8Array(bytes);
  crypto.getRandomValues(buf);
  return toHex(buf);
}

async function hashPassword(password: string, saltHex: string): Promise<string> {
  const salt = fromHex(saltHex);
  const key = await crypto.subtle.importKey("raw", enc().encode(password), "PBKDF2", false, [
    "deriveBits",
  ]);
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", salt, iterations: PBKDF2_ITERS, hash: "SHA-256" },
    key,
    256,
  );
  return toHex(bits);
}

export function publicUser(u: Pick<UserRow, "user_id" | "email" | "username" | "name" | "role">) {
  return {
    user_id: u.user_id,
    email: u.email,
    username: u.username,
    name: u.name || u.username,
    role: u.role || "user",
  };
}

export async function registerUser(
  db: D1Database,
  emailRaw: string,
  password: string,
): Promise<{ user: ReturnType<typeof publicUser>; token: string; ws_token: string }> {
  const email = String(emailRaw || "").trim().toLowerCase();
  if (!email || !email.includes("@")) throw new Error("请填写有效邮箱");
  if (String(password || "").length < 8) throw new Error("密码至少 8 位");

  const existing = await db
    .prepare(`SELECT user_id FROM users WHERE email = ? OR username = ?`)
    .bind(email, email)
    .first();
  if (existing) throw new Error("该邮箱已注册");

  const salt = randomHex(16);
  const password_hash = await hashPassword(password, salt);
  const user_id = `u_${randomHex(8)}`;
  const now = Date.now();
  const username = email.split("@")[0] || user_id;
  await db
    .prepare(
      `INSERT INTO users (user_id, email, username, name, role, password_salt, password_hash, created_at_ms)
       VALUES (?, ?, ?, ?, 'user', ?, ?, ?)`,
    )
    .bind(user_id, email, username, username, salt, password_hash, now)
    .run();

  const token = await createSession(db, user_id);
  return {
    user: publicUser({ user_id, email, username, name: username, role: "user" }),
    token,
    ws_token: token,
  };
}

export async function loginUser(
  db: D1Database,
  accountRaw: string,
  password: string,
): Promise<{ user: ReturnType<typeof publicUser>; token: string; ws_token: string }> {
  const account = String(accountRaw || "").trim().toLowerCase();
  if (!account || !password) throw new Error("账号或密码错误");

  const row = await db
    .prepare(
      `SELECT user_id, email, username, name, role, password_salt, password_hash
       FROM users WHERE email = ? OR username = ?`,
    )
    .bind(account, account)
    .first<UserRow>();
  if (!row) throw new Error("账号或密码错误");

  const got = await hashPassword(password, row.password_salt);
  if (got !== row.password_hash) throw new Error("账号或密码错误");

  const token = await createSession(db, row.user_id);
  return { user: publicUser(row), token, ws_token: token };
}

async function createSession(db: D1Database, user_id: string): Promise<string> {
  const token = randomHex(32);
  const now = Date.now();
  await db
    .prepare(
      `INSERT INTO sessions (token, user_id, created_at_ms, expires_at_ms) VALUES (?, ?, ?, ?)`,
    )
    .bind(token, user_id, now, now + SESSION_TTL_MS)
    .run();
  return token;
}

export async function sessionUser(
  db: D1Database,
  token: string,
): Promise<ReturnType<typeof publicUser> | null> {
  if (!token) return null;
  const now = Date.now();
  const row = await db
    .prepare(
      `SELECT u.user_id, u.email, u.username, u.name, u.role
       FROM sessions s
       JOIN users u ON u.user_id = s.user_id
       WHERE s.token = ? AND s.expires_at_ms > ?`,
    )
    .bind(token, now)
    .first<{ user_id: string; email: string; username: string; name: string; role: string }>();
  return row ? publicUser(row) : null;
}

export async function destroySession(db: D1Database, token: string): Promise<void> {
  if (!token) return;
  await db.prepare(`DELETE FROM sessions WHERE token = ?`).bind(token).run();
}

export function bearer(request: Request): string {
  const raw = request.headers.get("Authorization") || "";
  return raw.toLowerCase().startsWith("bearer ") ? raw.slice(7).trim() : "";
}

/** Match mino_nexus.core.ids.serial: prefix + zero-padded increment, no separator. */
export function serial(existing: string[], prefix: string): string {
  let highest = 0;
  for (const raw of existing) {
    const text = String(raw || "");
    if (!text.startsWith(prefix)) continue;
    const tail = text.slice(prefix.length);
    if (/^\d+$/.test(tail)) highest = Math.max(highest, Number.parseInt(tail, 10));
  }
  const number = highest + 1;
  const body = number < 1000 ? String(number).padStart(3, "0") : String(number);
  return prefix + body;
}

export async function listProjects(db: D1Database, owner_user_id: string): Promise<unknown[]> {
  /** Match mino_nexus.resources.project_store.list_projects shape (id + apps). */
  const { results: projects } = await db
    .prepare(
      `SELECT project_id, name, created_at_ms FROM projects WHERE owner_user_id = ? ORDER BY created_at_ms DESC`,
    )
    .bind(owner_user_id)
    .all<{ project_id: string; name: string; created_at_ms: number }>();
  const { results: apps } = await db
    .prepare(
      `SELECT app_id, project_id, name, platform, created_at_ms FROM apps WHERE owner_user_id = ?`,
    )
    .bind(owner_user_id)
    .all<{
      app_id: string;
      project_id: string;
      name: string;
      platform: string | null;
      created_at_ms: number;
    }>();
  const { results: repos } = await db
    .prepare(`SELECT project_id, url, branch FROM project_script_repos`)
    .all<{ project_id: string; url: string; branch: string }>();
  const repoByProject = new Map(
    (repos || []).map((r) => [r.project_id, r] as const),
  );
  const appsByProject = new Map<string, Record<string, unknown>[]>();
  for (const a of apps || []) {
    const list = appsByProject.get(a.project_id) || [];
    list.push({
      id: a.app_id,
      app_id: a.app_id,
      name: a.name,
      platforms: a.platform || "",
      project_id: a.project_id,
      created_at: a.created_at_ms,
    });
    appsByProject.set(a.project_id, list);
  }
  return (projects || []).map((p) => {
    const projectApps = appsByProject.get(p.project_id) || [];
    const repo = repoByProject.get(p.project_id);
    return {
      project_id: p.project_id,
      id: p.project_id,
      name: p.name,
      created_at: p.created_at_ms,
      repo_url: repo?.url || "",
      repo_branch: repo?.branch || "",
      apps: projectApps,
      app_count: projectApps.length,
      case_count: 0,
      knowledge_count: 0,
    };
  });
}

export async function createProject(
  db: D1Database,
  owner_user_id: string,
  nameRaw: string,
): Promise<Record<string, unknown>> {
  const name = String(nameRaw || "").trim() || "未命名项目";
  const { results } = await db
    .prepare(`SELECT project_id FROM projects`)
    .all<{ project_id: string }>();
  const project_id = serial(
    (results || []).map((r) => r.project_id),
    "PRJ",
  );
  const now = Date.now();
  await db
    .prepare(
      `INSERT INTO projects (project_id, owner_user_id, name, created_at_ms) VALUES (?, ?, ?, ?)`,
    )
    .bind(project_id, owner_user_id, name, now)
    .run();
  return { project_id, id: project_id, name, created_at: now };
}

export async function createApp(
  db: D1Database,
  owner_user_id: string,
  project_id: string,
  nameRaw: string,
  platforms = "",
): Promise<Record<string, unknown>> {
  /** Match project_store.create_app: platforms is a comma-separated string. */
  const project = await db
    .prepare(`SELECT project_id FROM projects WHERE project_id = ? AND owner_user_id = ?`)
    .bind(project_id, owner_user_id)
    .first();
  if (!project) throw new Error("没有这个项目");
  const name = String(nameRaw || "").trim() || "未命名应用";
  const allowed = new Set(["android", "ios", "web", "mac", "win"]);
  const normalized = String(platforms || "")
    .split(",")
    .map((p) => p.trim().toLowerCase())
    .filter((p) => allowed.has(p));
  const platformsStr = [...new Set(normalized)].join(",");
  const { results } = await db.prepare(`SELECT app_id FROM apps`).all<{ app_id: string }>();
  const app_id = serial(
    (results || []).map((r) => r.app_id),
    "APP",
  );
  const now = Date.now();
  await db
    .prepare(
      `INSERT INTO apps (app_id, project_id, owner_user_id, name, platform, meta_json, created_at_ms)
       VALUES (?, ?, ?, ?, ?, NULL, ?)`,
    )
    .bind(app_id, project_id, owner_user_id, name, platformsStr || null, now)
    .run();
  return {
    app_id,
    id: app_id,
    project_id,
    name,
    platforms: platformsStr,
    platform: platformsStr,
  };
}
