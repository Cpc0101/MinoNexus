/**
 * Match mino_nexus.assistant.store MCP token helpers.
 * Prefix mino_mcp_; only hashes stored; plaintext returned once on create.
 */

const MCP_TOKEN_PREFIX = "mino_mcp_";

function nowSec(): number {
  return Math.floor(Date.now() / 1000);
}

async function hashToken(token: string): Promise<string> {
  const buf = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(String(token || "")),
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

export async function createMcpToken(
  db: D1Database,
  userId: string,
  nameRaw: string,
): Promise<{ id: number; name: string; token: string; prefix: string; created_at: number; mcp_url: string }> {
  const name = (String(nameRaw || "").trim() || "未命名").slice(0, 60);
  const token = MCP_TOKEN_PREFIX + randomUrlSafe(32);
  const prefix = token.slice(0, MCP_TOKEN_PREFIX.length + 4);
  const token_hash = await hashToken(token);
  const created_at = nowSec();
  const result = await db
    .prepare(
      `INSERT INTO assistant_mcp_tokens
       (user_id, name, kind, prefix, token_hash, created_at, last_used_at, revoked)
       VALUES (?, ?, 'mcp', ?, ?, ?, 0, 0)`,
    )
    .bind(userId, name, prefix, token_hash, created_at)
    .run();
  const id = Number(result.meta.last_row_id || 0);
  return {
    id,
    name,
    token,
    prefix,
    created_at,
    mcp_url: "https://nexus.minocc.top/mcp",
  };
}

export async function listMcpTokens(
  db: D1Database,
  userId: string,
): Promise<Array<Record<string, unknown>>> {
  const { results } = await db
    .prepare(
      `SELECT id, name, prefix, created_at, last_used_at, revoked
       FROM assistant_mcp_tokens
       WHERE user_id = ? AND kind = 'mcp'
       ORDER BY id DESC`,
    )
    .bind(userId)
    .all<{
      id: number;
      name: string;
      prefix: string;
      created_at: number;
      last_used_at: number;
      revoked: number;
    }>();
  return (results || []).map((r) => ({
    id: r.id,
    name: r.name,
    prefix: r.prefix,
    created_at: Number(r.created_at || 0),
    last_used_at: Number(r.last_used_at || 0),
    revoked: Boolean(r.revoked),
  }));
}

export async function revokeMcpToken(
  db: D1Database,
  userId: string,
  tokenId: number,
): Promise<boolean> {
  const row = await db
    .prepare(
      `SELECT id FROM assistant_mcp_tokens WHERE id = ? AND user_id = ? AND kind = 'mcp'`,
    )
    .bind(tokenId, userId)
    .first();
  if (!row) return false;
  await db
    .prepare(`UPDATE assistant_mcp_tokens SET revoked = 1 WHERE id = ?`)
    .bind(tokenId)
    .run();
  return true;
}

/** Resolve mino_mcp_ token → user_id; updates last_used_at. */
export async function resolveMcpToken(
  db: D1Database,
  token: string,
): Promise<string | null> {
  const raw = String(token || "");
  if (!raw.startsWith(MCP_TOKEN_PREFIX)) return null;
  const token_hash = await hashToken(raw);
  const row = await db
    .prepare(
      `SELECT user_id, revoked FROM assistant_mcp_tokens
       WHERE token_hash = ? AND kind IN ('mcp', 'assistant')`,
    )
    .bind(token_hash)
    .first<{ user_id: string; revoked: number }>();
  if (!row || row.revoked) return null;
  await db
    .prepare(`UPDATE assistant_mcp_tokens SET last_used_at = ? WHERE token_hash = ?`)
    .bind(nowSec(), token_hash)
    .run();
  return row.user_id;
}
