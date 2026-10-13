from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import app.main  # noqa: F401
from app.db.base import Base


def test_generation_models_are_registered_in_application_metadata() -> None:
    assert "mock_generation_jobs" in Base.metadata.tables
    assert "mock_generation_invocations" in Base.metadata.tables


def test_generation_migration_is_the_single_successor_to_task_actions() -> None:
    migration_path = (
        Path(__file__).resolve().parents[3]
        / "migrations"
        / "versions"
        / "s008_mock_generation.py"
    )
    spec = spec_from_file_location("s008_mock_generation_unit_test", migration_path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.revision == "s008_mock_generation"
    assert migration.down_revision == "s007_task_actions"
