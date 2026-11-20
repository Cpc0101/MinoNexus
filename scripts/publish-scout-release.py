#!/usr/bin/env python3
"""Validate a complete Scout release before atomically publishing its manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

REPO = "https://github.com/Cpc0101/MinoScout/releases/download/"
PLATFORMS = {("darwin", "arm64"), ("linux", "x64"), ("win32", "x64")}
PUBLIC_RELEASE_PATH = "/scout-release"


def version(value):
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("invalid_release_version")
    return tuple(map(int, value.split(".")))


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def validate_public_url(value):
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid_public_release_url") from exc
    if (parsed.scheme != "https" or not parsed.hostname or port != 8443
            or parsed.path != PUBLIC_RELEASE_PATH or parsed.query or parsed.fragment):
        raise ValueError("invalid_public_release_url")
    return value


def fetch_asset(path, url):
    partial = path.with_name(path.name + ".part")
    subprocess.run(
        ["curl", "--fail", "--location", "--silent", "--show-error", "--retry", "4",
         "--retry-all-errors", "--output", str(partial), url],
        check=True,
    )
    partial.replace(path)


def validate(source, tag, *, fetch_missing=False):
    source = Path(source).resolve()
    want = tag.removeprefix("v")
    version(want)
    if tag != "v" + want:
        raise ValueError("invalid_release_tag")
    manifest = json.loads((source / "manifest.json").read_text())
    commit = str(manifest.get("source_commit") or "")
    if (manifest.get("component") != "scout" or manifest.get("version") != want
            or not re.fullmatch(r"[0-9a-f]{40}", commit)
            or manifest.get("build_id") != want + "-" + commit[:12]):
        raise ValueError("release_build_identity_mismatch")
    items = manifest.get("items") or []
    if len(items) != 3 or {(r.get("os"), r.get("arch")) for r in items} != PLATFORMS:
        raise ValueError("release_platforms_incomplete")
    files = {}
    for item in items:
        if item.get("version") != want or set(item.get("layers") or {}) != {"app", "runtime", "browser"}:
            raise ValueError("release_layers_incomplete")
        layers = item["layers"]
        if (layers["app"].get("key") != want
                or not layers["runtime"].get("key") or not layers["browser"].get("key")
                or layers["app"].get("requires_runtime") != layers["runtime"]["key"]
                or layers["app"].get("requires_browser") != layers["browser"]["key"]):
            raise ValueError("release_layer_dependencies_mismatch")
        for row in (item, *item["layers"].values()):
            name = str(row.get("filename") or "")
            expected = str(row.get("sha256") or "")
            if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.zip", name)
                    or row.get("url") != REPO + tag + "/" + name
                    or not re.fullmatch(r"[0-9a-f]{64}", expected)):
                raise ValueError("release_asset_invalid")
            path = source / name
            if fetch_missing and not path.exists():
                fetch_asset(path, str(row["url"]))
            if path.is_symlink() or not path.is_file() or path.stat().st_size != row.get("bytes") or digest(path) != expected:
                raise ValueError("release_asset_checksum_mismatch: " + name)
            if name in files and files[name] != expected:
                raise ValueError("release_asset_identity_conflict")
            files[name] = expected
    for name in ("bootstrap.sh", "bootstrap.ps1"):
        path = source / name
        if path.is_symlink() or not path.is_file() or not path.stat().st_size:
            raise ValueError("release_bootstrap_missing")
    return manifest, files


def publish(source, destination, tag, public_url):
    manifest, files = validate(source, tag, fetch_missing=True)
    source, destination = Path(source).resolve(), Path(destination).resolve()
    validate_public_url(public_url)
    destination.mkdir(parents=True, exist_ok=True)
    current = destination / "manifest.json"
    if current.exists():
        old = json.loads(current.read_text())
        if version(str(old.get("version") or "")) > version(manifest["version"]):
            raise ValueError("release_downgrade_rejected")
        if old.get("version") == manifest["version"] and old.get("build_id") != manifest["build_id"]:
            raise ValueError("release_version_is_immutable")
    for item in manifest["items"]:
        for row in (item, *item["layers"].values()):
            row["url"] = public_url + "/" + tag + "/" + row["filename"]
    encoded = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    release = destination / tag
    if release.exists():
        if release.is_symlink() or (release / "manifest.json").read_text() != encoded:
            raise ValueError("published_release_conflict")
        if any(digest(release / name) != expected for name, expected in files.items()):
            raise ValueError("published_release_corrupt")
    else:
        stage = Path(tempfile.mkdtemp(prefix=".stage-" + tag + "-", dir=destination))
        try:
            for name in (*files, "bootstrap.sh", "bootstrap.ps1"):
                try:
                    os.link(source / name, stage / name)
                except OSError:
                    shutil.copy2(source / name, stage / name)
            (stage / "manifest.json").write_text(encoded)
            os.chmod(stage, 0o755)
            stage.rename(release)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    # All immutable packages are now in place; publish the mutable entry last.
    for name in ("bootstrap.sh", "bootstrap.ps1", "manifest.json"):
        pending = destination / ("." + name + "." + tag + ".next")
        with pending.open("wb") as stream:
            stream.write((release / name).read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(pending, 0o644)
        pending.replace(destination / name)
    print(json.dumps({"version": manifest["version"], "build_id": manifest["build_id"], "assets": len(files)}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--destination")
    parser.add_argument("--public-url", default="https://nexus.minocc.top:8443/scout-release")
    args = parser.parse_args()
    if args.destination:
        publish(args.source, args.destination, args.release_tag, args.public_url)
    else:
        manifest, files = validate(args.source, args.release_tag)
        print(json.dumps({"version": manifest["version"], "build_id": manifest["build_id"], "assets": len(files)}))


if __name__ == "__main__":
    main()
