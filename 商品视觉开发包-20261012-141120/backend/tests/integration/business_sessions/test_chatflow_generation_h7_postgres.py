from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import UUID, uuid4, uuid5

import pytest
from app.api.internal_dify import create_internal_dify_router
from app.api.tasks import router as tasks_router
from app.core.domain_errors import DomainError
from app.db.action_models import Action
from app.db.chatflow_generation_models import (
    ChatflowGenerationDelivery,
    ChatflowGenerationRequestSlot,
    ChatflowGenerationTracking,
)
from app.db.chatflow_generation_models import (
    ChatflowGenerationResultManifest as ResultManifestRow,
)
from app.db.generation_models import GenerationResult
from app.db.outbox_models import OutboxEvent, OutboxReceipt
from app.db.task_models import Task, TaskItem
from app.integrations.storage.local import LocalPrivateStorage
from app.integrations.storage.protocol import StorageInventoryEntry
from app.schemas.chatflow_generation import (
    ChatflowGenerationEvent,
    ChatflowGenerationSlotEvent,
    chatflow_generation_event_digest,
    service_generation_status,
)
from app.schemas.chatflow_generation_results import (
    ChatflowGenerationManifestSlot,
    ChatflowGenerationResultManifest,
    ChatflowGenerationSourceArtifact,
)
from app.services.chatflow_generation import ChatflowGenerationEventService
from app.services.chatflow_generation_actions import (
    GENERATION_RECOVERY_ACTION,
    build_generation_recovery_registration,
)
from app.services.chatflow_generation_delivery_actions import (
    DELIVERY_CONFIRM_ACTION,
    DELIVERY_DOWNLOAD_ACTION,
    DeliveryPortError,
    build_generation_delivery_registrations,
)
from app.services.chatflow_generation_results import ChatflowGenerationResultService
from app.services.retention_cleanup import _inventory_entry_is_protected
from app.services.tasks import TaskOptions, TaskService
from app.workers.action_processor import (
    ActionProcessor,
    ActionRegistration,
)
from app.workers.messages import OutboxMessage
from app.workers.options import QueueOptions
from pydantic import SecretStr
from sqlalchemy import func, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from tests.integration.business_sessions.test_business_sessions_postgres import _new_guest
from tests.integration.business_sessions.test_chatflow_generation_h6_postgres import (
    _h5_snapshot,
)
from tests.integration.business_sessions.test_chatflow_prompt_packages_postgres import (
    TEST_INTERNAL_CALLBACK_SECRET,
    _post_submission,
    _ready_h4_submission,
)

pytest_plugins = ["tests.integration.business_sessions.test_chatflow_prompt_packages_postgres"]

_DELIVERY_NAMESPACE = UUID("df2c1369-8087-4f3b-a219-9dbde02b3025")


@pytest.fixture
def h7_pg_engine(h4_pg_engine: Engine) -> Engine:
    required = {
        "chatflow_generation_result_manifests",
        "chatflow_generation_deliveries",
    }
    if not required.issubset(set(inspect(h4_pg_engine).get_table_names())):
        pytest.skip("requires isolated PostgreSQL schema through S018 H7 delivery")
    return h4_pg_engine


