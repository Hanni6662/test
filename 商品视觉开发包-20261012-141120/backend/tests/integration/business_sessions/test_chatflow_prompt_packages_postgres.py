from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from app.api.internal_dify import create_internal_dify_router
from app.api.internal_image_generation import create_internal_image_generation_router
from app.core.domain_errors import DomainError
from app.db.action_models import Action
from app.db.chatflow_generation_models import (
    ChatflowGenerationPermit,
    ChatflowGenerationRequest,
    ChatflowGenerationRequestSlot,
    ChatflowGenerationTracking,
    ChatflowPromptImage,
    ChatflowPromptPackage,
    ChatflowPromptReference,
)
from app.db.outbox_models import OutboxEvent, OutboxReceipt
from app.db.reference_models import ImageAsset, ReferenceSetImage
from app.db.task_models import Task, TaskItem, TaskItemAttempt
from app.schemas.chatflow_prompt_packages import (
    ChatflowPromptPackageSubmission,
)
from app.schemas.chatflow_prompt_packages import (
    ChatflowPromptReference as PromptReferenceSubmission,
)
from app.schemas.prompt_packages import PromptPackage
from app.schemas.prompt_workflow import canonical_sha256
from app.services.chatflow_prompt_packages import ChatflowPromptPackageService
from app.services.generation_runtime import GENERATION_ACTION_KINDS, GENERATION_START_EVENT_TYPE
from pydantic import SecretStr
from sqlalchemy import create_engine, event, func, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from tests.integration.business_sessions.test_business_questions_postgres import (
    _select_and_accept_continuation,
)
from tests.integration.business_sessions.test_business_sessions_postgres import (
    TEST_INTERNAL_CALLBACK_SECRET,
    _create_reference_set_with_content,
    _make_callback_event,
    _make_ready_business_session,
    _new_guest,
    _set_current_run,
)
from tests.unit.prompt_packages.test_prompt_validation import _package

TEST_GENERATION_PERMIT_SECRET = "h4-isolated-permit-secret-value-123456789"
TEST_SERVICE_AUTHORITY_ID = UUID("d04be537-6b6d-4696-a0e9-bf36d633ef34")
_REFERENCE_IDS = ("PRIMARY", "AUX01", "AUX02", "AUX03")
_PREFERENCE_KEYS = {
    "1": "language",
    "3": "usage_scene",
    "4": "visual_style",
    "5": "selling_points",
}
TEST_REFERENCE_SERVICE_TOKEN = "h8-reference-service-token-0123456789"


