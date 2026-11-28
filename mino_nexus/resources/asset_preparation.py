"""Deduplicate preparation decisions; the Agent chooses a recipe, Scout executes."""
import hashlib
import json
import time
from sqlalchemy import text
from mino_nexus.core.database import session_scope
from mino_nexus.models.asset_preparation import AssetPreparation
from mino_nexus.models.asset_storage import AssetStorage
from mino_nexus.models.agent_task import AgentTask
from mino_nexus.models.run import AppRegressionRun
from mino_nexus.resources import asset_store
from mino_nexus.resources.project_members import require_project_access


def _view(row):
    return {key: getattr(row, key) for key in ("preparation_id", "task_id", "demand", "recipe_ref", "state", "asset_ref", "evidence", "updated_at", "attempts")}


def claim(project_id, sess, value):
    require_project_access(project_id, sess)
    task_id = asset_store._ident(value.get("task_id"), "task_id")
    demand = asset_store._metadata(value.get("demand"))
    if not demand.get("kind") or not demand.get("environment") or not demand.get("app_id"):
        raise asset_store.AssetError("asset_preparation_demand_incomplete", 422)
    asset_store._ident(demand["kind"], "kind")
    recipe = value.get("recipe_ref")
    if not isinstance(recipe, str) or not 1 <= len(recipe) <= 300:
        raise asset_store.AssetError("asset_preparation_recipe_required", 422)
    # Recipe is not part of the key: selecting another recipe cannot duplicate an in-flight demand.
    ident = hashlib.sha256(json.dumps(demand, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        task = db.get(AgentTask, (sess["user_id"], task_id))
        run = db.get(AppRegressionRun, task_id)
        if not ((task and task.project_id == project_id and task.state.get("status") not in {"completed", "failed", "cancelled"})
                or (run and run.owner_user_id == sess["user_id"] and (run.payload or {}).get("project_id") == project_id and run.status not in {"completed", "failed", "cancelled"})):
            raise asset_store.AssetError("asset_preparation_owned_task_required", 403)
        from mino_nexus.models.project import App
        app = db.get(App, demand["app_id"])
        if not app or app.project_id != project_id:
            raise asset_store.AssetError("asset_preparation_app_mismatch", 422)
        row = db.get(AssetPreparation, (project_id, ident))
        retry = value.get("retry", False)
        retry_evidence = asset_store._metadata(value.get("retry_evidence", {}))
        if row and not (row.state == "failed" and retry is True):
            return {**_view(row), "action": "reuse" if row.state == "ready" else "inspect" if row.state == "failed" else "wait", "claimed": False}
        if row and (row.owner_user_id != sess["user_id"] or not retry_evidence):
            raise asset_store.AssetError("asset_preparation_retry_evidence_required")
        storage = db.query(AssetStorage).filter_by(project_id=project_id, active=1).one_or_none()
        maximum = storage.max_preparations_per_task if storage else 0
        rows = db.query(AssetPreparation).filter_by(project_id=project_id).all()
        count = sum(1 for item in rows for attempt in item.attempts if attempt["task_id"] == task_id)
        if count >= maximum:
            raise asset_store.AssetError("asset_preparation_budget_exhausted")
        now = int(time.time())
        attempt = {"task_id": task_id, "recipe_ref": recipe, "state": "preparing", "started_at": now,
                   "retry_evidence": retry_evidence}
        if row:
            if len(row.attempts) >= 100:
                raise asset_store.AssetError("asset_preparation_attempt_limit")
            row.task_id, row.recipe_ref, row.state, row.updated_at = task_id, recipe, "preparing", now
            row.asset_ref, row.evidence = {}, {}
            row.attempts = [*row.attempts, attempt]
        else:
            row = AssetPreparation(project_id=project_id, preparation_id=ident, task_id=task_id,
                owner_user_id=sess["user_id"], demand=demand, recipe_ref=recipe, state="preparing",
                asset_ref={}, evidence={}, updated_at=now, attempts=[attempt])
            db.add(row)
        db.flush()
        return {**_view(row), "action": "prepare", "claimed": True}


def finish(project_id, sess, preparation_id, value):
    require_project_access(project_id, sess)
    state = value.get("state")
    if state not in {"ready", "failed"}:
        raise asset_store.AssetError("asset_preparation_state_invalid", 422)
    evidence = asset_store._metadata(value.get("evidence", {}))
    if not evidence:
        raise asset_store.AssetError("asset_preparation_evidence_required", 422)
    reference = asset_store._metadata(value.get("asset_ref", {}))
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(AssetPreparation, (project_id, preparation_id))
        if not row or row.owner_user_id != sess["user_id"]:
            raise asset_store.AssetError("asset_preparation_not_found", 404)
        if row.state != "preparing":
            if row.state == state and row.asset_ref == reference and row.evidence == evidence:
                return _view(row)
            raise asset_store.AssetError("asset_preparation_terminal_immutable")
        if state == "ready":
            asset, blob, _ = asset_store._rows(db, project_id, reference.get("asset_id"), reference.get("version", 1))
            if blob.state != "ready" or asset.kind != row.demand["kind"]:
                raise asset_store.AssetError("asset_preparation_result_not_ready")
            attrs = row.demand.get("attributes", {})
            if not isinstance(attrs, dict) or any(asset.attributes.get(k) != v for k, v in attrs.items()):
                raise asset_store.AssetError("asset_preparation_result_attributes_mismatch")
        row.state, row.asset_ref, row.evidence, row.updated_at = state, reference, evidence, int(time.time())
        row.attempts = [*row.attempts[:-1], {**row.attempts[-1], "state": state, "evidence": evidence,
                         "asset_ref": reference, "finished_at": row.updated_at}]
        return _view(row)


def list_preparations(project_id, sess, limit=50):
    require_project_access(project_id, sess)
    with session_scope() as db:
        return {"items": [_view(r) for r in db.query(AssetPreparation).filter_by(project_id=project_id).order_by(AssetPreparation.updated_at.desc()).limit(min(100, max(1, limit)))]}
