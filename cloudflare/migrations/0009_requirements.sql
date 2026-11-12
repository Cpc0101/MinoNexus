CREATE TABLE IF NOT EXISTS project_requirements (
  project_id TEXT NOT NULL,
  requirement_id TEXT NOT NULL,
  app_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL,
  source_url TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (project_id, requirement_id)
);