def _png_bytes() -> bytes:
    from PIL import Image

    image = Image.new("RGB", (32, 24), (30, 90, 170))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _h7_context(
    engine: Engine,
    *,
    completed_positions: set[int] | None = None,
    expired_source: bool = False,
):
    client, _h4_service, task_id, session_id, submission = _ready_h4_submission(engine)
    accepted = _post_submission(client, submission)
    assert accepted.status_code == 200, accepted.text
    h4_body = accepted.json()
    image_bytes = _png_bytes()
    source_digest = hashlib.sha256(image_bytes).hexdigest()
    done = completed_positions or {1}
    result_slots: list[ChatflowGenerationManifestSlot] = []
    event_slots: list[ChatflowGenerationSlotEvent] = []
    expiry = datetime.now(UTC) - timedelta(minutes=1) if expired_source else None
    for raw in h4_body["slots"]:
        position = raw["position"]
        status = "completed" if position in done else "queued"
        version = 2 if status == "completed" else 1
        status_slot = ChatflowGenerationSlotEvent(
            slot_id=raw["slot_id"],
            task_item_id=UUID(raw["task_item_id"]),
            attempt_id=UUID(raw["attempt_id"]),
            position=position,
            image_type=raw["image_type"],
            status=status,
            status_version=version,
            known_retry_count=0,
        )
        event_slots.append(status_slot)
        artifact = (
            ChatflowGenerationSourceArtifact(
                file_id=f"{raw['slot_id']}-original-1.png",
                sha256=source_digest,
                mime_type="image/png",
                size_bytes=len(image_bytes),
                expires_at=expiry,
            )
            if status == "completed"
            else None
        )
        result_slots.append(
            ChatflowGenerationManifestSlot(
                slot_id=raw["slot_id"],
                task_item_id=UUID(raw["task_item_id"]),
                attempt_id=UUID(raw["attempt_id"]),
                position=position,
                image_type=raw["image_type"],
                status_version=version,
                original_artifact=artifact,
            )
        )
    event = ChatflowGenerationEvent.model_validate(
        {
            "contract_version": "chatflow-generation-event-v1",
            "business_session_id": session_id,
            "task_id": task_id,
            "generation_request_id": UUID(h4_body["generation_request_id"]),
            "dify_run_id": submission.dify_run_id,
            "batch_id": f"batch-h7-{h4_body['generation_request_id']}",
            "permit_version": 2,
            "permit_contract_version": "chatflow-generation-permit-v2",
            "package_digest": h4_body["package_digest"],
            "slot_mapping_digest": h4_body["permit"]["slot_mapping_digest"],
            "reference_manifest_digest": h4_body["permit"]["reference_manifest_digest"],
            "event_id": uuid4(),
            "source": "image_generation",
            "source_sequence": 1,
            "status": service_generation_status(event_slots),
            "digest_version": 1,
            "digest": "0" * 64,
            "slots": event_slots,
        },
        strict=True,
    )
    event = event.model_copy(update={"digest": chatflow_generation_event_digest(event)})
    manifest = ChatflowGenerationResultManifest(
        contract_version="chatflow-generation-result-manifest-v1",
        status_event=event,
        slots=result_slots,
    )
    event_service = ChatflowGenerationEventService(engine)
    result_service = ChatflowGenerationResultService(engine, generation_event_service=event_service)
    client.app.include_router(
        create_internal_dify_router(
            engine=engine,
            callback_credential=SecretStr(TEST_INTERNAL_CALLBACK_SECRET),
            generation_event_service=event_service,
            generation_result_service=result_service,
        )
    )
    return {
        "client": client,
        "task_id": task_id,
        "session_id": session_id,
        "owner_id": _task_owner(engine, task_id),
        "request_id": event.generation_request_id,
        "event": event,
        "manifest": manifest,
        "h4_body": h4_body,
        "submission": submission,
        "service": result_service,
        "bytes": image_bytes,
        "sha256": source_digest,
    }


def _task_owner(engine: Engine, task_id: UUID) -> UUID:
    with Session(engine) as db:
        task = db.get(Task, task_id)
        assert task is not None
        return task.owner_user_id


class _FakeDeliveryPort:
    def __init__(self, content: bytes, *, not_found_count: int = 0) -> None:
        self.content = content
        self.not_found_count = not_found_count
        self.get_calls: list[tuple[str, str, str]] = []
        self.confirm_calls: list[tuple[str, str, dict[str, str]]] = []

    def get_artifact(
        self,
        batch_id: str,
        slot_id: str,
        file_id: str,
        *,
        contract_version: str,
    ):
        assert contract_version == "chatflow-image-generation-v2"
        self.get_calls.append((batch_id, slot_id, file_id))
        if len(self.get_calls) <= self.not_found_count:
            raise DeliveryPortError("IMAGE_SOURCE_UNAVAILABLE")
        return self.content, "image/png"

    def confirm_delivery(self, batch_id: str, slot_id: str, payload, *, contract_version: str):
        assert contract_version == "chatflow-image-generation-v2"
        self.confirm_calls.append((batch_id, slot_id, dict(payload)))


def _queue_options() -> QueueOptions:
    return QueueOptions(
        broker_url="amqp://integration:integration@localhost:5672/image-backend-test",
        queue_name="generation-delivery-integration",
    )


def _message_for_action(engine: Engine, action_id: UUID) -> dict[str, object]:
    with Session(engine) as db:
        action = db.get(Action, action_id)
        assert action is not None
        event = db.get(OutboxEvent, action.outbox_event_id)
        assert event is not None
        return OutboxMessage(
            event_id=event.id,
            aggregate_id=event.aggregate_id,
            operation_version=event.operation_version,
            schema_version=event.schema_version,
            payload={key: UUID(value) for key, value in event.payload.items()},
        ).model_dump(mode="json")


def _delivery_action(engine: Engine, request_id: UUID, kind: str, sequence: int = 1) -> Action:
    with Session(engine) as db:
        return db.execute(
            select(Action).where(
                Action.generation_request_id == request_id,
                Action.action_kind == kind,
                Action.sequence == sequence,
            )
        ).scalar_one()


