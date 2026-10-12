from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from app.core.domain_errors import DomainError
from app.db.business_session_models import BusinessSession
from app.db.task_models import Task
from tests.integration.business_sessions.test_business_sessions_postgres import (
    TRUSTED_ORIGIN,
    _create_reference_set,
    _new_guest,
    _selection_request,
)
from tests.integration.business_sessions.test_chatflow_v34_sessions_postgres import (
    _prepare_v34_selection_ready,
    v34_pg_engine,
)


def completed_handoff(engine, status):
    client, owner, _, session_id, recommendation_id, _, _ = _prepare_v34_selection_ready(
        engine, idempotency_key=f"capacity-start-{uuid4()}"
    )
    service = client.app.state.business_session_service
    service.options = replace(service.options, max_active_per_owner=1)
    response = client.post(
        f"/api/v1/business-sessions/{session_id}/selection",
        json=_selection_request(recommendation_id).model_dump(mode="json"),
        headers={"Origin": TRUSTED_ORIGIN, "Idempotency-Key": f"capacity-select-{uuid4()}"},
    )
    assert response.status_code == 202, response.text
    with Session(engine) as db, db.begin():
        task = db.get(Task, UUID(response.json()["task_id"]))
        task.status = status
    return client, owner, session_id, service


@pytest.mark.parametrize("status", ["succeeded", "partially_succeeded", "failed"])
def test_terminal_task_releases_owner_capacity_and_keeps_replay(v34_pg_engine, status):
    _, owner, _, service = completed_handoff(v34_pg_engine, status)
    ref = _create_reference_set(v34_pg_engine, owner)
    key = f"next-after-terminal-{uuid4()}"
    first = service.create_session(owner_user_id=owner, reference_set_id=ref, idempotency_key=key)
    replay = service.create_session(owner_user_id=owner, reference_set_id=ref, idempotency_key=key)
    assert replay.business_session_id == first.business_session_id
    assert replay.status == "replayed"
    with pytest.raises(DomainError) as blocked:
        service.create_session(owner_user_id=owner, reference_set_id=_create_reference_set(v34_pg_engine, owner),
                               idempotency_key=f"still-one-active-{uuid4()}")
    assert blocked.value.code == "BUSINESS_SESSION_LIMIT_REACHED"


@pytest.mark.parametrize("status", ["queued", "prompting", "generating", "reconciling"])
def test_unfinished_task_still_occupies_owner_capacity(v34_pg_engine, status):
    _, owner, _, service = completed_handoff(v34_pg_engine, status)
    with pytest.raises(DomainError) as blocked:
        service.create_session(owner_user_id=owner, reference_set_id=_create_reference_set(v34_pg_engine, owner),
                               idempotency_key=f"blocked-{uuid4()}")
    assert blocked.value.code == "BUSINESS_SESSION_LIMIT_REACHED"


@pytest.mark.parametrize("status", ["succeeded", "partially_succeeded", "failed"])
def test_terminal_task_releases_stale_reconciling_session(v34_pg_engine, status):
    _, owner, session_id, service = completed_handoff(v34_pg_engine, status)
    with Session(v34_pg_engine) as db, db.begin():
        db.get(BusinessSession, session_id).status = "reconciling"
    created = service.create_session(owner_user_id=owner, reference_set_id=_create_reference_set(v34_pg_engine, owner),
                                     idempotency_key=f"stale-session-{uuid4()}")
    assert created.business_session_id


def test_reconciling_session_with_unknown_task_still_blocks(v34_pg_engine):
    _, owner, session_id, service = completed_handoff(v34_pg_engine, "reconciling")
    with Session(v34_pg_engine) as db, db.begin():
        db.get(BusinessSession, session_id).status = "reconciling"
    with pytest.raises(DomainError) as blocked:
        service.create_session(owner_user_id=owner, reference_set_id=_create_reference_set(v34_pg_engine, owner),
                               idempotency_key=f"unknown-session-{uuid4()}")
    assert blocked.value.code == "BUSINESS_SESSION_LIMIT_REACHED"


def test_parallel_new_sessions_after_terminal_task_keep_one_slot(v34_pg_engine):
    _, owner, _, service = completed_handoff(v34_pg_engine, "succeeded")
    refs = [_create_reference_set(v34_pg_engine, owner) for _ in range(2)]

    def create(ref):
        try:
            service.create_session(owner_user_id=owner, reference_set_id=ref, idempotency_key=f"parallel-{uuid4()}")
            return "accepted"
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(create, refs))
    assert sorted(outcomes) == ["BUSINESS_SESSION_LIMIT_REACHED", "accepted"]
    _, other_owner = _new_guest(v34_pg_engine)
    other = service.create_session(owner_user_id=other_owner, reference_set_id=_create_reference_set(v34_pg_engine, other_owner),
                                   idempotency_key=f"other-owner-{uuid4()}")
    assert other.business_session_id
