-- Materialize the debug-origin lookup and add indexes for the polling paths.
ALTER TABLE app_regression_runs ADD COLUMN debug_origin_session_id TEXT NOT NULL DEFAULT '';

UPDATE app_regression_runs
SET debug_origin_session_id = COALESCE(json_extract(payload_json, '$.debug_origin.session_id'), '')
WHERE debug_origin_session_id = ''
  AND json_type(payload_json, '$.debug_origin.session_id') = 'text';

CREATE INDEX IF NOT EXISTS app_regression_runs_by_debug_origin
  ON app_regression_runs(debug_origin_session_id, owner_user_id, created_at_ms DESC);

-- Task lists filter by app and optionally status, including requests that do
-- not carry project_id. Keep both the recent-page and status-count paths indexed.
CREATE INDEX IF NOT EXISTS app_regression_runs_by_app_created
  ON app_regression_runs(app_id, created_at_ms DESC, run_id DESC);

CREATE INDEX IF NOT EXISTS app_regression_runs_by_app_status_created
  ON app_regression_runs(app_id, status, created_at_ms DESC, run_id DESC);

CREATE INDEX IF NOT EXISTS app_regression_runs_by_project_app_created
  ON app_regression_runs(project_id, app_id, created_at_ms DESC, run_id DESC);

-- Settlement repair starts from resources that are still held. Keep those
-- lookups narrow so the 10-second dispatch alarm does not scan every run.
CREATE INDEX IF NOT EXISTS script_device_leases_state_run
  ON script_device_leases(state, run_id)
  WHERE state!='released' AND run_id!='';
CREATE INDEX IF NOT EXISTS pool_accounts_lease_run
  ON pool_accounts(lease_run_id)
  WHERE lease_run_id!='';
CREATE INDEX IF NOT EXISTS script_admissions_live_run
  ON script_admissions(live, run_id)
  WHERE live=1;

-- The alarm polls every 10 seconds. A state-leading index still walks every
-- historical terminal dispatch; partial indexes keep the hot path bounded by
-- pending work and by the small set of dispatches in an active run.
CREATE INDEX IF NOT EXISTS script_dispatches_pending_due
  ON script_dispatches(next_attempt_ms, created_at_ms)
  WHERE state!='terminal';
CREATE INDEX IF NOT EXISTS script_dispatches_open_by_run
  ON script_dispatches(run_id, created_at_ms)
  WHERE state!='terminal';
CREATE INDEX IF NOT EXISTS script_dispatches_terminal_sync_by_run
  ON script_dispatches(run_id, updated_at_ms)
  WHERE state='terminal' AND sync_version>0;

-- The task list and app cards need counts over all history. Maintaining four
-- counters and the deterministic latest pointer avoids rescanning that history
-- on every poll. The run table remains the only source of task detail.
CREATE TABLE IF NOT EXISTS app_regression_run_stats (
  project_id TEXT NOT NULL,
  app_id TEXT NOT NULL,
  total_count INTEGER NOT NULL DEFAULT 0,
  running_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0,
  succeeded_count INTEGER NOT NULL DEFAULT 0,
  latest_run_id TEXT NOT NULL DEFAULT '',
  latest_created_at_ms INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(project_id, app_id)
);
CREATE INDEX IF NOT EXISTS app_regression_run_stats_by_app
  ON app_regression_run_stats(app_id);
CREATE INDEX IF NOT EXISTS app_regression_run_stats_by_latest
  ON app_regression_run_stats(latest_created_at_ms DESC, latest_run_id DESC);

INSERT INTO app_regression_run_stats
  (project_id, app_id, total_count, running_count, failed_count, succeeded_count, latest_run_id, latest_created_at_ms)
SELECT r.project_id, r.app_id, COUNT(*),
  SUM(CASE WHEN r.status IN ('running','queued','cancel_requested','unknown') THEN 1 ELSE 0 END),
  SUM(CASE WHEN r.status='fail' THEN 1 ELSE 0 END),
  SUM(CASE WHEN r.status='pass' THEN 1 ELSE 0 END),
  (SELECT latest.run_id FROM app_regression_runs latest
    WHERE latest.project_id=r.project_id AND latest.app_id=r.app_id
    ORDER BY latest.created_at_ms DESC, latest.run_id DESC LIMIT 1),
  MAX(r.created_at_ms)
FROM app_regression_runs r
GROUP BY r.project_id, r.app_id;

CREATE TRIGGER IF NOT EXISTS app_regression_run_stats_after_insert
AFTER INSERT ON app_regression_runs
BEGIN
  INSERT INTO app_regression_run_stats
    (project_id, app_id, total_count, running_count, failed_count, succeeded_count, latest_run_id, latest_created_at_ms)
  VALUES
    (NEW.project_id, NEW.app_id, 1,
      CASE WHEN NEW.status IN ('running','queued','cancel_requested','unknown') THEN 1 ELSE 0 END,
      CASE WHEN NEW.status='fail' THEN 1 ELSE 0 END,
      CASE WHEN NEW.status='pass' THEN 1 ELSE 0 END,
      NEW.run_id, NEW.created_at_ms)
  ON CONFLICT(project_id, app_id) DO UPDATE SET
    total_count=total_count+excluded.total_count,
    running_count=running_count+excluded.running_count,
    failed_count=failed_count+excluded.failed_count,
    succeeded_count=succeeded_count+excluded.succeeded_count,
    latest_run_id=CASE
      WHEN excluded.latest_created_at_ms>latest_created_at_ms
        OR (excluded.latest_created_at_ms=latest_created_at_ms AND excluded.latest_run_id>latest_run_id)
      THEN excluded.latest_run_id ELSE latest_run_id END,
    latest_created_at_ms=MAX(latest_created_at_ms, excluded.latest_created_at_ms);
