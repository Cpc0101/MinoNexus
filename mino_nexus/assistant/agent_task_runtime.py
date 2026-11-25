"""Capture real Nexus entry points; external local work requires explicit events."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from threading import Event, Thread
from time import monotonic
from uuid import uuid4
from mino_nexus.assistant import agent_tasks as tasks

current = ContextVar("agent_task", default=None)


@contextmanager
def bound(owner, task_id, heartbeat=False):
    tasks.get(owner, task_id)
    token = current.set((owner, task_id))
    stop = Event()
    worker = None
    if heartbeat:
        def pulse():
            while not stop.wait(20):
                try:
                    tasks.mutate(owner, task_id, "heartbeat", {})
                except Exception:
                    # Failure leaves the previous heartbeat visible, never a false renewal.
                    return
        worker = Thread(target=pulse, name="agent-task-heartbeat", daemon=True)
        worker.start()
    try:
        yield
    finally:
        stop.set()
        if worker:
            worker.join(timeout=1)
        current.reset(token)


def _event(owner, task_id, summary, status):
    return tasks.mutate(owner, task_id, "event", {"event_id": uuid4().hex, "summary": summary, "status": status})


def observe_links(owner, task_id, args, outcome):
    if not outcome.ok:
        return
    data = outcome.data if isinstance(outcome.data, dict) else {}
    if isinstance(data.get("data"), dict):
        data = data["data"]
    project_id = args.get("project_id")
    if args.get("app_id"):
        try:
            from mino_nexus.resources.project_store import require_app
            project_id = require_app(str(args["app_id"])).get("project_id")
        except (KeyError, ValueError, PermissionError):
            pass
    if project_id:
        try:
            tasks.assign_project(owner, task_id, str(project_id))
        except (KeyError, ValueError, PermissionError):
            pass
    execution_id = data.get("task_id") or data.get("run_id")
    if isinstance(outcome.data, str) and outcome.tool_id == "case.run":
        execution_id = outcome.data
    refs = [("workflow", args.get("workflow_id") or data.get("workflow_id")), ("execution", execution_id),
            ("debug", data.get("session_id"))]
    for kind, reference in refs:
        if reference:
            try:
                tasks.link(owner, task_id, kind, str(reference))
            except Exception:
                # A tool receipt is not proof that every returned reference is an owned object.
                continue


def tracked_tool(call, spec, args, *, owner):
    binding = current.get()
    if not binding or str(spec.get("id", "")).startswith("agent.task."):
        return call()
    if binding[0] != owner:
        raise PermissionError("Agent execution identity mismatch")
    task_id = binding[1]
    call_id, started = uuid4().hex, monotonic()
    name = str(spec.get("id") or "unknown")
    tasks.tool_begin(owner, task_id, call_id, name, str(spec.get("title") or name))
    try:
        outcome = call()
    except BaseException:
        tasks.tool_end(owner, task_id, call_id, name, False, round((monotonic() - started) * 1000))
        raise
    data = outcome.data if isinstance(outcome.data, dict) else {}
    tasks.tool_end(owner, task_id, call_id, name, bool(outcome.ok), round((monotonic() - started) * 1000),
                   int(outcome.status or 0), str(data.get("reason_code") or data.get("code") or ""))
    observe_links(owner, task_id, outcome.args or args, outcome)
    if outcome.ask and tasks.get(owner, task_id)["source"] != "nexus_agent":
        tasks.mutate(owner, task_id, "question", {"event_id": call_id + ":question", "question_id": call_id,
                     "question": str(outcome.ask)[:2000], "missing": str(outcome.field or "")[:2000]})
    return outcome


def _end_standalone(uid, task_id, ok, summary):
    """A standalone MCP task must never stay active after its call returned or raised."""
    try:
        state = tasks.get(uid, task_id)
        if state["reported_status"] in {"running", "waiting_tool"} and not state["active_calls"]:
            _event(uid, task_id, summary, "completed" if ok else "failed")
    except (ValueError, KeyError, PermissionError):
        pass


def tracked_mcp(fn):
    @wraps(fn)
    def wrapper(uid, name, args):
        if name.startswith("agent_task_") or uid == "system:probe":
            return fn(uid, name, args)
        args = dict(args) if isinstance(args, dict) else {}
        from mino_nexus.contract.agent_task_policy import requires_task
        import json
        guarded = requires_task(name)
        supplied = args.pop("agent_task_id", "")
        def rejected(code, message, state=None):
            payload = {"reason_code": code, "message": message,
                       "required_action": "agent_task_start → 可见输出 startup_message → agent_task_event(startup_delivered=true, summary 含 board_url) → 原 task_id 继续调用"}
            if state:
                payload.update({key: state[key] for key in ("task_id", "board_url", "startup_message", "requires_visible_startup")})
            return {"isError": True, "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}
        if guarded and not supplied:
            return rejected("agent_task_binding_required", "测试及工作流动作必须绑定业务任务；不得创建独立调用代替任务看板")
        if supplied:
            try:
                state = tasks.get(uid, str(supplied))
            except (ValueError, KeyError, PermissionError):
                return rejected("agent_task_binding_invalid", "任务不存在或不属于当前账号，请提供原业务任务 ID")
            if state["reported_status"] in tasks.TERMINAL or state["source"] == "tool_call":
                return rejected("agent_task_binding_invalid", "不能绑定已结束任务或独立调用", state)
            if guarded and state["source"] == "external_agent" and state["requires_visible_startup"]:
                return rejected("agent_task_startup_required", "先在对话发送以下看板链接，再上报发送声明；声明不代表用户已读", state)
            if name in {"debug_case_execute", "case_run"} and state["source"] == "external_agent":
                flows = state["presentation"].get("workflows", [])
                if not flows or flows[0]["source_status"] == "drafting":
                    return rejected("case_source_not_locked", "人工用例未锁定：先补写并发布源用例，再机器回读锁定；不能绕过后直接测试", state)
        if supplied:
            task_id = str(supplied)
            standalone = False
        else:
            task_id = "MCP-" + uuid4().hex
            tasks.create(uid, task_id, "MCP: " + name[:450], source="tool_call")
            standalone = True
        try:
            with bound(uid, task_id, heartbeat=True):
                result = fn(uid, name, args)
        except (ValueError, PermissionError) as exc:
            if standalone:
                _end_standalone(uid, task_id, False, "MCP 调用被拒绝")
            return {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
        except Exception:
            if standalone:
                _end_standalone(uid, task_id, False, "MCP 调用异常结束")
            raise
        if standalone:
            _end_standalone(uid, task_id, not result.get("isError"), "MCP 调用已返回")
        import json
        for content in result.get("content", []):
            if content.get("type") == "text":
                try:
                    payload = json.loads(content["text"])
                except (TypeError, ValueError):
                    continue
                if isinstance(payload, dict):
                    state = tasks.get(uid, task_id)
                    content["text"] = json.dumps({**payload, "agent_task_id": task_id,
                        **{key: state[key] for key in ("board_url", "startup_message", "requires_visible_startup")}}, ensure_ascii=False)
                    break
        return result
    return wrapper


def _finish_result(owner, task_id, result):
    request = ((result.get("card") or {}).get("request") or {})
    if request:
        tasks.bind_request(owner, task_id, request)
    if request.get("state") == "cancelled":
        _event(owner, task_id, "原请求已取消", "cancelled")
    elif request.get("question", {}).get("field"):
        tasks.mutate(owner, task_id, "question", {"event_id": "request:" + request["id"] + ":" + str(request["revision"]),
                     "question_id": "request:" + request["id"] + ":" + str(request["revision"]),
                     "question": str(result.get("answer") or "Please complete the pending request")[:2000], "missing": request["question"]["field"]})
    else:
        failed = bool(result.get("model_error") or result.get("runtime_stop") or (result.get("card") or {}).get("status") in {"blocked", "uncertain"})
        _event(owner, task_id, "Agent 本轮处理已结束", "failed" if failed else "completed")
    if result.get("task_id"):
        try:
            tasks.link(owner, task_id, "execution", result["task_id"])
        except (ValueError, PermissionError):
            pass
    state = tasks.get(owner, task_id)
    result["agent_board_url"] = state["board_url"]
    if state["board_url"]:
        result["answer"] = str(result.get("answer") or "") + "\n\n实时任务看板：" + state["board_url"]
    result["agent_task_id"] = task_id
    return result


def tracked_agent(fn):
    @wraps(fn)
    def wrapper(**kwargs):
        if current.get():
            return fn(**kwargs)
        owner = kwargs["user_id"]
        from mino_nexus.assistant import assistant_conversation as conversation
        origin = conversation.origin_for(kwargs.get("channel", ""), kwargs.get("chat_id", ""),
                                         kwargs.get("node_id", ""), kwargs.get("reply_ctx") or {})
        pending = conversation.pending_for(origin, reply_to=str((kwargs.get("inbound") or {}).get("reply_to") or ""))
        task_id = tasks.for_request(owner, pending["id"]) if pending else ""
        existing = bool(task_id)
        task_id = task_id or "AG-" + uuid4().hex
        if not existing:
            tasks.create(owner, task_id, str(kwargs.get("text") or "Agent request")[:500], source="nexus_agent")
        with bound(owner, task_id, heartbeat=True):
            try:
                return _finish_result(owner, task_id, fn(**kwargs))
            except BaseException:
                _event(owner, task_id, "Agent 本轮处理中断", "failed")
                raise
    return wrapper


def tracked_resume(fn):
    @wraps(fn)
    def wrapper(request_id, revision, patch, **kwargs):
        from mino_nexus.platform.identity import require_user_id
        owner = require_user_id()
        task_id = tasks.for_request(owner, request_id)
        if not task_id:
            return fn(request_id, revision, patch, **kwargs)
        # Original request processing validates revision before any answer is acknowledged.
        nested = current.get() == (owner, task_id)
        with bound(owner, task_id, heartbeat=True):
            prior_questions = {q["question_id"] for q in tasks.get(owner, task_id)["questions"] if q["status"] == "open"}
            result = fn(request_id, revision, patch, **kwargs)
            state = tasks.get(owner, task_id)
            for question in state["questions"]:
                if question["status"] == "open" and question["question_id"] in prior_questions:
                    tasks.mutate(owner, task_id, "answer", {"event_id": uuid4().hex,
                                 "question_id": question["question_id"], "answer": "答案已通过原请求提交"})
                    tasks.mutate(owner, task_id, "acknowledge", {"event_id": uuid4().hex,
                                 "question_id": question["question_id"]})
            return result if nested else _finish_result(owner, task_id, result)
    return wrapper
