from __future__ import annotations

import hashlib
import io
import os
from collections.abc import Iterator
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from app.api.auth import router as auth_router
from app.api.business_sessions import router as business_sessions_router
from app.api.internal_dify import create_internal_dify_router
from app.core.domain_errors import DomainError
from app.core.errors import install_exception_handlers
from app.db.action_models import Action
from app.db.business_session_models import BusinessSession
from app.db.chatflow_reference_files import ChatflowDifyReferenceFile
from app.db.outbox_models import OutboxEvent
from app.db.recommendation_models import Recommendation
from app.db.reference_models import ImageAsset, ReferenceSet, ReferenceSetImage
from app.db.task_models import (
    Task,
    TaskIdempotencyRecord,
    TaskItem,
    TaskRecommendationConsumption,
)
from app.integrations.dify.chatflow import StubDifyChatflowAdapter
from app.integrations.dify.mock import build_mock_options
from app.schemas.business_sessions import (
    BusinessSessionSelectionRequest,
    ChatflowBusinessEvent,
    chatflow_business_event_digest,
)
from app.schemas.recommendations import IMAGE_TYPES
from app.services.auth import AuthOptions, AuthService
from app.services.business_sessions import BusinessSessionService
from app.services.chatflow_actions import (
    CHATFLOW_CONTINUE_ACTION,
    CHATFLOW_REFERENCE_UPLOAD_ACTION,
    CHATFLOW_START_ACTION,
    build_chatflow_action_registrations,
    execute_chatflow_continuation,
    prepare_chatflow_continuation,
)
from app.services.tasks import (
    CHATFLOW_SELECTION_OPERATION,
    TASK_PROMPT_EVENT_TYPE,
    TaskOptions,
    TaskService,
    idempotency_key_hash,
)
from app.workers.action_processor import ActionProcessor
from app.workers.messages import OutboxMessage
from app.workers.options import QueueOptions
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

TRUSTED_ORIGIN = "http://testserver"
TEST_INTERNAL_CALLBACK_SECRET = "h2-test-internal-callback-secret-0123456789"
_REFERENCE_IDENTITY_KEYS = (
    "reference_set_image_id",
    "position",
    "slot",
    "source_asset_id",
    "model_asset_id",
    "content_sha256",
    "source_sha256",
)


@pytest.fixture
def pg_engine() -> Iterator[Engine]:
    database_url = os.getenv("IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL")
    if database_url is None:
        pytest.skip("requires the dedicated PostgreSQL test runtime URL")
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
        "outbox_receipts",
        "tasks",
        "task_items",
        "task_idempotency_records",
        "task_recommendation_consumptions",
        "task_actions",
    }
    if not required_tables.issubset(set(inspect(engine).get_table_names())):
        engine.dispose()
        pytest.skip("requires the integrated PostgreSQL schema through S013 Chatflow handoff")
    yield engine
    engine.dispose()


def _build_app(engine: Engine) -> FastAPI:
    app = FastAPI()
    app.state.auth_service = AuthService(
        engine,
        AuthOptions(
            trusted_origins=frozenset({TRUSTED_ORIGIN}),
            production=False,
            use_dev_cookie=True,
        ),
    )
    app.state.business_session_service = BusinessSessionService(
        engine,
        task_service=TaskService(engine, TaskOptions(max_nonterminal_per_owner=8)),
    )
    app.include_router(auth_router)
    app.include_router(business_sessions_router)
    app.include_router(
        create_internal_dify_router(
            engine=engine,
            callback_credential=SecretStr(TEST_INTERNAL_CALLBACK_SECRET),
            business_event_service=app.state.business_session_service,
        )
    )
    install_exception_handlers(app)
    return app


def _new_guest(engine: Engine) -> tuple[TestClient, UUID]:
    client = TestClient(_build_app(engine))
    response = client.post("/api/v1/auth/guest", headers={"Origin": TRUSTED_ORIGIN})
    assert response.status_code == 200
    return client, UUID(response.json()["user_id"])


