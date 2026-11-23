"""Persist Agent progress in Nexus SQL; no device or case actions."""
from datetime import datetime, timezone
import json

from sqlalchemy import text as sql
from mino_nexus.contract.agent_workflow import text, transition, unit_list, workflow_source
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
        if operation == "init":
            source_read_id = text(args.get("source_read_id", ""), "source_read_id", 128, required=False)
            if not source_read_id:
                raise ValueError("source_read_id is required")
            # Every init, including an idempotent retry or a legacy-workflow
            # upgrade, revalidates the successful receipt in the same immediate
            # transaction. Explicit compatibility fields must agree instead of
            # being silently replaced by cached workflow values.
            from mino_nexus.casework.case_sources import resolve_successful_read

            canonical = resolve_successful_read(source_read_id, owner, db=db)
            expected_units = canonical["expected_units"]
            if "expected_units" in args and args["expected_units"] is not None:
                supplied_units = unit_list(args["expected_units"], "expected_units", required=False)
                if supplied_units != expected_units:
                    raise ValueError("expected_units do not match successful source read")
            if "source" in args and args["source"] is not None:
                supplied_source = workflow_source(args["source"])
                if supplied_source != canonical["source"]:
                    raise ValueError("source does not match successful source read")
            # Use only the validated receipt snapshot. The transition still
            # compares it with any already-locked workflow identity.
            args = {**args, "source_read_id": canonical["read_id"],
                    "expected_units": expected_units, "source": canonical["source"]}
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
