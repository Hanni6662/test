from __future__ import annotations

from uuid import uuid4

from app.db.operations_models import OperationsLoopHeartbeat
from app.services.operations_heartbeat import _build_upsert_statement
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import dialect


def test_counter_expressions_are_only_in_conflict_update_clause() -> None:
    statement = _build_upsert_statement(
        "dispatcher",
        uuid4(),
        {
            "last_heartbeat_at": func.clock_timestamp(),
            "iteration_count": OperationsLoopHeartbeat.iteration_count + 1,
            "successful_iteration_count": (OperationsLoopHeartbeat.successful_iteration_count + 1),
        },
        first_write_counters={"iteration_count": 1, "successful_iteration_count": 1},
    )
    sql = str(statement.compile(dialect=dialect()))
    insert_values, update_clause = sql.split(" ON CONFLICT ", maxsplit=1)

    assert "operations_loop_heartbeats.iteration_count" not in insert_values
    assert "operations_loop_heartbeats.successful_iteration_count" not in insert_values
    assert "operations_loop_heartbeats.iteration_count +" in update_clause
    assert "operations_loop_heartbeats.successful_iteration_count +" in update_clause
    assert "iteration_count" in insert_values
    assert "successful_iteration_count" in insert_values