def _create_reference_set(engine: Engine, owner_user_id: UUID) -> UUID:
    with Session(engine) as db:
        now = db.scalar(select(func.clock_timestamp()))
    assert now is not None
    reference_set_id = uuid4()
    slots = ("PRIMARY", "AUX01", "AUX02")
    with Session(engine) as db, db.begin():
        db.add(
            ReferenceSet(
                id=reference_set_id,
                owner_user_id=owner_user_id,
                product_category="测试品类",
                status="ready",
                lease_token=None,
                upload_lease_until=None,
                created_at=now,
                last_activity_at=now,
                ready_at=now,
                expires_at=now + timedelta(days=10),
            )
        )
        db.flush()
        for position, slot in enumerate(slots):
            image_id = uuid4()
            source_id = uuid4()
            model_id = uuid4()
            content_digest = hashlib.sha256(f"model:{image_id}".encode()).hexdigest()
            source_digest = hashlib.sha256(f"source:{image_id}".encode()).hexdigest()
            db.add_all(
                [
                    ImageAsset(
                        id=source_id,
                        owner_user_id=owner_user_id,
                        asset_kind="reference_source",
                        object_key=f"h2-test/{source_id}/source.png",
                        content_sha256=source_digest,
                        mime_type="image/png",
                        size_bytes=256,
                        width=64,
                        height=64,
                    ),
                    ImageAsset(
                        id=model_id,
                        owner_user_id=owner_user_id,
                        asset_kind="reference_model_input",
                        object_key=f"h2-test/{model_id}/model.png",
                        content_sha256=content_digest,
                        mime_type="image/png",
                        size_bytes=128,
                        width=32,
                        height=32,
                    ),
                ]
            )
            db.flush()
            db.add(
                ReferenceSetImage(
                    id=image_id,
                    reference_set_id=reference_set_id,
                    owner_user_id=owner_user_id,
                    slot=slot,
                    position=position,
                    source_asset_id=source_id,
                    model_asset_id=model_id,
                    content_sha256=content_digest,
                    created_at=now,
                )
            )
    return reference_set_id


def _create_reference_set_with_content(
    engine: Engine, owner_user_id: UUID, *, count: int
) -> tuple[UUID, dict[str, bytes]]:
    assert 1 <= count <= 4
    now = _db_now(engine)
    reference_set_id = uuid4()
    object_contents: dict[str, bytes] = {}
    slots = ("PRIMARY", "AUX01", "AUX02", "AUX03")[:count]
    with Session(engine) as db, db.begin():
        db.add(
            ReferenceSet(
                id=reference_set_id,
                owner_user_id=owner_user_id,
                product_category="测试品类",
                status="ready",
                lease_token=None,
                upload_lease_until=None,
                created_at=now,
                last_activity_at=now,
                ready_at=now,
                expires_at=now + timedelta(days=10),
            )
        )
        db.flush()
        for position, slot in enumerate(slots):
            image_id = uuid4()
            source_id = uuid4()
            model_id = uuid4()
            content = f"h8-model-input-{reference_set_id}-{position}".encode()
            digest = hashlib.sha256(content).hexdigest()
            object_key = f"h8-test/{owner_user_id}/{model_id}.png"
            object_contents[object_key] = content
            db.add_all(
                [
                    ImageAsset(
                        id=source_id,
                        owner_user_id=owner_user_id,
                        asset_kind="reference_source",
                        object_key=f"h8-test/{owner_user_id}/{source_id}.png",
                        content_sha256=hashlib.sha256(f"source-{position}".encode()).hexdigest(),
                        mime_type="image/png",
                        size_bytes=128,
                        width=32,
                        height=32,
                    ),
                    ImageAsset(
                        id=model_id,
                        owner_user_id=owner_user_id,
                        asset_kind="reference_model_input",
                        object_key=object_key,
                        content_sha256=digest,
                        mime_type="image/png",
                        size_bytes=len(content),
                        width=32,
                        height=32,
                    ),
                ]
            )
            db.flush()
            db.add(
                ReferenceSetImage(
                    id=image_id,
                    reference_set_id=reference_set_id,
                    owner_user_id=owner_user_id,
                    slot=slot,
                    position=position,
                    source_asset_id=source_id,
                    model_asset_id=model_id,
                    content_sha256=digest,
                    created_at=now,
                )
            )
    return reference_set_id, object_contents


def _db_now(engine: Engine):
    with Session(engine) as db:
        return db.scalar(select(func.clock_timestamp()))


class _MemoryReferenceStorage:
    def __init__(self, contents: dict[str, bytes], *, engine: Engine | None = None) -> None:
        self.contents = contents
        self.engine = engine
        self.read_keys: list[str] = []

    def open_reader(self, key: str):
        if self.engine is not None:
            assert self.engine.pool.checkedout() == 0
        self.read_keys.append(key)
        return io.BytesIO(self.contents[key])


def _process_chatflow_action(
    engine: Engine,
    *,
    action_id: UUID,
    processor: ActionProcessor,
) -> str:
    with Session(engine) as db:
        action = db.get(Action, action_id)
        assert action is not None
        event = db.get(OutboxEvent, action.outbox_event_id)
        assert event is not None
        message = OutboxMessage(
            event_id=event.id,
            aggregate_id=event.aggregate_id,
            operation_version=event.operation_version,
            schema_version=event.schema_version,
            payload={key: UUID(value) for key, value in event.payload.items()},
        ).model_dump(mode="json")
    return processor.process_payload(message).status


