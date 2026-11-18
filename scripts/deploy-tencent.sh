#!/usr/bin/env bash
set -Eeuo pipefail

version=${1:?usage: deploy-tencent.sh VERSION DEPLOY_DIR}
deploy_dir=${2:?usage: deploy-tencent.sh VERSION DEPLOY_DIR}
case "$version" in
  ''|*[!0-9A-Za-z.+-]*) echo "Invalid version: $version" >&2; exit 1 ;;
esac
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
container=""
updated=0
trap 'test -n "${backup_file:-}" && [ -f "$backup_file" ] && echo "Data backup: $backup_file"' EXIT

if [ -f .env ]; then
  previous_env="$backup_dir/env-$stamp"
  mkdir -p "$backup_dir"
  cp .env "$previous_env"
  previous_version=$(sed -n 's/^MINO_NEXUS_VERSION=//p' .env | tail -n 1)
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

MINO_NEXUS_VERSION="$version" docker compose --env-file .env.next -f docker-compose.yml.next.validated config > /dev/null
if [ "${MINO_DEPLOY_SKIP_PULL:-0}" = "1" ]; then
  docker image inspect "ghcr.io/cpc0101/mino-nexus:$version" > /dev/null
else
  MINO_NEXUS_VERSION="$version" docker compose --env-file .env.next -f docker-compose.yml.next.validated pull nexus
fi

rollback() {
  status=$?
  trap - ERR
  set +e
  echo "Deployment failed; attempting rollback." >&2
  if [ -n "$previous_compose" ] && [ -f "$previous_compose" ]; then cp "$previous_compose" docker-compose.yml; fi
  if [ -n "$previous_env" ] && [ -f "$previous_env" ]; then cp "$previous_env" .env; fi
  if [ -n "$previous_version" ]; then
    MINO_NEXUS_VERSION="$previous_version" docker compose up -d --no-build --pull never --wait --wait-timeout 180
    echo "Rollback attempted with version $previous_version." >&2
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

MINO_NEXUS_VERSION="$version" docker compose up -d --no-build --pull never --wait --wait-timeout 180
health=$(curl --fail --silent --show-error --retry 10 --retry-delay 3 --max-time 10 http://127.0.0.1:50104/health)
printf '%s' "$health" | grep -Fq "\"nexus_version\":\"$version\""
test "$(docker compose ps --status running --services | grep -c '^nexus$')" -eq 1
updated=0
trap - ERR
echo "Deployed Nexus $version to $deploy_dir"