def _assert_h7_parent_tables(engine: Engine) -> None:
    names = set(inspect(engine).get_table_names())
    assert "chatflow_generation_deliveries" in names


def test_manifest_acceptance_is_authenticated_idempotent_and_concurrency_safe(
    h7_pg_engine: Engine,
) -> None:
    ctx = _h7_context(h7_pg_engine)
    client = ctx["client"]
    manifest: ChatflowGenerationResultManifest = ctx["manifest"]
    request_id = ctx["request_id"]
    task_id = ctx["task_id"]
    event = ctx["event"]
    _assert_h7_parent_tables(h7_pg_engine)
    endpoint = "/internal/v1/dify/generation-results"
    body = manifest.model_dump(mode="json")
    assert client.post(endpoint, json=body).status_code == 401
    headers = {"Authorization": f"Bearer {TEST_INTERNAL_CALLBACK_SECRET}"}
    accepted = client.post(endpoint, json=body, headers=headers)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "accepted"
    assert accepted.json()["manifest_status"] == "durably_received"
    assert accepted.json()["image_save_status"] == "pending"

    with Session(h7_pg_engine) as db:
        item_rows = (
            db.execute(
                select(TaskItem).where(TaskItem.task_id == task_id).order_by(TaskItem.position)
            )
            .scalars()
            .all()
        )
        assert item_rows[0].preview_ready is False
        assert all(not item.preview_ready for item in item_rows)
        assert (
            db.scalar(
                select(func.count(GenerationResult.attempt_id)).where(
                    GenerationResult.task_id == task_id
                )
            )
            == 0
        )
        delivery = db.execute(
            select(ChatflowGenerationDelivery).where(
                ChatflowGenerationDelivery.generation_request_id == request_id
            )
        ).scalar_one()
        assert delivery.status == "pending_download"
        assert delivery.original_asset_id is None and delivery.preview_asset_id is None
        assert delivery.source_file_id == f"{delivery.slot_id}-original-1.png"
        assert (
            db.scalar(
                select(func.count(ResultManifestRow.source_sequence)).where(
                    ResultManifestRow.generation_request_id == request_id
                )
            )
            == 1
        )
        h6_action_count = db.scalar(
            select(func.count(Action.id)).where(
                Action.scope_kind == "generation_request",
                Action.generation_request_id == request_id,
            )
        )
        h7_actions = (
            db.execute(
                select(Action).where(
                    Action.scope_kind == "generation_delivery",
                    Action.generation_request_id == request_id,
                    Action.generation_delivery_id == delivery.id,
                )
            )
            .scalars()
            .all()
        )
        assert h6_action_count == 1
        assert len(h7_actions) == 1
        assert h7_actions[0].action_kind == DELIVERY_DOWNLOAD_ACTION
        assert h7_actions[0].sequence == 1
        outbox_count = db.scalar(
            select(func.count(OutboxEvent.id)).where(
                OutboxEvent.aggregate_id == h7_actions[0].id,
                OutboxEvent.event_type == "task.action.v1",
            )
        )
        assert outbox_count == 1
        entry = StorageInventoryEntry(
            kind="object",
            relative_locator=delivery.original_object_key,
            object_key=delivery.original_object_key,
            modified_at=datetime.now(UTC),
            size_bytes=0,
        )
        assert _inventory_entry_is_protected(db, entry, now=datetime.now(UTC)) is True

    # A lost response and concurrent duplicate callbacks must converge on the
    # same immutable manifest, delivery, Action, and outbox event.
    with ThreadPoolExecutor(max_workers=4) as pool:
        replay_results = list(pool.map(lambda _index: ctx["service"].accept(manifest), range(4)))
    assert all(result.status == "replayed" for result in replay_results)
    replay = client.post(endpoint, json=body, headers=headers)
    assert replay.status_code == 200 and replay.json()["status"] == "replayed"

    changed_slot = manifest.slots[0].model_copy(
        update={
            "original_artifact": manifest.slots[0].original_artifact.model_copy(
                update={"sha256": "f" * 64}
            )
        }
    )
    changed = manifest.model_copy(update={"slots": [changed_slot, *manifest.slots[1:]]})
    with pytest.raises(DomainError) as conflict:
        ctx["service"].accept(changed)
    assert conflict.value.status_code == 409
    with Session(h7_pg_engine) as db:
        assert (
            db.scalar(
                select(func.count(ChatflowGenerationDelivery.id)).where(
                    ChatflowGenerationDelivery.generation_request_id == request_id
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(Action.id)).where(
                    Action.scope_kind == "generation_delivery",
                    Action.generation_request_id == request_id,
                )
            )
            == 1
        )
        assert event.source_sequence == 1


