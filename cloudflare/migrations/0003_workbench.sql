ALTER TABLE scouts ADD COLUMN owner_user_id TEXT;

CREATE TABLE IF NOT EXISTS install_tokens (
  token TEXT PRIMARY KEY NOT NULL,
  install_id TEXT NOT NULL UNIQUE,
  user_id TEXT NOT NULL,
  node_id TEXT,
  created_at_ms INTEGER NOT NULL,
  expires_at_ms INTEGER NOT NULL,
  registered_at_ms INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS install_tokens_by_user ON install_tokens(user_id);

CREATE TABLE IF NOT EXISTS user_settings (
  owner_user_id TEXT NOT NULL,
  key TEXT NOT NULL,
  value_json TEXT NOT NULL,
  updated_at_ms INTEGER NOT NULL,
  PRIMARY KEY (owner_user_id, key)
);

CREATE TABLE IF NOT EXISTS apps (
  app_id TEXT PRIMARY KEY NOT NULL,
  project_id TEXT NOT NULL,
  owner_user_id TEXT NOT NULL,
  name TEXT NOT NULL,
  platform TEXT,
  meta_json TEXT,
  created_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS apps_by_project ON apps(project_id);
