#!/usr/bin/env bash
set -eu

# Values are generated from URL-safe random characters by scripts/init-local-env.ps1.
# Do not enable shell tracing here: the role passwords are process environment values.
: "${IMAGE_BACKEND_DATABASE_NAME:?IMAGE_BACKEND_DATABASE_NAME is required}"
: "${IMAGE_BACKEND_MIGRATOR_PASSWORD:?IMAGE_BACKEND_MIGRATOR_PASSWORD is required}"
: "${IMAGE_BACKEND_RUNTIME_PASSWORD:?IMAGE_BACKEND_RUNTIME_PASSWORD is required}"

for credential in "$IMAGE_BACKEND_MIGRATOR_PASSWORD" "$IMAGE_BACKEND_RUNTIME_PASSWORD"; do
  case "$credential" in
    *[!A-Za-z0-9_-]*|'') echo "Database passwords must be 64 URL-safe characters" >&2; exit 1 ;;
  esac
  if [ "${#credential}" -ne 64 ]; then
    echo "Database passwords must be 64 URL-safe characters" >&2
    exit 1
  fi
done

case "$IMAGE_BACKEND_DATABASE_NAME" in
  image_backend|image_backend_test) ;;
  *) echo "Unsupported business database name" >&2; exit 1 ;;
esac

psql --set=ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE ROLE image_backend_migrator LOGIN PASSWORD '$IMAGE_BACKEND_MIGRATOR_PASSWORD';
CREATE ROLE image_backend_runtime LOGIN PASSWORD '$IMAGE_BACKEND_RUNTIME_PASSWORD';
CREATE DATABASE $IMAGE_BACKEND_DATABASE_NAME OWNER image_backend_migrator ENCODING 'UTF8';
REVOKE ALL ON DATABASE $IMAGE_BACKEND_DATABASE_NAME FROM PUBLIC;
GRANT CONNECT ON DATABASE $IMAGE_BACKEND_DATABASE_NAME TO image_backend_runtime;
SQL

psql --set=ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$IMAGE_BACKEND_DATABASE_NAME" <<'SQL'
ALTER SCHEMA public OWNER TO image_backend_migrator;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO image_backend_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE image_backend_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO image_backend_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE image_backend_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO image_backend_runtime;
SQL