def _make_ready_business_session(
    engine: Engine,
) -> tuple[TestClient, UUID, UUID, UUID, BusinessSessionService, ChatflowBusinessEvent]:
    client, owner_user_id = _new_guest(engine)
    reference_set_id = _create_reference_set(engine, owner_user_id)
    service: BusinessSessionService = client.app.state.business_session_service
    created = service.create_session(
        owner_user_id=owner_user_id,
        reference_set_id=reference_set_id,
        idempotency_key=f"create-{uuid4().hex}",
    )
    run_id = f"stub-run-{uuid4().hex}"
    _set_current_run(
        engine,
        created.business_session_id,
        run_id=run_id,
        revision=1,
    )
    event = _make_callback_event(
        engine,
        business_session_id=created.business_session_id,
        recommendation_id=created.recommendation_id,
        run_id=run_id,
        revision=1,
    )
    assert service.accept_business_event(event).status == "accepted"
    return (
        client,
        owner_user_id,
        reference_set_id,
        created.business_session_id,
        service,
        event,
    )


def _set_current_run(
    engine: Engine,
    business_session_id: UUID,
    *,
    run_id: str,
    revision: int,
) -> None:
    with Session(engine) as db, db.begin():
        session = db.get(BusinessSession, business_session_id)
        assert session is not None
        recommendation = db.execute(
            select(Recommendation).where(Recommendation.business_session_id == business_session_id)
        ).scalar_one()
        session.status = "awaiting_recommendation"
        session.start_outcome = "accepted"
        session.dify_run_id = run_id
        session.run_revision = revision
        recommendation.dify_run_id = run_id
        recommendation.revision = revision
        recommendation.status = "analyzing"
        recommendation.original_recommendation = None
        recommendation.public_options = None
        recommendation.ready_at = None
        recommendation.valid_until = None
        recommendation.callback_event_id = None
        recommendation.callback_digest_version = None
        recommendation.callback_digest = None
        recommendation.state = {
            "stage": "awaiting_recommendation",
            "provenance_kind": "dify_chatflow",
            "reference_asset_digest": session.reference_asset_digest.hex(),
        }


def _make_callback_event(
    engine: Engine,
    *,
    business_session_id: UUID,
    recommendation_id: UUID,
    run_id: str,
    revision: int,
    expired: bool = False,
) -> ChatflowBusinessEvent:
    with Session(engine) as db:
        session = db.get(BusinessSession, business_session_id)
        now = db.scalar(select(func.clock_timestamp()))
    assert session is not None and now is not None
    options = build_mock_options()
    fields = {
        name: [choice["text"] for choice in options[name]["choices"]]
        for name in ("language", "usage_scene", "visual_style", "selling_points")
    }
    original = {
        "category_snapshot": "测试品类",
        "field_texts": fields,
        "image_types": list(IMAGE_TYPES),
    }
    references = [
        {key: entry[key] for key in _REFERENCE_IDENTITY_KEYS}
        for entry in session.reference_assets_snapshot
    ]
    payload = {
        "reference_set_id": str(session.reference_set_id),
        "reference_asset_digest": session.reference_asset_digest.hex(),
        "reference_assets": references,
        "original_recommendation": original,
        "public_options": options,
        "image_types": list(IMAGE_TYPES),
        "valid_until": (
            now - timedelta(seconds=1) if expired else now + timedelta(days=1)
        ).isoformat(),
    }
    event = ChatflowBusinessEvent(
        contract_version="chatflow-handoff-v1",
        business_session_id=business_session_id,
        recommendation_id=recommendation_id,
        recommendation_revision=revision,
        dify_run_id=run_id,
        event_id=uuid4(),
        source="dify",
        source_sequence=revision,
        event_type="recommendation.ready",
        digest_version=1,
        digest="0" * 64,
        payload=payload,
    )
    return event.model_copy(update={"digest": chatflow_business_event_digest(event)})


def _selection_request(
    recommendation_id: UUID,
    *,
    revision: int = 1,
    image_types: list[str] | None = None,
) -> BusinessSessionSelectionRequest:
    return BusinessSessionSelectionRequest.model_validate(
        {
            "recommendation_id": str(recommendation_id),
            "recommendation_revision": revision,
            "preferences": {
                "language": {"mode": "A"},
                "platform": "TEMU",
                "usage_scene": {"mode": "B"},
                "visual_style": {"mode": "C"},
                "selling_points": {"mode": "custom", "text": "仅采用可核验的商品信息"},
            },
            "image_types": image_types or list(IMAGE_TYPES[:2]),
        }
    )


