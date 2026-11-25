from __future__ import annotations

from mino_nexus.resources import scout_record_store as records


def _record(**overrides):
    summary = {
        "status": "cancelled", "sealed": True, "execution_started": "yes",
        "execution_stopped": True, "browser_closed": True,
        "resource_isolated": True, "state": "completed",
        "reason_code": "debug_session_closed", "scout_process_generation": "gen-1",
        "summary_version": 7, "platform": "web",
    }
    summary.update(overrides)
    return {
        "session_id": "s-1", "attempt_id": "a-1", "task_id": "t-1",
        "owner_user_id": "u-1", "node_id": "n-1", "sn": "web1",
        "case_id": "debug", "status": summary.get("status", "cancelled"),
        "cancel_requested": False, "summary_version": 7, "summary": summary,
        "payload": {"platform": "web"}, "context": {},
    }


def _proof(record):
    return {
        "task_id": "t-1", "session_id": "s-1", "attempt_id": "a-1", "owner_user_id": "u-1",
        "node_id": "n-1", "sn": "web1", "case_id": "debug", "platform": "web",
        "status": record["status"], "sealed": True, "execution_stopped": True,
        "browser_closed": True, "resource_isolated": False,
        "summary_version": 7, "scout_process_generation": "gen-1",
        "resource_stop": {
            "contract": "script_resource_stop.v1", "stop_confirmed": True,
            "worker_active": False, "browser_closed": True,
            "reason_code": "web_lane_stopped", "mode": "same_process",
            "scout_process_generation": "gen-1", "process_started_at": 1.0,
        },
    }


def test_isolation_alone_does_not_deny_stop_evidence():
    record = _record()
    record["context"] = {"resource_stop": _proof(record)}

    assert records.stop_evidence_confirmed(record) is True
    assert records.release_evidence_confirmed(record) is True


def test_unknown_execution_still_blocks_release():
    record = _record(execution_started="unknown")
    record["context"] = {"resource_stop": _proof(record)}

    assert records.stop_evidence_confirmed(record) is False
    assert records.release_evidence_confirmed(record) is False


def test_cancelled_isolated_web_record_is_recoverable():
    record = _record()
    record["cancel_requested"] = True

    assert records.web_stop_recovery_candidate(record) is True
    record["context"] = {"resource_stop": _proof(record)}
    assert records.release_evidence_confirmed(record) is True


def test_interrupted_record_is_not_a_recovery_candidate():
    record = _record(state="interrupted")
    record["cancel_requested"] = True

    assert records.web_stop_recovery_candidate(record) is False