def test_h6_readonly_get_uses_the_same_result_receipt_and_download_intents(
    h7_pg_engine: Engine,
    tmp_path,
) -> None:
    ctx = _h7_context(h7_pg_engine, completed_positions=set(range(1, 9)))
    event: ChatflowGenerationEvent = ctx["event"]
    request_id = ctx["request_id"]
    h6_snapshot = _h5_snapshot(event)
    h6_snapshot["slots"] = [
        {
            **slot.model_dump(mode="json"),
            "artifact_ref": [
                {
                    "kind": "original",
                    "file_id": f"{slot.slot_id}-original-1.png",
                    "sha256": ctx["sha256"],
                    "mime_type": "image/png",
                    "size_bytes": len(ctx["bytes"]),
                    "expires_at": None,
                }
            ],
        }
        for slot in event.slots
    ]

    class ReadOnlyPort:
        def __init__(self) -> None:
            self.request_reads: list[UUID] = []
            self.batch_reads: list[str] = []

        def get_by_request(self, generation_request_id: UUID, *, contract_version: str):
            assert contract_version == "chatflow-image-generation-v2"
            self.request_reads.append(generation_request_id)
            return h6_snapshot

        def get_by_batch(self, batch_id: str, *, contract_version: str):
            assert contract_version == "chatflow-image-generation-v2"
            self.batch_reads.append(batch_id)
            raise AssertionError("the H4 recovery Action has no established batch yet")

    port = ReadOnlyPort()
    storage = LocalPrivateStorage(tmp_path / "private-chatflow-h7-get")
    registrations = {
        GENERATION_RECOVERY_ACTION: build_generation_recovery_registration(port),
        **build_generation_delivery_registrations(_FakeDeliveryPort(ctx["bytes"]), storage),
    }
    recovery = _delivery_action(h7_pg_engine, request_id, GENERATION_RECOVERY_ACTION)
    processor = ActionProcessor(h7_pg_engine, _queue_options(), registrations)
    recovery_message = _message_for_action(h7_pg_engine, recovery.id)
    with Session(h7_pg_engine) as db, db.begin():
        action = db.get(Action, recovery.id)
        recovery_event = db.get(OutboxEvent, recovery.outbox_event_id)
        receipt = db.get(OutboxReceipt, recovery.outbox_event_id)
        assert action is not None and recovery_event is not None and receipt is not None
        due_now = db.scalar(select(func.clock_timestamp()))
        action.due_at = due_now
        recovery_event.available_at = due_now
        receipt.available_at = due_now
    assert processor.process_payload(recovery_message).status == "completed"
    assert port.request_reads == [request_id]
    assert port.batch_reads == []

    # A Dify callback for the same snapshot reuses the GET receipt and does not
    # enqueue a second set of downloads.
    assert ctx["service"].accept(ctx["manifest"]).status == "replayed"
    with Session(h7_pg_engine) as db:
        tracking = db.execute(
            select(ChatflowGenerationTracking).where(
                ChatflowGenerationTracking.generation_request_id == request_id
            )
        ).scalar_one()
        deliveries = (
            db.execute(
                select(ChatflowGenerationDelivery).where(
                    ChatflowGenerationDelivery.generation_request_id == request_id
                )
            )
            .scalars()
            .all()
        )
        slot_count = db.scalar(
            select(func.count(ChatflowGenerationRequestSlot.slot_id)).where(
                ChatflowGenerationRequestSlot.generation_request_id == request_id
            )
        )
        assert tracking.batch_id == event.batch_id
        assert tracking.status == "completed"
        assert len(deliveries) == slot_count
        assert all(row.status == "pending_download" for row in deliveries)
        assert all(row.source_file_id == f"{row.slot_id}-original-1.png" for row in deliveries)
        assert (
            db.scalar(
                select(func.count(ResultManifestRow.source_sequence)).where(
                    ResultManifestRow.generation_request_id == request_id
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(Action.id)).where(
                    Action.scope_kind == "generation_delivery",
                    Action.generation_request_id == request_id,
                    Action.action_kind == DELIVERY_DOWNLOAD_ACTION,
                )
            )
            == slot_count
        )
        assert (
            db.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.event_type == "task.action.v1",
                    OutboxEvent.aggregate_id.in_(
                        select(Action.id).where(
                            Action.scope_kind == "generation_delivery",
                            Action.generation_request_id == request_id,
                        )
                    ),
                )
            )
            == slot_count
        )


