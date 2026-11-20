"""Scout 执行脚本时，账号、步骤、失败和证据回调本仓。验证码和模型在 Scout 本机完成。"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from mino_nexus.core import protocol as P
from mino_nexus.core.log import SLog

TAG = "ScriptCallback"
# 脚本回调不再是一张固定白名单：认不认识由 `_call()` 的分支决定，
# 不认识的 op 直接回 FAIL 并带上名字，不再静默吞掉。
STEP_CAP = "script.step"
SESSION_CAP = "script.session"
LEASE_CAP = "script.lease"
_EVIDENCE_CAPS = frozenset({STEP_CAP, SESSION_CAP})
_ALIASES = {
    "account": "case.account",
    "note": "case.note",
    "step": "case.step",
    "fail": "case.fail",
}
HANDLED = frozenset({
    "case.account", "case.note", "case.step", "case.fail",
    "account", "note", "step", "fail", STEP_CAP, SESSION_CAP, LEASE_CAP, "record.sync",
})

_GUARD = threading.Lock()
_ADMISSIONS: dict[str, "ScriptAdmission"] = {}


@dataclass
class ScriptAdmission:
    run_id: str
    session_id: str
    case_id: str
    sn: str
    node_id: str
    project_id: str
    app_id: str
    provider_id: str
    owner_user_id: str
    platform: str
    case: dict[str, Any]
    ctx: Any
    writer: Any
    steps: list[dict[str, Any]] = field(default_factory=list)
    step_seq: int = 0
    step_open: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


def register(admission: ScriptAdmission) -> None:
    with _GUARD:
        _ADMISSIONS[admission.session_id] = admission


def unregister(run_id: str, session_id: str = "") -> None:
    with _GUARD:
        for key, value in list(_ADMISSIONS.items()):
            if value.run_id == run_id and (not session_id or key == session_id):
                _ADMISSIONS.pop(key, None)


def handle(node_id: str, req: P.Execute) -> P.Result:
    cap = str(req.capability_id or "")
    params = dict(req.params or {})
    if not str(params.get("run_id") or "").strip() and req.run_id:
        params["run_id"] = str(req.run_id)
    if cap == "script.callback":
        cap = str(params.get("op") or "")
    cap = _ALIASES.get(cap, cap)
    try:
        if cap == "record.sync":
            from mino_nexus.loop.scout_record_tasks import receive
            row = receive(node_id, params)
            return P.Result(run_id=req.run_id, step_idx=req.step_idx, status=P.EventStatus.PASS,
                            summary="summary persisted", executor_used="nexus",
                            data={"attempt_id": row["attempt_id"], "ack_version": row["summary_version"]})
        admission = _require(node_id, params)
        with admission.lock:
            data = _call(admission, cap, params)
        summary = str(data.pop("_summary", "") or "ok")[:300]
        return P.Result(
            run_id=str(req.run_id or admission.run_id),
            step_idx=int(req.step_idx),
            status=P.EventStatus.PASS,
            summary=summary,
            executor_used="nexus",
            data=data,
        )
    except Exception as exc:
        code = str(getattr(exc, "code", "") or getattr(exc, "reason_code", "") or "")
        message = str(exc)[:300] or code or "callback_rejected"
        SLog.w(TAG, f"callback {cap} run={params.get('run_id')} rejected: {code or type(exc).__name__}")
        return P.Result(
            run_id=str(req.run_id or params.get("run_id") or ""),
            step_idx=int(req.step_idx),
            status=P.EventStatus.FAIL,
            summary=message,
            error=code or type(exc).__name__,
            executor_used="nexus",
        )


def _require(node_id: str, params: dict[str, Any]) -> ScriptAdmission:
    run_id = str(params.get("run_id") or "").strip()
    session_id = str(params.get("session_id") or "").strip()
    with _GUARD:
        admission = _ADMISSIONS.get(session_id)
    if admission is None and session_id:
        from mino_nexus.loop.scout_record_tasks import restore_admission
        admission = restore_admission(node_id, session_id)
    if admission is None or not run_id or not session_id:
        raise PermissionError("这次运行没有准入")
    if run_id != admission.run_id or session_id != admission.session_id:
        raise PermissionError("session_id 与这次运行不一致")
    if str(node_id or "") != admission.node_id:
        raise PermissionError("调用方不是这台设备所在节点")
    from mino_nexus.casework import run_store
    from mino_nexus.resources.node_registry import get_registry
    from mino_nexus.resources.scout_record_store import for_node, TERMINAL
    record = for_node(session_id, node_id)
    if record and record["status"] in TERMINAL:
        raise PermissionError("这次执行已经结束")

    if not run_store.task_is_live(run_store.get(run_id)):
        raise PermissionError("这次运行已经结束")
    node, reason = get_registry().resolve(admission.sn)
    if node is None or str(node.node_id) != str(node_id or "") or node.owner_user_id != admission.owner_user_id:
        raise PermissionError(reason or "设备所在节点已变化")
    return admission


def _call(admission: ScriptAdmission, cap: str, params: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.contract.ownership import execution_user

    owner = admission.owner_user_id
    if cap in _EVIDENCE_CAPS:
        _write_step(admission, params)
        return {"_summary": "已写入动作"}
    if cap == LEASE_CAP:
        return _lease(admission, params)
    if not owner:
        raise PermissionError("这次运行没有执行账号")
    with execution_user(owner):
        if cap == "case.account":
            return _account(admission, params)
        if cap == "case.note":
            return _note(admission, params)
        if cap == "case.step":
            return _step(admission, params)
        if cap == "case.fail":
            return _fail(admission, params)
    raise PermissionError("Nexus 不处理这个回调")


def _account(admission: ScriptAdmission, params: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.resources import scout_record_store
    from mino_nexus.resources.resource_claim_tx import _claim
    record = scout_record_store.for_node(admission.session_id, admission.node_id)
    request = (record or {}).get("context", {}).get("resource_request")
    if not request:
        return _account_impl(admission, params)
    token = _claim.set(request)
    try:
        result = _account_impl(admission, params)
        scout_record_store.save_resources(admission.session_id, request)
        return result
    finally:
        _claim.reset(token)


def _account_impl(admission: ScriptAdmission, params: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.loop.script_runtime import ScriptAbort
    from mino_nexus.resources.accounts.lease import (
        account_login_policy,
        lease_scoped_script_account,
        validate_account_slot,
    )
    from mino_nexus.resources.account_data import validate_data_need, record_request
    from mino_nexus.resources.account_data_match import validate_data_requirements, rank_account_data
    from mino_nexus.resources.otp_resolve import resolve_otp

    data_need = validate_data_need(params.get("data_need", ""))
    data_requirements = validate_data_requirements(params.get("data_requirements"))
    try:
        wanted_slot = validate_account_slot(params.get("slot"))
    except ValueError as exc:
        raise ScriptAbort("account_slot_invalid", reason_code="account_slot_invalid") from exc

    explicit = params.get("requirements")
    prefer = params.get("prefer")
    if explicit is not None and (not isinstance(explicit, dict) or any(
            not isinstance(explicit.get(bucket, []), list) for bucket in ("all", "prefer"))):
        raise ValueError("account_requirements_invalid")
    if explicit is not None and set(explicit) - {"all", "prefer"}:
        raise ValueError("account_requirements_invalid")
    if prefer is not None and not isinstance(prefer, list):
        raise ValueError("account_requirements_invalid")

    selected_case = dict(admission.case)
    selected_case["data_requirements"] = data_requirements
    if explicit is not None:
        selected_case["account_requirements"] = explicit
    if prefer:
        filters = dict(selected_case.get("account_requirements") or {})
        if not isinstance(filters.get("prefer", []), list):
            raise ValueError("account_requirements_invalid")
        filters["prefer"] = [*(filters.get("prefer") or []), *prefer]
        selected_case["account_requirements"] = filters

    row, scoped_ctx, err = lease_scoped_script_account(
        admission.ctx,
        selected_case,
        slot=wanted_slot,
        app_id=params.get("app_id"),
        platform=params.get("platform"),
    )
    row = dict(row or {})
    if not str(row.get("id") or row.get("account_id") or "").strip():
        raise ScriptAbort(err or "这条用例没有租到账号", reason_code=err or "resource_account_missing")

    account_app = str(row.get("app_id") or getattr(scoped_ctx, "app_id", "") or admission.app_id).strip()
    project_id = str(getattr(scoped_ctx, "project_id", "") or admission.project_id)
    ranked = rank_account_data(project_id, [row], data_requirements, app_id=account_app)
    if not ranked:
        raise ScriptAbort("租到的账号不属于当前应用", reason_code="account_scope_mismatch")
    row = ranked[0]
    record_request(project_id, str(row.get("id") or row.get("account_id") or ""),
                   run_id=admission.run_id, session_id=admission.session_id, app_id=account_app,
                   case_id=admission.case_id, data_need=data_need)
    otp = str(row.get("otp") or row.get("sms_code") or "").strip()
    if not otp:
        # 账号行没有码时，按该 slot 的 app/env/surface 策略给固定码。
        otp = str(resolve_otp(scoped_ctx, row)[0] or "").strip()
    policy = account_login_policy(scoped_ctx)
    return {
        "_summary": f"已返回 slot={wanted_slot} 的账号",
        "project_id": project_id,
        "slot": wanted_slot,
        "app_id": account_app,
        "platform": str(row.get("platform") or getattr(scoped_ctx, "platform", "") or ""),
        "target_package": str(row.get("target_package") or getattr(scoped_ctx, "target_package", "") or ""),
        "data_matches": row.get("data_matches", []),
        "data_match_count": row.get("data_match_count", 0),
        "id": str(row.get("id") or row.get("account_id") or ""),
        "email": str(row.get("email") or ""),
        "phone": str(row.get("phone") or ""),
        "username": str(row.get("username") or ""),
        "password": str(row.get("password") or ""),
        "otp": otp,
        "ident": str(row.get("ident") or ""),
        "login_policy": policy,
    }


def _login_policy(admission: ScriptAdmission, ctx: Any | None = None) -> dict[str, Any]:
    """把该 slot 的 app/env/surface 生效登录策略交给脚本。"""
    from mino_nexus.resources.accounts.lease import account_login_policy

    return account_login_policy(ctx if ctx is not None else getattr(admission, "ctx", None))


def _step_text(case: dict[str, Any], role: str, number: int) -> str:
    from mino_nexus.contract.step_pointer import build_seq_nodes
    from mino_nexus.loop.script_runtime import _prep_lines

    if role == "prep":
        return str(_prep_lines(case).get(number) or "")
    for node in build_seq_nodes(case):
        if node.n == number:
            return str(node.instruction if role == "do" else node.expected or "")
    return ""


def _step(admission: ScriptAdmission, params: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.loop.script_runtime import ScriptAbort

    action = str(params.get("action") or "")
    role = str(params.get("role") or "")
    title = params.get("title")
    titled = params.get("number") is None and isinstance(title, str) and bool(title.strip())
    extra: dict[str, Any] = {}
    if titled:
        # 按标题写的步骤没有用例原文可对：n 是本次运行里 enter 的先后序号，enter/leave 配对。
        text = title.strip()[:300]
        extra["title"] = text
        if action == "enter":
            admission.step_seq += 1
            admission.step_open = admission.step_seq
        number = admission.step_open or admission.step_seq
        if action == "leave":
            admission.step_open = 0
    else:
        try:
            number = int(params.get("number"))
        except (TypeError, ValueError) as exc:
            raise ScriptAbort("步骤编号无效", reason_code="script_step_missing") from exc
        text = _step_text(admission.case, role, number)
    writer = admission.writer
    if action == "enter" and writer is not None:
        if hasattr(writer, "set_turn"):
            writer.set_turn(number)
        writer.append("node/enter", {"n": number, "role": role, "text": text, "case_step": number, **extra})
    elif action == "leave" and writer is not None:
        status = "fail" if params.get("failed") else "pass"
        writer.append("node/leave", {"n": number, "role": role, "status": status, "case_step": number, "text": text, **extra})
    elif action not in {"enter", "leave"}:
        raise ScriptAbort("步骤动作无效", reason_code="script_step_missing")
    return {"_summary": text or str(number)}


def _fail(admission: ScriptAdmission, params: dict[str, Any]) -> dict[str, Any]:
    message = str(params.get("message") or "脚本判定失败")[:300]
    if admission.writer is not None:
        admission.writer.append("tool/result", {
            "capability_id": "fail",
            "status": "fail",
            "summary": message,
            "error": message,
            "executor_used": "scout",
            "source": "script_run",
        })
    return {"_summary": message}


def _note(admission: ScriptAdmission, params: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.loop.script_runtime import _note_fields

    body = str(params.get("text") or "")[:500]
    fields = {key: value for key, value in params.items() if key not in {"run_id", "session_id", "text"}}
    payload = {
        "name": "note",
        "summary": body[:300],
        "status": "pass",
        **_note_fields(fields),
        "text": body,
    }
    if admission.writer is not None:
        admission.writer.append("script/note", payload)
    return {"_summary": body[:300]}


def _lease(admission: ScriptAdmission, params: dict[str, Any]) -> dict[str, Any]:
    case_id = str(params.get("case_id") or "").strip()
    if case_id and case_id != admission.case_id:
        raise PermissionError("case_id 与这次运行不一致")
    from mino_nexus.resources.scout_record_store import for_node
    record = for_node(admission.session_id, admission.node_id)
    if record and (record["cancel_requested"] or record["status"] not in {"queued", "running"}):
        raise ValueError("task_cancel_requested" if record["cancel_requested"] else "attempt_completed")
    if record:
        from mino_nexus.loop.scout_record_tasks import refresh_resources
        if not refresh_resources(record):
            raise ValueError("resource_lease_lost")
    return {
        "_summary": "这次运行仍占用该节点和设备",
        "occupied": True,
        "sn": admission.sn,
        "node_id": admission.node_id,
    }


_HIERARCHY_KEYS = ("resource_id", "text", "content_desc", "class", "role", "clickable", "editable")


def _hierarchy_nodes(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    nodes: list[dict[str, Any]] = []
    for node in raw:
        if not isinstance(node, dict):
            continue
        row: dict[str, Any] = {}
        for key in _HIERARCHY_KEYS:
            value = node.get(key)
            if isinstance(value, bool):
                row[key] = value
            elif isinstance(value, str) and value.strip():
                row[key] = value.strip()[:160]
        bounds = node.get("bounds")
        if isinstance(bounds, (list, tuple)) and len(bounds) == 4 and all(isinstance(n, (int, float)) for n in bounds):
            row["bounds"] = [int(n) for n in bounds]
        labeled = bool(row.get("resource_id") or row.get("text") or row.get("content_desc"))
        if labeled or row.get("clickable") or row.get("editable"):
            nodes.append(row)
        if len(nodes) >= 160:
            break
    return nodes


def _write_step(admission: ScriptAdmission, params: dict[str, Any]) -> None:
    event = params.get("event") if isinstance(params.get("event"), dict) else params
    body = {key: value for key, value in dict(event).items() if key not in {"run_id", "session_id", "image_base64", "image_mime", "event"}}
    image = str(params.get("image_base64") or event.get("image_base64") or "")
    if image and admission.writer is not None:
        mime = str(params.get("image_mime") or event.get("image_mime") or "image/png")
        _store_image(admission, image, mime)
    cap = str(body.get("capability_id") or "script")[:80]
    status = str(body.get("status") or "pass")[:40]
    record = {
        "capability_id": cap,
        "status": status,
        "summary": str(body.get("summary") or "")[:800],
        "error": str(body.get("error") or "")[:800],
        "executor_used": "scout",
        "source": "script_run",
    }
    for key in ("app", "script_sha256", "script_length", "role"):
        value = body.get(key)
        if isinstance(value, (str, int)) and value != "":
            record[key] = value if isinstance(value, int) else value[:80]
    elapsed = body.get("elapsed_ms")
    if isinstance(elapsed, int):
        record["elapsed_ms"] = elapsed
    case_step = body.get("case_step")
    if isinstance(case_step, int) or (isinstance(case_step, str) and case_step.strip()):
        record["case_step"] = case_step if isinstance(case_step, int) else case_step.strip()[:300]
    if cap == "hierarchy":
        nodes = _hierarchy_nodes(body.get("nodes"))
        if nodes:
            record["nodes"] = nodes
        tree = body.get("hierarchy_text")
        if isinstance(tree, str) and tree.strip():
            record["hierarchy_text"] = tree[:8000]
    if admission.writer is not None:
        call = {"capability_id": cap, "params": {}, "source": "script_run"}
        if record.get("app"):
            call["app"] = record["app"]
        admission.writer.append("tool/call", call)
        admission.writer.append("tool/result", record)
    admission.steps.append(record)


def _store_image(admission: ScriptAdmission, encoded: str, mime: str) -> None:
    if len(encoded) > 12_000_000 or admission.writer is None:
        return
    from mino_nexus.loop.observe.agent_stream import make_thumb
    from mino_nexus.loop.observe.screen_store import publish_screen

    thumb = make_thumb(encoded)
    if not thumb:
        return
    session_id = str(getattr(admission.writer, "session_id", "") or admission.session_id)
    ref, _shot_hash = publish_screen(session_id, f"script-{len(admission.steps) + 1}", thumb)
    if not ref:
        return
    admission.writer.append("observe/screen", {
        "mime": "image/jpeg",
        "thumb": ref,
        "source": "script",
        "original_mime": mime,
    })