def test_selection_rolls_back_task_consumption_idempotency_and_action_on_db_failure(
    pg_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _client, owner_id, _reference_set_id, session_id, service, callback = (
        _make_ready_business_session(pg_engine)
    )
    request = _selection_request(callback.recommendation_id)
    key = f"rollback-{uuid4().hex}"

    import app.services.business_sessions as business_sessions_module

    def fail_continuation_schedule(*_args, **_kwargs):
        raise SQLAlchemyError("injected action persistence failure")

    monkeypatch.setattr(
        business_sessions_module,
        "schedule_action_in_transaction",
        fail_continuation_schedule,
    )
    with pytest.raises(DomainError) as failure:
        service.create_selection(
            owner_user_id=owner_id,
            business_session_id=session_id,
            idempotency_key=key,
            request=request,
        )
    assert failure.value.status_code == 503

    with Session(pg_engine) as db:
        assert (
            db.scalar(select(func.count(Task.id)).where(Task.business_session_id == session_id))
            == 0
        )
        assert (
            db.scalar(
                select(func.count(TaskItem.id))
                .join(Task, Task.id == TaskItem.task_id)
                .where(Task.business_session_id == session_id)
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(TaskRecommendationConsumption.recommendation_id)).where(
                    TaskRecommendationConsumption.recommendation_id == callback.recommendation_id
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(TaskIdempotencyRecord.id)).where(
                    TaskIdempotencyRecord.owner_user_id == owner_id,
                    TaskIdempotencyRecord.operation == CHATFLOW_SELECTION_OPERATION,
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(Action.id)).where(
                    Action.business_session_id == session_id,
                    Action.action_kind == "dify.chatflow.continue.v1",
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.event_type == TASK_PROMPT_EVENT_TYPE,
                    OutboxEvent.aggregate_id == session_id,
                )
            )
            == 0
        )
        persisted = db.get(BusinessSession, session_id)
        assert persisted is not None and persisted.status == "recommendation_ready"


def test_create_session_response_loss_replays_the_same_first_upload_intent(
    pg_engine: Engine,
) -> None:
    client, owner_id = _new_guest(pg_engine)
    reference_set_id = _create_reference_set(pg_engine, owner_id)
    service: BusinessSessionService = client.app.state.business_session_service
    key = f"response-loss-{uuid4().hex}"

    first = service.create_session(
        owner_user_id=owner_id,
        reference_set_id=reference_set_id,
        idempotency_key=key,
    )
    # Treat the first response as lost and retry the exact request.
    replay = service.create_session(
        owner_user_id=owner_id,
        reference_set_id=reference_set_id,
        idempotency_key=key,
    )

    assert first.status == "starting"
    assert replay.status == "replayed"
    assert replay.business_session_id == first.business_session_id
    assert replay.recommendation_id == first.recommendation_id
    key_hash = idempotency_key_hash(key)
    with Session(pg_engine) as db:
        assert (
            db.scalar(
                select(func.count(BusinessSession.id)).where(
                    BusinessSession.owner_user_id == owner_id,
                    BusinessSession.idempotency_key_hash == key_hash,
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(Recommendation.id)).where(
                    Recommendation.business_session_id == first.business_session_id,
                    Recommendation.source_kind == "dify_chatflow",
                )
            )
            == 1
        )
        upload_action = db.execute(
            select(Action).where(
                Action.business_session_id == first.business_session_id,
                Action.action_kind == "dify.chatflow.reference.upload.v1",
            )
        ).scalar_one()
        assert upload_action.task_id is None
        assert upload_action.status == "scheduled"
        assert upload_action.sequence == 10
        mappings = db.execute(
            select(ChatflowDifyReferenceFile)
            .where(ChatflowDifyReferenceFile.business_session_id == first.business_session_id)
            .order_by(ChatflowDifyReferenceFile.position)
        ).scalars().all()
        assert [row.position for row in mappings] == [0, 1, 2]
        assert [row.slot for row in mappings] == ["PRIMARY", "AUX01", "AUX02"]
        assert [row.status for row in mappings] == ["pending", "pending", "pending"]
        assert all(row.owner_user_id == owner_id for row in mappings)
        assert all(row.dify_user_id == f"bs-{first.business_session_id.hex}" for row in mappings)
        assert all(row.dify_app_id == "mock-chatflow-v1" for row in mappings)
        assert (
            db.scalar(
                select(func.count(Action.id)).where(
                    Action.business_session_id == first.business_session_id,
                    Action.action_kind == "dify.chatflow.start.v1",
                )
            )
            == 0
        )


