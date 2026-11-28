"""Asset catalog. File bytes stay in the publishing Scout's local library."""
from __future__ import annotations

import json
import re
import time

from sqlalchemy import func, text
from mino_nexus.core.database import session_scope
from mino_nexus.models.asset_storage import AssetStorage
from mino_nexus.models.asset_blob import AssetBlob
from mino_nexus.models.test_asset import TestAsset
from mino_nexus.resources.project_members import require_project_access, require_owner

MAX_METADATA_BYTES = 8192
LOCAL_STORAGE_ID = "local"
DEFAULT_QUOTA_BYTES = 50 * 1024 ** 3
DEFAULT_MAX_FILE_BYTES = 512 * 1024 ** 2
_EXTERNAL_FIELDS = ("endpoint", "bucket", "region", "prefix", "checksum_mode", "access_key", "secret_key")


class AssetError(RuntimeError):
    def __init__(self, code, status=409):
        self.code = code
        self.status = status
        super().__init__(code)


def _ident(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise AssetError("asset_invalid_" + field, 422)
    return value


def _number(value, field, maximum=1024**5):
    if type(value) is not int or not 0 < value <= maximum:
        raise AssetError("asset_invalid_" + field, 422)
    return value


def _metadata(value):
    from mino_nexus.resources.credential_safety import sanitize_credentials
    if not isinstance(value, dict):
        raise AssetError("asset_metadata_invalid", 422)
    try:
        raw = json.dumps(value, allow_nan=False, ensure_ascii=False)
        if sanitize_credentials(value) != value:
            raise ValueError("credentials")
    except (ValueError, TypeError):
        raise AssetError("asset_metadata_invalid", 422) from None
    if len(raw.encode()) > MAX_METADATA_BYTES:
        raise AssetError("asset_metadata_too_large", 422)
    # Signed URLs and inline file bodies are not durable metadata.
    if re.search(r"(?i)x-amz-|data:[^\s]*;base64,", raw):
        raise AssetError("asset_metadata_contains_transfer_secret", 422)
    return value


def _storage_public(row):
    return {"storage_id": row.storage_id, "location": "scout_library", "active": row.active,
            "max_preparations_per_task": row.max_preparations_per_task, "quota_bytes": row.quota_bytes,
            "max_file_bytes": row.max_file_bytes, "updated_at": row.updated_at}


def _limits(value):
    quota = _number(value.get("quota_bytes"), "quota_bytes")
    maximum = _number(value.get("max_file_bytes"), "max_file_bytes", DEFAULT_MAX_FILE_BYTES)
    if maximum > quota:
        raise AssetError("asset_file_limit_exceeds_quota", 422)
    preparations = value.get("max_preparations_per_task", 0)
    if type(preparations) is not int or not 0 <= preparations <= 100:
        raise AssetError("asset_preparation_budget_invalid", 422)
    return quota, maximum, preparations


def _reject_external(value):
    if any(value.get(field) not in (None, "") for field in _EXTERNAL_FIELDS):
        raise AssetError("asset_storage_external_not_supported", 422)


def _fill_local(row, quota, maximum, preparations, now):
    row.endpoint, row.bucket, row.region, row.prefix = "scout-library", "local", "local", "library"
    row.checksum_mode, row.access_key, row.secret_key = "sha256", "", ""
    row.max_preparations_per_task = preparations
    row.quota_bytes, row.max_file_bytes, row.active, row.updated_at = quota, maximum, 1, now


def configure(project_id, sess, value):
    require_owner(project_id, sess)
    _reject_external(value)
    sid = _ident(value.get("storage_id") or LOCAL_STORAGE_ID, "storage_id")
    if sid != LOCAL_STORAGE_ID:
        raise AssetError("asset_storage_local_only", 422)
    quota, maximum, preparations = _limits(value)
    now = int(time.time())
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        db.query(AssetStorage).filter_by(project_id=project_id).update({"active": 0})
        row = db.get(AssetStorage, (project_id, sid))
        if row is None:
            row = AssetStorage(project_id=project_id, storage_id=sid)
            db.add(row)
        _fill_local(row, quota, maximum, preparations, now)
        db.flush()
        return _storage_public(row)


def storages(project_id, sess):
    require_project_access(project_id, sess)
    with session_scope() as db:
        used = int(db.query(func.coalesce(func.sum(AssetBlob.size_bytes), 0)).filter_by(project_id=project_id).scalar())
        from mino_nexus.models.project import Project
        can_configure = db.get(Project, project_id).created_by == sess["user_id"]
        return {"can_configure": can_configure, "items": [_storage_public(r) for r in db.query(AssetStorage).filter_by(project_id=project_id)],
                "used_bytes": used, "location": "scout_library", "asset_api_version": 1}


def _ensure_local(db, project_id):
    row = db.get(AssetStorage, (project_id, LOCAL_STORAGE_ID))
    if row is None:
        row = AssetStorage(project_id=project_id, storage_id=LOCAL_STORAGE_ID)
        db.add(row)
        _fill_local(row, DEFAULT_QUOTA_BYTES, DEFAULT_MAX_FILE_BYTES, 0, int(time.time()))
    return row


def _view(asset, blob):
    source = asset.source if isinstance(asset.source, dict) else {}
    return {key: getattr(asset, key) for key in ("asset_id", "version", "title", "filename", "kind", "attributes", "source", "sha256", "created_at")} | {
        "project_id": asset.project_id, "size_bytes": blob.size_bytes, "state": blob.state,
        "storage_id": blob.storage_id, "md5": blob.md5, "verified_at": blob.verified_at,
        "location": "scout_library", "library_key": blob.object_key, "holder_node_id": str(source.get("node_id") or "")}


def begin(project_id, sess, value):
    require_project_access(project_id, sess)
    aid = _ident(value.get("asset_id"), "asset_id")
    version = _number(value.get("version", 1), "version", 2**31-1)
    sha = value.get("sha256")
    if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha):
        raise AssetError("asset_sha256_invalid", 422)
    md5 = value.get("md5")
    if not isinstance(md5, str) or not re.fullmatch(r"[a-f0-9]{32}", md5):
        raise AssetError("asset_md5_invalid", 422)
    size = _number(value.get("size_bytes"), "size_bytes")
    kind = _ident(value.get("kind"), "kind")
    title, filename = value.get("title", aid), value.get("filename", aid)
    if not isinstance(title, str) or not 1 <= len(title) <= 300 or not isinstance(filename, str) or not 1 <= len(filename) <= 255:
        raise AssetError("asset_title_or_filename_invalid", 422)
    if any(c in filename for c in ("/", "\\", "\x00", "\r", "\n")) or filename in (".", ".."):
        raise AssetError("asset_filename_invalid", 422)
    attrs, source = _metadata(value.get("attributes", {})), _metadata(value.get("source", {}))
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        asset = db.get(TestAsset, (project_id, aid, version))
        blob = db.get(AssetBlob, (project_id, sha))
        if asset and (asset.sha256 != sha or asset.kind != kind or asset.attributes != attrs or asset.filename != filename or asset.title != title):
            raise AssetError("asset_version_immutable")
        if blob and (blob.size_bytes != size or blob.md5 != md5):
            raise AssetError("asset_content_size_conflict")
        storage = _ensure_local(db, project_id)
        if size > storage.max_file_bytes:
            raise AssetError("asset_file_too_large", 413)
        now = int(time.time())
        if blob is None:
            used = int(db.query(func.coalesce(func.sum(AssetBlob.size_bytes), 0)).filter_by(project_id=project_id).scalar())
            if used + size > storage.quota_bytes:
                raise AssetError("asset_project_quota_exceeded", 413)
            blob = AssetBlob(project_id=project_id, sha256=sha, md5=md5, storage_id=storage.storage_id,
                    object_key=f"library/{sha}", size_bytes=size, state="ready", created_at=now, verified_at=now)
            db.add(blob)
            deduplicated = False
        else:
            deduplicated = blob.state == "ready"
            if blob.state != "ready":
                raise AssetError("asset_not_ready")
        if asset is None:
            asset = TestAsset(project_id=project_id, asset_id=aid, version=version, sha256=sha,
                kind=kind, title=title, filename=filename, attributes=attrs, source=source,
                created_by=sess["user_id"], created_at=now)
            db.add(asset)
        db.flush()
        # The Scout already committed this hash into its local library. Nexus stores the reference only.
        return {"asset": _view(asset, blob), "location": "scout_library", "deduplicated": deduplicated}


