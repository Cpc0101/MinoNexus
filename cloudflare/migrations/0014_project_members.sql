CREATE TABLE IF NOT EXISTS project_members (
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  added_by TEXT NOT NULL,
  added_at_ms INTEGER NOT NULL,
  PRIMARY KEY(project_id,user_id)
);
CREATE INDEX IF NOT EXISTS project_members_user ON project_members(user_id,project_id);
