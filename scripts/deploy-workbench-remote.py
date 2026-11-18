#!/usr/bin/env python3
"""Deploy a validated Workbench archive into the nginx root serving its host."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

HOST = "workbench.minocc.top"


def run(argv: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def nginx_config() -> str:
    errors: list[str] = []
    for argv in (["nginx", "-T"], ["sudo", "-n", "nginx", "-T"]):
        try:
            return run(argv, capture=True).stdout
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            errors.append(f"{' '.join(argv)}: {exc}")
    raise RuntimeError("Cannot read nginx configuration: " + "; ".join(errors))


def tokens(text: str) -> list[str]:
    text = re.sub(r"(?m)#.*$", "", text)
    return re.findall(r"[{};]|[^\s{};]+", text)


def find_workbench_roots(text: str) -> list[str]:
    items = tokens(text)
    roots: list[str] = []
    index = 0
    while index < len(items):
        if items[index] != "server" or index + 1 >= len(items) or items[index + 1] != "{":
            index += 1
            continue
        depth = 1
        cursor = index + 2
        names: list[str] = []
        block_roots: list[str] = []
        while cursor < len(items) and depth:
            token = items[cursor]
            if token == "{":
                depth += 1
            elif token == "}":
                depth -= 1
            elif depth >= 1 and token == "server_name":
                end = cursor + 1
                while end < len(items) and items[end] not in {";", "{"}:
                    names.append(items[end])
                    end += 1
            elif depth >= 1 and token in {"root", "alias"}:
                if cursor + 1 < len(items) and items[cursor + 1] not in {";", "{", "}"}:
                    block_roots.append(items[cursor + 1].strip("'\""))
            cursor += 1
        if HOST in names:
            roots.extend(block_roots)
        index = max(cursor, index + 1)
    return roots


def choose_root(candidates: list[str]) -> Path:
    candidates = list(dict.fromkeys(candidates))
    if not candidates:
        raise RuntimeError(f"No nginx server block for {HOST}")
    for value in candidates:
        path = Path(value)
        if path.is_absolute() and (path / "index.html").is_file():
            return path.resolve()
    path = Path(candidates[0])
    if not path.is_absolute() or path == Path("/"):
        raise RuntimeError(f"Unsafe nginx root: {path}")
    return path


def privileged_prefix(root: Path) -> list[str]:
    parent = root.parent
    if os.access(parent, os.W_OK | os.X_OK) and os.access(root, os.W_OK | os.X_OK):
        return []
    run(["sudo", "-n", "true"])
    return ["sudo", "-n"]


def privileged(argv: list[str], prefix: list[str]) -> None:
    run(prefix + argv)


def extract_archive(archive: Path) -> Path:
    target = Path(tempfile.mkdtemp(prefix="mino-workbench-stage-"))
    with tarfile.open(archive, "r:gz") as bundle:
        root = target.resolve()
        for member in bundle.getmembers():
            destination = (target / member.name).resolve()
            if destination != root and root not in destination.parents:
                raise RuntimeError(f"Unsafe archive member: {member.name}")
            if not member.isfile() and not member.isdir():
                raise RuntimeError(f"Unsupported archive member type: {member.name}")
        bundle.extractall(target)
    index = target / "index.html"
    if not index.is_file():
        raise RuntimeError("Archive does not contain index.html")
    return target


def verify_local(expected_asset: str) -> str:
    html = run(
        [
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            "20",
            "--resolve",
            f"{HOST}:443:127.0.0.1",
            f"https://{HOST}/",
        ],
        capture=True,
    ).stdout
    if expected_asset not in html:
        raise RuntimeError(f"Live index does not reference expected asset {expected_asset}")
    if f"{HOST}:8443" in html:
        raise RuntimeError("Live index still references the legacy :8443 endpoint")
    return html


def rollback(root: Path, backup: Path, prefix: list[str]) -> None:
    if root.exists():
        privileged(["rm", "-rf", str(root)], prefix)
    privileged(["mv", str(backup), str(root)], prefix)
    print(f"ROLLED_BACK={root}")


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: deploy-workbench-remote.py ARCHIVE EXPECTED_INDEX_ASSET", file=sys.stderr)
        return 2
    archive = Path(sys.argv[1])
    expected_asset = sys.argv[2]
    root = choose_root(find_workbench_roots(nginx_config()))
    extracted = extract_archive(archive)
    built_index = (extracted / "index.html").read_text()
    if expected_asset not in built_index:
        raise RuntimeError(f"Archive index does not reference {expected_asset}")
    if f"{HOST}:8443" in built_index:
        raise RuntimeError("Archive index still references the legacy :8443 endpoint")

    prefix = privileged_prefix(root)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = root.parent / f".mino-workbench-backup-{stamp}-{os.getpid()}"
    stage = root.parent / f".mino-workbench-stage-{stamp}-{os.getpid()}"
    privileged(["mkdir", "-p", str(stage)], prefix)
    try:
        privileged(["cp", "-a", f"{extracted}/.", str(stage)], prefix)
        privileged(["chmod", "-R", "a+rX", str(stage)], prefix)
        privileged(["mv", str(root), str(backup)], prefix)
        try:
            privileged(["mv", str(stage), str(root)], prefix)
            verify_local(expected_asset)
        except Exception:
            rollback(root, backup, prefix)
            raise
    except Exception:
        if stage.exists():
            privileged(["rm", "-rf", str(stage)], prefix)
        raise
    finally:
        shutil.rmtree(extracted, ignore_errors=True)
    print(f"DEPLOYED_ROOT={root}")
    print(f"BACKUP_ROOT={backup}")
    print(f"EXPECTED_ASSET={expected_asset}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