def test_late_lower_source_sequence_cannot_replace_saved_image(
    h7_pg_engine: Engine,
    tmp_path,
) -> None:
    ctx = _h7_context(h7_pg_engine)
    base_event: ChatflowGenerationEvent = ctx["event"]
    latest_event = base_event.model_copy(
        update={"event_id": uuid4(), "source_sequence": 2, "digest": "0" * 64}
    )
    latest_event = latest_event.model_copy(
        update={"digest": chatflow_generation_event_digest(latest_event)}
    )
    latest_manifest = ctx["manifest"].model_copy(update={"status_event": latest_event})
    assert ctx["service"].accept(latest_manifest).status == "accepted"
    request_id = ctx["request_id"]
    action = _delivery_action(h7_pg_engine, request_id, DELIVERY_DOWNLOAD_ACTION)
    storage = LocalPrivateStorage(tmp_path / "private-chatflow-h7-late-sequence")
    processor = ActionProcessor(
        h7_pg_engine,
        _queue_options(),
        build_generation_delivery_registrations(_FakeDeliveryPort(ctx["bytes"]), storage),
    )
    assert processor.process_payload(_message_for_action(h7_pg_engine, action.id)).status == (
        "completed"
    )

    with Session(h7_pg_engine) as db:
        delivery = db.execute(
            select(ChatflowGenerationDelivery).where(
                ChatflowGenerationDelivery.generation_request_id == request_id
            )
        ).scalar_one()
        item = db.get(TaskItem, delivery.task_item_id)
        assert item is not None and item.preview_ready
        saved_identity = (
            delivery.source_sequence,
            delivery.source_file_id,
            delivery.source_sha256,
            delivery.original_asset_id,
            delivery.preview_asset_id,
            item.image_id,
        )
        assert delivery.status == "awaiting_confirmation"
        assert saved_identity[0] == 2

    old_event_slots = [
        slot.model_copy(update={"status_version": 1}) if slot.position == 1 else slot
        for slot in latest_event.slots
    ]
    old_event = latest_event.model_copy(
        update={
            "event_id": uuid4(),
            "source_sequence": 1,
            "slots": old_event_slots,
            "status": service_generation_status(old_event_slots),
            "digest": "0" * 64,
        }
    )
    old_event = old_event.model_copy(update={"digest": chatflow_generation_event_digest(old_event)})
    old_manifest_slots = [
        slot.model_copy(
            update={
                "status_version": 1,
                "original_artifact": slot.original_artifact.model_copy(update={"sha256": "f" * 64}),
            }
        )
        if slot.position == 1
        else slot
        for slot in latest_manifest.slots
    ]
    old_manifest = latest_manifest.model_copy(
        update={"status_event": old_event, "slots": old_manifest_slots}
    )
    assert ctx["service"].accept(old_manifest).status == "accepted"

    with Session(h7_pg_engine) as db:
        delivery = db.execute(
            select(ChatflowGenerationDelivery).where(
                ChatflowGenerationDelivery.generation_request_id == request_id
            )
        ).scalar_one()
        item = db.get(TaskItem, delivery.task_item_id)
        assert item is not None and item.preview_ready
        assert (
            delivery.source_sequence,
            delivery.source_file_id,
            delivery.source_sha256,
            delivery.original_asset_id,
            delivery.preview_asset_id,
            item.image_id,
        ) == saved_identity
        assert delivery.status == "awaiting_confirmation"
        assert (
            db.scalar(
                select(func.count(Action.id)).where(
                    Action.generation_delivery_id == delivery.id,
                    Action.action_kind == DELIVERY_DOWNLOAD_ACTION,
                )
            )
            == 1
        )


