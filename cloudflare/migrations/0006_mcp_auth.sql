-- Match mino_nexus.models.assistant.AssistantMcpToken + models.mcp_oauth.*

CREATE TABLE IF NOT EXISTS assistant_mcp_tokens (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'mcp',
  prefix TEXT NOT NULL DEFAULT '',
  token_hash TEXT NOT NULL UNIQUE,
  created_at INTEGER NOT NULL DEFAULT 0,
  last_used_at INTEGER NOT NULL DEFAULT 0,
  revoked INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS assistant_mcp_tokens_by_user
  ON assistant_mcp_tokens(user_id, id DESC);

CREATE TABLE IF NOT EXISTS oauth_clients (
  client_id TEXT PRIMARY KEY NOT NULL,
  client_name TEXT NOT NULL DEFAULT '',
  redirect_uris TEXT NOT NULL DEFAULT '[]',
  auth_method TEXT NOT NULL DEFAULT 'none',
  secret_hash TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'dcr',
  created_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS oauth_requests (
  txn TEXT PRIMARY KEY NOT NULL,
  client_id TEXT NOT NULL,
  client_name TEXT NOT NULL DEFAULT '',
  redirect_uri TEXT NOT NULL DEFAULT '',
  code_challenge TEXT NOT NULL DEFAULT '',
  resource TEXT NOT NULL DEFAULT '',
  scope TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT '',
  expires_at INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS oauth_requests_by_client ON oauth_requests(client_id);

CREATE TABLE IF NOT EXISTS oauth_codes (
  code_hash TEXT PRIMARY KEY NOT NULL,
  user_id TEXT NOT NULL,
  client_id TEXT NOT NULL,
  client_name TEXT NOT NULL DEFAULT '',
  redirect_uri TEXT NOT NULL DEFAULT '',
  code_challenge TEXT NOT NULL DEFAULT '',
  resource TEXT NOT NULL DEFAULT '',
  scope TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT '',
  expires_at INTEGER NOT NULL DEFAULT 0,
  used INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS oauth_codes_by_user ON oauth_codes(user_id);

CREATE TABLE IF NOT EXISTS oauth_tokens (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  family_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  client_id TEXT NOT NULL DEFAULT '',
  client_name TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT '',
  token_hash TEXT NOT NULL UNIQUE,
  prefix TEXT NOT NULL DEFAULT '',
  resource TEXT NOT NULL DEFAULT '',
  scope TEXT NOT NULL DEFAULT '',
  expires_at INTEGER NOT NULL DEFAULT 0,
  family_created INTEGER NOT NULL DEFAULT 0,
  replaced INTEGER NOT NULL DEFAULT 0,
  revoked INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL DEFAULT 0,
  last_used_at INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS oauth_tokens_by_family ON oauth_tokens(family_id);
CREATE INDEX IF NOT EXISTS oauth_tokens_by_user ON oauth_tokens(user_id);
CREATE INDEX IF NOT EXISTS oauth_tokens_by_hash ON oauth_tokens(token_hash);
