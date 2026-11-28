"""Asset catalog API. File bytes stay on the publishing Scout."""
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict
from mino_nexus.api.deps import current_session
from mino_nexus.core.http_util import ok
from mino_nexus.resources import asset_store as store

router = APIRouter(prefix="/project/{project_id}/assets", tags=["Assets"])


class StorageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    storage_id: str = "local"
    quota_bytes: int = Field(ge=1, le=1024**5)
    max_preparations_per_task: int = Field(default=0, ge=0, le=100)
    max_file_bytes: int = Field(ge=1, le=512*1024**2)


class BeginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: str
    version: int = Field(default=1, ge=1)
    title: str
    filename: str
    kind: str
    md5: str
    sha256: str
    size_bytes: int = Field(ge=1)
    attributes: dict[str, Any] = Field(default_factory=dict)
    source: dict[str, Any] = Field(default_factory=dict)


def _invoke(fn, *args, **kwargs):
    try:
        return ok(fn(*args, **kwargs))
    except store.AssetError as exc:
        raise HTTPException(exc.status, detail={"reason_code": exc.code}) from None
    except PermissionError:
        raise HTTPException(403, detail="asset_access_denied") from None
    except KeyError:
        raise HTTPException(404, detail="project_not_found") from None


@router.get("/storage")
def storage_get(project_id: str, sess: dict = Depends(current_session)):
    return _invoke(store.storages, project_id, sess)


@router.put("/storage")
def storage_set(project_id: str, body: StorageBody, sess: dict = Depends(current_session)):
    return _invoke(store.configure, project_id, sess, body.model_dump())


@router.get("")
def asset_list(project_id: str, kind: str = "", state: str = "", limit: int = Query(50, ge=1, le=100),
               offset: int = Query(0, ge=0), sess: dict = Depends(current_session)):
    return _invoke(store.list_assets, project_id, sess, kind=kind, state=state, limit=limit, offset=offset)


@router.post("/uploads")
def upload_begin(project_id: str, body: BeginBody, sess: dict = Depends(current_session)):
    return _invoke(store.begin, project_id, sess, body.model_dump())


@router.post("/{asset_id}/{version}/upload-ticket")
def upload_ticket(project_id: str, asset_id: str, version: int, sess: dict = Depends(current_session)):
    return _invoke(store.ticket, project_id, sess, asset_id, version, "upload")


@router.post("/{asset_id}/{version}/complete")
def upload_complete(project_id: str, asset_id: str, version: int, sess: dict = Depends(current_session)):
    return _invoke(store.complete, project_id, sess, asset_id, version)


@router.post("/{asset_id}/{version}/download-ticket")
def download_ticket(project_id: str, asset_id: str, version: int, sess: dict = Depends(current_session)):
    return _invoke(store.ticket, project_id, sess, asset_id, version)


class PreparationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    demand: dict[str, Any]
    recipe_ref: str
    retry: bool = False
    retry_evidence: dict[str, Any] = Field(default_factory=dict)


class PreparationFinishBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: str
    asset_ref: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)


@router.get("/preparations")
def preparations_get(project_id: str, sess: dict = Depends(current_session)):
    from mino_nexus.resources.asset_preparation import list_preparations
    return _invoke(list_preparations, project_id, sess)


@router.post("/preparations")
def preparation_claim(project_id: str, body: PreparationBody, sess: dict = Depends(current_session)):
    from mino_nexus.resources.asset_preparation import claim
    return _invoke(claim, project_id, sess, body.model_dump())


@router.put("/preparations/{preparation_id}")
def preparation_finish(project_id: str, preparation_id: str, body: PreparationFinishBody, sess: dict = Depends(current_session)):
    from mino_nexus.resources.asset_preparation import finish
    return _invoke(finish, project_id, sess, preparation_id, body.model_dump())