@pytest.fixture
def h4_pg_engine() -> Iterator[Engine]:
    database_url = os.getenv("IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL")
    if database_url is None:
        pytest.skip("requires the dedicated PostgreSQL test runtime URL")
    engine = create_engine(database_url, pool_pre_ping=True)
    required = {
        "users",
        "reference_sets",
        "image_assets",
        "reference_set_images",
        "recommendations",
        "business_sessions",
        "task_actions",
        "outbox_events",
        "tasks",
        "task_items",
        "task_item_attempts",
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
    if not required.issubset(set(inspect(engine).get_table_names())):
        engine.dispose()
        pytest.skip("requires isolated PostgreSQL schema through S015")
    yield engine
    engine.dispose()


def _ready_h4_submission(
    engine: Engine,
) -> tuple[object, ChatflowPromptPackageService, UUID, UUID, ChatflowPromptPackageSubmission]:
    client, owner_id, _reference_set_id, session_id, business_service, callback = (
        _make_ready_business_session(engine)
    )
    task_id = _select_and_accept_continuation(
        engine,
        session_id=session_id,
        owner_id=owner_id,
        service=business_service,
        callback=callback,
    )
    service = ChatflowPromptPackageService(
        engine,
        permit_secret=SecretStr(TEST_GENERATION_PERMIT_SECRET),
        service_authority_id=TEST_SERVICE_AUTHORITY_ID,
    )
    client.app.include_router(
        create_internal_dify_router(
            engine=engine,
            callback_credential=SecretStr(TEST_INTERNAL_CALLBACK_SECRET),
            prompt_package_service=service,
        )
    )
    submission = _submission_for_task(
        engine,
        task_id=task_id,
        session_id=session_id,
        callback=callback,
    )
    return client, service, task_id, session_id, submission


class _ReferenceBytesStore:
    def __init__(self, contents: dict[str, bytes]) -> None:
        self.contents = contents
        self.read_keys: list[str] = []

    def open_reader(self, key: str):
        self.read_keys.append(key)
        return io.BytesIO(self.contents[key])


def _png_bytes(color: tuple[int, int, int]) -> bytes:
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(output, format="PNG")
    return output.getvalue()


@pytest.mark.parametrize("reference_count", (1, 2, 3, 4))
def test_h5_reference_read_is_h4_bound_private_and_ordered(
    h4_pg_engine: Engine, reference_count: int
) -> None:
    client, owner_id = _new_guest(h4_pg_engine)
    reference_set_id, content_by_key = _create_reference_set_with_content(
        h4_pg_engine, owner_id, count=reference_count
    )
    expected_contents: list[bytes] = []
    colors = ((220, 20, 60), (20, 120, 220), (30, 170, 90), (230, 180, 20))
    with Session(h4_pg_engine) as db, db.begin():
        images = (
            db.execute(
                select(ReferenceSetImage)
                .where(ReferenceSetImage.reference_set_id == reference_set_id)
                .order_by(ReferenceSetImage.position)
            )
            .scalars()
            .all()
        )
        for image, color in zip(images, colors[:reference_count], strict=True):
            asset = db.get(ImageAsset, image.model_asset_id)
            assert asset is not None
            content = _png_bytes(color)
            digest = hashlib.sha256(content).hexdigest()
            asset.content_sha256 = digest
            asset.size_bytes = len(content)
            image.content_sha256 = digest
            content_by_key[asset.object_key] = content
            expected_contents.append(content)

    business_service = client.app.state.business_session_service
    created = business_service.create_session(
        owner_user_id=owner_id,
        reference_set_id=reference_set_id,
        idempotency_key=f"h5-reference-read-{uuid4().hex}",
    )
    run_id = f"h5-reference-run-{uuid4().hex}"
    _set_current_run(h4_pg_engine, created.business_session_id, run_id=run_id, revision=1)
    callback = _make_callback_event(
        h4_pg_engine,
        business_session_id=created.business_session_id,
        recommendation_id=created.recommendation_id,
        run_id=run_id,
        revision=1,
    )
    assert business_service.accept_business_event(callback).status == "accepted"
    task_id = _select_and_accept_continuation(
        h4_pg_engine,
        session_id=created.business_session_id,
        owner_id=owner_id,
        service=business_service,
        callback=callback,
    )
    prompt_service = ChatflowPromptPackageService(
        h4_pg_engine,
        permit_secret=SecretStr(TEST_GENERATION_PERMIT_SECRET),
        service_authority_id=TEST_SERVICE_AUTHORITY_ID,
    )
    client.app.include_router(
        create_internal_dify_router(
            engine=h4_pg_engine,
            callback_credential=SecretStr(TEST_INTERNAL_CALLBACK_SECRET),
            prompt_package_service=prompt_service,
        )
    )
    submission = _submission_for_task(
        h4_pg_engine,
        task_id=task_id,
        session_id=created.business_session_id,
        callback=callback,
    )
    h4_response = _post_submission(client, submission)
    assert h4_response.status_code == 200, h4_response.text
    generation_request_id = h4_response.json()["generation_request_id"]
    reference_store = _ReferenceBytesStore(content_by_key)
    client.app.include_router(
        create_internal_image_generation_router(
            engine=h4_pg_engine,
            storage=reference_store,
            service_credential=TEST_REFERENCE_SERVICE_TOKEN,
        )
    )

    with Session(h4_pg_engine) as db:
        expected_assets = (
            db.execute(
                select(ReferenceSetImage, ImageAsset)
                .join(ImageAsset, ImageAsset.id == ReferenceSetImage.model_asset_id)
                .where(ReferenceSetImage.reference_set_id == reference_set_id)
                .order_by(ReferenceSetImage.position)
            )
            .all()
        )
    for index, ((_image, asset), expected_content) in enumerate(
        zip(expected_assets, expected_contents, strict=True),
        1,
    ):
        path = (
            f"/internal/v1/image-generation/generation-requests/{generation_request_id}"
            f"/references/{index}/content"
        )
        expected_headers = {
            "X-Expected-Task-ID": str(task_id),
            "X-Expected-Asset-ID": str(asset.id),
            "X-Expected-Ref-ID": "PRIMARY" if index == 1 else f"AUX{index - 1:02d}",
            "X-Expected-SHA256": hashlib.sha256(expected_content).hexdigest(),
        }
        assert client.get(path, headers=expected_headers).status_code == 401
        response = client.get(
            path,
            headers={
                "Authorization": f"Bearer {TEST_REFERENCE_SERVICE_TOKEN}",
                **expected_headers,
            },
        )
        assert response.status_code == 200, response.text
        assert response.content == expected_content
        assert response.headers["content-type"] == "image/png"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-generation-request-id"] == generation_request_id
        assert response.headers["x-task-id"] == str(task_id)
        assert response.headers["x-asset-id"] == str(asset.id)
        assert response.headers["x-reference-position"] == str(index)
        assert response.headers["x-content-sha256"] == expected_headers["X-Expected-SHA256"]
        assert reference_store.read_keys.count(asset.object_key) == 1

        wrong_identity = client.get(
            path,
            headers={
                "Authorization": f"Bearer {TEST_REFERENCE_SERVICE_TOKEN}",
                **{**expected_headers, "X-Expected-Task-ID": str(uuid4())},
            },
        )
        assert wrong_identity.status_code == 404

    # A fresh source read is still denied after H4 permit expiry. H5 accepted
    # batch recovery is separately covered without calling this endpoint.
    with Session(h4_pg_engine) as db, db.begin():
        permit = db.execute(
            select(ChatflowGenerationPermit).where(
                ChatflowGenerationPermit.generation_request_id == generation_request_id
            )
        ).scalar_one()
        permit.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    _, first_asset = expected_assets[0]
    expired_read = client.get(
        f"/internal/v1/image-generation/generation-requests/{generation_request_id}"
        "/references/1/content",
        headers={
            "Authorization": f"Bearer {TEST_REFERENCE_SERVICE_TOKEN}",
            "X-Expected-Task-ID": str(task_id),
            "X-Expected-Asset-ID": str(first_asset.id),
            "X-Expected-Ref-ID": "PRIMARY",
            "X-Expected-SHA256": hashlib.sha256(expected_contents[0]).hexdigest(),
        },
    )
    assert expired_read.status_code == 404
    assert reference_store.read_keys == [asset.object_key for _, asset in expected_assets]

    # A different guest's PRIMARY asset cannot be substituted into this request,
    # even when the caller supplies the first task and position.
    other_owner_id = _new_guest(h4_pg_engine)[1]
    other_reference_set_id, other_content_by_key = _create_reference_set_with_content(
        h4_pg_engine, other_owner_id, count=1
    )
    with Session(h4_pg_engine) as db:
        other_asset = db.execute(
            select(ImageAsset)
            .join(ReferenceSetImage, ReferenceSetImage.model_asset_id == ImageAsset.id)
            .where(ReferenceSetImage.reference_set_id == other_reference_set_id)
        ).scalar_one()
    other_digest = other_asset.content_sha256
    assert other_digest is not None
    cross_guest = client.get(
        f"/internal/v1/image-generation/generation-requests/{generation_request_id}"
        "/references/1/content",
        headers={
            "Authorization": f"Bearer {TEST_REFERENCE_SERVICE_TOKEN}",
            "X-Expected-Task-ID": str(task_id),
            "X-Expected-Asset-ID": str(other_asset.id),
            "X-Expected-Ref-ID": "PRIMARY",
            "X-Expected-SHA256": other_digest,
        },
    )
    assert cross_guest.status_code == 404
    assert other_asset.object_key not in reference_store.read_keys
    assert reference_store.read_keys == [asset.object_key for _, asset in expected_assets]

    public_session = client.get(f"/api/v1/business-sessions/{created.business_session_id}")
    assert public_session.status_code == 200
    public = public_session.json()
    assert "dify_file_id" not in str(public)
    assert "dify_user_id" not in str(public)
    assert "generation_request_id" not in str(public)


def _submission_for_task(
    engine: Engine,
    *,
    task_id: UUID,
    session_id: UUID,
    callback,
) -> ChatflowPromptPackageSubmission:
    with Session(engine) as db:
        task = db.get(Task, task_id)
        items = (
            db.execute(
                select(TaskItem).where(TaskItem.task_id == task_id).order_by(TaskItem.position)
            )
            .scalars()
            .all()
        )
    assert task is not None
    assert [item.position for item in items] == list(range(1, len(items) + 1))
    assert [item.image_type for item in items] == list(task.image_types)

    package_dict, _reference_aliases, _relations = _package(
        list(task.image_types),
        platform=task.resolved_preferences["platform"]["value"],
        auxiliary_count=len(task.reference_assets_snapshot) - 1,
    )
    package_dict["basis"]["declared_category"] = task.category_snapshot
    for key, field_name in _PREFERENCE_KEYS.items():
        saved = task.resolved_preferences[field_name]
        mode = saved["mode"]
        package_dict["basis"]["preference_form"][key]["source"] = mode
        if mode == "D":
            package_dict["basis"]["preference_form"][key]["value"] = saved["instruction"]
        else:
            package_dict["basis"]["preference_form"][key]["value"] = saved["text"]
    package = PromptPackage.model_validate(package_dict, strict=True)
    references = [
        PromptReferenceSubmission(
            ref_id=_REFERENCE_IDS[position],
            asset_id=UUID(snapshot["model_asset_id"]),
            order=position + 1,
            content_sha256=snapshot["content_sha256"],
        )
        for position, snapshot in enumerate(task.reference_assets_snapshot)
    ]
    package_value = package.model_dump(mode="json")
    return ChatflowPromptPackageSubmission(
        contract_version="chatflow-handoff-v1",
        business_session_id=session_id,
        recommendation_id=callback.recommendation_id,
        recommendation_revision=callback.recommendation_revision,
        dify_run_id=callback.dify_run_id,
        task_id=task_id,
        submission_id=uuid4(),
        package_version="chatflow-prompt-package-v1",
        package_digest_version=1,
        package_digest=canonical_sha256(package_value).hex(),
        image_types=list(task.image_types),
        references=references,
        prompt_package=package,
    )


def _post_submission(client, submission: ChatflowPromptPackageSubmission):
    return client.post(
        "/internal/v1/dify/prompt-packages",
        json=submission.model_dump(mode="json"),
        headers={"Authorization": f"Bearer {TEST_INTERNAL_CALLBACK_SECRET}"},
    )


def _with_package(submission: ChatflowPromptPackageSubmission, package: PromptPackage):
    values = submission.model_dump(mode="python")
    values["prompt_package"] = package
    values["package_digest"] = canonical_sha256(package.model_dump(mode="json")).hex()
    return ChatflowPromptPackageSubmission.model_validate(values, strict=True)


def test_h4_commit_then_response_loss_replays_same_permit_and_slots(
    h4_pg_engine: Engine,
) -> None:
    client, _service, task_id, session_id, submission = _ready_h4_submission(h4_pg_engine)

    first = _post_submission(client, submission)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "accepted"
    first_body = first.json()

    # Treat the first response as lost; the caller retries the same Task and submission.
    replayed = _post_submission(client, submission)
    assert replayed.status_code == 200
    assert replayed.json()["status"] == "replayed"
    replay_body = replayed.json()
    assert replay_body["task_id"] == first_body["task_id"] == str(task_id)
    assert replay_body["submission_id"] == first_body["submission_id"]
    assert replay_body["generation_request_id"] == first_body["generation_request_id"]
    assert replay_body["slots"] == first_body["slots"]
    assert replay_body["permit"] == first_body["permit"]
    assert first_body["permit"]["permit_version"] == 2
    assert first_body["permit"]["contract_version"] == "chatflow-generation-permit-v2"
    assert len(first_body["permit"]["slot_mapping_digest"]) == 64
    assert len(first_body["permit"]["reference_manifest_digest"]) == 64
    assert "prompt_package" not in replay_body
    assert submission.prompt_package.basis.product not in replayed.text

    with Session(h4_pg_engine) as db:
        package_ids = select(ChatflowPromptPackage.id).where(
            ChatflowPromptPackage.task_id == task_id
        )
        item_ids = select(TaskItem.id).where(TaskItem.task_id == task_id)
        assert (
            db.scalar(
                select(func.count(ChatflowPromptPackage.id)).where(
                    ChatflowPromptPackage.task_id == task_id
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(ChatflowPromptImage.id)).where(
                    ChatflowPromptImage.task_id == task_id
                )
            )
            == len(submission.image_types)
        )
        assert db.scalar(
            select(func.count(ChatflowPromptReference.id)).where(
                ChatflowPromptReference.package_id.in_(package_ids)
            )
        ) == len(submission.references)
        assert (
            db.scalar(
                select(func.count(ChatflowGenerationRequest.id)).where(
                    ChatflowGenerationRequest.task_id == task_id
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(ChatflowGenerationRequestSlot.slot_id)).where(
                    ChatflowGenerationRequestSlot.task_id == task_id
                )
            )
            == len(submission.image_types)
        )
        assert (
            db.scalar(
                select(func.count(ChatflowGenerationPermit.permit_id)).where(
                    ChatflowGenerationPermit.task_id == task_id
                )
            )
            == 1
        )
        generation_request_id = UUID(first_body["generation_request_id"])
        recovery_action = db.execute(
            select(Action).where(
                Action.scope_kind == "generation_request",
                Action.generation_request_id == generation_request_id,
                Action.action_kind == "chatflow.generation.recover.v1",
            )
        ).scalar_one()
        assert recovery_action.sequence == 1
        assert recovery_action.status == "scheduled"
        assert db.get(ChatflowGenerationTracking, generation_request_id) is not None
        assert (
            db.scalar(
                select(func.count(TaskItemAttempt.id)).where(
                    TaskItemAttempt.task_item_id.in_(item_ids)
                )
            )
            == len(submission.image_types)
        )
        assert (
            db.scalar(
                select(func.count(Action.id)).where(
                    Action.task_id == task_id,
                    Action.action_kind.in_(GENERATION_ACTION_KINDS),
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.event_type == GENERATION_START_EVENT_TYPE,
                    OutboxEvent.aggregate_id == task_id,
                )
            )
            == 0
        )

    public = client.get(f"/api/v1/business-sessions/{session_id}")
    assert public.status_code == 200
    assert submission.prompt_package.basis.product not in public.text
    assert first_body["permit"]["token"] not in public.text


def _legacy_h4_v1_token(
    *, permit: dict[str, object], package_digest: str, task_id: UUID, request_id: UUID
) -> str:
    payload = {
        "contract_version": "chatflow-handoff-v1",
        "expires_at": permit["expires_at"],
        "generation_request_id": str(request_id),
        "package_digest": package_digest,
        "permit_id": permit["permit_id"],
        "task_id": str(task_id),
        "token_version": 1,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).rstrip(b"=")
    signature = hmac.digest(
        TEST_GENERATION_PERMIT_SECRET.encode("ascii"),
        b"chatflow-generation-permit-v1." + encoded,
        "sha256",
    )
    return (
        f"h4p1.{encoded.decode('ascii')}."
        f"{base64.urlsafe_b64encode(signature).rstrip(b'=').decode('ascii')}"
    )


def test_h4_unexpired_v1_permit_replay_upgrades_same_permit_in_place(
    h4_pg_engine: Engine,
) -> None:
    client, _service, task_id, _session_id, submission = _ready_h4_submission(h4_pg_engine)
    first = _post_submission(client, submission)
    assert first.status_code == 200, first.text
    first_body = first.json()
    request_id = UUID(first_body["generation_request_id"])
    old_token = _legacy_h4_v1_token(
        permit=first_body["permit"], package_digest=first_body["package_digest"],
        task_id=task_id, request_id=request_id
    )
    old_expiry = first_body["permit"]["expires_at"]

    # Model an S015 row whose v1 permit was persisted before the S016 rollout.
    with Session(h4_pg_engine) as db, db.begin():
        permit = db.execute(
            select(ChatflowGenerationPermit)
            .where(ChatflowGenerationPermit.generation_request_id == request_id)
            .with_for_update()
        ).scalar_one()
        permit.permit_version = 1
        permit.slot_mapping_digest = None
        permit.reference_manifest_digest = None
        permit.token_digest = hashlib.sha256(old_token.encode("ascii")).digest()

    # Model an S016 legacy request whose H6 tracker was backfilled but whose
    # recovery Action was not yet scheduled; H4 replay upgrades the same permit
    # and atomically restores the GET-only Action.
    _discard_generation_recovery_action(h4_pg_engine, request_id)
    replay = _post_submission(client, submission)
    assert replay.status_code == 200, replay.text
    replay_body = replay.json()
    assert replay_body["status"] == "replayed"
    assert replay_body["generation_request_id"] == first_body["generation_request_id"]
    assert replay_body["permit"]["permit_id"] == first_body["permit"]["permit_id"]
    assert replay_body["permit"]["expires_at"] == old_expiry
    assert replay_body["permit"]["token"].startswith("h4p2.")
    assert (
        replay_body["permit"]["slot_mapping_digest"]
        == first_body["permit"]["slot_mapping_digest"]
    )
    assert (
        replay_body["permit"]["reference_manifest_digest"]
        == first_body["permit"]["reference_manifest_digest"]
    )

    with Session(h4_pg_engine) as db:
        upgraded = db.execute(
            select(ChatflowGenerationPermit).where(
                ChatflowGenerationPermit.generation_request_id == request_id
            )
        ).scalar_one()
        assert upgraded.permit_version == 2
        expected_expiry = first_body["permit"]["expires_at"].replace("Z", "+00:00")
        assert upgraded.expires_at.isoformat() == expected_expiry
        assert upgraded.slot_mapping_digest.hex() == replay_body["permit"]["slot_mapping_digest"]
        assert (
            upgraded.reference_manifest_digest.hex()
            == replay_body["permit"]["reference_manifest_digest"]
        )
        recovered_actions = db.execute(
            select(Action).where(
                Action.scope_kind == "generation_request",
                Action.generation_request_id == request_id,
                Action.action_kind == "chatflow.generation.recover.v1",
                Action.status == "scheduled",
            )
        ).scalars().all()
        assert len(recovered_actions) == 1


def _discard_generation_recovery_action(engine: Engine, request_id: UUID) -> None:
    with Session(engine) as db, db.begin():
        actions = db.execute(
            select(Action).where(
                Action.scope_kind == "generation_request",
                Action.generation_request_id == request_id,
                Action.action_kind == "chatflow.generation.recover.v1",
            )
        ).scalars().all()
        for action in actions:
            event_id = action.outbox_event_id
            db.delete(action)
            db.flush()
            receipt = db.get(OutboxReceipt, event_id)
            event = db.get(OutboxEvent, event_id)
            if receipt is not None:
                db.delete(receipt)
                db.flush()
            if event is not None:
                db.delete(event)


def test_s016_rejects_v2_permit_without_both_mapping_digests(
    h4_pg_engine: Engine,
) -> None:
    client, _service, _task_id, _session_id, submission = _ready_h4_submission(h4_pg_engine)
    accepted = _post_submission(client, submission)
    assert accepted.status_code == 200
    request_id = UUID(accepted.json()["generation_request_id"])

    with pytest.raises(IntegrityError):
        with Session(h4_pg_engine) as db, db.begin():
            permit = db.execute(
                select(ChatflowGenerationPermit).where(
                    ChatflowGenerationPermit.generation_request_id == request_id
                )
            ).scalar_one()
            permit.permit_version = 2
            permit.slot_mapping_digest = b"s" * 32
            permit.reference_manifest_digest = None


def test_h4_rollback_has_no_permit_request_attempt_or_generation_action(
    h4_pg_engine: Engine,
) -> None:
    _client, service, task_id, _session_id, submission = _ready_h4_submission(h4_pg_engine)

    def reject_h4_flush(session, _flush_context, _instances) -> None:
        # Fail at the final slot stage, after all other H4 rows have already
        # been flushed but remain inside the same uncommitted transaction.
        if any(isinstance(row, ChatflowGenerationRequestSlot) for row in session.new):
            raise SQLAlchemyError("injected H4 persistence failure")

    event.listen(Session, "before_flush", reject_h4_flush)
    try:
        with pytest.raises(DomainError) as raised:
            service.accept(submission)
    finally:
        event.remove(Session, "before_flush", reject_h4_flush)
    assert raised.value.status_code == 503

    with Session(h4_pg_engine) as db:
        package_ids = select(ChatflowPromptPackage.id).where(
            ChatflowPromptPackage.task_id == task_id
        )
        item_ids = select(TaskItem.id).where(TaskItem.task_id == task_id)
        assert (
            db.scalar(
                select(func.count(ChatflowPromptPackage.id)).where(
                    ChatflowPromptPackage.task_id == task_id
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(ChatflowPromptImage.id)).where(
                    ChatflowPromptImage.task_id == task_id
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(ChatflowPromptReference.id)).where(
                    ChatflowPromptReference.package_id.in_(package_ids)
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(ChatflowGenerationRequest.id)).where(
                    ChatflowGenerationRequest.task_id == task_id
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(ChatflowGenerationRequestSlot.slot_id)).where(
                    ChatflowGenerationRequestSlot.task_id == task_id
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(ChatflowGenerationPermit.permit_id)).where(
                    ChatflowGenerationPermit.task_id == task_id
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(TaskItemAttempt.id)).where(
                    TaskItemAttempt.task_item_id.in_(item_ids)
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(Action.id)).where(
                    Action.task_id == task_id,
                    Action.action_kind.in_(GENERATION_ACTION_KINDS),
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.event_type == GENERATION_START_EVENT_TYPE,
                    OutboxEvent.aggregate_id == task_id,
                )
            )
            == 0
        )


def test_h4_rejects_tampered_digest_linkage_and_same_submission_changed_content(
    h4_pg_engine: Engine,
) -> None:
    client, _service, task_id, _session_id, submission = _ready_h4_submission(h4_pg_engine)

    package_value = submission.prompt_package.model_dump(mode="json")
    package_value["images"][0]["design_prompt"] = "changed after package digest"
    changed_package = PromptPackage.model_validate(package_value, strict=True)
    changed_digest_submission = submission.model_copy(
        update={"prompt_package": changed_package}
    )
    bad_digest = _post_submission(client, changed_digest_submission)
    assert bad_digest.status_code == 422

    wrong_references = list(submission.references)
    wrong_references[0] = wrong_references[0].model_copy(update={"asset_id": uuid4()})
    wrong_link = submission.model_copy(
        update={"submission_id": uuid4(), "references": wrong_references}
    )
    rejected_link = _post_submission(client, wrong_link)
    assert rejected_link.status_code == 409

    old_run = submission.model_copy(
        update={"submission_id": uuid4(), "dify_run_id": "old-dify-run"}
    )
    rejected_run = _post_submission(client, old_run)
    assert rejected_run.status_code == 409

    accepted = _post_submission(client, submission)
    assert accepted.status_code == 200, accepted.text
    changed_content = _with_package(submission, changed_package)
    conflict = _post_submission(client, changed_content)
    assert conflict.status_code == 409
    with Session(h4_pg_engine) as db:
        assert db.scalar(
            select(func.count(ChatflowGenerationRequest.id)).where(
                ChatflowGenerationRequest.task_id == task_id
            )
        ) == 1


def test_h4_expired_permit_cannot_be_reissued(h4_pg_engine: Engine) -> None:
    client, _service, task_id, _session_id, submission = _ready_h4_submission(h4_pg_engine)
    accepted = _post_submission(client, submission)
    assert accepted.status_code == 200
    request_id = UUID(accepted.json()["generation_request_id"])

    with Session(h4_pg_engine) as db, db.begin():
        permit = db.execute(
            select(ChatflowGenerationPermit).where(
                ChatflowGenerationPermit.generation_request_id == request_id
            )
        ).scalar_one()
        permit.expires_at = db.scalar(select(func.clock_timestamp()))

    expired = _post_submission(client, submission)
    assert expired.status_code == 410
    with Session(h4_pg_engine) as db:
        assert db.scalar(
            select(func.count(ChatflowGenerationRequest.id)).where(
                ChatflowGenerationRequest.task_id == task_id
            )
        ) == 1
