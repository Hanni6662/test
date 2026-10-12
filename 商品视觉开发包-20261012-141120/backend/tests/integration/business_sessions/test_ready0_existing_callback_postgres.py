from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from app.api.internal_dify import create_internal_dify_router
from app.api.tasks import router as tasks_router
from app.db.business_session_models import BusinessSession
from app.db.chatflow_generation_models import ChatflowGenerationRequest, ChatflowPromptPackage
from app.db.chatflow_v34_models import ChatflowV34StageEventReceipt
from app.db.task_models import Task, TaskItem
from app.schemas.chatflow_v34 import (
    V34_FLOW_VERSION,
    V34_STAGE_CONTRACT,
    ChatflowV34StageEvent,
    chatflow_v34_stage_event_digest,
)
from app.services.chatflow_v34_events import ChatflowV34StageEventService
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session
from tests.integration.business_sessions.test_business_sessions_postgres import (
    TEST_INTERNAL_CALLBACK_SECRET,
    _new_guest,
)
from tests.integration.business_sessions.test_chatflow_v34_sessions_postgres import (
    _accept_v34_selection_evidence,
    _prepare_v34_selection_ready,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_V8_PATH = _REPO_ROOT / "chatflow/work-copies/v34-backend-handoff-v8-20261009.yml"
_V8_SHA256 = "f3c5d84dfefcffbfd2695081dd20f7b0e3606521a5679fecb2067280e1132861"
_REVIEW_MARKER = "private-review-phrase-must-not-reach-owner-dto"


@pytest.fixture
def ready0_pg_engine() -> Iterator[Engine]:
    database_url = os.getenv("IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL")
    if database_url is None:
        pytest.skip("requires the dedicated image-backend-test PostgreSQL runtime URL")
    url = make_url(database_url)
    if url.database != "image_backend_test":
        pytest.fail("refusing to run the ready_0 integration test outside image_backend_test")
    engine = create_engine(database_url, pool_pre_ping=True)
    required_tables = {
        "users",
        "sessions",
        "session_policies",
        "retention_policies",
        "reference_sets",
        "image_assets",
        "reference_set_images",
        "recommendations",
        "business_sessions",
        "outbox_events",
        "tasks",
        "task_items",
        "task_idempotency_records",
        "task_recommendation_consumptions",
        "task_actions",
        "chatflow_v34_session_state",
        "chatflow_v34_stage_event_receipts",
        "chatflow_prompt_packages",
        "chatflow_prompt_images",
        "chatflow_prompt_references",
        "chatflow_generation_requests",
        "chatflow_generation_request_slots",
        "chatflow_generation_permits",
        "chatflow_generation_tracking",
        "chatflow_generation_slot_states",
        "chatflow_generation_event_receipts",
    }
    if not required_tables.issubset(set(inspect(engine).get_table_names())):
        engine.dispose()
        pytest.skip("requires the isolated PostgreSQL schema through S020 v3.4 handoff")
    yield engine
    engine.dispose()


def _yaml_code_main(node_id: str):
    """Compile the unchanged code block belonging to one named V8 Code node."""
    assert hashlib.sha256(_V8_PATH.read_bytes()).hexdigest() == _V8_SHA256
    lines = _V8_PATH.read_text(encoding="utf-8").splitlines()
    node_matches = [index for index, line in enumerate(lines) if line.strip() == f"id: {node_id}"]
    assert len(node_matches) == 1, f"expected one V8 node {node_id!r}, got {node_matches}"
    node_index = node_matches[0]
    node_start = max(index for index in range(node_index) if lines[index].startswith("    - data:"))
    code_matches = [
        index for index in range(node_start, node_index) if lines[index].strip() == "code: |"
    ]
    assert len(code_matches) == 1, f"expected one literal code block in {node_id!r}"
    code_index = code_matches[0]

    body: list[str] = []
    for line in lines[code_index + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) <= 8:
            break
        body.append(line[10:] if line.strip() else "")
    namespace: dict[str, object] = {}
    exec("\n".join(body).rstrip() + "\n", namespace)
    main = namespace.get("main")
    assert callable(main), f"V8 node {node_id!r} did not define main"
    return main


def _build_yaml_incomplete_event(
    *,
    business_session_id: UUID,
    recommendation_id: UUID,
    run_id: str,
    conflicting_field_ids: tuple[str, ...] = ("3",),
) -> tuple[dict[str, object], ChatflowV34StageEvent]:
    context = {
        "contract_version": V34_STAGE_CONTRACT,
        "flow_version": V34_FLOW_VERSION,
        "business_session_id": str(business_session_id),
        "recommendation_id": str(recommendation_id),
        "recommendation_revision": 1,
        "start_operation_id": str(uuid4()),
    }
    review_state = {
        "selections": {str(field_id): f"choice-{field_id}" for field_id in range(1, 6)},
        "issues": {field_id: _REVIEW_MARKER for field_id in conflicting_field_ids},
    }
    result = _yaml_code_main("v34_identity_conflict_event")(
        json.dumps(context, ensure_ascii=False),
        run_id,
        json.dumps(review_state, ensure_ascii=False),
        "no",
    )
    assert result["ready"] == "yes"
    event_data = json.loads(result["body"])
    event = ChatflowV34StageEvent.model_validate_json(
        json.dumps(event_data, ensure_ascii=False), strict=True
    )
    return result, event


def _event_with(event: ChatflowV34StageEvent, **updates: object) -> ChatflowV34StageEvent:
    updated = event.model_copy(update={**updates, "digest": "0" * 64})
    return updated.model_copy(update={"digest": chatflow_v34_stage_event_digest(updated).hex()})


def _install_test_api_routes(client: TestClient, engine: Engine) -> None:
    client.app.state.task_service = client.app.state.business_session_service.task_service
    client.app.include_router(tasks_router)
    client.app.include_router(
        create_internal_dify_router(
            engine=engine,
            callback_credential=SecretStr(TEST_INTERNAL_CALLBACK_SECRET),
            v34_stage_event_service=ChatflowV34StageEventService(engine),
        )
    )


def _post_stage_event(client: TestClient, event: ChatflowV34StageEvent):
    return client.post(
        "/internal/v1/dify/stage-events",
        json=event.model_dump(mode="json"),
        headers={
            "Authorization": f"Bearer {TEST_INTERNAL_CALLBACK_SECRET}",
            "Content-Type": "application/json",
        },
    )


def _ready_selected_case(engine: Engine, *, key: str):
    (
        client,
        owner_user_id,
        reference_set_id,
        session_id,
        recommendation_id,
        run_id,
        form_token,
    ) = _prepare_v34_selection_ready(engine, idempotency_key=key)
    task_id = _accept_v34_selection_evidence(
        engine,
        client=client,
        session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
    )
    _install_test_api_routes(client, engine)
    return (
        client,
        owner_user_id,
        reference_set_id,
        session_id,
        recommendation_id,
        run_id,
        task_id,
        form_token,
    )


def test_api_pg_accepts_and_replays_original_yaml_event_with_owner_safe_dtos(
    ready0_pg_engine: Engine,
) -> None:
    assert hashlib.sha256(_V8_PATH.read_bytes()).hexdigest() == _V8_SHA256
    (
        client,
        _owner_user_id,
        _reference_set_id,
        session_id,
        recommendation_id,
        run_id,
        task_id,
        form_token,
    ) = _ready_selected_case(
        ready0_pg_engine,
        key=f"ready0-api-accepted-{uuid4().hex}",
    )
    _node_result, event = _build_yaml_incomplete_event(
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
    )
    receipt_main = _yaml_code_main("v34_identity_conflict_receipt")

    accepted = _post_stage_event(client, event)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {
        "status": "accepted",
        "business_session_id": str(session_id),
        "event_key": event.event_key,
    }
    assert receipt_main(
        accepted.text,
        accepted.status_code,
        str(session_id),
        event.event_key,
    ) == {"ok": "yes"}

    with Session(ready0_pg_engine) as db:
        receipt_before = db.execute(
            select(ChatflowV34StageEventReceipt).where(
                ChatflowV34StageEventReceipt.business_session_id == session_id,
                ChatflowV34StageEventReceipt.event_key == event.event_key,
            )
        ).scalar_one()
        task_before = db.get(Task, task_id)
        task_items_before = (
            db.execute(
                select(TaskItem).where(TaskItem.task_id == task_id).order_by(TaskItem.position)
            )
            .scalars()
            .all()
        )
        terminal_at_before = task_before.terminal_at if task_before is not None else None
        expires_at_before = task_before.expires_at if task_before is not None else None
        receipt_processed_at_before = receipt_before.processed_at
        generation_request_count = db.scalar(
            select(func.count(ChatflowGenerationRequest.id)).where(
                ChatflowGenerationRequest.business_session_id == session_id
            )
        )
    assert receipt_before.disposition == "accepted"
    assert receipt_before.safe_reason_code == "PRODUCT_IDENTITY_CONFLICT"
    assert task_before is not None and task_before.status == "failed"
    assert terminal_at_before is not None and expires_at_before is not None
    assert len(task_items_before) == task_before.planned_count
    assert all(item.status == "failed" for item in task_items_before)
    assert all(item.error_code == "V34_PRE_GENERATION_FAILED" for item in task_items_before)
    assert all(item.image_id is None for item in task_items_before)
    assert generation_request_count == 0

    replayed = _post_stage_event(client, event)
    assert replayed.status_code == 200, replayed.text
    assert replayed.json() == {
        "status": "replayed",
        "business_session_id": str(session_id),
        "event_key": event.event_key,
    }
    assert receipt_main(
        replayed.text,
        replayed.status_code,
        str(session_id),
        event.event_key,
    ) == {"ok": "yes"}

    with Session(ready0_pg_engine) as db:
        receipt_after = db.execute(
            select(ChatflowV34StageEventReceipt).where(
                ChatflowV34StageEventReceipt.business_session_id == session_id,
                ChatflowV34StageEventReceipt.event_key == event.event_key,
            )
        ).scalar_one()
        task_after = db.get(Task, task_id)
        incomplete_receipt_count = db.scalar(
            select(func.count(ChatflowV34StageEventReceipt.id)).where(
                ChatflowV34StageEventReceipt.business_session_id == session_id,
                ChatflowV34StageEventReceipt.event_type == "pre_generation.incomplete",
            )
        )
        assert receipt_after.disposition == "accepted"
        assert receipt_after.processed_at == receipt_processed_at_before
        assert task_after is not None
        assert task_after.terminal_at == terminal_at_before
        assert task_after.expires_at == expires_at_before
        assert task_after.status == "failed"
        assert incomplete_receipt_count == 1

    conflicting = _event_with(
        event,
        payload={
            "reason_code": "PRODUCT_IDENTITY_CONFLICT",
            "conflicting_field_ids": ["4"],
        },
    )
    conflict_response = _post_stage_event(client, conflicting)
    assert conflict_response.status_code == 409
    assert conflict_response.json()["code"] == "EVENT_CONTENT_CONFLICT"

    session_response = client.get(f"/api/v1/business-sessions/{session_id}")
    task_response = client.get(f"/api/v1/tasks/{task_id}")
    assert session_response.status_code == 200, session_response.text
    assert task_response.status_code == 200, task_response.text
    session_dto = session_response.json()
    task_dto = task_response.json()
    assert session_dto["status"] == "failed"
    assert session_dto["safe_reason_code"] == "PRODUCT_IDENTITY_CONFLICT"
    assert task_dto["status"] == "failed"
    assert task_dto["counts"] == {
        "planned": len(task_dto["items"]),
        "succeeded": 0,
        "failed": len(task_dto["items"]),
        "pending": 0,
        "unresolved": 0,
    }
    assert all(
        item["error"]
        == {
            "code": "V34_PRE_GENERATION_FAILED",
            "message": "商品身份核对未通过，未发起生图。",
        }
        for item in task_dto["items"]
    )
    for public_payload in (json.dumps(session_dto, ensure_ascii=False), json.dumps(task_dto)):
        assert form_token not in public_payload
        assert run_id not in public_payload
        assert _REVIEW_MARKER not in public_payload
        assert "prompt" not in public_payload.lower()
        assert "token" not in public_payload.lower()

    other_owner_client, _other_owner_id = _new_guest(ready0_pg_engine)
    _install_test_api_routes(other_owner_client, ready0_pg_engine)
    assert other_owner_client.get(f"/api/v1/business-sessions/{session_id}").status_code == 404
    assert other_owner_client.get(f"/api/v1/tasks/{task_id}").status_code == 404


def test_api_pg_rejected_wrong_run_replay_preserves_rejection_and_yaml_fails_closed(
    ready0_pg_engine: Engine,
) -> None:
    (
        client,
        _owner_user_id,
        _reference_set_id,
        session_id,
        recommendation_id,
        run_id,
        task_id,
        _form_token,
    ) = _ready_selected_case(
        ready0_pg_engine,
        key=f"ready0-api-wrong-run-{uuid4().hex}",
    )
    _node_result, original_event = _build_yaml_incomplete_event(
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
    )
    wrong_run_event = _event_with(original_event, dify_run_id=f"other-{run_id}")
    first = _post_stage_event(client, wrong_run_event)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "rejected"

    replay = _post_stage_event(client, wrong_run_event)
    assert replay.status_code == 200, replay.text
    assert replay.json()["status"] == "rejected"
    actual_receipt_ok = _yaml_code_main("v34_identity_conflict_receipt")(
        replay.text,
        replay.status_code,
        str(session_id),
        wrong_run_event.event_key,
    )

    with Session(ready0_pg_engine) as db:
        stored = db.execute(
            select(ChatflowV34StageEventReceipt).where(
                ChatflowV34StageEventReceipt.business_session_id == session_id,
                ChatflowV34StageEventReceipt.event_key == wrong_run_event.event_key,
            )
        ).scalar_one()
        task = db.get(Task, task_id)
        event_receipt_count = db.scalar(
            select(func.count(ChatflowV34StageEventReceipt.id)).where(
                ChatflowV34StageEventReceipt.business_session_id == session_id,
                ChatflowV34StageEventReceipt.event_key == wrong_run_event.event_key,
            )
        )
    assert stored.disposition == "rejected"
    assert stored.safe_reason_code == "DIFY_RUN_ID_MISMATCH"
    assert task is not None and task.status != "failed" and task.terminal_at is None
    assert event_receipt_count == 1

    # Keep the cross-layer safety expectation executable: a persisted rejection must not
    # become `replayed`, which the unmodified YAML receipt function would treat as success.
    assert actual_receipt_ok == {"ok": "no"}, {
        "first_api_response": first.json(),
        "replay_api_response": replay.json(),
        "persisted_disposition": stored.disposition,
        "persisted_reason": stored.safe_reason_code,
        "original_yaml_receipt_result": actual_receipt_ok,
    }


def test_api_pg_incomplete_event_cannot_terminalize_task_after_generation_request_exists(
    ready0_pg_engine: Engine,
) -> None:
    (
        client,
        _owner_user_id,
        _reference_set_id,
        session_id,
        recommendation_id,
        run_id,
        task_id,
        _form_token,
    ) = _ready_selected_case(
        ready0_pg_engine,
        key=f"ready0-api-generation-protection-{uuid4().hex}",
    )
    request_id = uuid4()
    package_id = uuid4()
    package_digest = b"p" * 32
    request_digest = b"r" * 32
    with Session(ready0_pg_engine) as db:
        task = db.get(Task, task_id)
    assert task is not None
    with Session(ready0_pg_engine) as db, db.begin():
        db.add(
            ChatflowPromptPackage(
                id=package_id,
                submission_id=uuid4(),
                task_id=task.id,
                business_session_id=session_id,
                recommendation_id=recommendation_id,
                owner_user_id=task.owner_user_id,
                reference_set_id=task.reference_set_id,
                recommendation_revision=task.recommendation_revision,
                dify_run_id=run_id,
                contract_version="chatflow-handoff-v1",
                package_version="ready0-generation-protection-fixture-v1",
                package_digest_version=1,
                package_digest=package_digest,
                request_digest_version=1,
                request_digest=request_digest,
                image_types=list(task.image_types),
                package={},
            )
        )
        db.flush()
        db.add(
            ChatflowGenerationRequest(
                id=request_id,
                package_id=package_id,
                task_id=task.id,
                business_session_id=session_id,
                dify_run_id=run_id,
                package_digest=package_digest,
                request_digest=request_digest,
            )
        )

    _node_result, event = _build_yaml_incomplete_event(
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
    )
    response = _post_stage_event(client, event)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"

    with Session(ready0_pg_engine) as db:
        receipt = db.execute(
            select(ChatflowV34StageEventReceipt).where(
                ChatflowV34StageEventReceipt.business_session_id == session_id,
                ChatflowV34StageEventReceipt.event_key == event.event_key,
            )
        ).scalar_one()
        session = db.get(BusinessSession, session_id)
        task_after = db.get(Task, task_id)
        items = (
            db.execute(
                select(TaskItem).where(TaskItem.task_id == task_id).order_by(TaskItem.position)
            )
            .scalars()
            .all()
        )
        generation_request_count = db.scalar(
            select(func.count(ChatflowGenerationRequest.id)).where(
                ChatflowGenerationRequest.business_session_id == session_id
            )
        )
    assert receipt.disposition == "rejected"
    assert receipt.safe_reason_code == "V34_GENERATION_ALREADY_STARTED"
    assert session is not None and session.status == "task_created"
    assert task_after is not None and task_after.status == "queued"
    assert task_after.terminal_at is None
    assert all(item.status == "queued" for item in items)
    assert generation_request_count == 1
