-- Account object memory is advisory and independent of scalar account facets.
CREATE TABLE IF NOT EXISTS pool_account_data (
  project_id TEXT NOT NULL,
  account_id TEXT NOT NULL REFERENCES pool_accounts(id) ON DELETE CASCADE,
  data_id TEXT NOT NULL,
  app_id TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL,
  object_ref TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  attributes_json TEXT NOT NULL DEFAULT '{}',
  source_task_id TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(project_id, account_id, data_id)
);
CREATE INDEX IF NOT EXISTS pool_account_data_lookup ON pool_account_data(project_id, account_id, app_id, kind, updated_at);
CREATE TABLE IF NOT EXISTS pool_account_data_requests (
  project_id TEXT NOT NULL,
  account_id TEXT NOT NULL REFERENCES pool_accounts(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  app_id TEXT NOT NULL DEFAULT '',
  case_id TEXT NOT NULL DEFAULT '',
  data_need TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(project_id, account_id, run_id, session_id)
);