def test_private_save_commit_preview_isolation_ack_and_rollback_replay(
    h7_pg_engine: Engine,
    tmp_path,
) -> None:
    ctx = _h7_context(h7_pg_engine)
    task_id = ctx["task_id"]
    request_id = ctx["request_id"]
    owner_id = ctx["owner_id"]
    accepted = ctx["service"].accept(ctx["manifest"])
    assert accepted.status == "accepted"
    client = ctx["client"]
    delivery_action = _delivery_action(h7_pg_engine, request_id, DELIVERY_DOWNLOAD_ACTION)
    download_message = _message_for_action(h7_pg_engine, delivery_action.id)
    storage = LocalPrivateStorage(tmp_path / "private-chatflow-h7")
    client.app.state.task_service = TaskService(h7_pg_engine, TaskOptions())
    client.app.state.storage = storage
    client.app.include_router(tasks_router)
    port = _FakeDeliveryPort(ctx["bytes"])
    registration = build_generation_delivery_registrations(port, storage)[DELIVERY_DOWNLOAD_ACTION]

    # The source bytes and both immutable objects are written, then the DB
    # association transaction is forced to roll back. No preview may appear.
    def fail_after_database_mutation(db, action, action_input, result):
        registration.finalize(db, action, action_input, result)
        raise RuntimeError("isolated injected H7 association rollback")

    failing_registration = ActionRegistration(
        kind=registration.kind,
        prepare=registration.prepare,
        execute=registration.execute,
        recover=registration.recover,
        finalize=fail_after_database_mutation,
        scope_kinds=registration.scope_kinds,
    )
    failing_processor = ActionProcessor(
        h7_pg_engine,
        _queue_options(),
        {DELIVERY_DOWNLOAD_ACTION: failing_registration},
    )
    claim, claim_result = failing_processor._claim(OutboxMessage.model_validate(download_message))
    assert claim is not None, claim_result
    execution = failing_processor._run_io(claim.registration.execute, claim.action_input)
    assert execution.outcome == "accepted"
    action_payload = claim.action_input.payload
    assert isinstance(action_payload, dict) or hasattr(action_payload, "__getitem__")
    original_key = str(action_payload["original_object_key"])
    preview_key = str(action_payload["preview_object_key"])
    original_asset_id = uuid5(_DELIVERY_NAMESPACE, f"{action_payload['attempt_id']}:original")
    preview_path = f"/api/v1/tasks/{task_id}/images/{original_asset_id}/preview"
    assert storage.stat(original_key) is not None
    assert storage.stat(preview_key) is not None
    assert client.get(preview_path).status_code == 404
    with pytest.raises(DomainError) as unavailable_before_commit:
        TaskService(h7_pg_engine, TaskOptions()).open_preview(
            owner_user_id=owner_id,
            task_id=task_id,
            image_id=original_asset_id,
            storage=storage,
        )
    assert unavailable_before_commit.value.code == "PREVIEW_NOT_FOUND"
    with pytest.raises(RuntimeError, match="injected H7 association rollback"):
        failing_processor._finalize(claim, execution)
    with Session(h7_pg_engine) as db:
        delivery = db.execute(
            select(ChatflowGenerationDelivery).where(
                ChatflowGenerationDelivery.generation_request_id == request_id
            )
        ).scalar_one()
        item = db.get(TaskItem, delivery.task_item_id)
        assert delivery.status == "pending_download"
        assert delivery.original_asset_id is None
        assert item is not None and not item.preview_ready
        assert (
            db.scalar(
                select(func.count(GenerationResult.attempt_id)).where(
                    GenerationResult.task_id == task_id
                )
            )
            == 0
        )
        assert original_key == delivery.original_object_key
        assert preview_key == delivery.preview_object_key
    assert storage.stat(original_key) is not None
    assert storage.stat(preview_key) is not None
    with pytest.raises(DomainError) as unavailable_before_commit:
        TaskService(h7_pg_engine, TaskOptions()).open_preview(
            owner_user_id=owner_id,
            task_id=task_id,
            image_id=original_asset_id,
            storage=storage,
        )
    assert unavailable_before_commit.value.code == "PREVIEW_NOT_FOUND"

    # Simulate a worker restart after lease expiry. The same download Action
    # fetches the same file and reuses its immutable keys; it creates no batch.
    with Session(h7_pg_engine) as db, db.begin():
        action = db.get(Action, delivery_action.id)
        receipt = db.get(OutboxReceipt, delivery_action.outbox_event_id)
        assert action is not None and receipt is not None
        action.lease_until = func.clock_timestamp() - timedelta(seconds=1)
        receipt.lease_until = func.clock_timestamp() - timedelta(seconds=1)
    processor = ActionProcessor(
        h7_pg_engine,
        _queue_options(),
        build_generation_delivery_registrations(port, storage),
    )
    assert processor.process_payload(download_message).status == "completed"
    assert len(port.get_calls) == 2

    with Session(h7_pg_engine) as db:
        delivery = db.execute(
            select(ChatflowGenerationDelivery).where(
                ChatflowGenerationDelivery.generation_request_id == request_id
            )
        ).scalar_one()
        items = (
            db.execute(
                select(TaskItem).where(TaskItem.task_id == task_id).order_by(TaskItem.position)
            )
            .scalars()
            .all()
        )
        assert delivery.status == "awaiting_confirmation"
        assert delivery.original_asset_id is not None and delivery.preview_asset_id is not None
        assert items[0].preview_ready is True
        assert all(not item.preview_ready for item in items[1:])
        result = db.execute(
            select(GenerationResult).where(GenerationResult.task_item_id == items[0].id)
        ).scalar_one()
        assert result.original_asset_id == delivery.original_asset_id
        assert result.preview_asset_id == delivery.preview_asset_id
        assert (
            db.scalar(
                select(func.count(Action.id)).where(
                    Action.scope_kind == "generation_request",
                    Action.generation_request_id == request_id,
                )
            )
            == 1
        )
        preview_image_id = items[0].image_id
        confirm_action = db.execute(
            select(Action).where(
                Action.generation_delivery_id == delivery.id,
                Action.action_kind == DELIVERY_CONFIRM_ACTION,
                Action.sequence == 2,
            )
        ).scalar_one()
    assert preview_image_id is not None
    owner_response = client.get(preview_path)
    assert owner_response.status_code == 200
    assert owner_response.headers["content-type"] == "image/webp"
    assert owner_response.content

    service = client.app.state.task_service
    preview_stream = service.open_preview(
        owner_user_id=owner_id,
        task_id=task_id,
        image_id=preview_image_id,
        storage=storage,
    )
    try:
        assert preview_stream.body.read()
    finally:
        preview_stream.body.close()
    _other_client, other_owner_id = _new_guest(h7_pg_engine)
    _other_client.app.state.task_service = service
    _other_client.app.state.storage = storage
    _other_client.app.include_router(tasks_router)
    assert _other_client.get(preview_path).status_code == 404
    with pytest.raises(DomainError) as cross_user:
        service.open_preview(
            owner_user_id=other_owner_id,
            task_id=task_id,
            image_id=preview_image_id,
            storage=storage,
        )
    assert cross_user.value.code == "PREVIEW_NOT_FOUND"

    confirm_message = _message_for_action(h7_pg_engine, confirm_action.id)
    assert processor.process_payload(confirm_message).status == "completed"
    assert len(port.confirm_calls) == 1
    replayed = ctx["service"].accept(ctx["manifest"])
    assert replayed.status == "replayed"
    assert processor.process_payload(download_message).status == "completed"
    assert processor.process_payload(confirm_message).status == "completed"
    assert len(port.get_calls) == 2
    assert len(port.confirm_calls) == 1
    with Session(h7_pg_engine) as db:
        delivery = db.execute(
            select(ChatflowGenerationDelivery).where(
                ChatflowGenerationDelivery.generation_request_id == request_id
            )
        ).scalar_one()
        assert delivery.status == "confirmed"
        assert delivery.confirmed_at is not None
        assert (
            db.scalar(
                select(func.count(Action.id)).where(
                    Action.generation_delivery_id == delivery.id,
                    Action.action_kind == DELIVERY_CONFIRM_ACTION,
                )
            )
            == 1
        )


