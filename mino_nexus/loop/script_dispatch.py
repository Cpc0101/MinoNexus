"""一次运行只触发：告诉持有该设备的 Scout 跑哪条用例、用哪个仓库和提交。

拉取仓库、定位脚本、读 config.yaml、执行都在 Scout，Nexus 不参与脚本仓库的执行，也不逐步决策。
"""
from __future__ import annotations

import time
from typing import Any

from mino_nexus.core import protocol as P
from mino_nexus.core.async_util import run_sync
from mino_nexus.core.log import SLog
from mino_nexus.loop.script_callback import ScriptAdmission, register, unregister

TAG = "ScriptDispatch"


def dispatch_script_case(
    *,
    run_id: str,
    case: dict[str, Any],
    sn: str,
    app_id: str,
    package: str = "",
    provider_id: str = "",
    cancel_check=None,
    case_seq: int = 0,
    platform: str = "",
    project_id: str = "",
    repo: dict[str, str] | None = None,
) -> dict[str, Any]:
    from mino_nexus.contract.ownership import current_user_id
    from mino_nexus.contract.step_pointer import compute_case_wall_budget_sec
    from mino_nexus.records.eventlog.writer import SessionPersistenceError, open_session
    from mino_nexus.runtime.run_context import bind_case_project, build_run_context

    cid = str(case.get("case_id") or "")
    stream_id = str(case.get("report_run_id") or "").strip() or run_id
    t0 = time.time()
    ctx = build_run_context(sn, run_id=run_id, app_id=app_id, provider_id=provider_id, target_package=package)
    ctx.session_id = stream_id
    ctx.case_id = cid
    ctx.case_seq = case_seq
    ctx.scout_run_id = stream_id
    project_error = ""
    try:
        bind_case_project(ctx, case)
    except ValueError as exc:
        project_error = str(exc)
    project_id = str(project_id or getattr(ctx, "project_id", "") or case.get("project_id") or "")
    try:
        from mino_nexus.resources.accounts.lease import restore_lease_for_run

        if not project_error:
            restore_lease_for_run(ctx, run_id)
    except Exception as exc:
        SLog.w(TAG, f"restore lease failed case={cid}: {exc!r}")
    try:
        from mino_nexus.casework import run_store as _rs

        doc = _rs.get(run_id) or {}
        ctx.env_profile = str(doc.get("env_profile") or getattr(ctx, "env_profile", "") or "test")
    except Exception:
        doc = {}
    try:
        writer = open_session(
            session_id=stream_id,
            run_id=run_id,
            case_id=cid,
            app_id=app_id,
            provider_id=provider_id,
            sn=sn,
            sop_id="run-case-script",
            extra={
                "goal": str(case.get("name") or cid),
                "execution_kind": "script_run",
                "project_id": project_id,
                "platform": str(platform or ""),
            },
        )
    except SessionPersistenceError as exc:
        return _outcome("blocked", str(exc), t0, reason_code=exc.reason_code, failure_stage=exc.failure_stage, started="no")

    status, summary, reason_code, failure_stage, started = "fail", "", "", "", "no"
    steps: list[dict[str, Any]] = []
    try:
        if cancel_check and cancel_check():
            status, summary, reason_code = "cancelled", "任务已取消", "task_cancelled"
        elif project_error:
            status, summary, reason_code = "fail", project_error[:240], "project_scope_invalid"
        else:
            from mino_nexus.casework.script_workspace import public_url

            repo_url = public_url((repo or {}).get("url") or "")
            if not repo_url.strip():
                status, summary, reason_code = "fail", "项目还没有配置用例脚本仓库", "script_repo_missing"
            else:
                from mino_nexus.resources.node_registry import get_registry

                node, reason = get_registry().resolve(sn)
                if node is None or getattr(node, "send", None) is None:
                    status, summary, reason_code = "fail", (reason or "节点不在线")[:240], "node_offline"
                else:
                    payload = P.load_script_run(P.dump_script_run(P.ScriptRun(
                        run_id=run_id,
                        session_id=stream_id,
                        case_id=cid,
                        sn=sn,
                        platform=str(platform or ""),
                        repo_url=repo_url,
                        branch=str((repo or {}).get("branch") or ""),
                        revision=str(doc.get("revision") or ""),
                        script_path=str(case.get("script_path") or ""),
                    )))
                    body = P.dump_script_run(payload)
                    admission = ScriptAdmission(
                        run_id=run_id,
                        session_id=stream_id,
                        case_id=cid,
                        sn=sn,
                        node_id=str(node.node_id),
                        project_id=project_id,
                        app_id=app_id,
                        provider_id=str(provider_id or ""),
                        owner_user_id=str(current_user_id() or doc.get("plugin_user_id") or ""),
                        platform=str(platform or ""),
                        case=case,
                        ctx=ctx,
                        writer=writer,
                    )
                    register(admission)
                    started = "yes"
                    budget = max(30.0, float(compute_case_wall_budget_sec(case) or 30))
                    request = P.Execute(
                        run_id=run_id,
                        step_idx=-1,
                        sn=sn,
                        capability_id=P.SCRIPT_RUN_CAPABILITY,
                        platform=str(platform or ""),
                        params=body,
                        timeout_sec=budget,
                        executor_order=[],
                        device_hint=_device_hint(
                            node, sn, str(platform or ""),
                            str(getattr(ctx, "env_profile", "") or ""),
                            app_id, project_id=project_id,
                        ),
                    )
                    result = run_sync(node.send(P.MsgType.EXECUTE, request, timeout=budget))
                    steps = list(admission.steps)
                    if result is None:
                        status, summary, reason_code, failure_stage = "fail", "节点没有返回脚本结果", "script_no_result", "run"
                    else:
                        raw_status = getattr(getattr(result, "status", None), "value", None) or getattr(result, "status", "")
                        status = _map_status(str(raw_status))
                        summary = str(getattr(result, "summary", "") or getattr(result, "error", "") or "")[:240]
                        reason_code = "" if status == "pass" else str(getattr(result, "error", "") or "")[:80]
                        failure_stage = "" if status == "pass" else "run"
                        if status != "pass" and not summary:
                            summary = "脚本执行失败"
    except Exception as exc:
        summary = f"{type(exc).__name__}: {exc}"[:240] or "执行异常"
        reason_code = "script_dispatch_error"
        failure_stage = "run"
        SLog.e(TAG, f"script dispatch failed run={run_id} case={cid}: {summary}")
    finally:
        unregister(run_id)
        try:
            from mino_nexus.casework.run_write_safety import protected

            if protected(run_id, sn=sn):
                status, summary, reason_code, failure_stage = (
                    "blocked", "写动作结果未核验，设备与账号资源仍隔离", "write_outcome_unknown", "run",
                )
        except Exception:
            pass
        try:
            writer.close(status=status, summary=(summary or status)[:240], step_count=len(steps), reason_code=reason_code)
        except Exception as exc:
            SLog.w(TAG, f"close session failed case={cid}: {exc!r}")
    return _outcome(
        status, summary or status, t0,
        reason_code=reason_code, failure_stage=failure_stage, started=started, steps=steps,
        isolated=status == "blocked",
    )


