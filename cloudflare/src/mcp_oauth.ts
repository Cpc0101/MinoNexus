/**
 * Port of mino_nexus.platform.mcp_oauth — same prefixes, TTLs, PKCE, token rotation.
 */

export const SCOPE = "mcp";
export const ACCESS_PREFIX = "mino_oat_";
export const REFRESH_PREFIX = "mino_ort_";
const ACCESS_TTL = 3600;
const REFRESH_TTL = 30 * 24 * 3600;
const CODE_TTL = 60;
const REQUEST_TTL = 600;
const CLIENT_PREFIX = "mino_client_";
const VERIFIER = /^[A-Za-z0-9._~-]{43,128}$/;
const CHALLENGE = /^[A-Za-z0-9\-_]{43,128}$/;

const HITS = new Map<string, number[]>();
const CIMD = new Map<string, { until: number; doc: Record<string, unknown> }>();
const CIMD_TTL_MS = 600_000;

export class OAuthPage extends Error {
  error: string;
  status: number;
  constructor(error: string, description: string, status = 400) {
    super(description);
    this.error = error;
    this.status = status;
  }
  get description(): string {
    return this.message;
  }
}

export class OAuthRedirect extends Error {
  location: string;
  constructor(location: string) {
    super(location);
    this.location = location;
  }
}

function now(): number {
  return Math.floor(Date.now() / 1000);
}

