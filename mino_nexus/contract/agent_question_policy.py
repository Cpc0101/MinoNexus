"""Technical tool parameters are Agent repairs, not operator decisions."""
import re

INTERNAL_FIELDS = frozenset({"session_id", "client_request_id", "attempt_id", "plugin", "command",
    "workflow_id", "agent_task_id", "node_id", "operation_id", "revision", "expected_revision",
    "request_id", "source_hash", "script_targets", "execution_id"})


def internal_question(field):
    parts = set(re.split(r"[\s,、]+", str(field or "").strip().lower())) - {""}
    return bool(parts) and parts <= INTERNAL_FIELDS