def test_reference_uploads_are_durable_ordered_and_gate_original_run_start(
    pg_engine: Engine,
) -> None:
    client, owner_id = _new_guest(pg_engine)
    reference_set_id, content_by_key = _create_reference_set_with_content(
        pg_engine, owner_id, count=4
    )
    service: BusinessSessionService = client.app.state.business_session_service
    created = service.create_session(
        owner_user_id=owner_id,
        reference_set_id=reference_set_id,
        idempotency_key=f"h8-upload-{uuid4().hex}",
    )
    storage = _MemoryReferenceStorage(content_by_key, engine=pg_engine)
    adapter = StubDifyChatflowAdapter()
    processor = ActionProcessor(
        pg_engine,
        QueueOptions(broker_url="amqp://integration:integration@localhost:5672/h8-test"),
        build_chatflow_action_registrations(adapter, reference_storage=storage),
    )

    for expected_position in range(4):
        with Session(pg_engine) as db:
            action = db.execute(
                select(Action).where(
                    Action.business_session_id == created.business_session_id,
                    Action.action_kind == CHATFLOW_REFERENCE_UPLOAD_ACTION,
                    Action.status == "scheduled",
                )
            ).scalar_one()
            mapping = db.execute(
                select(ChatflowDifyReferenceFile).where(
                    ChatflowDifyReferenceFile.upload_action_id == action.id
                )
            ).scalar_one()
            assert mapping.position == expected_position
            assert db.scalar(
                select(func.count(Action.id)).where(
                    Action.business_session_id == created.business_session_id,
                    Action.action_kind == CHATFLOW_START_ACTION,
                )
            ) == 0
            action_id = action.id
        assert _process_chatflow_action(
            pg_engine, action_id=action_id, processor=processor
        ) == "completed"
        assert not adapter.start_calls

    with Session(pg_engine) as db:
        mappings = db.execute(
            select(ChatflowDifyReferenceFile)
            .where(ChatflowDifyReferenceFile.business_session_id == created.business_session_id)
            .order_by(ChatflowDifyReferenceFile.position)
        ).scalars().all()
        start_action = db.execute(
            select(Action).where(
                Action.business_session_id == created.business_session_id,
                Action.action_kind == CHATFLOW_START_ACTION,
            )
        ).scalar_one()
        assert [item.status for item in mappings] == ["uploaded"] * 4
        assert [item.position for item in mappings] == [0, 1, 2, 3]
        assert all(item.dify_file_id for item in mappings)
        start_action_id = start_action.id

    assert _process_chatflow_action(
        pg_engine, action_id=start_action_id, processor=processor
    ) == "completed"
    assert len(storage.read_keys) == 4
    assert len(adapter.reference_upload_calls) == 4
    assert len(adapter.start_calls) == 1
    assert [item["position"] for item in adapter.start_calls[0].reference_files] == [
        0,
        1,
        2,
        3,
    ]
    assert [item["slot"] for item in adapter.start_calls[0].reference_files] == [
        "PRIMARY",
        "AUX01",
        "AUX02",
        "AUX03",
    ]
    assert [item.dify_user_id for item in adapter.reference_upload_calls] == [
        f"bs-{created.business_session_id.hex}"
    ] * 4
    public = service.get_session(
        owner_user_id=owner_id,
        business_session_id=created.business_session_id,
    )
    assert "dify_file_id" not in str(public.model_dump(mode="json"))