def test_expired_source_metadata_still_uses_fixed_file_id_download(
    h7_pg_engine: Engine,
    tmp_path,
) -> None:
    ctx = _h7_context(h7_pg_engine, expired_source=True)
    accepted = ctx["service"].accept(ctx["manifest"])
    assert accepted.status == "accepted"
    request_id = ctx["request_id"]
    action = _delivery_action(h7_pg_engine, request_id, DELIVERY_DOWNLOAD_ACTION)
    port = _FakeDeliveryPort(ctx["bytes"])
    storage = LocalPrivateStorage(tmp_path / "private-chatflow-h7-expired-metadata")
    processor = ActionProcessor(
        h7_pg_engine,
        _queue_options(),
        build_generation_delivery_registrations(port, storage),
    )

    assert processor.process_payload(_message_for_action(h7_pg_engine, action.id)).status == (
        "completed"
    )
    with Session(h7_pg_engine) as db:
        delivery = db.execute(
            select(ChatflowGenerationDelivery).where(
                ChatflowGenerationDelivery.generation_request_id == request_id
            )
        ).scalar_one()
        item = db.get(TaskItem, delivery.task_item_id)
        assert delivery.source_expires_at is not None
        assert delivery.source_expires_at < datetime.now(UTC)
        assert delivery.status == "awaiting_confirmation"
        assert item is not None and item.preview_ready
        assert (
            db.scalar(
                select(func.count(Action.id)).where(
                    Action.scope_kind == "generation_request",
                    Action.generation_request_id == request_id,
                )
            )
            == 1
        )
    slot = ctx["event"].slots[0]
    assert port.get_calls == [
        (ctx["event"].batch_id, slot.slot_id, f"{slot.slot_id}-original-1.png")
    ]


