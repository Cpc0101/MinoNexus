-- Cloud and Python Nexus share the same task/record contract. Detailed evidence
-- stays on Scout; these tables contain dispatch, result indexes and resource leases.
ALTER TABLE scouts ADD COLUMN record_api_version INTEGER NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS script_dispatches (
  attempt_id TEXT PRIMARY KEY NOT NULL,
  run_id TEXT NOT NULL,
  case_id TEXT NOT NULL,
  session_id TEXT NOT NULL UNIQUE,
  scout_id TEXT NOT NULL,
  owner_user_id TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'queued',
  params_json TEXT NOT NULL,
  hint_json TEXT NOT NULL DEFAULT '{}',
  result_json TEXT NOT NULL DEFAULT '{}',
  sync_version INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_ms INTEGER NOT NULL DEFAULT 0,
  accepted_at_ms INTEGER NOT NULL DEFAULT 0,
  updated_at_ms INTEGER NOT NULL,
  created_at_ms INTEGER NOT NULL,
  UNIQUE(run_id, case_id)
);
CREATE INDEX IF NOT EXISTS script_dispatches_due ON script_dispatches(state, next_attempt_ms);
CREATE INDEX IF NOT EXISTS script_dispatches_run ON script_dispatches(run_id, created_at_ms);
CREATE INDEX IF NOT EXISTS script_dispatches_owner ON script_dispatches(owner_user_id, created_at_ms);

CREATE TABLE IF NOT EXISTS script_device_leases (
  resource_key TEXT PRIMARY KEY NOT NULL,
  scout_id TEXT NOT NULL,
  sn TEXT NOT NULL,
  slot INTEGER NOT NULL DEFAULT 0,
  run_id TEXT NOT NULL DEFAULT '',
  owner_user_id TEXT NOT NULL DEFAULT '',
  lease_id TEXT NOT NULL DEFAULT '',
  generation INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'released',
  expires_at_ms INTEGER NOT NULL DEFAULT 0,
  updated_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS script_device_leases_run ON script_device_leases(run_id);

CREATE TABLE IF NOT EXISTS script_request_keys (
  owner_user_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  created_at_ms INTEGER NOT NULL,
  PRIMARY KEY(owner_user_id, request_id)
);

CREATE TABLE IF NOT EXISTS pool_accounts (
  id TEXT PRIMARY KEY NOT NULL,
  project_id TEXT NOT NULL,
  owner_user_id TEXT NOT NULL,
  env TEXT NOT NULL DEFAULT 'test',
  display_name TEXT NOT NULL DEFAULT '',
  phone TEXT NOT NULL DEFAULT '',
  email TEXT NOT NULL DEFAULT '',
  username TEXT NOT NULL DEFAULT '',
  password TEXT NOT NULL DEFAULT '',
  otp TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'mixed',
  profile_id TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  locked INTEGER NOT NULL DEFAULT 0,
  facets_json TEXT NOT NULL DEFAULT '{}',
  lease_run_id TEXT NOT NULL DEFAULT '',
  lease_session_id TEXT NOT NULL DEFAULT '',
  lease_id TEXT NOT NULL DEFAULT '',
  lease_expires_at_ms INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS pool_accounts_available ON pool_accounts(owner_user_id, project_id, env, locked, lease_run_id);
