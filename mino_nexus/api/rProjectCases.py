"""项目用例库与导入。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from mino_nexus.core.http_util import ok
from mino_nexus.api.deps import current_session, project_maintainer
from mino_nexus.casework import case_import as cim
from mino_nexus.resources import project_store as ps

router = APIRouter(prefix="/project", tags=["Project Cases"])


class ResourceKeyPreviewBody(BaseModel):
    precondition: str = ""
    platform: str = ""
    package_id: str = ""
    env: str = "test"


class ImportRequirementCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    requirement_id: str = ""
    source_url: str = ""


class RequirementUpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_id: str = Field(min_length=1, max_length=200)
    title: str = ""
    source_url: str = ""


class CaseImportPreviewBody(BaseModel):
    requirement_id: str
    table: list[list[Any]] | None = None
    text: str = ""
    filename: str = ""
    header_row: int = 0
    skip_rows: list[int] = Field(default_factory=list)
    column_map: dict[str, int] = Field(default_factory=dict)
    default_platform: str = ""


class CaseImportSourceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    configuration: dict[str, Any]
    operation: str = "read"


class CaseImportCommitRow(BaseModel):
    row_index: int | None = None
    selected: bool = True
    on_conflict: str = "skip"
    payload: dict[str, Any] | None = None


class CaseImportCommitBody(BaseModel):
    requirement_id: str
    client_request_id: str = Field(default="", max_length=160)
    preview_token: str = ""
    default_on_conflict: str = "skip"
    default_platform: str = ""
    block_on_step_key_issues: bool | None = None
    import_policy: str = ""
    rows: list[CaseImportCommitRow] = Field(default_factory=list)


class CasePreflightBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_id: str = Field(min_length=1, max_length=200)
    case_ids: list[str] = Field(default_factory=list, max_length=200)
    recompile_stale: bool = False


class CaseDeleteBody(BaseModel):
    case_ids: list[str] = Field(default_factory=list)


class CaseUpdateBody(BaseModel):
    requirement_id: str = ""
    name: str = ""
    module: str = ""
    platform: str = ""
    aspect: str = ""
    precondition: str = ""
    steps: list[str] = Field(default_factory=list)
    expected: list[str] = Field(default_factory=list)
    steps_raw: str = ""
    expected_raw: str = ""
    resource_key: dict[str, Any] | None = None
    case_scene: dict[str, Any] | None = None
    script: str | None = None
    pretest: str | None = None
    posttest: str | None = None
    script_platform: str = ""
    script_path: str = ""


class ProjectScriptBody(BaseModel):
    pretest: str = ""
    posttest: str = ""
    script_platform: str = ""


class ScriptRepoBody(BaseModel):
    url: str = ""
    branch: str = ""


class ScriptCheckBody(BaseModel):
    case_id: str = ""
    path: str = ""


def _case_error(exc):
    code = str(exc)
    return HTTPException(status_code=404 if code.endswith("missing") else 409 if "conflict" in code else 400,
                         detail={"reason_code": code})


class V5BaselineBody(BaseModel):
    expected_revision: int = Field(ge=1)
    control_id: str = Field(min_length=1)


def _project(project_id: str) -> dict[str, Any]:
    row = ps.find_project(project_id)
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    return row


class EventPolicyReviewBody(BaseModel):
    kind: str
    entry_id: str
    source_digest: str = Field(min_length=64, max_length=64)
    request_id: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=1000)
    action: str
    replacement: dict[str, Any] = Field(default_factory=dict)


@router.get("/{project_id}/event-policies/migration-preview")
def event_policy_preview(project_id: str, sess: dict = Depends(current_session)):
    from mino_nexus.api.deps import require_packs_writer
    from mino_nexus.casework.event_policy_review import inventory
    from mino_nexus.core.secret_redaction import sanitize
    require_packs_writer(sess)
    _project(project_id)
    return ok(sanitize({'items': inventory()}))


@router.post("/{project_id}/event-policies/migration-apply")
def event_policy_apply(project_id: str, body: EventPolicyReviewBody, sess: dict = Depends(current_session)):
    from mino_nexus.api.deps import require_packs_writer
    from mino_nexus.casework.event_policy_review import apply
    from mino_nexus.platform.identity import require_user_id
    require_packs_writer(sess)
    _project(project_id)
    try:
        values = body.model_dump()
        return ok(apply(**values, actor=require_user_id()))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail={"reason_code": "policy_admin_required"}) from exc
    except ValueError as exc:
        raise _case_error(exc) from exc


@router.post("/{project_id}/resource-key/preview")
def preview_resource_key(project_id: str, body: ResourceKeyPreviewBody, _sess: dict = Depends(current_session)):
    _project(project_id)
    from mino_nexus.resources import project_store as ps
    from mino_nexus.resources.case_resource_claim import (
        compile_resource_key_from_precondition,
        default_package_for_project,
    )
    from mino_nexus.resources.resource_claim_summary import resource_claim_summary

    try:
        env_doc = ps.project_env(project_id)
    except KeyError:
        env_doc = None
    pkg = str(body.package_id or "").strip() or default_package_for_project(project_id)
    claim = compile_resource_key_from_precondition(
        body.precondition,
        env_doc=env_doc,
        platform=str(body.platform or "").strip(),
        package=pkg,
        env=str(body.env or "test"),
    )
    scene = claim.get("case_scene") if isinstance(claim.get("case_scene"), dict) else {}
    return ok({
        "resource_key": claim,
        "case_scene": scene,
        "summary": resource_claim_summary(claim, scene=scene, precondition=body.precondition),
    })


@router.get("/{project_id}/cases")
def list_project_cases(project_id: str, limit: int | None = Query(None, ge=1, le=200), offset: int = Query(0, ge=0),
                       search: str = "", module: str = "", app_id: str = "", result: str = "all",
                       _sess: dict = Depends(current_session)):
    _project(project_id)
    if limit is not None:
        if result not in ("all", "failed", "never"):
            raise HTTPException(status_code=400, detail="未知结果筛选")
        if app_id:
            app = ps.find_app(app_id)
            if not app or str(app.get("project_id") or "") != project_id:
                raise HTTPException(status_code=400, detail="应用不属于当前项目")
        elif result != "all":
            raise HTTPException(status_code=400, detail="结果筛选需要 app_id")
        from mino_nexus.casework.case_store import list_case_page

        return ok(list_case_page(project_id, limit=limit, offset=offset, search=search, module=module, app_id=app_id, result=result))
    return ok(cim.cases_payload(project_id))


@router.get("/{project_id}/case-modules")
def list_case_modules(project_id: str, _sess: dict = Depends(current_session)):
    _project(project_id)
    from mino_nexus.casework.case_store import case_modules

    return ok(case_modules(project_id))


@router.post("/{project_id}/cases/preflight")
def post_case_preflight(project_id: str, body: CasePreflightBody, _sess: dict = Depends(current_session)):
    _project(project_id)
    try:
        app = ps.require_app(body.app_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="App not found") from exc
    if str(app.get("project_id") or "") != str(project_id):
        raise HTTPException(status_code=400, detail="app 与项目不匹配")
    if body.recompile_stale:
        project_maintainer(project_id, _sess)
    from mino_nexus.casework.case_preflight import preflight

    try:
        return ok(preflight(app, body.case_ids, recompile_stale=body.recompile_stale))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{project_id}/script-repo")
def get_script_repo(project_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.casework.script_workspace import ensure_workspace, get_repo, public_url

    _project(project_id)
    row = get_repo(project_id)
    place = ensure_workspace(project_id)
    return ok({
        "url": public_url(row["url"]),
        "branch": row["branch"],
        "updated_at": row["updated_at"],
        "workspace": place,
    })


@router.put("/{project_id}/script-repo", dependencies=[Depends(project_maintainer)])
def put_script_repo(project_id: str, body: ScriptRepoBody, _sess: dict = Depends(current_session)):
    from mino_nexus.casework.script_workspace import ensure_workspace, save_repo

    _project(project_id)
    try:
        saved = save_repo(project_id, body.url, body.branch)
    except ValueError as exc:
        raise _case_error(exc) from exc
    place = ensure_workspace(project_id)
    return ok({
        "url": saved["public_url"],
        "branch": saved["branch"],
        "updated_at": saved["updated_at"],
        "workspace": place,
    }, msg="脚本仓库已保存" if place.get("ready") or not str(body.url or "").strip() else "地址已保存，检出失败")


@router.post("/{project_id}/script-repo/check", dependencies=[Depends(project_maintainer)])
def check_script_repo_file(project_id: str, body: ScriptCheckBody, _sess: dict = Depends(current_session)):
    from mino_nexus.casework.script_check import ScriptRejected, check_script
    from mino_nexus.casework.script_workspace import local_script_text, safe_script_path
    from mino_nexus.casework.case_store import get_case

    _project(project_id)
    path = str(body.path or "").strip()
    if body.case_id and not path:
        case = get_case(project_id, body.case_id) or {}
        path = str(case.get("script_path") or "")
    try:
        path = safe_script_path(path) if path else ""
    except ValueError as exc:
        raise _case_error(exc) from exc
    text = local_script_text(project_id, body.case_id, script_path=path)
    if not text:
        raise HTTPException(status_code=404, detail={"reason_code": "script_file_missing", "message": "工作目录里还没有这个脚本文件"})
    try:
        check_script(text, role="run")
    except ScriptRejected as exc:
        raise HTTPException(status_code=400, detail={"reason_code": exc.reason_code, "message": str(exc), "line": exc.line}) from exc
    return ok({"path": path, "ok": True})


@router.get("/{project_id}/case-scripts")
def get_project_case_scripts(project_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.casework.script_store import get_scripts, scripts_by_platform

    _project(project_id)
    row = get_scripts(project_id, "")
    by_end = {
        plat: {"pretest": item["pretest"], "posttest": item["posttest"]}
        for plat, item in scripts_by_platform(project_id, "").items()
    }
    return ok({
        "pretest": row["pretest"], "posttest": row["posttest"], "updated_at": row["updated_at"],
        "scripts_by_platform": by_end,
    })


@router.put("/{project_id}/case-scripts", dependencies=[Depends(project_maintainer)])
def put_project_case_scripts(project_id: str, body: ProjectScriptBody, _sess: dict = Depends(current_session)):
    _project(project_id)
    del body
    raise HTTPException(status_code=400, detail={
        "reason_code": "script_edited_locally",
        "message": "脚本在本机仓库里修改，不通过接口保存",
    })


@router.get("/{project_id}/cases/{case_id}")
def get_project_case(project_id: str, case_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.casework.case_store import get_case

    _project(project_id)
    cid = str(case_id or "").strip()
    row = get_case(project_id, cid)
    if not row:
        raise HTTPException(status_code=404, detail="用例不存在")
    from mino_nexus.casework.script_store import attach
    from mino_nexus.casework.script_workspace import local_script_text

    row = attach(row)
    repo_script = local_script_text(project_id, cid, str(row.get("platform") or ""), str(row.get("script_path") or ""))
    if repo_script:
        row = {**row, "script": repo_script}
    return ok({"case": row})


@router.get("/{project_id}/requirements")
def list_import_requirements(project_id: str, _sess: dict = Depends(current_session)):
    _project(project_id)
    return ok({"requirements": cim.list_project_requirements(project_id)})


@router.post("/{project_id}/requirements")
def create_import_requirement(project_id: str, body: ImportRequirementCreateBody, _sess: dict = Depends(project_maintainer)):
    try:
        return ok({"requirement": cim.create_project_requirement(
            project_id, body.app_id, body.title, body.requirement_id, body.source_url)})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/{project_id}/requirements/{requirement_id}")
def update_import_requirement(
    project_id: str,
    requirement_id: str,
    body: RequirementUpdateBody,
    _sess: dict = Depends(project_maintainer),
):
    try:
        return ok({"requirement": cim.update_project_requirement(
            project_id, body.app_id, requirement_id, source_url=body.source_url, title=body.title)})
    except ValueError as exc:
        status = 404 if str(exc) == "需求不存在" else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.get("/{project_id}/cases/import/sources")
def import_source_nodes(project_id: str, _sess: dict = Depends(project_maintainer)):
    from mino_nexus.casework.case_import_sources import nodes
    return ok(nodes(project_id))


@router.post("/{project_id}/cases/import/read-source")
def import_read_source(project_id: str, body: CaseImportSourceBody, _sess: dict = Depends(project_maintainer)):
    from mino_nexus.casework.case_import_sources import read
    try:
        return ok(read(project_id, body.kind, body.configuration, body.operation))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"reason_code": str(exc)}) from exc


@router.post("/{project_id}/cases/import/preview")
def import_preview(project_id: str, body: CaseImportPreviewBody, _sess: dict = Depends(current_session)):
    _project(project_id)
    try:
        data = cim.preview_import(
            project_id=project_id,
            requirement_id=body.requirement_id,
            table=body.table,
            text=body.text,
            filename=body.filename,
            header_row=body.header_row,
            skip_rows=body.skip_rows,
            column_map=body.column_map,
            default_platform=body.default_platform,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ok(data)


@router.get("/{project_id}/cases/import/preview/{token}")
def import_preview_status(project_id: str, token: str, _sess: dict = Depends(current_session)):
    _project(project_id)
    data = cim.preview_status(token, project_id)
    if not data:
        raise HTTPException(status_code=404, detail="预览已过期")
    return ok(data)


@router.post("/{project_id}/cases/import/preview/{token}/cancel")
def import_preview_cancel(project_id: str, token: str, _sess: dict = Depends(current_session)):
    _project(project_id)
    if not cim.cancel_preview(token, project_id):
        raise HTTPException(status_code=404, detail="预览已过期")
    return ok({"cancelled": True})


class PreviewRetryBody(BaseModel):
    row_index: int


@router.post("/{project_id}/cases/import/preview/{token}/retry")
def import_preview_retry(
    project_id: str,
    token: str,
    body: PreviewRetryBody,
    _sess: dict = Depends(current_session),
):
    _project(project_id)
    if not cim.retry_preview_row(token, body.row_index, project_id):
        raise HTTPException(status_code=400, detail="这一行不能重试")
    return ok(cim.preview_status(token, project_id) or {})


@router.put("/{project_id}/cases/{case_id}", dependencies=[Depends(project_maintainer)])
def update_one_case(
    project_id: str,
    case_id: str,
    body: CaseUpdateBody,
    _sess: dict = Depends(current_session),
):
    from mino_nexus.casework.case_store import get_case, save_case

    _project(project_id)
    cid = str(case_id or "").strip()
    existing = get_case(project_id, cid)
    if not existing:
        raise HTTPException(status_code=404, detail="用例不存在")
    patch = body.model_dump(exclude_unset=True)
    patch.pop("script_platform", None)
    script_path = patch.pop("script_path", None)
    if any(key in patch for key in ("script", "pretest", "posttest")):
        raise HTTPException(status_code=400, detail={
            "reason_code": "script_edited_locally",
            "message": "脚本在本机仓库里修改，不通过接口保存",
        })
    for key in ("script", "pretest", "posttest"):
        patch.pop(key, None)
    merged = {**existing, **patch, "case_id": cid}
    if script_path is not None:
        from mino_nexus.casework.script_workspace import safe_script_path
        try:
            merged["script_path"] = safe_script_path(str(script_path))
        except ValueError as exc:
            raise _case_error(exc) from exc
    rid = str(patch.get("requirement_id") or existing.get("requirement_id") or "").strip()
    try:
        saved = save_case(project_id, merged, requirement_id=rid)
    except ValueError as exc:
        raise _case_error(exc) from exc
    from mino_nexus.casework.script_store import attach

    return ok({"case": attach(saved)}, msg="用例已保存")


@router.delete("/{project_id}/cases/{case_id}", dependencies=[Depends(project_maintainer)])
def delete_one_case(project_id: str, case_id: str, _sess: dict = Depends(current_session)):
    from mino_nexus.casework.case_store import delete_case

    _project(project_id)
    if not delete_case(project_id, case_id):
        raise HTTPException(status_code=404, detail="用例不存在")
    return ok({"deleted": 1, "case_id": case_id}, msg="已删除")


@router.post("/{project_id}/cases/delete", dependencies=[Depends(project_maintainer)])
def delete_many_cases(project_id: str, body: CaseDeleteBody, _sess: dict = Depends(current_session)):
    from mino_nexus.casework.case_store import delete_cases

    _project(project_id)
    ids = [str(x).strip() for x in (body.case_ids or []) if str(x).strip()]
    if not ids:
        raise HTTPException(status_code=400, detail="请指定要删除的用例编号")
    removed = delete_cases(project_id, ids)
    return ok({"deleted": removed, "case_ids": ids[:50]}, msg=f"已删除 {removed} 条")


@router.post("/{project_id}/cases/import/commit", dependencies=[Depends(project_maintainer)])
def import_commit(project_id: str, body: CaseImportCommitBody, _sess: dict = Depends(current_session)):
    _project(project_id)
    try:
        rows = [r.model_dump() for r in body.rows] if body.rows else None
        default_action = "overwrite" if body.default_on_conflict == "overwrite" else "skip"
        from mino_nexus.platform.settings_store import resolve_block_on_step_key_issues

        data = cim.commit_import(
            project_id=project_id,
            requirement_id=body.requirement_id,
            preview_token=body.preview_token,
            rows=rows,
            default_on_conflict=default_action,
            default_platform=str(body.default_platform or "").strip(),
            block_on_step_key_issues=resolve_block_on_step_key_issues(body.block_on_step_key_issues),
            import_policy=body.import_policy,
            client_request_id=body.client_request_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409 if str(exc) == "import_request_conflict" else 400, detail=str(exc)) from exc
    if data.get("blocked") and not data.get("created") and not data.get("updated"):
        return ok(data, msg="没有可导入的 UI 可执行用例；阻断项和前置准备项见导入结果")
    return ok(data, msg="用例已导入")


@router.get("/{project_id}/cases/import/receipts/by-request/{client_request_id}")
def import_receipt_by_request(project_id: str, client_request_id: str, _sess: dict = Depends(current_session)):
    _project(project_id)
    from mino_nexus.casework.case_import_receipts import get
    data = get(project_id, client_request_id=client_request_id)
    if data is None:
        raise HTTPException(status_code=404, detail="未找到本次提交收据")
    return ok(data)


@router.get("/{project_id}/cases/import/receipts/{receipt_id}")
def import_receipt(project_id: str, receipt_id: str, _sess: dict = Depends(current_session)):
    _project(project_id)
    from mino_nexus.casework.case_import_receipts import get
    data = get(project_id, receipt_id=receipt_id)
    if data is None:
        raise HTTPException(status_code=404, detail="未找到本次提交收据")
    return ok(data)


@router.post("/{project_id}/cases/import/receipts/{receipt_id}/resume", dependencies=[Depends(project_maintainer)])
def resume_import_receipt(project_id: str, receipt_id: str, _sess: dict = Depends(current_session)):
    _project(project_id)
    from mino_nexus.casework.case_import_receipts import get, resume
    if get(project_id, receipt_id=receipt_id) is None:
        raise HTTPException(status_code=404, detail="未找到本次提交收据")
    return ok(resume(project_id, receipt_id))
