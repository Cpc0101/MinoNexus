"""Authenticated Agent activity, independent of workflow and Scout verdicts."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from mino_nexus.api.deps import current_session
from mino_nexus.core.http_util import ok
from mino_nexus.assistant import agent_tasks

router = APIRouter(prefix="/agent-tasks", tags=["Agent tasks"])


class AgentTaskBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str | None = None
    scope: str | None = None
    environment: str | None = None
    phase: str | None = None
    next_action: str | None = None
    startup_delivered: bool | None = None
    project_id: str | None = None
    event_id: str | None = None
    status: str | None = None
    summary: str | None = None
    evidence: list[str] | None = None
    question_id: str | None = None
    question: str | None = None
    missing: str | None = None
    answer: str | None = None
    kind: str | None = None
    reference: str | None = None


def _owner(session):
    uid = str(session.get("user_id") or "")
    if not uid:
        raise HTTPException(401, "请先登录")
    return uid


def _call(fn, *args):
    try:
        return ok(fn(*args))
    except (ValueError, KeyError, PermissionError) as exc:
        code = 404 if "not found" in str(exc).lower() else 403 if isinstance(exc, PermissionError) else 409
        raise HTTPException(code, str(exc)) from exc


@router.get("")
def list_agent_tasks(project_id: str = "", limit: int = Query(50, ge=1, le=100),
                     offset: int = Query(0, ge=0), view: str = "tasks", sess: dict = Depends(current_session)):
    return _call(agent_tasks.listing, _owner(sess), project_id, limit, offset, view)


@router.get("/{task_id}")
def get_agent_task(task_id: str, sess: dict = Depends(current_session)):
    return _call(agent_tasks.get, _owner(sess), task_id)


@router.get("/{task_id}/events")
def get_agent_events(task_id: str, before_seq: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200),
                     after_seq: int | None = Query(None, ge=0), sess: dict = Depends(current_session)):
    return _call(agent_tasks.events, _owner(sess), task_id, before_seq, limit, after_seq)


@router.post("/{task_id}/{operation}")
def update_agent_task(task_id: str, operation: str, body: AgentTaskBody, sess: dict = Depends(current_session)):
    args = body.model_dump(exclude_unset=True)
    uid = _owner(sess)
    if operation == "start":
        return _call(agent_tasks.create, uid, task_id, args.get("title"), args.get("project_id", ""), "external_agent", args.get("scope", ""), args.get("environment", ""))
    if operation == "link":
        return _call(agent_tasks.link, uid, task_id, args.get("kind"), args.get("reference"))
    if operation not in {"event", "heartbeat", "question", "answer", "acknowledge"}:
        raise HTTPException(404, "Unknown agent operation")
    state = _call(agent_tasks.get, uid, task_id)["data"]
    if state["source"] == "nexus_agent":
        raise HTTPException(409, "Nexus Agent state is captured by its runtime; answer through the original request")
    return _call(agent_tasks.mutate, uid, task_id, operation, args)