def _rows(db, project_id, asset_id, version):
    asset = db.get(TestAsset, (project_id, _ident(asset_id, "asset_id"), _number(version, "version", 2**31-1)))
    if asset is None:
        raise AssetError("asset_not_found", 404)
    blob = db.get(AssetBlob, (project_id, asset.sha256))
    storage = db.get(AssetStorage, (project_id, blob.storage_id))
    return asset, blob, storage


def holder_node_id(project_id, asset_id, version):
    try:
        aid = _ident(asset_id, "asset_id")
        ver = _number(version if type(version) is int else 1, "version", 2**31 - 1)
    except AssetError:
        return ""
    with session_scope() as db:
        asset = db.get(TestAsset, (project_id, aid, ver))
        source = asset.source if asset is not None and isinstance(asset.source, dict) else {}
        return str(source.get("node_id") or "")


def describe(project_id, sess, asset_id, version):
    """Catalog lookup. There is no download URL; the holding Scout opens its library."""
    require_project_access(project_id, sess)
    with session_scope() as db:
        asset, blob, _storage = _rows(db, project_id, asset_id, version)
        if blob.state != "ready":
            raise AssetError("asset_not_ready")
        return {"asset": _view(asset, blob), "location": "scout_library"}


def ticket(project_id, sess, asset_id, version, operation="download"):
    found = describe(project_id, sess, asset_id, version)
    if operation == "upload":
        found["deduplicated"] = True
    return found


def complete(project_id, sess, asset_id, version):
    return describe(project_id, sess, asset_id, version)


def list_assets(project_id, sess, *, kind="", state="", limit=50, offset=0, attributes=None):
    require_project_access(project_id, sess)
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise AssetError("asset_pagination_invalid", 422)
    attrs = _metadata(attributes or {})
    with session_scope() as db:
        q = db.query(TestAsset, AssetBlob).join(AssetBlob,
            (TestAsset.project_id == AssetBlob.project_id) & (TestAsset.sha256 == AssetBlob.sha256)).filter(TestAsset.project_id == project_id)
        if kind:
            q = q.filter(TestAsset.kind == kind)
        if state:
            q = q.filter(AssetBlob.state == state)
        for key, value in attrs.items():
            if isinstance(value, (dict, list)) or value is None:
                raise AssetError("asset_search_attributes_scalar_required", 422)
            q = q.filter(TestAsset.attributes[key].as_string() == str(value)) if isinstance(value, str) else q.filter(TestAsset.attributes[key].as_boolean() == value) if isinstance(value, bool) else q.filter(TestAsset.attributes[key].as_float() == value)
        total = q.count()
        return {"items": [_view(a, b) for a, b in q.order_by(TestAsset.created_at.desc(), TestAsset.asset_id, TestAsset.version.desc()).offset(offset).limit(limit)], "total": total}
