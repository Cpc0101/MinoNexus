-- An explicit, owner-authorized Web lane review. Original Scout verdicts stay sealed.
CREATE TABLE IF NOT EXISTS script_resource_resolutions (
  run_id TEXT PRIMARY KEY NOT NULL,
  owner_user_id TEXT NOT NULL,
  scout_id TEXT NOT NULL,
  sn TEXT NOT NULL,
  lease_id TEXT NOT NULL,
  generation INTEGER NOT NULL,
  proof_json TEXT NOT NULL,
  created_at_ms INTEGER NOT NULL
);
