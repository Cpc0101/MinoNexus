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

export async function listProjects(db: D1Database, owner_user_id: string): Promise<unknown[]> {
  const { results } = await db
    .prepare(
      `SELECT project_id, name, created_at_ms FROM projects WHERE owner_user_id = ? ORDER BY created_at_ms DESC`,
    )
    .bind(owner_user_id)
    .all<{ project_id: string; name: string; created_at_ms: number }>();
  return (results || []).map((p) => ({
    project_id: p.project_id,
    id: p.project_id,
    name: p.name,
    created_at: p.created_at_ms,
  }));
}

export async function createProject(
  db: D1Database,
  owner_user_id: string,
  nameRaw: string,
): Promise<Record<string, unknown>> {
  const name = String(nameRaw || "").trim() || "未命名项目";
  const project_id = `p_${randomHex(8)}`;
  const now = Date.now();
  await db
    .prepare(
      `INSERT INTO projects (project_id, owner_user_id, name, created_at_ms) VALUES (?, ?, ?, ?)`,
    )
    .bind(project_id, owner_user_id, name, now)
    .run();
  return { project_id, id: project_id, name, created_at: now };
}