def _device_hint(node: Any, sn: str, platform: str, env: str, app_id: str,
                project_id: str = "") -> dict[str, Any]:
    """Scout 读仓库里的 config.yaml；Nexus 只给环境、应用 id/key 和设备标识。"""
    hint: dict[str, Any] = {"env": env, "app_id": str(app_id or "")}
    try:
        from mino_nexus.resources import repo_apps

        app_key = repo_apps.key_for_app(project_id, app_id)
        if app_key:
            hint["app_key"] = app_key
    except Exception:
        pass
    dev = getattr(node, "devices", {}).get(sn)
    if platform == "android":
        serial = ((getattr(dev, "channels", None) or {}).get("serial") or "") if dev is not None else ""
        hint["adb_serial"] = serial or sn
        if dev is not None and getattr(dev, "model", ""):
            hint["model"] = dev.model
    elif platform == "ios":
        hint["udid"] = sn
    return hint


def _map_status(value: str) -> str:
    text = str(value or "").lower()
    if text in {"pass", "passed", "ok", "success"}:
        return "pass"
    if text in {"cancelled", "canceled"}:
        return "cancelled"
    if text == "blocked":
        return "blocked"
    if text == "declined":
        return "declined"
    if text in {"skipped", "skip"}:
        return "skipped"
    return "fail"


def list_requirement_scripts(
    *,
    sn: str,
    platform: str,
    repo_url: str,
    branch: str,
    revision: str,
    requirement_id: str,
    run_id: str,
) -> list[dict[str, str]]:
    """让持有设备的 Scout 列出 tests/<需求ID> 下的脚本。Nexus 不克隆仓库。"""
    from mino_nexus.resources.node_registry import get_registry

    node, reason = get_registry().resolve(sn)
    if node is None or getattr(node, "send", None) is None:
        raise ValueError((reason or "节点不在线，无法列出需求下的脚本")[:240])
    timeout = 200.0
    request = P.Execute(
        run_id=run_id,
        step_idx=-1,
        sn=sn,
        capability_id="script.list",
        platform=str(platform or ""),
        params={
            "repo_url": repo_url,
            "branch": branch,
            "revision": revision,
            "requirement_id": requirement_id,
        },
        timeout_sec=timeout,
        executor_order=[],
    )
    result = run_sync(node.send(P.MsgType.EXECUTE, request, timeout=timeout))
    if result is None:
        raise ValueError("节点没有返回需求目录")
    raw_status = getattr(getattr(result, "status", None), "value", None) or getattr(result, "status", "")
    if str(raw_status).lower() != "pass":
        summary = str(getattr(result, "summary", "") or getattr(result, "error", "") or "列出脚本失败")
        raise ValueError(summary[:240])
    data = dict(getattr(result, "data", None) or {})
    extra = getattr(result, "extra", None)
    if isinstance(extra, dict):
        data = {**extra, **data}
    block = data.get("script_list") if isinstance(data.get("script_list"), dict) else {}
    rows = block.get("cases") if isinstance(block.get("cases"), list) else []
    found: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("case_id") or "").strip()
        path = str(row.get("script_path") or "").strip()
        if cid and path:
            found.append({"case_id": cid, "script_path": path})
    return found


def _outcome(
    status: str,
    summary: str,
    t0: float,
    *,
    reason_code: str = "",
    failure_stage: str = "",
    started: str = "no",
    steps: list[dict[str, Any]] | None = None,
    isolated: bool = False,
) -> dict[str, Any]:
    return {
        "status": status,
        "summary": summary[:240],
        "steps": list(steps or []),
        "elapsed_ms": int((time.time() - t0) * 1000),
        "reason_code": reason_code,
        "failure_stage": failure_stage,
        "case_step": 0,
        "execution_started": started,
        "resource_isolated": bool(isolated),
    }
