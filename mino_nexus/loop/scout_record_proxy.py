"""Read Scout evidence through its original node connection; never persist media here."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any

from mino_nexus.core import protocol as P
from mino_nexus.core.async_util import run_sync
from mino_nexus.platform.identity import require_user_id
from mino_nexus.resources.access_scope import is_physical_device
from mino_nexus.resources.scout_transport_owner import normalize_incoming, wire_owner
from mino_nexus.resources.node_registry import get_registry
from mino_nexus.resources import scout_record_store

MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
_CHUNK_BYTES = 512 * 1024
_ERRORS = {
    "record_not_found": (404, "执行记录不存在"),
    "artifact_not_found": (404, "执行证据不存在"),
    "artifact_expired": (410, "执行证据已过期"),
    "record_owner_mismatch": (403, "没有这份执行记录的读取权限"),
    "record_unavailable": (503, "执行记录暂不可读取"),
    "evidence_temporarily_unavailable": (503, "原执行节点暂不可用"),
    "record_contract_invalid": (502, "执行节点返回的记录不完整"),
    "artifact_too_large": (413, "证据超过单次读取大小限制"),
}


class RecordReadError(RuntimeError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code if reason_code in _ERRORS else "record_unavailable"
        self.status_code, message = _ERRORS[self.reason_code]
        super().__init__(message)

    @property
    def detail(self) -> dict[str, str]:
        return {"reason_code": self.reason_code, "message": str(self)}


@dataclass(frozen=True)
class Artifact:
    content: bytes
    mime_type: str
    sha256: str


def find_record(session_id: str, owner_user_id: str) -> dict[str, Any] | None:
    """The durable index, not current device liveness, chooses the record backend."""
    return scout_record_store.get(session_id, owner_user_id)


def _integer(value: Any, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _request(record: dict[str, Any], capability_id: str, params: dict[str, Any] | None = None,
             *, timeout: float = 15.0) -> dict[str, Any]:
    owner = require_user_id(str(record.get("owner_user_id") or ""))
    scope = {key: str(record.get(key) or "") for key in ("task_id", "session_id", "attempt_id")}
    if not all(scope.values()) or not record.get("node_id"):
        raise RecordReadError("record_contract_invalid")
    # A device may now belong to another node. Historical evidence must stay on
    # the node admitted for this exact attempt, including while it is offline.
    node = get_registry().get_node(str(record["node_id"]))
    if node is None:
        raise RecordReadError("evidence_temporarily_unavailable")
    # 实体手机不归账号所有：同一台手机插到别的账号的节点上，也能读回证据。
    # 记录本身的 owner_user_id 已在上面校验，Web/桌面仍要求节点账号一致。
    if node.owner_user_id != owner and not is_physical_device(str(record.get("sn") or "")):
        raise RecordReadError("record_owner_mismatch")
    # Scout 只认节点认证账号：实体手机跨账号时线路用节点 owner，返回后换回本地 owner。
    scope["owner_user_id"] = wire_owner(record, node)
    if not node.alive or node.send is None:
        raise RecordReadError("evidence_temporarily_unavailable")
    request = P.Execute(
        run_id=str((record.get("payload") or {}).get("run_id") or record["task_id"]),
        step_idx=-1, sn=str(record.get("sn") or ""), capability_id=capability_id,
        params={**(params or {}), **scope}, timeout_sec=timeout,
    )
    try:
        result = run_sync(node.send(P.MsgType.EXECUTE, request, timeout=timeout))
    except Exception as exc:
        raise RecordReadError("evidence_temporarily_unavailable") from exc
    if not isinstance(result, P.Result):
        raise RecordReadError("record_contract_invalid")
    if result.status != P.EventStatus.PASS:
        raise RecordReadError(str(result.error or "record_unavailable"))
    if not isinstance(result.data, dict):
        raise RecordReadError("record_contract_invalid")
    return normalize_incoming(record, result.data, node)


def read_summary(record: dict[str, Any]) -> dict[str, Any]:
    data = _request(record, "record.status")
    for key in ("task_id", "session_id", "attempt_id", "owner_user_id", "node_id", "case_id"):
        if data.get(key) != record.get(key):
            raise RecordReadError("record_contract_invalid")
    if (data.get("status") not in {"queued", "running", "pass", "fail", "blocked", "declined", "skipped", "cancelled"}
            or data.get("state") not in {"accepted", "running", "completed", "interrupted", "cancel_requested"}
            or not _integer(data.get("summary_version"), 1)
            or not _integer(data.get("last_event_seq")) or type(data.get("sealed")) is not bool
            or data.get("detail_availability") not in {"available", "missing", "expired"}
            or len(json.dumps(data, ensure_ascii=False).encode()) > 131072):
        raise RecordReadError("record_contract_invalid")
    if data["summary_version"] < int(record.get("summary_version") or 0):
        raise RecordReadError("record_unavailable")
    return {**data, "record_source": "scout"}


def read_events(record: dict[str, Any], *, after_seq: int = 0, limit: int = 100, max_bytes: int = _CHUNK_BYTES) -> dict[str, Any]:
    if not _integer(after_seq) or not _integer(limit, 1) or not _integer(max_bytes, 4096):
        raise ValueError("事件游标必须非负，分页大小必须大于零")
    limit = min(limit, 200)
    data = _request(record, "record.events", {"after_seq": after_seq, "limit": limit, "max_bytes": min(max_bytes, 1048576)})
    items = data.get("items")
    if (not isinstance(items, list) or len(items) > limit
            or not _integer(data.get("next_seq")) or not _integer(data.get("high_watermark"))
            or type(data.get("has_more")) is not bool or type(data.get("sealed")) is not bool
            or len(json.dumps(data, ensure_ascii=False).encode()) > 1024 * 1024):
        raise RecordReadError("record_contract_invalid")
    previous = after_seq
    for event in items:
        if (not isinstance(event, dict) or not _integer(event.get("seq"), previous + 1)
                or event["seq"] > data["high_watermark"] or not isinstance(event.get("type"), str)
                or not event["type"] or not isinstance(event.get("ts"), str)
                or not isinstance(event.get("payload"), dict)):
            raise RecordReadError("record_contract_invalid")
        previous = event["seq"]
    if (data["next_seq"] != previous or data["has_more"] and (not items or previous >= data["high_watermark"])
            or not data["has_more"] and previous < data["high_watermark"]):
        raise RecordReadError("record_contract_invalid")
    return {"record_source": "scout", "items": items, "next_seq": previous,
            "high_watermark": data["high_watermark"], "has_more": data["has_more"], "sealed": data["sealed"]}


def _read_chunk(record: dict[str, Any], artifact_id: str, offset: int, length: int,
                *, timeout: float = 15.0) -> tuple[dict[str, Any], bytes]:
    if not artifact_id or len(artifact_id) > 256 or any(char in artifact_id for char in "/\\\x00"):
        raise RecordReadError("artifact_not_found")
    if not _integer(offset) or not _integer(length, 1) or length > _CHUNK_BYTES:
        raise ValueError("证据偏移或分段大小无效")
    data = _request(record, "record.artifact", {"artifact_id": artifact_id, "offset": offset, "length": length}, timeout=timeout)
    size, mime, digest = data.get("total_size"), data.get("mime_type"), data.get("sha256")
    if (data.get("artifact_id") != artifact_id or not _integer(data.get("offset")) or data["offset"] != offset
            or not _integer(size) or not _integer(data.get("bytes_read"))
            or type(data.get("eof")) is not bool or not isinstance(mime, str)
            or not re.fullmatch(r"[\w.+-]+/[\w.+-]+(?:;[^\r\n]*)?", mime)
            or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or not isinstance(data.get("data_base64"), str)
            or len(data["data_base64"]) > (length + 2) // 3 * 4):
        raise RecordReadError("record_contract_invalid")
    try:
        chunk = base64.b64decode(data["data_base64"], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RecordReadError("record_contract_invalid") from exc
    if (len(chunk) != data["bytes_read"] or len(chunk) > length or offset + len(chunk) > size
            or data["eof"] != (offset + len(chunk) == size) or not chunk and not data["eof"]):
        raise RecordReadError("record_contract_invalid")
    return data, chunk


def read_artifact_chunk(record: dict[str, Any], artifact_id: str, *, offset: int = 0, length: int = 32768) -> dict[str, Any]:
    """Bounded MCP-readable bytes; SHA refers to the complete artifact, not this chunk."""
    if not _integer(length, 1) or length > 65536:
        raise ValueError("分段大小必须在 1 到 65536 字节之间")
    data, chunk = _read_chunk(record, artifact_id, offset, length)
    result = {"record_source": "scout", **{key: data[key] for key in (
        "artifact_id", "mime_type", "total_size", "offset", "bytes_read", "eof", "data_base64", "sha256",
    )}}
    # Text is a convenience view only. Always retain exact bytes so a UTF-8
    # character split at a range boundary never silently changes the evidence.
    if data["mime_type"].startswith("text/") or any(kind in data["mime_type"] for kind in ("json", "xml")):
        try:
            decoded = chunk.decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            if len(json.dumps(decoded, ensure_ascii=False).encode()) <= 98304:
                result["text"] = decoded
    return result


def read_artifact(record: dict[str, Any], artifact_id: str, *, max_size: int = MAX_ARTIFACT_BYTES) -> Artifact:
    content = bytearray()
    metadata: tuple[int, str, str] | None = None
    deadline = time.monotonic() + 60
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RecordReadError("evidence_temporarily_unavailable")
        offset = len(content)
        data, chunk = _read_chunk(record, artifact_id, offset, _CHUNK_BYTES, timeout=min(15.0, remaining))
        size, mime, digest = data["total_size"], data["mime_type"], data["sha256"]
        if size > min(max_size, MAX_ARTIFACT_BYTES):
            raise RecordReadError("artifact_too_large")
        current = (size, mime, digest)
        if metadata is not None and current != metadata:
            raise RecordReadError("record_contract_invalid")
        metadata = current
        content.extend(chunk)
        if data["eof"]:
            if hashlib.sha256(content).hexdigest() != digest:
                raise RecordReadError("record_contract_invalid")
            return Artifact(bytes(content), mime, digest)


def read_screenshot(record: dict[str, Any], seq: int) -> dict[str, Any]:
    page = read_events(record, after_seq=max(0, seq - 1), limit=1)
    event = next((item for item in page["items"] if item["seq"] == seq), None)
    payload = (event or {}).get("payload") or {}
    artifact_id = payload.get("artifact_id")
    if not isinstance(artifact_id, str) or not str(payload.get("mime_type") or payload.get("mime") or "").startswith("image/"):
        raise RecordReadError("artifact_not_found")
    artifact = read_artifact(record, artifact_id, max_size=8 * 1024 * 1024)
    raw = artifact.content
    mime = ("image/png" if raw.startswith(b"\x89PNG\r\n\x1a\n") else
            "image/jpeg" if raw.startswith(b"\xff\xd8\xff") else
            "image/webp" if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP" else "")
    if not mime or artifact.mime_type.split(";", 1)[0] != mime:
        raise RecordReadError("record_contract_invalid")
    return {"session_id": record["session_id"], "seq": seq, "source": "scout", "record_source": "scout",
            "artifact_id": artifact_id, "mimeType": mime, "image_base64": base64.b64encode(raw).decode(), "truncated": False}
