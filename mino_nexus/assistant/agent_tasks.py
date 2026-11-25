"""Observe execution, not model thoughts or Scout test verdicts."""
from copy import deepcopy
from datetime import datetime, timezone
from sqlalchemy import text as sql
from mino_nexus.contract.agent_workflow import text, facts
from mino_nexus.core.database import session_scope
from mino_nexus.models.agent_task import AgentTask, AgentTaskEvent
from mino_nexus.models.agent_workflow import AgentWorkflow

ACTIVE = {"running", "waiting_tool"}
TERMINAL = {"completed", "failed", "cancelled"}
STATUSES = ACTIVE | TERMINAL | {"waiting_user", "paused"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def view(row):
    state = deepcopy(row.state)
    from mino_nexus.assistant.assistant_reports import configuration, agent_board_url
    config = configuration(row.owner_user_id)
    board_url = agent_board_url(row.task_id, base_url=config['effective_url'])
    seen = datetime.fromisoformat(state["last_heartbeat_at"].replace("Z", "+00:00"))
    age = max(0, (datetime.now(timezone.utc) - seen).total_seconds())
    status = "unknown" if state["status"] in ACTIVE and age > 60 else state["status"]
    if status == "waiting_user" and state["questions"] and not any(q["status"] == "open" for q in state["questions"]):
        status = "waiting_agent"
    return {**state, "task_id": row.task_id, "project_id": row.project_id, "revision": row.revision,
            "board_url": board_url, "board_url_source": config['source'],
            "startup_message": f"已开始：{state['title']}\n实时任务看板：{board_url or '入口未配置'}\n当前阶段：{state['current_action'] or '准备'}\n任务 ID：{row.task_id}",
            "requires_visible_startup": state['source'] != 'tool_call',
            "updated_at": row.updated_at, "reported_status": state["status"],
            "status": status,
            "heartbeat_age_sec": round(age), "observation_scope": "nexus_execution_and_explicit_client_events"}


def _row(db, owner, task_id):
    row = db.get(AgentTask, (owner, task_id))
    if row is None:
        raise ValueError("Agent task not found")
    return row


def _append(db, row, event_id, payload, timestamp):
    previous = db.query(AgentTaskEvent).filter_by(owner_user_id=row.owner_user_id,
                task_id=row.task_id, event_id=event_id).first()
    if previous:
        if previous.payload != payload:
            raise ValueError("Agent event ID already used for different content")
        return False
    db.add(AgentTaskEvent(owner_user_id=row.owner_user_id, task_id=row.task_id, seq=row.next_seq,
                         event_id=event_id, payload=deepcopy(payload), created_at=timestamp))
    row.next_seq += 1
    row.revision += 1
    row.updated_at = timestamp
    return True


def create(owner, task_id, title, project_id="", source="external_agent"):
    owner, task_id, title = text(owner, "owner", 128), text(task_id, "task_id", 128), text(title, "title", 500)
    if "/" in task_id or "\\" in task_id:
        raise ValueError("Agent task ID cannot contain path separators")
    project_id = text(project_id, "project_id", 128, required=False)
    if source not in {"external_agent", "nexus_agent", "tool_call"}:
        raise ValueError("Invalid agent source")
    if project_id:
        from mino_nexus.resources.project_store import require_project
        from mino_nexus.platform.identity import execution_user
        with execution_user(owner):
            require_project(project_id)
    timestamp = now()
    with session_scope() as db:
        db.execute(sql("BEGIN IMMEDIATE"))
        row = db.get(AgentTask, (owner, task_id))
        if row:
            if row.state["title"] != title or row.project_id != project_id or row.state["source"] != source:
                raise ValueError("Agent task identity differs")
        else:
            row = AgentTask(owner_user_id=owner, task_id=task_id, project_id=project_id,
                            state={"title": title, "source": source, "status": "running", "current_action": "",
                                   "started_at": timestamp, "finished_at": "", "last_heartbeat_at": timestamp,
                                   "active_calls": {}, "questions": [], "links": [], "request_id": ""},
                            updated_at=timestamp, revision=0, next_seq=1)
            db.add(row)
            _append(db, row, "start", {"kind": "task_started", "summary": title}, timestamp)
        return view(row)


def get(owner, task_id):
    with session_scope() as db:
        return view(_row(db, owner, task_id))


def listing(owner, project_id="", limit=50, offset=0):
    with session_scope() as db:
        query = db.query(AgentTask).filter(AgentTask.owner_user_id == owner)
        if project_id:
            query = query.filter(AgentTask.project_id == project_id)
        total = query.count()
        rows = query.order_by(AgentTask.updated_at.desc(), AgentTask.task_id).offset(offset).limit(limit).all()
        items = []
        for row in rows:
            item = view(row)
            item["pending_questions"] = sum(q["status"] == "open" for q in item.pop("questions"))
            item.pop("active_calls")
            items.append(item)
        return {"items": items, "total": total}


def events(owner, task_id, before_seq=0, limit=100, after_seq=None):
    if before_seq and after_seq is not None:
        raise ValueError("Use before_seq or after_seq, not both")
    with session_scope() as db:
        row = _row(db, owner, task_id)
        query = db.query(AgentTaskEvent).filter_by(owner_user_id=owner, task_id=task_id)
        if before_seq:
            query = query.filter(AgentTaskEvent.seq < before_seq)
        if after_seq is not None:
            query = query.filter(AgentTaskEvent.seq > after_seq)
        rows = query.order_by(AgentTaskEvent.seq.asc() if after_seq is not None else AgentTaskEvent.seq.desc()).limit(limit + 1).all()
        return {"items": [{"seq": e.seq, "event_id": e.event_id, "created_at": e.created_at,
                           **deepcopy(e.payload)} for e in rows[:limit]],
                "has_more": len(rows) > limit, "high_watermark": row.next_seq - 1}


def mutate(owner, task_id, operation, args):
    timestamp = now()
    with session_scope() as db:
        db.execute(sql("BEGIN IMMEDIATE"))
        row = _row(db, owner, task_id)
        state = deepcopy(row.state)
        if operation == "heartbeat":
            if state["status"] not in TERMINAL:
                state["last_heartbeat_at"] = timestamp
                row.state, row.updated_at, row.revision = state, timestamp, row.revision + 1
            return view(row)
        event_id = text(args.get("event_id"), "event_id", 128)
        if operation == "event":
            status = args.get("status", "running")
            if status not in STATUSES:
                raise ValueError("Invalid agent task status")
            if state["active_calls"] and status in TERMINAL:
                raise ValueError("Reconcile active tool calls before ending agent task")
            payload = {"kind": "agent_activity", "summary": text(args.get("summary"), "summary"),
                       "status": status, "evidence": facts(args.get("evidence", []))}
            if state["status"] in TERMINAL:
                previous = db.query(AgentTaskEvent).filter_by(owner_user_id=owner, task_id=task_id, event_id=event_id).first()
                if previous and previous.payload == payload:
                    return view(row)
                raise ValueError("Ended agent task is immutable; start a new task")
            if status in TERMINAL and state["source"] == "external_agent":
                for reference in state["links"]:
                    if reference["kind"] != "workflow":
                        continue
                    workflow = db.get(AgentWorkflow, (owner, reference["id"]))
                    if workflow is None or workflow.state.get("delivery_status") not in {"delivered", "delivered_with_gaps"} or not any(r.get("final") for r in workflow.state.get("reports", [])):
                        raise ValueError("Final workflow report required before ending Agent task: " + reference["id"])
            if _append(db, row, event_id, payload, timestamp):
                state["status"], state["current_action"] = status, payload["summary"]
                state["last_heartbeat_at"] = timestamp
                if status in TERMINAL:
                    state["finished_at"] = timestamp
        elif operation == "question":
            if state["status"] in TERMINAL:
                raise ValueError("Ended agent task cannot ask a question")
            question_id = text(args.get("question_id"), "question_id", 128)
            payload = {"kind": "question", "question_id": question_id,
                       "summary": text(args.get("question"), "question"),
                       "missing": text(args.get("missing", ""), "missing", required=False),
                       "evidence": facts(args.get("evidence", []))}
            existing = next((q for q in state["questions"] if q["question_id"] == question_id), None)
            if existing and any(existing.get(k) != v for k, v in payload.items()):
                raise ValueError("Question is immutable")
            if not existing and len(state["questions"]) >= 1000:
                raise ValueError("Agent question limit reached")
            if not existing and _append(db, row, event_id, payload, timestamp):
                state["questions"].append({**payload, "status": "open", "answer": "", "answered_at": "", "acknowledged_at": ""})
                state["status"] = "waiting_user"
        elif operation in {"answer", "acknowledge"}:
            question = next((q for q in state["questions"] if q["question_id"] == args.get("question_id")), None)
            if not question:
                raise ValueError("Agent question not found")
            answer = text(args.get("answer"), "answer") if operation == "answer" else ""
            if operation == "answer" and question["answer"] and question["answer"] != answer:
                raise ValueError("Answer is immutable")
            if operation == "acknowledge" and not question["answer"]:
                raise ValueError("Question must be answered before acknowledgement")
            payload = {"kind": operation, "question_id": question["question_id"], "summary": answer if answer else "Answer read by agent"}
            if state["status"] in TERMINAL:
                previous = db.query(AgentTaskEvent).filter_by(owner_user_id=owner, task_id=task_id, event_id=event_id).first()
                if previous and previous.payload == payload:
                    return view(row)
                raise ValueError("Ended agent task is immutable; start a new task")
            if _append(db, row, event_id, payload, timestamp):
                if operation == "answer":
                    question.update(answer=answer, status="answered", answered_at=timestamp)
                else:
                    question.update(status="acknowledged", acknowledged_at=timestamp)
                    if not any(q["status"] != "acknowledged" for q in state["questions"]):
                        state["status"] = "running"
                    state["last_heartbeat_at"] = timestamp
        else:
            raise ValueError("Invalid agent task operation")
        row.state = state
        return view(row)


def link(owner, task_id, kind, reference):
    from mino_nexus.platform.identity import execution_user
    reference = text(reference, "reference", 128)
    with execution_user(owner):
        if kind == "workflow":
            from mino_nexus.assistant.agent_workflow import operate
            obj = operate(owner, reference, "get", {})
        elif kind == "execution":
            from mino_nexus.casework import run_store
            obj = run_store.get(reference)
        elif kind == "debug":
            from mino_nexus.resources.scout_record_store import get as record_get
            obj = record_get(reference)
            if obj:
                obj = {**obj, "project_id": (obj.get("payload") or {}).get("project_id", "")}
        else:
            raise ValueError("Invalid agent link kind")
    if obj is None:
        raise ValueError("Linked object not found")
    project = str(obj.get("project_id") or "")
    with session_scope() as db:
        db.execute(sql("BEGIN IMMEDIATE"))
        row = _row(db, owner, task_id)
        if row.project_id and project and row.project_id != project:
            raise ValueError("Agent link project mismatch")
        state = deepcopy(row.state)
        item = {"kind": kind, "id": reference, "project_id": project, "app_id": str(obj.get("app_id") or "")}
        if kind == "debug":
            item["task_id"] = str(obj.get("task_id") or "")
        if item not in state["links"] and len(state["links"]) >= 1000:
            raise ValueError("Agent link limit reached")
        if item not in state["links"]:
            _append(db, row, "link:" + kind + ":" + reference, {"kind": "link", "summary": reference, "reference": item}, now())
            state["links"].append(item)
            row.state = state
            row.project_id = row.project_id or project
        return view(row)


def tool_begin(owner, task_id, call_id, name, label):
    timestamp = now()
    with session_scope() as db:
        db.execute(sql("BEGIN IMMEDIATE"))
        row = _row(db, owner, task_id)
        if row.state["status"] in TERMINAL:
            raise ValueError("Ended agent task cannot start a tool")
        state = deepcopy(row.state)
        if len(state["active_calls"]) >= 64:
            raise ValueError("Agent active call limit reached")
        _append(db, row, call_id + ":start", {"kind": "tool_started", "tool": name, "call_id": call_id, "summary": label}, timestamp)
        state["active_calls"][call_id] = {"tool": name, "started_at": timestamp}
        state.update(status="waiting_tool", current_action=label, last_heartbeat_at=timestamp)
        row.state = state


def tool_end(owner, task_id, call_id, name, ok, elapsed_ms, status_code=0, reason_code=""):
    timestamp = now()
    with session_scope() as db:
        db.execute(sql("BEGIN IMMEDIATE"))
        row = _row(db, owner, task_id)
        if row.state["status"] in TERMINAL:
            raise ValueError("Ended agent task cannot receive a new tool result")
        state = deepcopy(row.state)
        _append(db, row, call_id + ":end", {"kind": "tool_finished", "tool": name, "call_id": call_id,
                "ok": ok, "elapsed_ms": elapsed_ms, "status_code": status_code, "reason_code": str(reason_code)[:128], "summary": "工具返回成功" if ok else "工具返回失败"}, timestamp)
        state["active_calls"].pop(call_id, None)
        state.update(status="waiting_tool" if state["active_calls"] else "running", last_heartbeat_at=timestamp)
        row.state = state


def bind_request(owner, task_id, request):
    with session_scope() as db:
        db.execute(sql("BEGIN IMMEDIATE"))
        row = _row(db, owner, task_id)
        state = deepcopy(row.state)
        state["request_id"] = request["id"]
        state["native_request"] = deepcopy(request)
        row.state = state
        row.revision += 1
        row.updated_at = now()


def for_request(owner, request_id):
    with session_scope() as db:
        row = db.query(AgentTask).filter(AgentTask.owner_user_id == owner,
                AgentTask.state["request_id"].as_string() == request_id).order_by(AgentTask.updated_at.desc()).first()
        return row.task_id if row else ""


def assign_project(owner, task_id, project_id):
    from mino_nexus.platform.identity import execution_user
    from mino_nexus.resources.project_store import require_project
    with execution_user(owner):
        require_project(project_id)
    with session_scope() as db:
        db.execute(sql("BEGIN IMMEDIATE"))
        row = _row(db, owner, task_id)
        if row.project_id and row.project_id != project_id:
            raise ValueError("Agent task project mismatch")
        if not row.project_id:
            row.project_id = project_id
            _append(db, row, "project:" + project_id, {"kind": "project", "summary": project_id}, now())
