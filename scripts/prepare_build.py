"""Write release identity for a Nexus wheel without pulling any other repository."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


def git_value(root: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), *args], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    version = (root / "mino_nexus/VERSION").read_text(encoding="utf-8").strip()
    commit = os.environ.get("GITHUB_SHA") or os.environ.get("SOURCE_COMMIT") or git_value(root, "rev-parse", "HEAD")
    dirty = bool(git_value(root, "status", "--porcelain")) if not os.environ.get("GITHUB_ACTIONS") else False
    (root / "mino_nexus/BUILD.json").write_text(
        json.dumps({
            "build_id": f"nexus-{version}",
            "source_commit": commit,
            "dirty": dirty,
        }, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Prepared Nexus {version} build identity")


if __name__ == "__main__":
    main()
