#!/usr/bin/env bash
set -Eeuo pipefail

config_arg=${1:-/etc/nginx/sites-enabled/mino}
server_name=${2:-nexus.minocc.top}
alias_path=${3:-/var/www/scout-release}
https_port=${4:-443}
config=$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$config_arg")
nginx_bin=${NGINX_BIN:-nginx}
reload_command=${NGINX_RELOAD_COMMAND:-}
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup=""
candidate=""
installed=0

case "$server_name" in
  ''|*[!A-Za-z0-9.-]*) echo "Unsafe server name: $server_name" >&2; exit 1 ;;
esac
case "$alias_path" in
  /|*/|*[!A-Za-z0-9._/-]*) echo "Unsafe release asset path: $alias_path" >&2; exit 1 ;;
esac
case "$https_port" in
  ''|*[!0-9]*) echo "Unsafe HTTPS port: $https_port" >&2; exit 1 ;;
esac
if [ "$https_port" -lt 1 ] || [ "$https_port" -gt 65535 ]; then
  echo "Unsafe HTTPS port: $https_port" >&2
  exit 1
fi

test -f "$config" || { echo "Nginx config not found: $config" >&2; exit 1; }
test -d "$alias_path" || { echo "Scout release directory not found: $alias_path" >&2; exit 1; }
command -v "$nginx_bin" > /dev/null

reload_nginx() {
  if [ -n "$reload_command" ]; then
    sh -c "$reload_command"
  else
    systemctl reload nginx
  fi
}

cleanup() {
  status=$?
  trap - EXIT
  test -z "$candidate" || rm -f "$candidate"
  if [ "$status" -ne 0 ] && [ "$installed" -eq 1 ] && [ -n "$backup" ] && [ -f "$backup" ]; then
    echo "Restoring previous nginx config after failure." >&2
    cp -p "$backup" "$config"
    "$nginx_bin" -t > /dev/null 2>&1 || true
    reload_nginx > /dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT

candidate=$(mktemp "${config}.next.XXXXXX")
cp -p "$config" "$candidate"
python3 - "$candidate" "$server_name" "${alias_path%/}/" "$https_port" <<'PY'
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
server_name = sys.argv[2]
alias_path = sys.argv[3]
https_port = sys.argv[4]
text = path.read_text(encoding="utf-8")


def matching_brace(source: str, opening: int) -> int:
    depth = 0
    quote = ""
    escaped = False
    comment = False
    for index in range(opening, len(source)):
        char = source[index]
        if comment:
            if char == "\n":
                comment = False
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char == "#":
            comment = True
        elif char in {'"', "'"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    raise SystemExit(f"Unclosed block starting at offset {opening}")


targets = []
for match in re.finditer(r"(?m)^[ \t]*server\s*\{", text):
    end = matching_brace(text, match.end() - 1)
    block = text[match.start():end + 1]
    names = re.search(r"(?m)^[ \t]*server_name\s+([^;]+);", block)
    listen = re.search(
        rf"(?m)^[ \t]*listen\s+(?:\[::\]:)?{re.escape(https_port)}\s+ssl(?:\s+[^;]*)?;",
        block,
    )
    if names and listen and server_name in names.group(1).split():
        targets.append((match.start(), end, block))

if len(targets) != 1:
    raise SystemExit(
        f"Expected one HTTPS server block for {server_name}:{https_port}, found {len(targets)}"
    )

start, end, block = targets[0]
locations = list(re.finditer(r"(?m)^[ \t]*location\s+/scout-release/\s*\{", block))
if len(locations) > 1:
    raise SystemExit("Found duplicate /scout-release/ locations")
if locations:
    opening = locations[0].end() - 1
    closing = matching_brace(block, opening)
    body = block[opening + 1:closing]
    if not re.search(rf"(?m)^[ \t]*alias\s+{re.escape(alias_path)}\s*;", body):
        raise SystemExit(
            f"/scout-release/ exists but does not alias {alias_path}; refusing to rewrite it"
        )
    print("Scout release route already configured")
    raise SystemExit(0)

root_location = re.search(r"(?m)^[ \t]*location\s+/\s*\{", block)
if not root_location:
    raise SystemExit(
        f"HTTPS server block for {server_name}:{https_port} has no root location"
    )

indent = re.match(r"[ \t]*", root_location.group(0)).group(0)
snippet = (
    f"{indent}location /scout-release/ {{\n"
    f"{indent}    alias {alias_path};\n"
    f"{indent}    default_type application/octet-stream;\n"
    f"{indent}    add_header Cache-Control \"no-cache\";\n"
    f"{indent}}}\n\n"
)
absolute_insert = start + root_location.start()
updated = text[:absolute_insert] + snippet + text[absolute_insert:]
path.write_text(updated, encoding="utf-8")
print(f"Added /scout-release/ to the HTTPS server block on port {https_port}")
PY

if cmp -s "$config" "$candidate"; then
  exit 0
fi

backup="${config}.mino-backup-${stamp}"
cp -p "$config" "$backup"
cp -p "$candidate" "$config"
installed=1
"$nginx_bin" -t
reload_nginx
installed=0
echo "Updated nginx config: $config"
echo "Previous nginx config: $backup"
