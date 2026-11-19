"""Read the admitted target for one device, never a batch's display platform."""
from __future__ import annotations


def for_device(doc: dict, sn: str) -> dict:
    from mino_nexus.core.platform_contract import target_platform

    target = (doc.get("device_targets") or {}).get(sn) or {}
    platform = target.get("target_platform") or target.get("platform") or (
        doc.get("platforms_by_sn") or {}).get(sn) or doc.get("platform")
    return {
        "platform": target_platform(platform),
        "env_surface": str(target.get("target_surface", doc.get("env_surface")) or ""),
        "package": str(target.get("target_package", doc.get("package")) or ""),
    }
