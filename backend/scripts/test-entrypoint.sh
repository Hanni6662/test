#!/bin/sh
set -eu

export IMAGE_BACKEND_DATABASE_URL="$IMAGE_BACKEND_TEST_MIGRATION_DATABASE_URL"
alembic upgrade head
alembic upgrade head

python -m pytest -p no:cacheprovider -q tests/unit tests/integration
