CREATE TABLE IF NOT EXISTS mcp_elicitations (
  id TEXT PRIMARY KEY NOT NULL,
  user_id TEXT NOT NULL,
  result_json TEXT,
  created_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS mcp_elicitations_by_age ON mcp_elicitations(created_at_ms);
