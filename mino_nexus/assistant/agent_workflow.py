"""Persist Agent progress in Nexus SQL; no device or case actions."""
from datetime import datetime, timezone
import json

from sqlalchemy import text as sql
from mino_nexus.contract.agent_workflow import text, transition
from mino_nexus.core.database import session_scope
from mino_nexus.models.agent_workflow import AgentWorkflow


def operate(owner_user_id: str, workflow_id: str, operation: str, args: dict) -> dict:
    owner = text(owner_user_id, "owner_user_id", 128)
    workflow_id = text(workflow_id, "workflow_id", 128)
    with session_scope() as db:
        if operation != "get":
            db.execute(sql("BEGIN IMMEDIATE"))
        row = db.get(AgentWorkflow, (owner, workflow_id))
        old = row.state if row else None
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        state, result = transition(old, operation, workflow_id, args, now)
        if state != old and operation != "get":
            if len(json.dumps(state, ensure_ascii=False).encode()) > 2_000_000:
                raise ValueError("Workflow record limit reached; deliver this scope and start a new workflow")
            if row:
                row.state, row.revision, row.updated_at = state, row.revision + 1, now
            else:
                row = AgentWorkflow(owner_user_id=owner, workflow_id=workflow_id, state=state, revision=1, updated_at=now)
                db.add(row)
        return {**result, "revision": row.revision, "updated_at": row.updated_at}
