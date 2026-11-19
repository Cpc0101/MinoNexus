#!/usr/bin/env python3
"""Remove the temporary direct-public-IP Nginx virtual host safely."""
from __future__ import annotations

import datetime as dt
import pathlib
import shutil
import subprocess
import sys

CONFIG = pathlib.Path("/etc/nginx/conf.d/mino-ip-backend.conf")
BACKUP_ROOT = pathlib.Path("/var/backups/mino-nginx")


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def main() -> int:
    if not CONFIG.exists():
        print(f"No direct-IP Nginx config to remove: {CONFIG}")
        return 0

    text = CONFIG.read_text(encoding="utf-8")
    if "server_name 49.233.143.114;" not in text or "default_server" not in text:
        raise SystemExit(f"Refusing to remove unexpected Nginx config: {CONFIG}")

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = BACKUP_ROOT / stamp
    backup_dir.mkdir(parents=True, mode=0o700)
    backup = backup_dir / CONFIG.name
    shutil.copy2(CONFIG, backup)

    CONFIG.unlink()
    try:
        run("nginx", "-t")
        run("systemctl", "reload", "nginx")
    except BaseException:
        shutil.copy2(backup, CONFIG)
        subprocess.run(["nginx", "-t"], check=False)
        subprocess.run(["systemctl", "reload", "nginx"], check=False)
        raise

    print(f"Removed direct-public-IP Nginx config: {CONFIG}")
    print(f"Rollback copy: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
