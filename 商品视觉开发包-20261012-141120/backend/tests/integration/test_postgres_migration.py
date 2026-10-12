from __future__ import annotations

import os
from uuid import UUID

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool


@pytest.mark.integration
def test_migrated_schema_policy_and_runtime_role() -> None:
    database_url = os.environ["IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL"]
    engine = create_engine(database_url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            version = connection.execute(text("SELECT current_setting('server_version_num')::int"))
            assert version.scalar_one() >= 180000

            table_names = set(inspect(connection).get_table_names(schema="public"))
            assert {
                "users",
                "sessions",
                "session_policies",
                "auth_identities",
                "roles",
                "permissions",
                "user_roles",
                "role_permissions",
                "quota_accounts",
                "credit_reservations",
                "credit_ledger",
                "balance_accounts",
                "recharge_orders",
                "balance_ledger",
                "reference_sets",
                "image_assets",
                "reference_set_images",
                "reference_upload_objects",
                "retention_policies",
                "cleanup_runs",
                "cleanup_jobs",
                "cleanup_targets",
                "alembic_version",
            } <= table_names

            policy = connection.execute(
                text("SELECT id, ttl_days, revision FROM public.session_policies")
            ).one()
            assert tuple(policy) == (1, 120, 1)
            retention_policy = connection.execute(
                text(
                    "SELECT id, task_retention_days, reference_retention_days, "
                    "recommendation_retention_days, revision "
                    "FROM public.retention_policies"
                )
            ).one()
            assert tuple(retention_policy) == (1, 60, 60, 60, 1)
            assert connection.execute(text("SELECT current_user")).scalar_one() == (
                "image_backend_runtime"
            )
            connection.commit()

            transaction = connection.begin()
            try:
                with pytest.raises(DBAPIError):
                    connection.execute(
                        text("CREATE TABLE public.runtime_must_not_own_ddl (id integer)")
                    )
            finally:
                transaction.rollback()

        marker_id = UUID("0ea1ea5d-d28c-44be-ad9d-3566ddfa6e01")
        with engine.connect() as connection:
            existing_marker = connection.execute(
                text("SELECT identity_kind FROM public.users WHERE id = :id"),
                {"id": marker_id},
            ).scalar_one_or_none()
        if os.environ.get("IMAGE_BACKEND_TEST_EXPECT_PERSISTED_USER", "false").lower() == "true":
            assert existing_marker == "guest"

        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO public.users (id, identity_kind, status) "
                    "VALUES (:id, 'guest', 'active') ON CONFLICT (id) DO NOTHING"
                ),
                {"id": marker_id},
            )
            assert (
                connection.execute(
                    text("SELECT identity_kind FROM public.users WHERE id = :id"),
                    {"id": marker_id},
                ).scalar_one()
                == "guest"
            )
    finally:
        engine.dispose()