async function sha256Hex(value: string): Promise<string> {
  const buf = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(String(value || "")),
  );
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function randomUrlSafe(bytes = 32): string {
  const buf = new Uint8Array(bytes);
  crypto.getRandomValues(buf);
  let binary = "";
  for (const b of buf) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

export function publicOrigin(baseUrl: string): string {
  return String(baseUrl || "").replace(/\/+$/, "");
}

export function canonicalResource(baseUrl: string): string {
  return publicOrigin(baseUrl) + "/mcp";
}

function normResource(value: string): string {
  let url: URL;
  try {
    url = new URL(String(value || "").trim());
  } catch {
    return "";
  }
  if (url.username || url.password || url.search || url.hash) return "";
  if (url.protocol !== "http:" && url.protocol !== "https:") return "";
  const host = url.hostname.toLowerCase();
  let port = url.port;
  if ((url.protocol === "https:" && (port === "443" || !port)) ||
      (url.protocol === "http:" && (port === "80" || !port))) {
    port = "";
  }
  const path = url.pathname.replace(/\/+$/, "");
  const netloc = port ? `${host}:${port}` : host;
  return `${url.protocol}//${netloc}${path}`;
}

export function sameResource(got: string, expected: string): boolean {
  const left = normResource(got);
  const right = normResource(expected);
  return Boolean(left) && left === right;
}

function limit(bucket: string, max: number, windowSec: number): void {
  const nowMs = Date.now();
  const windowMs = windowSec * 1000;
  const rows = (HITS.get(bucket) || []).filter((t) => nowMs - t < windowMs);
  if (rows.length >= max) {
    throw new OAuthPage("too_many_requests", "请求过多，稍后再试", 429);
  }
  rows.push(nowMs);
  HITS.set(bucket, rows.slice(-max));
}

function cleanName(value: string): string {
  const name = String(value || "").split(/\s+/).join(" ").trim();
  if (!name || name.length > 80) {
    throw new OAuthPage("invalid_client_metadata", "客户端名称无效");
  }
  return name;
}

function loopback(hostname: string): boolean {
  const host = String(hostname || "").toLowerCase().replace(/^\[|\]$/g, "");
  return host === "localhost" || host === "127.0.0.1" || host === "::1";
}

export function validateRedirect(uri: string): string {
  const raw = String(uri || "").trim();
  let parts: URL;
  try {
    parts = new URL(raw);
  } catch {
    throw new OAuthPage("invalid_request", "回调地址无效");
  }
  if (!raw || raw.length > 500 || parts.hash || parts.username || parts.password) {
    throw new OAuthPage("invalid_request", "回调地址无效");
  }
  const host = (parts.hostname || "").toLowerCase();
  if (parts.protocol === "https:" && host) return raw;
  if (parts.protocol === "http:" && loopback(host)) return raw;
  throw new OAuthPage("invalid_request", "回调地址必须是 https，或这台电脑上的 http");
}

function assertPublicHost(hostname: string): void {
  const host = String(hostname || "").toLowerCase().replace(/^\[|\]$/g, "");
  if (
    !host ||
    host === "localhost" ||
    host.endsWith(".localhost") ||
    host.endsWith(".local") ||
    host.endsWith(".internal")
  ) {
    throw new OAuthPage("invalid_client", "客户端登记地址不可用");
  }
}

async function fetchCimd(url: string): Promise<Record<string, unknown>> {
  const cached = CIMD.get(url);
  if (cached && cached.until > Date.now()) return { ...cached.doc };
  let parts: URL;
  try {
    parts = new URL(url);
  } catch {
    throw new OAuthPage("invalid_client", "客户端编号必须是带路径的 https 地址");
  }
  if (
    parts.protocol !== "https:" ||
    !parts.hostname ||
    !parts.pathname ||
    parts.pathname === "/" ||
    parts.hash ||
    parts.username
  ) {
    throw new OAuthPage("invalid_client", "客户端编号必须是带路径的 https 地址");
  }
  assertPublicHost(parts.hostname);
  let response: Response;
  try {
    response = await fetch(url, {
      redirect: "manual",
      headers: { Accept: "application/json", "User-Agent": "MinoNexus" },
    });
  } catch {
    throw new OAuthPage("invalid_client", "读不到客户端登记");
  }
  if (response.status !== 200) {
    throw new OAuthPage("invalid_client", "读不到客户端登记");
  }
  const text = await response.text();
  if (text.length > 65536) {
    throw new OAuthPage("invalid_client", "读不到客户端登记");
  }
  let document: Record<string, unknown>;
  try {
    document = JSON.parse(text) as Record<string, unknown>;
  } catch {
    throw new OAuthPage("invalid_client", "客户端登记不是 JSON");
  }
  if (!document || typeof document !== "object" || document.client_id !== url) {
    throw new OAuthPage("invalid_client", "客户端登记与地址不一致");
  }
  const method = String(document.token_endpoint_auth_method || "none");
  if (method !== "none") {
    throw new OAuthPage("invalid_client", "目前只接受不带客户端密钥的登记");
  }
  CIMD.set(url, { until: Date.now() + CIMD_TTL_MS, doc: document });
  return { ...document };
}

type ClientRecord = {
  client_id: string;
  client_name: string;
  redirect_uris: string[];
  auth_method: string;
  secret_hash: string;
};

async function clientRecord(db: D1Database, clientId: string): Promise<ClientRecord> {
  const raw = String(clientId || "").trim();
  if (!raw || raw.length > 500) throw new OAuthPage("invalid_client", "缺少客户端");
  if (raw.startsWith("https://")) {
    const document = await fetchCimd(raw);
    const redirects = document.redirect_uris;
    if (!Array.isArray(redirects)) {
      throw new OAuthPage("invalid_client", "客户端登记缺少回调地址");
    }
    const name = String(document.client_name || "").split(/\s+/).join(" ").trim();
    if (!name || name.length > 80) {
      throw new OAuthPage("invalid_client", "客户端登记缺少名称");
    }
    return {
      client_id: raw,
      client_name: name,
      redirect_uris: redirects.filter((item): item is string => typeof item === "string" && Boolean(item)),
      auth_method: "none",
      secret_hash: "",
    };
  }
  const row = await db
    .prepare(
      `SELECT client_id, client_name, redirect_uris, auth_method, secret_hash, source
       FROM oauth_clients WHERE client_id = ?`,
    )
    .bind(raw)
    .first<{
      client_id: string;
      client_name: string;
      redirect_uris: string;
      auth_method: string;
      secret_hash: string;
      source: string;
    }>();
  if (!row || row.source !== "dcr") throw new OAuthPage("invalid_client", "客户端不存在");
  let redirects: unknown;
  try {
    redirects = JSON.parse(row.redirect_uris || "[]");
  } catch {
    throw new OAuthPage("invalid_client", "客户端登记已损坏");
  }
  return {
    client_id: row.client_id,
    client_name: row.client_name,
    redirect_uris: Array.isArray(redirects) ? redirects.map(String) : [],
    auth_method: row.auth_method,
    secret_hash: row.secret_hash,
  };
}

function matchRedirect(client: ClientRecord, redirectUri: string): string {
  const uri = validateRedirect(redirectUri);
  if (!client.redirect_uris.includes(uri)) {
    throw new OAuthPage("invalid_request", "回调地址没有登记");
  }
  return uri;
}

export function errorRedirect(
  redirectUri: string,
  error: string,
  description: string,
  state = "",
): string {
  const query = new URLSearchParams({ error, error_description: description });
  if (state) query.set("state", state);
  return redirectUri + (redirectUri.includes("?") ? "&" : "?") + query.toString();
}

export function metadata(baseUrl: string): Record<string, unknown> {
  const origin = publicOrigin(baseUrl);
  return {
    issuer: origin,
    authorization_endpoint: origin + "/oauth/authorize",
    token_endpoint: origin + "/oauth/token",
    registration_endpoint: origin + "/oauth/register",
    response_types_supported: ["code"],
    grant_types_supported: ["authorization_code", "refresh_token"],
    code_challenge_methods_supported: ["S256"],
    token_endpoint_auth_methods_supported: ["none", "client_secret_post"],
    scopes_supported: [SCOPE],
    client_id_metadata_document_supported: true,
  };
}

export function resourceMetadata(baseUrl: string): Record<string, unknown> {
  const origin = publicOrigin(baseUrl);
  return {
    resource: canonicalResource(origin),
    authorization_servers: [origin],
    scopes_supported: [SCOPE],
    bearer_methods_supported: ["header"],
    resource_name: "Mino",
  };
}

async function purge(db: D1Database): Promise<void> {
  const t = now();
  await db.prepare(`DELETE FROM oauth_requests WHERE expires_at <= ?`).bind(t).run();
  await db.prepare(`DELETE FROM oauth_codes WHERE expires_at <= ?`).bind(t).run();
  await db.prepare(`DELETE FROM oauth_tokens WHERE expires_at <= ?`).bind(t - 86400).run();
}

export async function registerClient(
  db: D1Database,
  body: Record<string, unknown>,
  remote: string,
): Promise<Record<string, unknown>> {
  limit(`register:${remote}`, 20, 3600);
  await purge(db);
  if (!body || typeof body !== "object") {
    throw new OAuthPage("invalid_client_metadata", "登记内容无效");
  }
  const name = cleanName(String(body.client_name || "MCP 客户端"));
  const redirects = body.redirect_uris;
  if (!Array.isArray(redirects) || !redirects.length || redirects.length > 8) {
    throw new OAuthPage("invalid_client_metadata", "需要 1 到 8 个回调地址");
  }
  const cleaned = redirects.map((item) => validateRedirect(String(item)));
  const method = String(body.token_endpoint_auth_method || "none");
  if (method !== "none" && method !== "client_secret_post") {
    throw new OAuthPage("invalid_client_metadata", "不支持这种客户端认证方式");
  }
  const grants = body.grant_types || ["authorization_code", "refresh_token"];
  const responses = body.response_types || ["code"];
  if (!Array.isArray(grants) || !grants.includes("authorization_code")) {
    throw new OAuthPage("invalid_client_metadata", "需要 authorization_code");
  }
  if (!Array.isArray(responses) || !responses.includes("code")) {
    throw new OAuthPage("invalid_client_metadata", "需要 response_type=code");
  }
  const client_id = CLIENT_PREFIX + randomUrlSafe(18);
  const secret = method === "client_secret_post" ? randomUrlSafe(32) : "";
  const secret_hash = secret ? await sha256Hex(secret) : "";
  const created_at = now();
  await db
    .prepare(
      `INSERT INTO oauth_clients
       (client_id, client_name, redirect_uris, auth_method, secret_hash, source, created_at)
       VALUES (?, ?, ?, ?, ?, 'dcr', ?)`,
    )
    .bind(client_id, name, JSON.stringify(cleaned), method, secret_hash, created_at)
    .run();
  const out: Record<string, unknown> = {
    client_id,
    client_name: name,
    redirect_uris: cleaned,
    grant_types: ["authorization_code", "refresh_token"],
    response_types: ["code"],
    token_endpoint_auth_method: method,
    client_id_issued_at: created_at,
  };
  if (secret) {
    out.client_secret = secret;
    out.client_secret_expires_at = 0;
  }
  return out;
}

export async function beginAuthorization(
  db: D1Database,
  params: Record<string, string>,
  resourceExpected: string,
  remote: string,
): Promise<string> {
  limit(`authorize:${remote}`, 30, 60);
  await purge(db);
  const client_id = String(params.client_id || "");
  const state = String(params.state || "");
  if (state.length > 512 || [...state].some((ch) => ch.charCodeAt(0) < 32)) {
    throw new OAuthPage("invalid_request", "state 无效");
  }
  const client = await clientRecord(db, client_id);
  const redirect_uri = matchRedirect(client, String(params.redirect_uri || ""));

  const fail = (error: string, description: string): never => {
    throw new OAuthRedirect(errorRedirect(redirect_uri, error, description, state));
  };

  if (String(params.response_type || "") !== "code") {
    fail("unsupported_response_type", "只支持授权码");
  }
  if (!state) fail("invalid_request", "缺少 state");
  if (
    String(params.code_challenge_method || "") !== "S256" ||
    !CHALLENGE.test(String(params.code_challenge || ""))
  ) {
    fail("invalid_request", "需要 S256 PKCE");
  }
  const scope = String(params.scope || SCOPE).trim();
  if (scope !== SCOPE) fail("invalid_scope", "只提供 mcp");
  const resource = String(params.resource || "");
  if (!sameResource(resource, resourceExpected)) {
    fail("invalid_target", "resource 必须是这个 MCP 地址");
  }

  const txn = randomUrlSafe(32);
  const t = now();
  await db
    .prepare(
      `INSERT INTO oauth_requests
       (txn, client_id, client_name, redirect_uri, code_challenge, resource, scope, state, expires_at, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    )
    .bind(
      txn,
      client.client_id,
      client.client_name,
      redirect_uri,
      String(params.code_challenge || ""),
      normResource(resource),
      SCOPE,
      state,
      t + REQUEST_TTL,
      t,
    )
    .run();
  return txn;
}

/** CF: Workbench Pages host; match mcp0 consent_target intent (logged-in UI). */
export function consentTarget(_baseUrl: string): string {
  return "https://workbench.minocc.top";
}

export async function requestView(
  db: D1Database,
  txn: string,
): Promise<Record<string, unknown>> {
  const row = await db
    .prepare(
      `SELECT client_name, redirect_uri, scope, expires_at FROM oauth_requests WHERE txn = ?`,
    )
    .bind(String(txn || ""))
    .first<{ client_name: string; redirect_uri: string; scope: string; expires_at: number }>();
  if (!row || Number(row.expires_at || 0) <= now()) return {};
  let host = "";
  try {
    host = new URL(row.redirect_uri).hostname.toLowerCase();
  } catch {
    host = "";
  }
  return {
    client_name: row.client_name,
    redirect_uri: row.redirect_uri,
    localhost: loopback(host),
    scope: row.scope,
  };
}

async function pkce(verifier: string, challenge: string): Promise<boolean> {
  if (!VERIFIER.test(verifier || "") || !CHALLENGE.test(challenge || "")) return false;
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(verifier),
  );
  const computed = btoa(String.fromCharCode(...new Uint8Array(digest)))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
  return computed === challenge;
}

async function addTokens(
  db: D1Database,
  user_id: string,
  client_id: string,
  client_name: string,
  resource: string,
  family_id = "",
  family_created = 0,
): Promise<Record<string, unknown>> {
  const t = now();
  const fid = family_id || randomUrlSafe(18);
  const fcreated = family_created || t;
  const access = ACCESS_PREFIX + randomUrlSafe(32);
  const refresh = REFRESH_PREFIX + randomUrlSafe(32);
  await db
    .prepare(
      `INSERT INTO oauth_tokens
       (family_id, user_id, client_id, client_name, kind, token_hash, prefix, resource, scope,
        expires_at, family_created, replaced, revoked, created_at, last_used_at)
       VALUES (?, ?, ?, ?, 'access', ?, ?, ?, ?, ?, ?, 0, 0, ?, 0)`,
    )
    .bind(
      fid,
      user_id,
      client_id,
      client_name,
      await sha256Hex(access),
      access.slice(0, 16),
      resource,
      SCOPE,
      t + ACCESS_TTL,
      fcreated,
      t,
    )
    .run();
  await db
    .prepare(
      `INSERT INTO oauth_tokens
       (family_id, user_id, client_id, client_name, kind, token_hash, prefix, resource, scope,
        expires_at, family_created, replaced, revoked, created_at, last_used_at)
       VALUES (?, ?, ?, ?, 'refresh', ?, ?, ?, ?, ?, ?, 0, 0, ?, 0)`,
    )
    .bind(
      fid,
      user_id,
      client_id,
      client_name,
      await sha256Hex(refresh),
      refresh.slice(0, 16),
      resource,
      SCOPE,
      t + REFRESH_TTL,
      fcreated,
      t,
    )
    .run();
  return {
    access_token: access,
    token_type: "Bearer",
    expires_in: ACCESS_TTL,
    refresh_token: refresh,
    scope: SCOPE,
  };
}

export async function decide(
  db: D1Database,
  txn: string,
  userId: string,
  allow: boolean,
): Promise<string> {
  const uid = String(userId || "").trim();
  if (!uid) throw new OAuthPage("access_denied", "请先登录", 401);
  const row = await db
    .prepare(`SELECT * FROM oauth_requests WHERE txn = ?`)
    .bind(String(txn || ""))
    .first<{
      txn: string;
      client_id: string;
      client_name: string;
      redirect_uri: string;
      code_challenge: string;
      resource: string;
      scope: string;
      state: string;
      expires_at: number;
    }>();
  if (!row || Number(row.expires_at || 0) <= now()) {
    throw new OAuthPage("invalid_request", "这次授权已经失效，回到客户端重新连接", 404);
  }
  const redirect_uri = row.redirect_uri;
  const state = row.state;
  if (!allow) {
    await db.prepare(`DELETE FROM oauth_requests WHERE txn = ?`).bind(row.txn).run();
    return errorRedirect(redirect_uri, "access_denied", "已拒绝", state);
  }
  const code = randomUrlSafe(32);
  await db
    .prepare(
      `INSERT INTO oauth_codes
       (code_hash, user_id, client_id, client_name, redirect_uri, code_challenge, resource, scope, state, expires_at, used)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)`,
    )
    .bind(
      await sha256Hex(code),
      uid,
      row.client_id,
      row.client_name,
      redirect_uri,
      row.code_challenge,
      row.resource,
      row.scope,
      state,
      now() + CODE_TTL,
    )
    .run();
  await db.prepare(`DELETE FROM oauth_requests WHERE txn = ?`).bind(row.txn).run();
  const q = new URLSearchParams({ code, state });
  return redirect_uri + (redirect_uri.includes("?") ? "&" : "?") + q.toString();
}

async function checkSecret(client: ClientRecord, presented: string): Promise<void> {
  if (client.auth_method === "none") {
    if (presented) throw new OAuthPage("invalid_client", "这个客户端不使用密钥", 401);
    return;
  }
  const secret = String(presented || "");
  if (!secret || (await sha256Hex(secret)) !== String(client.secret_hash || "")) {
    throw new OAuthPage("invalid_client", "客户端密钥不正确", 401);
  }
}

async function revokeFamily(db: D1Database, familyId: string): Promise<void> {
  await db
    .prepare(`UPDATE oauth_tokens SET revoked = 1 WHERE family_id = ?`)
    .bind(familyId)
    .run();
}

export async function exchange(
  db: D1Database,
  form: Record<string, string>,
  resourceExpected: string,
  remote: string,
): Promise<Record<string, unknown>> {
  limit(`token:${remote}`, 60, 60);
  const grant = String(form.grant_type || "");
  const client = await clientRecord(db, String(form.client_id || ""));
  await checkSecret(client, String(form.client_secret || ""));
  const resource = String(form.resource || "");
  if (!sameResource(resource, resourceExpected)) {
    throw new OAuthPage("invalid_target", "resource 必须是这个 MCP 地址");
  }
  if (grant === "authorization_code") {
    return exchangeCode(db, form, client, normResource(resource));
  }
  if (grant === "refresh_token") {
    return exchangeRefresh(db, form, client, normResource(resource));
  }
  throw new OAuthPage("unsupported_grant_type", "不支持这种授权方式");
}

async function exchangeCode(
  db: D1Database,
  form: Record<string, string>,
  client: ClientRecord,
  resource: string,
): Promise<Record<string, unknown>> {
  const code = String(form.code || "");
  const redirect_uri = String(form.redirect_uri || "");
  const verifier = String(form.code_verifier || "");
  if (!code || !redirect_uri || !verifier) {
    throw new OAuthPage("invalid_request", "授权码、回调和 code_verifier 都要带上");
  }
  const digest = await sha256Hex(code);
  const row = await db
    .prepare(`SELECT * FROM oauth_codes WHERE code_hash = ?`)
    .bind(digest)
    .first<{
      code_hash: string;
      user_id: string;
      client_id: string;
      client_name: string;
      redirect_uri: string;
      code_challenge: string;
      resource: string;
      used: number;
      expires_at: number;
    }>();
  if (!row || row.used || Number(row.expires_at || 0) <= now()) {
    throw new OAuthPage("invalid_grant", "授权码无效或已使用");
  }
  const updated = await db
    .prepare(
      `UPDATE oauth_codes SET used = 1
       WHERE code_hash = ? AND used = 0 AND expires_at > ?`,
    )
    .bind(digest, now())
    .run();
  if (!updated.meta.changes) {
    throw new OAuthPage("invalid_grant", "授权码无效或已使用");
  }
  if (
    row.client_id !== client.client_id ||
    row.redirect_uri !== redirect_uri ||
    row.resource !== resource
  ) {
    throw new OAuthPage("invalid_grant", "授权码与这次请求不一致");
  }
  if (!(await pkce(verifier, row.code_challenge))) {
    throw new OAuthPage("invalid_grant", "PKCE 校验失败");
  }
  return addTokens(db, row.user_id, row.client_id, row.client_name, resource);
}

async function exchangeRefresh(
  db: D1Database,
  form: Record<string, string>,
  client: ClientRecord,
  resource: string,
): Promise<Record<string, unknown>> {
  const presented = String(form.refresh_token || "");
  const scope = String(form.scope || SCOPE).trim();
  if (scope !== SCOPE) throw new OAuthPage("invalid_scope", "不能扩大权限");
  if (!presented.startsWith(REFRESH_PREFIX)) {
    throw new OAuthPage("invalid_grant", "刷新令牌无效");
  }
  const digest = await sha256Hex(presented);
  const row = await db
    .prepare(`SELECT * FROM oauth_tokens WHERE token_hash = ? AND kind = 'refresh'`)
    .bind(digest)
    .first<{
      id: number;
      family_id: string;
      user_id: string;
      client_id: string;
      client_name: string;
      resource: string;
      replaced: number;
      revoked: number;
      expires_at: number;
      family_created: number;
      created_at: number;
    }>();
  if (!row || row.revoked || Number(row.expires_at || 0) <= now()) {
    throw new OAuthPage("invalid_grant", "刷新令牌无效或已轮换");
  }
  if (row.replaced) {
    await revokeFamily(db, row.family_id);
    throw new OAuthPage("invalid_grant", "刷新令牌无效或已轮换");
  }
  const marked = await db
    .prepare(
      `UPDATE oauth_tokens SET replaced = 1
       WHERE token_hash = ? AND kind = 'refresh' AND replaced = 0 AND revoked = 0 AND expires_at > ?`,
    )
    .bind(digest, now())
    .run();
  if (!marked.meta.changes) {
    throw new OAuthPage("invalid_grant", "刷新令牌无效或已轮换");
  }
  if (row.client_id !== client.client_id || row.resource !== resource) {
    await revokeFamily(db, row.family_id);
    throw new OAuthPage("invalid_grant", "刷新令牌与这个客户端不一致");
  }
  return addTokens(
    db,
    row.user_id,
    row.client_id,
    row.client_name,
    resource,
    row.family_id,
    Number(row.family_created || row.created_at || now()),
  );
}

/** Returns user_id when valid. Empty string if wrong prefix. Throws OAuthPage if expired/revoked. */
export async function resolveAccessToken(
  db: D1Database,
  token: string,
  resourceExpected: string,
): Promise<string> {
  const raw = String(token || "");
  if (!raw.startsWith(ACCESS_PREFIX)) return "";
  const row = await db
    .prepare(
      `SELECT user_id, resource, revoked, expires_at FROM oauth_tokens
       WHERE token_hash = ? AND kind = 'access'`,
    )
    .bind(await sha256Hex(raw))
    .first<{ user_id: string; resource: string; revoked: number; expires_at: number }>();
  if (
    !row ||
    row.revoked ||
    Number(row.expires_at || 0) <= now() ||
    !sameResource(row.resource, resourceExpected)
  ) {
    throw new OAuthPage("invalid_token", "访问令牌无效或已过期", 401);
  }
  await db
    .prepare(`UPDATE oauth_tokens SET last_used_at = ? WHERE token_hash = ? AND kind = 'access'`)
    .bind(now(), await sha256Hex(raw))
    .run();
  return row.user_id;
}

export async function listGrants(
  db: D1Database,
  userId: string,
): Promise<Array<Record<string, unknown>>> {
  const { results } = await db
    .prepare(
      `SELECT family_id, client_id, client_name, prefix, created_at, last_used_at, revoked
       FROM oauth_tokens
       WHERE user_id = ? AND kind = 'refresh' AND replaced = 0
       ORDER BY id DESC`,
    )
    .bind(userId)
    .all<{
      family_id: string;
      client_id: string;
      client_name: string;
      prefix: string;
      created_at: number;
      last_used_at: number;
      revoked: number;
    }>();
  return (results || []).map((r) => ({
    family_id: r.family_id,
    client_id: r.client_id,
    client_name: r.client_name,
    prefix: r.prefix,
    created_at: Number(r.created_at || 0),
    last_used_at: Number(r.last_used_at || 0),
    revoked: Boolean(r.revoked),
  }));
}

export async function revokeGrant(
  db: D1Database,
  userId: string,
  familyId: string,
): Promise<boolean> {
  const row = await db
    .prepare(
      `SELECT id FROM oauth_tokens WHERE family_id = ? AND user_id = ? LIMIT 1`,
    )
    .bind(familyId, userId)
    .first();
  if (!row) return false;
  await revokeFamily(db, familyId);
  return true;
}