def test_reference_file_mappings_are_isolated_between_visitors(pg_engine: Engine) -> None:
    sessions: list[tuple[UUID, UUID, UUID, dict[str, bytes]]] = []
    contents: dict[str, bytes] = {}
    for visitor_number in (1, 2):
        client, owner_id = _new_guest(pg_engine)
        reference_set_id, visitor_contents = _create_reference_set_with_content(
            pg_engine, owner_id, count=2
        )
        contents.update(visitor_contents)
        service: BusinessSessionService = client.app.state.business_session_service
        created = service.create_session(
            owner_user_id=owner_id,
            reference_set_id=reference_set_id,
            idempotency_key=f"h8-visitor-isolation-{visitor_number}-{uuid4().hex}",
        )
        sessions.append((owner_id, created.business_session_id, reference_set_id, visitor_contents))

    storage = _MemoryReferenceStorage(contents, engine=pg_engine)
    adapter = StubDifyChatflowAdapter()
    processor = ActionProcessor(
        pg_engine,
        QueueOptions(broker_url="amqp://integration:integration@localhost:5672/h8-test"),
        build_chatflow_action_registrations(adapter, reference_storage=storage),
    )
    for _owner_id, session_id, _reference_set_id, _visitor_contents in sessions:
        for _position in range(2):
            with Session(pg_engine) as db:
                upload_action = db.execute(
                    select(Action).where(
                        Action.business_session_id == session_id,
                        Action.action_kind == CHATFLOW_REFERENCE_UPLOAD_ACTION,
                        Action.status == "scheduled",
                    )
                ).scalar_one()
                upload_action_id = upload_action.id
            assert _process_chatflow_action(
                pg_engine, action_id=upload_action_id, processor=processor
            ) == "completed"
        with Session(pg_engine) as db:
            start_action = db.execute(
                select(Action).where(
                    Action.business_session_id == session_id,
                    Action.action_kind == CHATFLOW_START_ACTION,
                    Action.status == "scheduled",
                )
            ).scalar_one()
            start_action_id = start_action.id
        assert _process_chatflow_action(
            pg_engine, action_id=start_action_id, processor=processor
        ) == "completed"

    with Session(pg_engine) as db:
        mappings_by_session = {
            session_id: db.execute(
                select(ChatflowDifyReferenceFile)
                .where(ChatflowDifyReferenceFile.business_session_id == session_id)
                .order_by(ChatflowDifyReferenceFile.position)
            ).scalars().all()
            for _owner_id, session_id, _reference_set_id, _visitor_contents in sessions
        }
    first_session_id = sessions[0][1]
    second_session_id = sessions[1][1]
    first_rows = mappings_by_session[first_session_id]
    second_rows = mappings_by_session[second_session_id]
    assert len(first_rows) == len(second_rows) == 2
    assert [[row.slot for row in rows] for rows in (first_rows, second_rows)] == [
        ["PRIMARY", "AUX01"],
        ["PRIMARY", "AUX01"],
    ]
    assert all(row.status == "uploaded" for row in (*first_rows, *second_rows))
    assert all(row.dify_app_id == "mock-chatflow-v1" for row in (*first_rows, *second_rows))
    assert {row.business_session_id for row in first_rows} == {first_session_id}
    assert {row.business_session_id for row in second_rows} == {second_session_id}
    assert {row.dify_user_id for row in first_rows} == {f"bs-{first_session_id.hex}"}
    assert {row.dify_user_id for row in second_rows} == {f"bs-{second_session_id.hex}"}
    assert {row.dify_user_id for row in first_rows}.isdisjoint(
        {row.dify_user_id for row in second_rows}
    )
    assert {row.dify_file_id for row in first_rows}.isdisjoint(
        {row.dify_file_id for row in second_rows}
    )
    assert len(adapter.start_calls) == 2
    for call, expected_rows in zip(adapter.start_calls, (first_rows, second_rows), strict=True):
        assert call.dify_user_id == expected_rows[0].dify_user_id
        assert [item["position"] for item in call.reference_files] == [0, 1]
        assert [item["slot"] for item in call.reference_files] == ["PRIMARY", "AUX01"]
        assert [item["dify_file_id"] for item in call.reference_files] == [
            row.dify_file_id for row in expected_rows
        ]
        assert [item["content_sha256"] for item in call.reference_files] == [
            row.content_sha256 for row in expected_rows
        ]
    assert all(len(row.content_sha256) == 64 for row in (*first_rows, *second_rows))


def test_ambiguous_reference_upload_is_unknown_and_never_retried_or_started(
    pg_engine: Engine,
) -> None:
    client, owner_id = _new_guest(pg_engine)
    reference_set_id, content_by_key = _create_reference_set_with_content(
        pg_engine, owner_id, count=3
    )
    created = client.app.state.business_session_service.create_session(
        owner_user_id=owner_id,
        reference_set_id=reference_set_id,
        idempotency_key=f"h8-upload-unknown-{uuid4().hex}",
    )
    storage = _MemoryReferenceStorage(content_by_key, engine=pg_engine)
    adapter = StubDifyChatflowAdapter(lose_reference_upload_response_at=1)
    processor = ActionProcessor(
        pg_engine,
        QueueOptions(broker_url="amqp://integration:integration@localhost:5672/h8-test"),
        build_chatflow_action_registrations(adapter, reference_storage=storage),
    )
    for expected_position in (0, 1):
        with Session(pg_engine) as db:
            action = db.execute(
                select(Action).where(
                    Action.business_session_id == created.business_session_id,
                    Action.action_kind == CHATFLOW_REFERENCE_UPLOAD_ACTION,
                    Action.status == "scheduled",
                )
            ).scalar_one()
            mapping = db.execute(
                select(ChatflowDifyReferenceFile).where(
                    ChatflowDifyReferenceFile.upload_action_id == action.id
                )
            ).scalar_one()
            assert mapping.position == expected_position
            action_id = action.id
        assert _process_chatflow_action(
            pg_engine, action_id=action_id, processor=processor
        ) == "completed"
    assert _process_chatflow_action(
        pg_engine, action_id=action_id, processor=processor
    ) == "completed"
    with Session(pg_engine) as db:
        mappings = db.execute(
            select(ChatflowDifyReferenceFile)
            .where(ChatflowDifyReferenceFile.business_session_id == created.business_session_id)
            .order_by(ChatflowDifyReferenceFile.position)
        ).scalars().all()
        assert [item.status for item in mappings] == ["uploaded", "unknown", "pending"]
        assert db.scalar(
            select(func.count(Action.id)).where(
                Action.business_session_id == created.business_session_id,
                Action.action_kind == CHATFLOW_START_ACTION,
            )
        ) == 0
        session = db.get(BusinessSession, created.business_session_id)
        assert session is not None
        assert session.status == "reconciling"
        assert session.start_outcome == "unknown"
    assert len(adapter.reference_upload_calls) == 2
    assert adapter.start_calls == []


