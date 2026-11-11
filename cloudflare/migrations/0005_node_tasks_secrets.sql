CREATE TABLE IF NOT EXISTS node_tool_tasks (
  task_id TEXT PRIMARY KEY NOT NULL,
  owner_user_id TEXT NOT NULL,
  operation_id TEXT NOT NULL,
  node_id TEXT NOT NULL,
  tool_id TEXT NOT NULL,
  action TEXT NOT NULL,
  state TEXT NOT NULL,
  active_slot TEXT,
  snapshot_json TEXT,
  created_at_ms INTEGER NOT NULL,
  updated_at_ms INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS node_tool_tasks_by_op
  ON node_tool_tasks(owner_user_id, operation_id);
CREATE INDEX IF NOT EXISTS node_tool_tasks_by_node
  ON node_tool_tasks(node_id, updated_at_ms DESC);

CREATE TABLE IF NOT EXISTS device_secrets (
  device_id TEXT PRIMARY KEY NOT NULL,
  owner_user_id TEXT NOT NULL,
  lock_password TEXT NOT NULL DEFAULT '',
  updated_at_ms INTEGER NOT NULL
);
