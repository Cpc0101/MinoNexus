-- Match mino_nexus.models.project_script_repo.ProjectScriptRepo

CREATE TABLE IF NOT EXISTS project_script_repos (
  project_id TEXT PRIMARY KEY NOT NULL,
  url TEXT NOT NULL DEFAULT '',
  branch TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT ''
);
