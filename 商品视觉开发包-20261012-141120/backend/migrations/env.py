from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from app.core.config import get_settings
from app.db import (  # noqa: F401
    action_models,
    auth_reserved_models,
    business_question_models,
    business_session_models,
    chatflow_generation_models,
    chatflow_reference_files,
    chatflow_v34_models,
    generation_models,
    models,
    operations_models,
    outbox_models,
    prompt_models,
    recommendation_models,
    reference_models,
    retention_models,
    task_models,
)
from app.db.base import Base
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
database_url = settings.database_url.get_secret_value()
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(database_url, poolclass=NullPool, pool_pre_ping=True)
    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                transaction_per_migration=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
