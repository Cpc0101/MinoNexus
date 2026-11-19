#!/usr/bin/env bash
set -Eeuo pipefail

version=${1:?usage: deploy-tencent.sh VERSION IMAGE_TAG DEPLOY_DIR}
image_tag=${2:?usage: deploy-tencent.sh VERSION IMAGE_TAG DEPLOY_DIR}
deploy_dir=${3:?usage: deploy-tencent.sh VERSION IMAGE_TAG DEPLOY_DIR}
case "$version" in
  ''|*[!0-9A-Za-z.+-]*) echo "Invalid version: $version" >&2; exit 1 ;;
esac
image_sha=${image_tag#"$version-"}
if [ "$image_tag" = latest ] || [[ "$image_tag" != "$version-"* ]] || ! [[ "$image_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Invalid immutable image tag for version $version: $image_tag" >&2
  exit 1
fi
image="hkccr.ccs.tencentyun.com/mino/mino-nexus:$image_tag"
case "$deploy_dir" in
  /|*/|*[!A-Za-z0-9._/-]*) echo "Unsafe deploy directory: $deploy_dir" >&2; exit 1 ;;
esac

cd "$deploy_dir"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_dir="$deploy_dir/backups"
backup_file="$backup_dir/mino-data-$stamp.tar.gz"
previous_env=""
previous_compose=""
previous_version=""
previous_image_tag=""
container=""
updated=0
trap 'test -n "${backup_file:-}" && [ -f "$backup_file" ] && echo "Data backup: $backup_file"' EXIT

if [ -f .env ]; then
  previous_env="$backup_dir/env-$stamp"
  mkdir -p "$backup_dir"
  cp .env "$previous_env"
  previous_version=$(sed -n 's/^MINO_NEXUS_VERSION=//p' .env | tail -n 1)
  previous_image_tag=$(sed -n 's/^MINO_NEXUS_IMAGE_TAG=//p' .env | tail -n 1)
fi
if [ -f docker-compose.yml ]; then
  mkdir -p "$backup_dir"
  previous_compose="$backup_dir/docker-compose-$stamp.yml"
  cp docker-compose.yml "$previous_compose"
fi

cp docker-compose.yml.next docker-compose.yml.next.validated
if [ -f .env ]; then
  cp .env .env.next
else
  : > .env.next
fi
if grep -q '^MINO_NEXUS_VERSION=' .env.next; then
  sed -i "s/^MINO_NEXUS_VERSION=.*/MINO_NEXUS_VERSION=$version/" .env.next
else
  printf 'MINO_NEXUS_VERSION=%s\n' "$version" >> .env.next
fi
if grep -q '^MINO_NEXUS_IMAGE_TAG=' .env.next; then
  sed -i "s/^MINO_NEXUS_IMAGE_TAG=.*/MINO_NEXUS_IMAGE_TAG=$image_tag/" .env.next
else
  printf 'MINO_NEXUS_IMAGE_TAG=%s\n' "$image_tag" >> .env.next
fi
# Production traffic reaches Nexus through the same-host HTTPS reverse proxy.
# Normalize legacy .env files so an old public bind cannot override the safe
# loopback-only default from docker-compose.yml.
if grep -q '^MINO_BIND_ADDRESS=' .env.next; then
  sed -i 's/^MINO_BIND_ADDRESS=.*/MINO_BIND_ADDRESS=127.0.0.1/' .env.next
else
  printf 'MINO_BIND_ADDRESS=127.0.0.1\n' >> .env.next
fi

set_public_env() {
  key=$1
  value=$2
  case "$value" in
    https://*) ;;
    *) echo "$key must use an https:// URL: $value" >&2; return 1 ;;
  esac
  case "$value" in
    *[[:space:]]*) echo "$key contains whitespace: $value" >&2; return 1 ;;
  esac
  sed -i "/^${key}=/d" .env.next
  printf '%s=%s\n' "$key" "$value" >> .env.next
}

set_public_env MINO_NEXUS_PUBLIC_URL "${MINO_NEXUS_PUBLIC_URL:-https://nexus.minocc.top}"
set_public_env MINO_WORKBENCH_PUBLIC_URL "${MINO_WORKBENCH_PUBLIC_URL:-https://workbench.minocc.top}"
set_public_env MINO_SCOUT_MANIFEST_URL "${MINO_SCOUT_MANIFEST_URL:-https://nexus.minocc.top/scout-release/manifest.json}"

MINO_NEXUS_IMAGE_TAG="$image_tag" docker compose --env-file .env.next -f docker-compose.yml.next.validated config > /dev/null
if [ "${MINO_DEPLOY_SKIP_PULL:-0}" = "1" ]; then
  docker image inspect "$image" > /dev/null
else
  MINO_NEXUS_IMAGE_TAG="$image_tag" docker compose --env-file .env.next -f docker-compose.yml.next.validated pull nexus
fi

rollback() {
  status=$?
  trap - ERR
  set +e
  echo "Deployment failed; attempting rollback." >&2
  if [ -n "$previous_compose" ] && [ -f "$previous_compose" ]; then cp "$previous_compose" docker-compose.yml; fi
  if [ -n "$previous_env" ] && [ -f "$previous_env" ]; then cp "$previous_env" .env; fi
  if [ -n "$previous_image_tag" ]; then
    MINO_NEXUS_IMAGE_TAG="$previous_image_tag" docker compose up -d --no-build --pull never --wait --wait-timeout 180
    echo "Rollback attempted with immutable image tag $previous_image_tag." >&2
  elif [ -n "$previous_version" ]; then
    MINO_NEXUS_VERSION="$previous_version" docker compose up -d --no-build --pull never --wait --wait-timeout 180
    echo "Rollback attempted with legacy version $previous_version." >&2
  fi
  exit "$status"
}
trap rollback ERR

if [ -f docker-compose.yml ]; then
  container=$(docker compose ps -q nexus 2>/dev/null | head -n 1 || true)
fi
if [ -n "$container" ]; then
  docker compose stop nexus
  if docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$container" | grep -q .; then
    mkdir -p "$backup_dir"
    docker run --rm --volumes-from "$container" --entrypoint tar "$(docker inspect -f '{{.Config.Image}}' "$container")" \
      -czf - -C /data . > "$backup_file"
    test -s "$backup_file"
    gzip -t "$backup_file"
  fi
else
  mkdir -p "$backup_dir"
fi

mv docker-compose.yml.next.validated docker-compose.yml
mv .env.next .env
updated=1

MINO_NEXUS_IMAGE_TAG="$image_tag" docker compose up -d --no-build --pull never --wait --wait-timeout 180
test "$(docker compose ps -q nexus | xargs -r docker inspect --format '{{.Config.Image}}')" = "$image"
health=$(curl --fail --silent --show-error --retry 10 --retry-delay 3 --max-time 10 http://127.0.0.1:50104/health)
printf '%s' "$health" | grep -Fq "\"nexus_version\":\"$version\""
test "$(docker compose ps --status running --services | grep -c '^nexus$')" -eq 1
port_mapping=$(docker compose port nexus 50104 2>/dev/null | head -n 1)
test "$port_mapping" = "127.0.0.1:50104" || {
  echo "Refusing deployment: Nexus port mapping is '$port_mapping', expected 127.0.0.1:50104" >&2
  false
}
updated=0
trap - ERR
echo "Deployed Nexus $version using $image_tag to $deploy_dir"
