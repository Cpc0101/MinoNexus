CREATE TABLE IF NOT EXISTS scouts (
  scout_id TEXT PRIMARY KEY NOT NULL,
  node_token TEXT NOT NULL,
  online INTEGER NOT NULL DEFAULT 0,
  last_seen_ms INTEGER NOT NULL DEFAULT 0,
  scout_version TEXT,
  host_json TEXT,
  meta_json TEXT
);

CREATE TABLE IF NOT EXISTS devices (
  device_id TEXT PRIMARY KEY NOT NULL,
  scout_id TEXT NOT NULL,
  platform TEXT,
  label TEXT,
  ready INTEGER NOT NULL DEFAULT 0,
  online INTEGER NOT NULL DEFAULT 0,
  meta_json TEXT,
  updated_at_ms INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS devices_by_scout ON devices(scout_id);
