-- Match mino_nexus.models.project_repo_apps.ProjectRepoApps
CREATE TABLE IF NOT EXISTS project_repo_apps (
  project_id TEXT PRIMARY KEY NOT NULL,
  revision TEXT NOT NULL DEFAULT '',
  branch TEXT NOT NULL DEFAULT '',
  apps_json TEXT NOT NULL DEFAULT '{}',
  synced_by TEXT NOT NULL DEFAULT '',
  synced_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS app_regression_runs (
  run_id TEXT PRIMARY KEY NOT NULL,
  owner_user_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  app_id TEXT NOT NULL,
  status TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at_ms INTEGER NOT NULL,
  updated_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS app_regression_runs_by_owner ON app_regression_runs(owner_user_id, created_at_ms);

CREATE TABLE IF NOT EXISTS script_admissions (
  run_id TEXT PRIMARY KEY NOT NULL,
  session_id TEXT NOT NULL,
  case_id TEXT NOT NULL DEFAULT '',
  sn TEXT NOT NULL,
  scout_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  app_id TEXT NOT NULL,
  owner_user_id TEXT NOT NULL,
  platform TEXT NOT NULL DEFAULT '',
  live INTEGER NOT NULL DEFAULT 1,
  created_at_ms INTEGER NOT NULL
);
