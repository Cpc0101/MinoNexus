"""设备类型 -> 允许的通道家族。纯映射，不含节点状态。

从 `loop/router_proxy.py` 下沉：`services` 需要它拼绑定，但不能因此 import 网关。
"""
from __future__ import annotations


WDA_CAPS = frozenset({
    "launch_app", "open_app", "close_app", "kill_app", "tap_element", "tap_xy",
    "input_text", "swipe", "press_key", "install_app", "uninstall_app",
    "self_prove",
})


def normalize_executor(executor: str, platform: str) -> str:
    """Map the retired native executor id `desktop` onto the real platform.

    Only for values read from persisted catalog rows / old payloads. New writes
    must use `mac` / `win` directly.
    """
    ex = str(executor or "").strip()
    plat = str(platform or "").strip().lower()
    if ex == "desktop" and plat in {"mac", "win"}:
        return plat
    return ex


def cap_uses_wda(capability_id: str) -> bool:
    return str(capability_id or "").strip() in WDA_CAPS


def executors_for_platform(platform: str) -> tuple[str, ...]:
    """这台设备类型允许的通道。家族互斥：Web 没有 adb，安卓/iOS 没有 playwright。"""
    plat = str(platform or "").strip().lower()
    if plat in ("web", "browser", "playwright"):
        return ("playwright",)
    if plat in ("ios",):
        return ("ios_wda",)
    if plat == "mac":
        return ("mac", "desktop")
    if plat == "win":
        return ("win", "desktop")
    if plat == "desktop":
        # Legacy platform alias stored before 2026-11; executor ids are mac/win now.
        return ("mac", "win")
    if plat == "android":
        return ("adb", "remote")
    return ()