END;

CREATE TRIGGER IF NOT EXISTS app_regression_run_stats_after_update
AFTER UPDATE OF project_id, app_id, status ON app_regression_runs
WHEN OLD.project_id<>NEW.project_id OR OLD.app_id<>NEW.app_id OR OLD.status<>NEW.status
BEGIN
  UPDATE app_regression_run_stats SET
    total_count=total_count-1,
    running_count=running_count-CASE WHEN OLD.status IN ('running','queued','cancel_requested','unknown') THEN 1 ELSE 0 END,
    failed_count=failed_count-CASE WHEN OLD.status='fail' THEN 1 ELSE 0 END,
    succeeded_count=succeeded_count-CASE WHEN OLD.status='pass' THEN 1 ELSE 0 END,
    latest_run_id=CASE WHEN latest_run_id=OLD.run_id THEN COALESCE((
      SELECT latest.run_id FROM app_regression_runs latest
      WHERE latest.project_id=OLD.project_id AND latest.app_id=OLD.app_id
      ORDER BY latest.created_at_ms DESC, latest.run_id DESC LIMIT 1), '') ELSE latest_run_id END,
    latest_created_at_ms=CASE WHEN latest_run_id=OLD.run_id THEN COALESCE((
      SELECT latest.created_at_ms FROM app_regression_runs latest
      WHERE latest.project_id=OLD.project_id AND latest.app_id=OLD.app_id
      ORDER BY latest.created_at_ms DESC, latest.run_id DESC LIMIT 1), 0) ELSE latest_created_at_ms END
  WHERE project_id=OLD.project_id AND app_id=OLD.app_id;
  DELETE FROM app_regression_run_stats WHERE project_id=OLD.project_id AND app_id=OLD.app_id AND total_count<=0;
  INSERT INTO app_regression_run_stats
    (project_id, app_id, total_count, running_count, failed_count, succeeded_count, latest_run_id, latest_created_at_ms)
  VALUES
    (NEW.project_id, NEW.app_id, 1,
      CASE WHEN NEW.status IN ('running','queued','cancel_requested','unknown') THEN 1 ELSE 0 END,
      CASE WHEN NEW.status='fail' THEN 1 ELSE 0 END,
      CASE WHEN NEW.status='pass' THEN 1 ELSE 0 END,
      NEW.run_id, NEW.created_at_ms)
  ON CONFLICT(project_id, app_id) DO UPDATE SET
    total_count=total_count+excluded.total_count,
    running_count=running_count+excluded.running_count,
    failed_count=failed_count+excluded.failed_count,
    succeeded_count=succeeded_count+excluded.succeeded_count,
    latest_run_id=CASE
      WHEN excluded.latest_created_at_ms>latest_created_at_ms
        OR (excluded.latest_created_at_ms=latest_created_at_ms AND excluded.latest_run_id>latest_run_id)
      THEN excluded.latest_run_id ELSE latest_run_id END,
    latest_created_at_ms=MAX(latest_created_at_ms, excluded.latest_created_at_ms);
END;

CREATE TRIGGER IF NOT EXISTS app_regression_run_stats_after_delete
AFTER DELETE ON app_regression_runs
BEGIN
  UPDATE app_regression_run_stats SET
    total_count=total_count-1,
    running_count=running_count-CASE WHEN OLD.status IN ('running','queued','cancel_requested','unknown') THEN 1 ELSE 0 END,
    failed_count=failed_count-CASE WHEN OLD.status='fail' THEN 1 ELSE 0 END,
    succeeded_count=succeeded_count-CASE WHEN OLD.status='pass' THEN 1 ELSE 0 END,
    latest_run_id=CASE WHEN latest_run_id=OLD.run_id THEN COALESCE((
      SELECT latest.run_id FROM app_regression_runs latest
      WHERE latest.project_id=OLD.project_id AND latest.app_id=OLD.app_id
      ORDER BY latest.created_at_ms DESC, latest.run_id DESC LIMIT 1), '') ELSE latest_run_id END,
    latest_created_at_ms=CASE WHEN latest_run_id=OLD.run_id THEN COALESCE((
      SELECT latest.created_at_ms FROM app_regression_runs latest
      WHERE latest.project_id=OLD.project_id AND latest.app_id=OLD.app_id
      ORDER BY latest.created_at_ms DESC, latest.run_id DESC LIMIT 1), 0) ELSE latest_created_at_ms END
  WHERE project_id=OLD.project_id AND app_id=OLD.app_id;
  DELETE FROM app_regression_run_stats WHERE project_id=OLD.project_id AND app_id=OLD.app_id AND total_count<=0;
END;