def test_expired_or_unavailable_file_id_404_retries_only_same_download_action(
    h7_pg_engine: Engine,
    tmp_path,
) -> None:
    ctx = _h7_context(h7_pg_engine, expired_source=True)
    request_id = ctx["request_id"]
    accepted = ctx["service"].accept(ctx["manifest"])
    assert accepted.status == "accepted"
    with Session(h7_pg_engine) as db:
        delivery = db.execute(
            select(ChatflowGenerationDelivery).where(
                ChatflowGenerationDelivery.generation_request_id == request_id
            )
        ).scalar_one()
        assert delivery.source_expires_at is not None
        assert delivery.source_expires_at < datetime.now(UTC)
        assert delivery.status == "pending_download"
    port = _FakeDeliveryPort(ctx["bytes"], not_found_count=5)
    storage = LocalPrivateStorage(tmp_path / "private-chatflow-h7-404")
    processor = ActionProcessor(
        h7_pg_engine,
        _queue_options(),
        build_generation_delivery_registrations(port, storage),
    )
    first_action = _delivery_action(h7_pg_engine, request_id, DELIVERY_DOWNLOAD_ACTION)
    first_message = _message_for_action(h7_pg_engine, first_action.id)
    assert processor.process_payload(first_message).status == "completed"
    with Session(h7_pg_engine) as db:
        delivery = db.execute(
            select(ChatflowGenerationDelivery).where(
                ChatflowGenerationDelivery.generation_request_id == request_id
            )
        ).scalar_one()
        assert delivery.status == "pending_download"
        assert delivery.download_attempt_count == 1
        actions = (
            db.execute(
                select(Action)
                .where(Action.generation_delivery_id == delivery.id)
                .order_by(Action.sequence)
            )
            .scalars()
            .all()
        )
        assert [action.sequence for action in actions] == [1, 2]
        assert actions[0].status == "completed"
        assert actions[1].status == "scheduled"
        assert (
            db.scalar(
                select(func.count(Action.id)).where(
                    Action.scope_kind == "generation_request",
                    Action.generation_request_id == request_id,
                )
            )
            == 1
        )
        item = db.get(TaskItem, delivery.task_item_id)
        assert item is not None and not item.preview_ready
        assert (
            db.scalar(
                select(func.count(GenerationResult.attempt_id)).where(
                    GenerationResult.task_id == ctx["task_id"]
                )
            )
            == 0
        )
    assert len(port.get_calls) == 1
    assert port.confirm_calls == []

    # Advance the scheduled delivery Action through its bounded backoff slots
    # without sleeping. Exhaustion is durable and does not create new generation
    # work or query an untrusted source URL.
    for sequence in range(2, 6):
        with Session(h7_pg_engine) as db, db.begin():
            action = db.execute(
                select(Action).where(
                    Action.generation_delivery_id == delivery.id,
                    Action.action_kind == DELIVERY_DOWNLOAD_ACTION,
                    Action.sequence == sequence,
                )
            ).scalar_one()
            event = db.get(OutboxEvent, action.outbox_event_id)
            receipt = db.get(OutboxReceipt, action.outbox_event_id)
            assert event is not None and receipt is not None
            now = db.scalar(select(func.clock_timestamp()))
            action.due_at = now
            event.available_at = now
            receipt.available_at = now
            message = OutboxMessage(
                event_id=event.id,
                aggregate_id=event.aggregate_id,
                operation_version=event.operation_version,
                schema_version=event.schema_version,
                payload={key: UUID(value) for key, value in event.payload.items()},
            ).model_dump(mode="json")
        expected_action_status = "unknown" if sequence == 5 else "completed"
        assert processor.process_payload(message).status == expected_action_status
    assert len(port.get_calls) == 5
    with Session(h7_pg_engine) as db:
        delivery = db.execute(
            select(ChatflowGenerationDelivery).where(
                ChatflowGenerationDelivery.generation_request_id == request_id
            )
        ).scalar_one()
        assert delivery.status == "source_unavailable"
        assert delivery.download_attempt_count == 5
        assert (
            db.scalar(
                select(func.count(Action.id)).where(
                    Action.scope_kind == "generation_delivery",
                    Action.generation_delivery_id == delivery.id,
                    Action.action_kind == DELIVERY_DOWNLOAD_ACTION,
                )
            )
            == 5
        )
        assert (
            db.scalar(
                select(func.count(Action.id)).where(
                    Action.scope_kind == "generation_request",
                    Action.generation_request_id == request_id,
                )
            )
            == 1
        )