def test_selection_commit_is_atomic_replayable_and_does_not_start_prompt_or_generation(
    pg_engine: Engine,
) -> None:
    _client, owner_id, _reference_set_id, session_id, service, callback = (
        _make_ready_business_session(pg_engine)
    )
    assert service.accept_business_event(callback).status == "replayed"
    public_session = service.get_session(
        owner_user_id=owner_id,
        business_session_id=session_id,
    )
    assert public_session.status == "recommendation_ready"
    assert public_session.recommendation is not None
    assert public_session.recommendation.recommendation_id == callback.recommendation_id
    assert public_session.recommendation.original_recommendation.category_snapshot == "测试品类"
    request = _selection_request(callback.recommendation_id)
    key = f"selection-{uuid4().hex}"
    result = service.create_selection(
        owner_user_id=owner_id,
        business_session_id=session_id,
        idempotency_key=key,
        request=request,
    )

    with Session(pg_engine) as db:
        task = db.get(Task, result.task_id)
        items = (
            db.execute(
                select(TaskItem)
                .where(TaskItem.task_id == result.task_id)
                .order_by(TaskItem.position)
            )
            .scalars()
            .all()
        )
        consumption = db.get(TaskRecommendationConsumption, callback.recommendation_id)
        idempotency = db.execute(
            select(TaskIdempotencyRecord).where(
                TaskIdempotencyRecord.owner_user_id == owner_id,
                TaskIdempotencyRecord.operation == CHATFLOW_SELECTION_OPERATION,
            )
        ).scalar_one()
        continuation = db.execute(
            select(Action).where(
                Action.business_session_id == session_id,
                Action.action_kind == "dify.chatflow.continue.v1",
            )
        ).scalar_one()
        session_actions = (
            db.execute(select(Action).where(Action.business_session_id == session_id))
            .scalars()
            .all()
        )
        task_prompt_count = db.scalar(
            select(func.count(OutboxEvent.id)).where(
                OutboxEvent.event_type == TASK_PROMPT_EVENT_TYPE,
                OutboxEvent.aggregate_id == result.task_id,
            )
        )
        task_actions = (
            db.execute(select(Action).where(Action.task_id == result.task_id)).scalars().all()
        )
        assert task is not None and task.provenance_kind == "dify_chatflow"
        assert task.business_session_id == session_id
        assert [item.image_type for item in items] == request.image_types
        assert len(items) == result.planned_count
        assert consumption is not None and consumption.task_id == task.id
        assert idempotency.task_id == task.id and idempotency.request_digest == task.request_digest
        assert continuation.task_id == task.id
        assert continuation.status == "scheduled"
        assert [action.action_kind for action in task_actions] == [CHATFLOW_CONTINUE_ACTION]
        assert {action.action_kind for action in session_actions} == {
            "dify.chatflow.reference.upload.v1",
            CHATFLOW_CONTINUE_ACTION,
        }
        assert task_prompt_count == 0

        action_input = prepare_chatflow_continuation(db, continuation)

    # Action prepare has closed its DB Session before the stub performs I/O.
    stub = StubDifyChatflowAdapter()
    execution = execute_chatflow_continuation(stub, action_input)
    assert execution.outcome == "accepted"
    assert len(stub.continuation_calls) == 1
    command = stub.continuation_calls[0]
    assert command.business_session_id == session_id
    assert command.recommendation_id == callback.recommendation_id
    assert command.dify_run_id == callback.dify_run_id
    assert command.task_id == result.task_id

    replay = service.create_selection(
        owner_user_id=owner_id,
        business_session_id=session_id,
        idempotency_key=key,
        request=request,
    )
    assert replay.status == "replayed"
    assert replay.task_id == result.task_id

    conflicting_request = _selection_request(
        callback.recommendation_id,
        image_types=[IMAGE_TYPES[0]],
    )
    with pytest.raises(DomainError) as conflict:
        service.create_selection(
            owner_user_id=owner_id,
            business_session_id=session_id,
            idempotency_key=key,
            request=conflicting_request,
        )
    assert conflict.value.status_code == 409
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"


