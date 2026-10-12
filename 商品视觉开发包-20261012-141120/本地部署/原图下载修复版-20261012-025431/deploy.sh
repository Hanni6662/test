#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
action=${1:?Usage: sh deploy.sh ACTION localimg-INSTANCE [PORT|BACKUP_NAME]}
instance=${2:?Instance required}
case "$instance" in localimg-*) ;; *) echo 'Instance must start with localimg-' >&2; exit 1;; esac
case "$instance" in *[!a-z0-9-]*) echo 'Unsafe instance name' >&2; exit 1;; esac
delivery_root=$(pwd)
env_file="instances/$instance.env"
python_image='public.ecr.aws/docker/library/python:3.12.15-slim-bookworm@sha256:7753c33391fc9f01d1984375bf375eb6686d52ba10db6043a86634a5ccf90dcf'
postgres_image='public.ecr.aws/docker/library/postgres:18.6-bookworm@sha256:7d3852670a11f0cb9cb1cd50916dc02a0021143fb22d96b78eeef26348abe55c'
rabbit_image='public.ecr.aws/docker/library/rabbitmq:4.3.6-management-alpine@sha256:3ef7f7e89fed709a78f79b8b5f4e30e1312a1d4089da752e23595566d9d61a95'
compose() {
 if [ -f images/offline-mode ]; then
  docker compose --env-file "$env_file" -p "$instance" -f compose.yaml -f images/compose.offline.yaml "$@"
 else
  docker compose --env-file "$env_file" -p "$instance" -f compose.yaml "$@"
 fi
}
tools() { compose --profile tools run --rm --no-deps -T tools "$@"; }
fresh() {
  for suffix in private ingress; do
    if docker network inspect "${instance}_$suffix" >/dev/null 2>&1; then
      echo 'Target has existing networks; use a fresh instance.' >&2; exit 1
    fi
  done
  for suffix in database_data queue_data private_data; do
    if docker volume inspect "${instance}_$suffix" >/dev/null 2>&1; then
      echo 'Target has existing volumes; init/restore refused. Use a fresh instance.' >&2; exit 1
    fi
  done
  if [ -n "$(docker ps -aq --filter "label=com.docker.compose.project=$instance")" ]; then
    echo 'Target has existing containers; use a fresh instance.' >&2; exit 1
  fi
}
if [ -f images/offline-mode ]; then
  python_image=$(cat images/python-image-id.txt)
  case "$python_image" in sha256:*) ;; *) echo 'Invalid offline Python image ID' >&2; exit 1;; esac
fi
if [ "$action" = init ]; then
  [ ! -e "$env_file" ] || { echo 'Existing credentials preserved; init refused.' >&2; exit 1; }
  fresh
  occupied_subnets=$(docker network inspect $(docker network ls -q) --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}')
  docker run --rm --network none --mount "type=bind,source=$delivery_root,target=/delivery" "$python_image" python /delivery/scripts/init_env.py "$instance" "${3:-18081}" "$occupied_subnets"
  exit
fi
if [ "$action" = import ]; then
  expected_hash=$(cat images/archive-sha256.txt)
  actual_hash=$(sha256sum images/local-private-amd64.tar | cut -d ' ' -f 1)
  [ "$actual_hash" = "$expected_hash" ] || { echo 'Offline archive checksum mismatch' >&2; exit 1; }
  docker load -i images/local-private-amd64.tar
  printf '%s\n' 'Use immutable local image IDs' > images/offline-mode
  exit
fi
[ -f "$env_file" ] || { echo 'Missing instance env. Run init first.' >&2; exit 1; }
docker run --rm --network none --mount "type=bind,source=$delivery_root,target=/delivery,readonly" "$python_image" python /delivery/scripts/validate_env.py "$instance"
# The validator accepts only literal safe values and the exact required keys.
set -a
. "./$env_file"
set +a
compose config -q
bundle=${3:-}
case "$action" in
 check) echo 'Configuration ready, local mock only';;
 build)
  if ! docker image inspect "$APP_IMAGE" >/dev/null 2>&1; then compose build migrate; fi
  docker run --rm --network none --mount "type=bind,source=$delivery_root,target=/delivery,readonly" "$APP_IMAGE" python /delivery/scripts/verify_image.py;;
 up) compose up -d --no-build --pull never --wait --wait-timeout 240;;
 stop) compose stop --timeout 60;;
 health)
  compose ps -a
  compose exec -T api python -c "from urllib.request import urlopen; print(urlopen('http://127.0.0.1:8000/health/ready',timeout=3).read().decode())"
  compose exec -T worker python -m app.workers.healthcheck
  compose exec -T dispatcher python /opt/local/loop_health.py dispatcher
  compose exec -T scheduler python /opt/local/loop_health.py scheduler
  compose run --rm --no-deps -T migrate alembic current;;
 backup|restore)
  case "$bundle" in ''|*[!a-zA-Z0-9_-]*) echo 'A safe backup name is required' >&2; exit 1;; esac
  if [ "$action" = backup ]; then
    [ ! -e "backups/$bundle" ] || { echo 'Backup exists; overwrite refused' >&2; exit 1; }
    compose stop --timeout 60 api worker dispatcher scheduler
    tools pack "$bundle"
    compose exec -T db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U image_backend_admin -d image_backend -Fc --no-owner --no-acl -f /tmp/local-private-backup.dump'
    docker cp "$(compose ps -q db):/tmp/local-private-backup.dump" "backups/$bundle/database.dump"
    compose exec -T db rm /tmp/local-private-backup.dump
    tools seal "$bundle"
    echo 'Verified paired backup. Business processes remain stopped; use up to resume.'
  else
    fresh
    compose up -d --no-build --pull never --wait --wait-timeout 180 db rabbitmq
    tools validate "$bundle"
    tools empty "$bundle"
    tools unpack "$bundle"
    docker cp "backups/$bundle/database.dump" "$(compose ps -q db):/tmp/local-private-restore.dump"
    compose exec -T db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_restore -U image_backend_admin -d image_backend --role=image_backend_migrator --no-owner --no-privileges --exit-on-error --single-transaction /tmp/local-private-restore.dump'
    compose exec -T db rm /tmp/local-private-restore.dump
    tools verify "$bundle"
    compose up -d --no-build --pull never --wait --wait-timeout 240
  fi;;
 export)
  if [ -f images/offline-mode ]; then
    postgres_image=$(sed -n '3s/ .*//p' images/image-ids.txt)
    rabbit_image=$(sed -n '4s/ .*//p' images/image-ids.txt)
  fi
  docker save -o images/local-private-amd64.tar "$APP_IMAGE" "$python_image" "$postgres_image" "$rabbit_image"
  docker image inspect "$APP_IMAGE" "$python_image" "$postgres_image" "$rabbit_image" --format '{{.Id}} {{json .RepoTags}}' > images/image-ids.txt
  docker run --rm --network none --mount "type=bind,source=$delivery_root/images,target=/images" "$python_image" python -c "import hashlib,pathlib; p=pathlib.Path('/images/local-private-amd64.tar'); h=hashlib.file_digest(p.open('rb'),'sha256').hexdigest(); pathlib.Path('/images/archive-sha256.txt').write_text(h+'\n')"
  docker run --rm --network none --mount "type=bind,source=$delivery_root,target=/delivery" "$python_image" python /delivery/scripts/offline_manifest.py;;
 *) echo 'Unknown action' >&2; exit 1;;
esac
