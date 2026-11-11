-- Scout plugin / download-centre contract from REGISTER + HEARTBEAT (match Nexus node_registry).
ALTER TABLE scouts ADD COLUMN plugins_json TEXT;
ALTER TABLE scouts ADD COLUMN plugins_ready INTEGER;
ALTER TABLE scouts ADD COLUMN tool_tasks_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE scouts ADD COLUMN plugin_warning TEXT;
ALTER TABLE scouts ADD COLUMN runtime_environment TEXT;
ALTER TABLE scouts ADD COLUMN tool_snapshot_json TEXT;
ALTER TABLE scouts ADD COLUMN tool_snapshot_at_ms INTEGER;
ALTER TABLE scouts ADD COLUMN tool_snapshot_owner TEXT;