def test_expired_old_recommendation_callback_cannot_replace_current_state(
    pg_engine: Engine,
) -> None:
    _client, _owner_id, _reference_set_id, session_id, service, accepted_event = (
        _make_ready_business_session(pg_engine)
    )
    current_run_id = f"current-run-{uuid4().hex}"
    _set_current_run(pg_engine, session_id, run_id=current_run_id, revision=2)
    stale_event = _make_callback_event(
        pg_engine,
        business_session_id=session_id,
        recommendation_id=accepted_event.recommendation_id,
        run_id=accepted_event.dify_run_id,
        revision=1,
        expired=True,
    )
    with Session(pg_engine) as db, db.begin():
        recommendation = db.execute(
            select(Recommendation).where(Recommendation.business_session_id == session_id)
        ).scalar_one()
        recommendation.state = {"stage": "current-run-marker"}

    with pytest.raises(DomainError) as stale:
        service.accept_business_event(stale_event)
    assert stale.value.status_code == 409
    assert stale.value.code == "CALLBACK_STALE"
    with Session(pg_engine) as db:
        session = db.get(BusinessSession, session_id)
        recommendation = db.execute(
            select(Recommendation).where(Recommendation.business_session_id == session_id)
        ).scalar_one()
        assert session is not None and session.dify_run_id == current_run_id
        assert recommendation.revision == 2
        assert recommendation.dify_run_id == current_run_id
        assert recommendation.state == {"stage": "current-run-marker"}


def test_business_session_api_returns_404_to_another_owner(pg_engine: Engine) -> None:
    owner_client, owner_id = _new_guest(pg_engine)
    reference_set_id = _create_reference_set(pg_engine, owner_id)
    service: BusinessSessionService = owner_client.app.state.business_session_service
    created = service.create_session(
        owner_user_id=owner_id,
        reference_set_id=reference_set_id,
        idempotency_key=f"owner-session-{uuid4().hex}",
    )
    other_client, other_owner_id = _new_guest(pg_engine)
    assert other_owner_id != owner_id

    response = other_client.get(f"/api/v1/business-sessions/{created.business_session_id}")

    assert response.status_code == 404
    assert response.json()["code"] == "BUSINESS_SESSION_NOT_FOUND"


def test_testclient_create_callback_read_and_selection_workflow(pg_engine: Engine) -> None:
    client, owner_id = _new_guest(pg_engine)
    reference_set_id = _create_reference_set(pg_engine, owner_id)
    create_response = client.post(
        "/api/v1/business-sessions",
        json={"reference_set_id": str(reference_set_id)},
        headers={"Origin": TRUSTED_ORIGIN, "Idempotency-Key": f"api-create-{uuid4().hex}"},
    )
    assert create_response.status_code == 202
    created = create_response.json()
    session_id = UUID(created["business_session_id"])
    recommendation_id = UUID(created["recommendation_id"])

    # Model the persisted result of the stub start Action without an external call.
    run_id = f"stub-api-run-{uuid4().hex}"
    _set_current_run(pg_engine, session_id, run_id=run_id, revision=1)
    callback = _make_callback_event(
        pg_engine,
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
        revision=1,
    )
    callback_response = client.post(
        "/internal/v1/dify/business-events",
        json=callback.model_dump(mode="json"),
        headers={"Authorization": f"Bearer {TEST_INTERNAL_CALLBACK_SECRET}"},
    )
    assert callback_response.status_code == 200
    assert callback_response.json()["status"] == "accepted"

    public_response = client.get(f"/api/v1/business-sessions/{session_id}")
    assert public_response.status_code == 200
    public_session = public_response.json()
    assert public_session["recommendation"]["recommendation_id"] == str(recommendation_id)
    assert public_session["recommendation"]["original_recommendation"]["category_snapshot"] == (
        "测试品类"
    )
    assert "dify_run_id" not in public_session

    selection_request = _selection_request(recommendation_id)
    selection_key = f"api-select-{uuid4().hex}"
    selection_response = client.post(
        f"/api/v1/business-sessions/{session_id}/selection",
        json=selection_request.model_dump(mode="json"),
        headers={"Origin": TRUSTED_ORIGIN, "Idempotency-Key": selection_key},
    )
    assert selection_response.status_code == 202
    selected = selection_response.json()
    assert selected["status"] == "queued"

    selection_replay = client.post(
        f"/api/v1/business-sessions/{session_id}/selection",
        json=selection_request.model_dump(mode="json"),
        headers={"Origin": TRUSTED_ORIGIN, "Idempotency-Key": selection_key},
    )
    assert selection_replay.status_code == 202
    assert selection_replay.json()["status"] == "replayed"
    assert selection_replay.json()["task_id"] == selected["task_id"]
