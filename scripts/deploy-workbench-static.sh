#!/usr/bin/env bash
set -Eeuo pipefail

version=${1:?usage: deploy-workbench-static.sh VERSION PACKAGE RELEASES_DIR ROOT SOURCE_COMMIT}
package=${2:?usage: deploy-workbench-static.sh VERSION PACKAGE RELEASES_DIR ROOT SOURCE_COMMIT}
releases_dir=${3:-/var/www/mino-workbench-releases}
root=${4:-/var/www/mino-workbench}
source_commit=${5:?expected full release source commit required}
validator="$(dirname "${BASH_SOURCE[0]}")/validate-workbench-release.py"

case "$version" in
  ''|*[!0-9A-Za-z.+-]*) echo "Invalid Workbench version: $version" >&2; exit 1 ;;
esac
for path in "$releases_dir" "$root"; do
  case "$path" in
    /|*/|*[!A-Za-z0-9._/-]*) echo "Unsafe Workbench path: $path" >&2; exit 1 ;;
  esac
done
package=$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$package")
test -f "$package" || { echo "Workbench package not found: $package" >&2; exit 1; }

stamp=$(date -u +%Y%m%dT%H%M%SZ)-$$
mkdir -p "$releases_dir"
exec 9>"$releases_dir/.deploy.lock"
flock -n 9 || { echo "Another Workbench deployment is active" >&2; exit 1; }
stage="$releases_dir/.stage-$version-$stamp-$$"
release="$releases_dir/$version"
previous_target=""
previous_legacy=""
switched=0
probe=$(mktemp)
pending="$root.next-$stamp"

cleanup() {
  status=$?
  trap - EXIT
  rm -f -- "$probe" "$pending"
  if [ -d "$stage" ]; then rm -rf -- "$stage"; fi
  if [ "$status" -ne 0 ] && [ "$switched" -eq 1 ]; then
    echo "Workbench deployment failed; restoring previous root." >&2
    if [ -n "$previous_target" ]; then
      pending="$root.restore-$stamp"
      rm -f -- "$pending"
      ln -s "$previous_target" "$pending"
      mv -Tf -- "$pending" "$root"
    elif [ -n "$previous_legacy" ]; then
      rm -f -- "$root"
      mv -- "$previous_legacy" "$root"
    else
      rm -f -- "$root"
    fi
    nginx -t >/dev/null 2>&1 || true
    systemctl reload nginx >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT

mkdir -p "$stage"
python3 "$validator" package "$package" "$version" "$source_commit" "$stage"

if [ -e "$release" ] || [ -L "$release" ]; then
  test -d "$release" && test ! -L "$release" || { echo "Unsafe existing release: $release" >&2; exit 1; }
  python3 "$validator" directory "$release" "$version" "$source_commit"
  cmp -s "$stage/BUILD.json" "$release/BUILD.json" || { echo "Immutable Workbench release already exists with different metadata: $release" >&2; exit 1; }
else
  mv -- "$stage" "$release"
fi

if [ -L "$root" ]; then
  previous_target=$(readlink "$root")
elif [ -d "$root" ]; then
  previous_legacy="$releases_dir/.legacy-$stamp"
  mv -- "$root" "$previous_legacy"
  switched=1
elif [ -e "$root" ]; then
  echo "Unsafe existing Workbench root: $root" >&2
  exit 1
fi

pending="$root.next-$stamp"
rm -f -- "$pending"
ln -s "$release" "$pending"
mv -Tf -- "$pending" "$root"
switched=1

nginx -t
systemctl reload nginx
curl --fail --silent --show-error --retry 8 --retry-all-errors --retry-delay 2 \
  --max-time 20 --resolve workbench.minocc.top:443:127.0.0.1 \
  https://workbench.minocc.top/BUILD.json > "$probe"
python3 "$validator" published "$probe" "$version" "$source_commit" "$release/BUILD.json"

switched=0
echo "Workbench $version deployed to $root -> $release"
