"""跑批结束时释放号池 / 设备×包 占用（与 web_env 设备锁分开）。"""
from __future__ import annotations

from typing import Any

from mino_nexus.core.log import SLog


def release_run_resource_holdings(doc: dict[str, Any] | None, *, run_id: str = "") -> None:
    rid = str(run_id or (doc or {}).get("run_id") or "").strip()
    if not rid:
        return
    from mino_nexus.casework.execution_resources import release
    release(rid)
    from mino_nexus.resources.device_resource_lease import release_device_leases_for_run

    release_device_leases_for_run(rid)
    app_id = str((doc or {}).get("app_id") or "")
    try:
        from mino_nexus.resources.accounts.lease import release_run_lease

        release_run_lease(rid, app_id=app_id)
    except Exception as exc:
        SLog.w(
            "RunResourceRelease",
            f"account lease release failed run_id={rid} app_id={app_id or '-'} "
            f"error={type(exc).__name__} detail={exc!r}",
        )
