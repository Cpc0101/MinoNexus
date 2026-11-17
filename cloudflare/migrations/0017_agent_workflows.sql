CREATE TABLE IF NOT EXISTS agent_workflows (
  owner_user_id TEXT NOT NULL,
  workflow_id TEXT NOT NULL,
  state_json TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (owner_user_id, workflow_id)
);
