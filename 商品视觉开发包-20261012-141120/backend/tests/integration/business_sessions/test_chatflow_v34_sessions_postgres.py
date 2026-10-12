from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from app.api.internal_dify import create_internal_dify_router
from app.core.domain_errors import DomainError
from app.db.action_models import Action
from app.db.business_session_models import BusinessSession
from app.db.chatflow_generation_models import (
    ChatflowGenerationPermit,
    ChatflowGenerationRequest,
    ChatflowGenerationTracking,
    ChatflowPromptPackage,
)
from app.db.chatflow_reference_files import ChatflowDifyReferenceFile
from app.db.chatflow_v34_models import ChatflowV34SessionState, ChatflowV34StageEventReceipt
from app.db.outbox_models import OutboxEvent, OutboxReceipt
from app.db.recommendation_models import Recommendation
from app.db.task_models import Task, TaskItem, TaskItemAttempt
from app.dispatcher import OutboxDispatcher
from app.integrations.dify.chatflow import (
    ChatflowCancelResult,
    ChatflowContinuationCommand,
    ChatflowContinuationResult,
    ChatflowFirstFormBinding,
    ChatflowFirstFormDefinitionCommand,
    ChatflowFirstFormDefinitionResult,
    ChatflowFirstFormField,
    ChatflowHumanInputCommand,
    ChatflowHumanInputResult,
    ChatflowOriginalRunEvent,
    ChatflowOriginalRunObservationResult,
    ChatflowStartCommand,
    ChatflowStartResult,
    DifyReferenceUploadCommand,
    DifyReferenceUploadResult,
)
from app.integrations.dify.mock import build_mock_options
from app.schemas.business_sessions import (
    ChatflowBusinessEvent,
    DifyOriginalRecommendation,
    chatflow_business_event_digest,
)
from app.schemas.chatflow_prompt_packages import ChatflowPromptPackageSubmission
from app.schemas.chatflow_v34 import (
    V34_FLOW_VERSION,
    V34_STAGE_CONTRACT,
    ChatflowV34StageEvent,
    V34ParsedSelection,
    chatflow_v34_stage_event_digest,
)
from app.schemas.recommendations import IMAGE_TYPES, PLATFORMS
from app.services.business_sessions import BusinessSessionOptions
from app.services.chatflow_actions import (
    CHATFLOW_CANCEL_ACTION,
    CHATFLOW_CANCEL_RECOVERY_ACTION,
    CHATFLOW_CANCEL_RECOVERY_SEQUENCE_BASE,
    CHATFLOW_CANCEL_SEQUENCE,
    CHATFLOW_CONTINUE_ACTION,
    CHATFLOW_FORM_BIND_ACTION,
    CHATFLOW_REFERENCE_UPLOAD_ACTION,
    CHATFLOW_RUN_OBSERVATION_ACTION,
    CHATFLOW_RUN_OBSERVATION_SEQUENCE_BASE,
    CHATFLOW_START_ACTION,
    CHATFLOW_START_SEQUENCE,
    build_chatflow_action_registrations,
    chatflow_action_digest,
    chatflow_action_id,
    finalize_chatflow_continuation,
    prepare_chatflow_continuation,
    schedule_chatflow_v34_cancel_in_transaction,
)
from app.services.chatflow_prompt_packages import ChatflowPromptPackageService
from app.services.chatflow_v34_conversion import selection_to_form_0
from app.services.chatflow_v34_events import (
    ChatflowV34ContextService,
    ChatflowV34StageEventService,
    recommendation_form_digest,
)
from app.services.chatflow_v34_prompt_contract import prompt_package_submission_id
from app.workers.action_processor import (
    ActionExecutionResult,
    ActionProcessor,
    mark_expired_action_unknown_in_transaction,
    schedule_action_in_transaction,
)
from app.workers.messages import ClaimedEvent, OutboxMessage
from app.workers.options import QueueOptions
from pydantic import SecretStr
from sqlalchemy import create_engine, event, func, inspect, null, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tests.integration.business_sessions.test_business_sessions_postgres import (
    TEST_INTERNAL_CALLBACK_SECRET,
    TRUSTED_ORIGIN,
    _create_reference_set,
    _new_guest,
    _process_chatflow_action,
    _selection_request,
)
from tests.integration.business_sessions.test_chatflow_prompt_packages_postgres import (
    TEST_GENERATION_PERMIT_SECRET,
    TEST_SERVICE_AUTHORITY_ID,
    _post_submission,
    _submission_for_task,
)


@pytest.fixture
def v34_pg_engine() -> Iterator[Engine]:
    database_url = os.getenv("IMAGE_BACKEND_TEST_RUNTIME_DATABASE_URL")
    if database_url is None:
        pytest.skip("requires the dedicated image-backend-test PostgreSQL runtime URL")
    url = make_url(database_url)
    if url.database != "image_backend_test":
        pytest.fail("refusing to run the v3.4 integration test outside image_backend_test")
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


def _create_session(
    engine: Engine,
    *,
    flow_version: str,
    idempotency_key: str,
):
    client, owner_id = _new_guest(engine)
    reference_set_id = _create_reference_set(engine, owner_id)
    service = client.app.state.business_session_service
    if flow_version == V34_FLOW_VERSION:
        service.options = BusinessSessionOptions(
            dify_app_id="test-v34-app",
            flow_version=V34_FLOW_VERSION,
            dify_workflow_id="test-v34-workflow-release",
        )
    created = client.post(
        "/api/v1/business-sessions",
        json={"reference_set_id": str(reference_set_id)},
        headers={"Origin": TRUSTED_ORIGIN, "Idempotency-Key": idempotency_key},
    )
    assert created.status_code == 202, created.text
    return client, owner_id, reference_set_id, created.json()


def _legacy_callback(
    *, business_session_id: UUID, recommendation_id: UUID, event_type: str
) -> ChatflowBusinessEvent:
    event = ChatflowBusinessEvent(
        contract_version="chatflow-handoff-v1",
        business_session_id=business_session_id,
        recommendation_id=recommendation_id,
        recommendation_revision=1,
        dify_run_id="v34-test-run",
        event_id=uuid4(),
        source="dify",
        source_sequence=1,
        event_type=event_type,
        digest_version=1,
        digest="0" * 64,
        payload={},
    )
    return event.model_copy(update={"digest": chatflow_business_event_digest(event)})


def _v34_recommendation_ready_event(
    *,
    business_session_id: UUID,
    recommendation_id: UUID,
    run_id: str,
    event_key: str,
) -> ChatflowV34StageEvent:
    payload = {
        "audit": json.dumps(
            {
                "declared_category": "测试品类",
                "fields": [
                    {"id": "1", "options": ["语言 A", "语言 B", "语言 C"]},
                    {"id": "2", "options": list(PLATFORMS)},
                    {"id": "3", "options": ["场景 A", "场景 B", "场景 C"]},
                    {"id": "4", "options": ["风格 A", "风格 B", "风格 C"]},
                    {"id": "5", "options": ["卖点 A", "卖点 B", "卖点 C"]},
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    }
    event = ChatflowV34StageEvent(
        contract_version=V34_STAGE_CONTRACT,
        flow_version=V34_FLOW_VERSION,
        business_session_id=business_session_id,
        recommendation_id=recommendation_id,
        recommendation_revision=1,
        dify_run_id=run_id,
        node_id="prepare",
        event_key=event_key,
        digest_version=1,
        digest="0" * 64,
        event_type="recommendation.ready",
        payload=payload,
    )
    return event.model_copy(update={"digest": chatflow_v34_stage_event_digest(event).hex()})


def _v34_selection_applied_event(
    *,
    business_session_id: UUID,
    recommendation_id: UUID,
    run_id: str,
    submission_id: UUID,
    parse_state: V34ParsedSelection,
    event_key: str,
) -> ChatflowV34StageEvent:
    event = ChatflowV34StageEvent(
        contract_version=V34_STAGE_CONTRACT,
        flow_version=V34_FLOW_VERSION,
        business_session_id=business_session_id,
        recommendation_id=recommendation_id,
        recommendation_revision=1,
        dify_run_id=run_id,
        node_id="parse_0",
        event_key=event_key,
        digest_version=1,
        digest="0" * 64,
        event_type="selection.applied",
        payload={
            "selection_submission_id": str(submission_id),
            "parse_state": parse_state.model_dump(mode="json"),
        },
    )
    return event.model_copy(update={"digest": chatflow_v34_stage_event_digest(event).hex()})


def _v34_selection_cancelled_event(
    *,
    business_session_id: UUID,
    recommendation_id: UUID,
    run_id: str,
    event_key: str,
) -> ChatflowV34StageEvent:
    event = ChatflowV34StageEvent(
        contract_version=V34_STAGE_CONTRACT,
        flow_version=V34_FLOW_VERSION,
        business_session_id=business_session_id,
        recommendation_id=recommendation_id,
        recommendation_revision=1,
        dify_run_id=run_id,
        node_id="selection_cancelled_callback",
        event_key=event_key,
        digest_version=1,
        digest="0" * 64,
        event_type="selection.cancelled",
        payload={"reason_code": "FORM_CANCELLED"},
    )
    return event.model_copy(update={"digest": chatflow_v34_stage_event_digest(event).hex()})


def _v34_selection_expired_event(
    *,
    business_session_id: UUID,
    recommendation_id: UUID,
    run_id: str,
    event_key: str,
) -> ChatflowV34StageEvent:
    event = ChatflowV34StageEvent(
        contract_version=V34_STAGE_CONTRACT,
        flow_version=V34_FLOW_VERSION,
        business_session_id=business_session_id,
        recommendation_id=recommendation_id,
        recommendation_revision=1,
        dify_run_id=run_id,
        node_id="selection_expired_callback",
        event_key=event_key,
        digest_version=1,
        digest="0" * 64,
        event_type="selection.expired",
        payload={"reason_code": "FORM_EXPIRED"},
    )
    return event.model_copy(update={"digest": chatflow_v34_stage_event_digest(event).hex()})


def _v34_workflow_failed_event(
    *,
    business_session_id: UUID,
    recommendation_id: UUID,
    run_id: str,
    event_key: str,
    phase: str,
) -> ChatflowV34StageEvent:
    event = ChatflowV34StageEvent(
        contract_version=V34_STAGE_CONTRACT,
        flow_version=V34_FLOW_VERSION,
        business_session_id=business_session_id,
        recommendation_id=recommendation_id,
        recommendation_revision=1,
        dify_run_id=run_id,
        node_id="workflow_failed_callback",
        event_key=event_key,
        digest_version=1,
        digest="0" * 64,
        event_type="workflow.failed",
        payload={"phase": phase, "failed_node_id": phase},
    )
    return event.model_copy(update={"digest": chatflow_v34_stage_event_digest(event).hex()})


def _v34_incomplete_event(
    *,
    business_session_id: UUID,
    recommendation_id: UUID,
    run_id: str,
    event_key: str,
    conflicting_field_ids: list[str],
) -> ChatflowV34StageEvent:
    event = ChatflowV34StageEvent(
        contract_version=V34_STAGE_CONTRACT,
        flow_version=V34_FLOW_VERSION,
        business_session_id=business_session_id,
        recommendation_id=recommendation_id,
        recommendation_revision=1,
        dify_run_id=run_id,
        node_id="ready_0",
        event_key=event_key,
        digest_version=1,
        digest="0" * 64,
        event_type="pre_generation.incomplete",
        payload={
            "reason_code": "PRODUCT_IDENTITY_CONFLICT",
            "conflicting_field_ids": conflicting_field_ids,
        },
    )
    return event.model_copy(update={"digest": chatflow_v34_stage_event_digest(event).hex()})


def _prepare_v34_selection_ready(
    engine: Engine,
    *,
    idempotency_key: str,
):
    client, owner_id, reference_set_id, created = _create_session(
        engine,
        flow_version=V34_FLOW_VERSION,
        idempotency_key=idempotency_key,
    )
    session_id = UUID(created["business_session_id"])
    recommendation_id = UUID(created["recommendation_id"])
    run_id = f"v34-selection-ready-{uuid4().hex}"
    token = f"private-v34-selection-token-{uuid4().hex}"
    with Session(engine) as db:
        now = db.scalar(select(func.clock_timestamp()))
    assert now is not None
    with Session(engine) as db, db.begin():
        session = db.get(BusinessSession, session_id)
        recommendation = db.get(Recommendation, recommendation_id)
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        assert session is not None and recommendation is not None
        session.status = "awaiting_recommendation"
        session.start_outcome = "accepted"
        session.dify_run_id = run_id
        session.run_revision = 1
        recommendation.dify_run_id = run_id
        recommendation.revision = 1
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
        state.form_status = "binding"
        state.form_node_id = "form_0"
        state.form_token = token
        state.form_token_digest = hashlib.sha256(token.encode("utf-8")).digest()
        state.form_definition_digest = None
        state.form_expires_at = None

    event = _v34_recommendation_ready_event(
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
        event_key=f"{run_id}:prepare:ready",
    )
    assert ChatflowV34StageEventService(engine).accept(event).status == "accepted"
    with Session(engine) as db, db.begin():
        recommendation = db.get(Recommendation, recommendation_id)
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        assert recommendation is not None
        definition_digest = recommendation_form_digest(recommendation)
        assert definition_digest is not None
        state.form_status = "ready"
        state.form_definition_digest = definition_digest
        state.form_expires_at = now + timedelta(hours=1)
    return client, owner_id, reference_set_id, session_id, recommendation_id, run_id, token


def _complete_v34_cancel_action(engine: Engine, business_session_id: UUID) -> None:
    """Install a deterministic dispatched/accepted cancel Action for callback fixtures."""
    with Session(engine) as db, db.begin():
        session = db.get(BusinessSession, business_session_id)
        recommendation = db.execute(
            select(Recommendation).where(
                Recommendation.business_session_id == business_session_id,
                Recommendation.source_kind == "dify_chatflow",
            )
        ).scalar_one()
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == business_session_id
            )
        ).scalar_one()
        assert session is not None
        now = db.scalar(select(func.clock_timestamp()))
        assert now is not None
        action = db.execute(
            select(Action).where(
                Action.business_session_id == business_session_id,
                Action.action_kind == CHATFLOW_CANCEL_ACTION,
                Action.sequence == CHATFLOW_CANCEL_SEQUENCE,
            )
        ).scalar_one_or_none()
        if action is None:
            state.form_status = "cancelling"
            state.safe_reason_code = "DIFY_CANCEL_OUTCOME_PENDING"
            session.status = "reconciling"
            action = schedule_chatflow_v34_cancel_in_transaction(
                db,
                session=session,
                recommendation=recommendation,
                state=state,
                due_at=now,
            )
        action.status = "completed"
        action.last_outcome = "accepted"
        action.completed_at = now


def _force_v34_cancel_intent_completed(engine: Engine, business_session_id: UUID) -> None:
    """Create a valid terminal-event fixture despite intentionally stale selection evidence."""
    with Session(engine) as db, db.begin():
        session = db.get(BusinessSession, business_session_id)
        recommendation = db.execute(
            select(Recommendation).where(
                Recommendation.business_session_id == business_session_id,
                Recommendation.source_kind == "dify_chatflow",
            )
        ).scalar_one()
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == business_session_id
            )
        ).scalar_one()
        assert session is not None
        now = db.scalar(select(func.clock_timestamp()))
        assert now is not None
        state.form_status = "cancelling"
        state.safe_reason_code = "DIFY_CANCEL_OUTCOME_PENDING"
        session.status = "reconciling"
        action = schedule_chatflow_v34_cancel_in_transaction(
            db,
            session=session,
            recommendation=recommendation,
            state=state,
            due_at=now,
        )
        action.status = "completed"
        action.last_outcome = "accepted"
        action.completed_at = now


def _expire_claimed_action_lease(engine: Engine, action_id: UUID) -> UUID:
    with Session(engine) as db, db.begin():
        action = db.get(Action, action_id)
        assert action is not None and action.outbox_event_id is not None
        event = db.get(OutboxEvent, action.outbox_event_id)
        receipt = db.get(OutboxReceipt, action.outbox_event_id)
        now = db.scalar(select(func.clock_timestamp()))
        assert event is not None and receipt is not None and now is not None
        expired = now - timedelta(seconds=1)
        action.status = "claimed"
        action.claim_token = uuid4()
        action.lease_until = expired
        action.claim_count += 1
        receipt.status = "processing"
        receipt.claim_token = uuid4()
        receipt.lease_until = expired
        receipt.attempt_count += 1
        event.status = "published"
        event.published_at = event.published_at or now
        return event.id


def _sweep_expired_action_lease(engine: Engine, event_id: UUID) -> bool:
    with Session(engine) as db, db.begin():
        event = db.execute(
            select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update()
        ).scalar_one()
        receipt = db.execute(
            select(OutboxReceipt).where(OutboxReceipt.event_id == event_id).with_for_update()
        ).scalar_one()
        now = db.scalar(select(func.clock_timestamp()))
        assert now is not None
        return mark_expired_action_unknown_in_transaction(
            db,
            event,
            receipt,
            now=now,
            reason_code="ACTION_LEASE_EXPIRED",
        )


class _UnknownSelectionContinuationPort:
    def __init__(self) -> None:
        self.calls: list[ChatflowContinuationCommand] = []

    def start_recommendation(self, _command: ChatflowStartCommand) -> ChatflowStartResult:
        raise AssertionError("selection continuation must not start a new workflow")

    def get_first_form_definition(
        self, _command: ChatflowFirstFormDefinitionCommand
    ) -> ChatflowFirstFormDefinitionResult:
        raise AssertionError("selection continuation must not bind another form")

    def upload_reference(self, _command: DifyReferenceUploadCommand) -> DifyReferenceUploadResult:
        raise AssertionError("selection continuation must not upload references")

    def continue_after_selection(
        self, command: ChatflowContinuationCommand
    ) -> ChatflowContinuationResult:
        self.calls.append(command)
        return ChatflowContinuationResult(
            "unknown", safe_reason_code="DIFY_SELECTION_OUTCOME_UNKNOWN"
        )

    def recover_after_selection(
        self, _command: ChatflowContinuationCommand
    ) -> ChatflowContinuationResult:
        raise AssertionError("this test does not authorize a second Dify request")

    def submit_human_input(self, _command: ChatflowHumanInputCommand) -> ChatflowHumanInputResult:
        raise AssertionError("H3 is not part of this v3.4 selection test")

    def recover_human_input(self, _command: ChatflowHumanInputCommand) -> ChatflowHumanInputResult:
        raise AssertionError("H3 is not part of this v3.4 selection test")


class _EarlySelectionAppliedPort(_UnknownSelectionContinuationPort):
    def __init__(self, event: ChatflowV34StageEvent, event_service: ChatflowV34StageEventService):
        super().__init__()
        self.event = event
        self.event_service = event_service
        self.callback_status: str | None = None

    def continue_after_selection(
        self, command: ChatflowContinuationCommand
    ) -> ChatflowContinuationResult:
        self.calls.append(command)
        self.callback_status = self.event_service.accept(self.event).status
        return ChatflowContinuationResult(
            "unknown", safe_reason_code="DIFY_SELECTION_OUTCOME_UNKNOWN"
        )


class _TerminalThenAcceptedContinuationPort(_UnknownSelectionContinuationPort):
    def __init__(
        self,
        events: tuple[ChatflowV34StageEvent, ...],
        event_service: ChatflowV34StageEventService,
    ) -> None:
        super().__init__()
        self.events = events
        self.event_service = event_service
        self.callback_statuses: list[str] = []

    def continue_after_selection(
        self, command: ChatflowContinuationCommand
    ) -> ChatflowContinuationResult:
        self.calls.append(command)
        self.callback_statuses = [self.event_service.accept(event).status for event in self.events]
        return ChatflowContinuationResult("accepted")


class _CancelLifecyclePort(_UnknownSelectionContinuationPort):
    def __init__(
        self,
        *,
        cancel_result: ChatflowCancelResult,
        recovery_result: ChatflowCancelResult,
    ) -> None:
        super().__init__()
        self.cancel_result = cancel_result
        self.recovery_result = recovery_result
        self.cancel_calls = []
        self.recovery_calls = []

    def cancel_before_selection(self, command) -> ChatflowCancelResult:
        self.cancel_calls.append(command)
        return self.cancel_result

    def recover_cancel_before_selection(self, command) -> ChatflowCancelResult:
        self.recovery_calls.append(command)
        return self.recovery_result


class _RecordingEventPublisher:
    def __init__(self) -> None:
        self.events: list[ClaimedEvent] = []

    def publish(self, event: ClaimedEvent) -> None:
        self.events.append(event)


class _CancelWithCallbackPort(_CancelLifecyclePort):
    def __init__(
        self,
        *,
        cancel_result: ChatflowCancelResult,
        recovery_result: ChatflowCancelResult,
        cancel_event: ChatflowV34StageEvent,
        event_service: ChatflowV34StageEventService,
    ) -> None:
        super().__init__(cancel_result=cancel_result, recovery_result=recovery_result)
        self.cancel_event = cancel_event
        self.event_service = event_service
        self.callback_status: str | None = None

    def cancel_before_selection(self, command) -> ChatflowCancelResult:
        result = super().cancel_before_selection(command)
        self.callback_status = self.event_service.accept(self.cancel_event).status
        return result


class _RunObservationPort(_UnknownSelectionContinuationPort):
    """Deterministic GET-only observation port for the registered Action path."""

    def __init__(self, result: ChatflowOriginalRunObservationResult) -> None:
        super().__init__()
        self.result = result
        self.observation_calls: list[tuple[str, str]] = []

    def observe_original_run(
        self, dify_run_id: str, dify_user_id: str
    ) -> ChatflowOriginalRunObservationResult:
        self.observation_calls.append((dify_run_id, dify_user_id))
        return self.result


def _accept_v34_selection_evidence(
    engine: Engine,
    *,
    client,
    session_id: UUID,
    recommendation_id: UUID,
    run_id: str,
) -> UUID:
    request = _selection_request(recommendation_id)
    selected = client.post(
        f"/api/v1/business-sessions/{session_id}/selection",
        json=request.model_dump(mode="json"),
        headers={
            "Origin": TRUSTED_ORIGIN,
            "Idempotency-Key": f"v34-accepted-selection-{uuid4().hex}",
        },
    )
    assert selected.status_code == 202, selected.text
    task_id = UUID(selected.json()["task_id"])

    with Session(engine) as db:
        action = db.execute(
            select(Action).where(
                Action.business_session_id == session_id,
                Action.action_kind == CHATFLOW_CONTINUE_ACTION,
                Action.sequence == 2,
            )
        ).scalar_one()
        recommendation = db.get(Recommendation, recommendation_id)
        assert recommendation is not None
        original = DifyOriginalRecommendation.model_validate(
            recommendation.original_recommendation, strict=True
        )
        action_id = action.id
    port = _UnknownSelectionContinuationPort()
    processor = ActionProcessor(
        engine,
        QueueOptions(
            broker_url=f"amqp://integration:integration@localhost:5672/v34-accepted-selection-{uuid4().hex}"
        ),
        build_chatflow_action_registrations(port),
    )
    assert _process_chatflow_action(engine, action_id=action_id, processor=processor) == "unknown"

    conversion = selection_to_form_0(request, original)
    applied = _v34_selection_applied_event(
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
        submission_id=action_id,
        parse_state=V34ParsedSelection(
            preferences=conversion.normalized_preferences,
            image_types=request.image_types,
        ),
        event_key=f"{run_id}:parse:accepted-{uuid4().hex}",
    )
    assert ChatflowV34StageEventService(engine).accept(applied).status == "accepted"
    return task_id


def _prepare_v34_run_observation_case(
    engine: Engine,
    *,
    idempotency_key: str,
):
    (
        client,
        _owner_id,
        _reference_set_id,
        session_id,
        recommendation_id,
        run_id,
        _form_token,
    ) = _prepare_v34_selection_ready(engine, idempotency_key=idempotency_key)
    task_id = _accept_v34_selection_evidence(
        engine,
        client=client,
        session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
    )
    with Session(engine) as db:
        observation_action = db.execute(
            select(Action).where(
                Action.business_session_id == session_id,
                Action.action_kind == CHATFLOW_RUN_OBSERVATION_ACTION,
                Action.sequence == CHATFLOW_RUN_OBSERVATION_SEQUENCE_BASE,
            )
        ).scalar_one()
        assert observation_action.task_id == task_id
        assert observation_action.scope_kind == "business_session"
        action_id = observation_action.id
    return client, session_id, recommendation_id, run_id, task_id, action_id


def _process_v34_run_observation(
    engine: Engine,
    *,
    action_id: UUID,
    observation: ChatflowOriginalRunObservationResult,
):
    port = _RunObservationPort(observation)
    processor = ActionProcessor(
        engine,
        QueueOptions(
            broker_url=f"amqp://integration:integration@localhost:5672/v34-run-observation-{uuid4().hex}"
        ),
        build_chatflow_action_registrations(port),
    )
    result = _process_chatflow_action(engine, action_id=action_id, processor=processor)
    return result, port


def _prepare_v34_observer_reconciling_handoff(
    engine: Engine,
    *,
    idempotency_key: str,
):
    client, session_id, recommendation_id, run_id, task_id, first_action_id = (
        _prepare_v34_run_observation_case(engine, idempotency_key=idempotency_key)
    )
    paused = ChatflowOriginalRunObservationResult(
        "received",
        events=(
            ChatflowOriginalRunEvent(
                event="workflow_paused",
                dify_run_id=run_id,
                workflow_status="paused",
            ),
        ),
    )
    assert _process_v34_run_observation(
        engine, action_id=first_action_id, observation=paused
    )[0] == "completed"

    with Session(engine) as db, db.begin():
        next_action = db.execute(
            select(Action)
            .where(
                Action.business_session_id == session_id,
                Action.action_kind == CHATFLOW_RUN_OBSERVATION_ACTION,
                Action.sequence == CHATFLOW_RUN_OBSERVATION_SEQUENCE_BASE + 1,
                Action.status == "scheduled",
            )
            .with_for_update()
        ).scalar_one()
        now = db.scalar(select(func.clock_timestamp()))
        assert now is not None
        next_action.due_at = now
        next_event = db.get(OutboxEvent, next_action.outbox_event_id)
        assert next_event is not None
        next_event.available_at = now
        next_receipt = db.execute(
            select(OutboxReceipt).where(OutboxReceipt.event_id == next_event.id).with_for_update()
        ).scalar_one_or_none()
        if next_receipt is not None:
            next_receipt.available_at = now
        next_observation_action_id = next_action.id
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        assert state.selection_status == "accepted"
        assert state.selection_submission_id is not None
        selection_action_id = state.selection_submission_id

    callback = SimpleNamespace(
        recommendation_id=recommendation_id,
        recommendation_revision=1,
        dify_run_id=run_id,
    )
    submission = _submission_for_task(
        engine,
        task_id=task_id,
        session_id=session_id,
        callback=callback,
    ).model_copy(update={"submission_id": prompt_package_submission_id(selection_action_id)})
    client.app.include_router(
        create_internal_dify_router(
            engine=engine,
            callback_credential=SecretStr(TEST_INTERNAL_CALLBACK_SECRET),
            prompt_package_service=ChatflowPromptPackageService(
                engine,
                permit_secret=SecretStr(TEST_GENERATION_PERMIT_SECRET),
                service_authority_id=TEST_SERVICE_AUTHORITY_ID,
            ),
        )
    )
    return (
        client,
        session_id,
        recommendation_id,
        run_id,
        task_id,
        next_observation_action_id,
        submission,
    )


def _v34_handoff_row_counts(engine: Engine, task_id: UUID) -> tuple[int, int, int, int, int]:
    with Session(engine) as db:
        package_count = db.scalar(
            select(func.count(ChatflowPromptPackage.id)).where(
                ChatflowPromptPackage.task_id == task_id
            )
        )
        request_count = db.scalar(
            select(func.count(ChatflowGenerationRequest.id)).where(
                ChatflowGenerationRequest.task_id == task_id
            )
        )
        permit_count = db.scalar(
            select(func.count(ChatflowGenerationPermit.permit_id)).where(
                ChatflowGenerationPermit.task_id == task_id
            )
        )
        tracking_count = db.scalar(
            select(func.count(ChatflowGenerationTracking.generation_request_id)).where(
                ChatflowGenerationTracking.task_id == task_id
            )
        )
        attempt_count = db.scalar(
            select(func.count(TaskItemAttempt.id))
            .join(TaskItem, TaskItem.id == TaskItemAttempt.task_item_id)
            .where(TaskItem.task_id == task_id)
        )
    return package_count, request_count, permit_count, tracking_count, attempt_count


def _wait_for_postgres_lock_wait(engine: Engine, backend_pid: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            wait_type = connection.execute(
                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": backend_pid},
            ).scalar_one_or_none()
        if wait_type == "Lock":
            return
        time.sleep(0.02)
    pytest.fail("terminal observer did not reach a PostgreSQL lock wait before H4 committed")


@pytest.mark.parametrize("first_request", ["cancel", "selection"])
def test_v34_cancel_and_selection_share_one_winner_and_cancel_creates_no_task(
    v34_pg_engine: Engine,
    first_request: str,
) -> None:
    (
        client,
        owner_id,
        reference_set_id,
        session_id,
        recommendation_id,
        _run_id,
        _form_token,
    ) = _prepare_v34_selection_ready(
        v34_pg_engine,
        idempotency_key=f"v34-cancel-race-{first_request}-{uuid4().hex}",
    )

    def cancel():
        return client.post(
            f"/api/v1/business-sessions/{session_id}/cancel",
            headers={"Origin": TRUSTED_ORIGIN},
        )

    def select_once():
        return client.post(
            f"/api/v1/business-sessions/{session_id}/selection",
            json=_selection_request(recommendation_id).model_dump(mode="json"),
            headers={
                "Origin": TRUSTED_ORIGIN,
                "Idempotency-Key": f"v34-cancel-race-selection-{uuid4().hex}",
            },
        )

    if first_request == "cancel":
        winner = cancel()
        assert winner.status_code == 202, winner.text
        assert winner.json()["status"] == "cancelling"
        assert winner.json()["replayed"] is False

        replay = cancel()
        assert replay.status_code == 202, replay.text
        assert replay.json()["status"] == "cancelling"
        assert replay.json()["replayed"] is True

        loser = select_once()
        assert loser.status_code == 409
    else:
        winner = select_once()
        assert winner.status_code == 202, winner.text
        assert winner.json()["status"] == "queued"

        loser = cancel()
        assert loser.status_code == 409
        assert loser.json()["code"] == "V34_CANCEL_AFTER_SELECTION"

    with Session(v34_pg_engine) as db:
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        tasks = (
            db.execute(select(Task).where(Task.business_session_id == session_id)).scalars().all()
        )
        cancel_actions = (
            db.execute(
                select(Action).where(
                    Action.business_session_id == session_id,
                    Action.action_kind == CHATFLOW_CANCEL_ACTION,
                    Action.sequence == CHATFLOW_CANCEL_SEQUENCE,
                )
            )
            .scalars()
            .all()
        )
        selection_evidence_count = db.scalar(
            select(func.count(ChatflowV34StageEventReceipt.id)).where(
                ChatflowV34StageEventReceipt.business_session_id == session_id,
                ChatflowV34StageEventReceipt.event_type == "selection.applied",
                ChatflowV34StageEventReceipt.disposition == "accepted",
            )
        )

    assert selection_evidence_count == 0
    if first_request == "cancel":
        assert state.form_status == "cancelling"
        assert state.selection_status == "not_started"
        assert state.selection_submission_id is None
        assert state.selection_input_digest is None
        assert state.selection_inputs is None
        assert tasks == []
        assert len(cancel_actions) == 1
        assert cancel_actions[0].task_id is None
        assert cancel_actions[0].sequence == CHATFLOW_CANCEL_SEQUENCE
        assert cancel_actions[0].status == "scheduled"
    else:
        assert state.form_status == "consumed"
        assert state.selection_status == "pending"
        assert state.selection_submission_id is not None
        assert len(tasks) == 1
        assert cancel_actions == []


def test_v34_cancel_receipt_is_bound_to_original_run_and_replays_idempotently(
    v34_pg_engine: Engine,
) -> None:
    (
        client,
        owner_id,
        reference_set_id,
        session_id,
        recommendation_id,
        run_id,
        _form_token,
    ) = _prepare_v34_selection_ready(
        v34_pg_engine,
        idempotency_key=f"v34-cancel-receipt-{uuid4().hex}",
    )
    cancel_request = client.post(
        f"/api/v1/business-sessions/{session_id}/cancel",
        headers={"Origin": TRUSTED_ORIGIN},
    )
    assert cancel_request.status_code == 202, cancel_request.text
    assert cancel_request.json()["status"] == "cancelling"
    # Model a previously dispatched cancel POST whose accepted result was
    # durably finalized before the callback fixture is delivered.
    _complete_v34_cancel_action(v34_pg_engine, session_id)
    event_service = ChatflowV34StageEventService(v34_pg_engine)
    wrong_run_event = _v34_selection_cancelled_event(
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=f"wrong-{uuid4().hex}",
        event_key=f"{run_id}:cancelled:wrong-run",
    )

    wrong_run = event_service.accept(wrong_run_event)
    assert wrong_run.status == "rejected"
    public_after_wrong_run = client.get(f"/api/v1/business-sessions/{session_id}")
    assert public_after_wrong_run.status_code == 200
    assert public_after_wrong_run.json()["status"] == "reconciling"
    assert public_after_wrong_run.json()["selection_ready"] is False

    cancelled_event = _v34_selection_cancelled_event(
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
        event_key=f"{run_id}:cancelled:matching-run",
    )
    accepted = event_service.accept(cancelled_event)
    replayed = event_service.accept(cancelled_event)
    assert accepted.status == "accepted"
    assert replayed.status == "replayed"

    public_cancelled = client.get(f"/api/v1/business-sessions/{session_id}")
    assert public_cancelled.status_code == 200
    assert public_cancelled.json()["status"] == "cancelled"
    assert public_cancelled.json()["phase"] == "cancelled"
    assert public_cancelled.json()["selection_ready"] is False
    assert _form_token not in public_cancelled.text
    with Session(v34_pg_engine) as db:
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        task_count = db.scalar(
            select(func.count(Task.id)).where(Task.business_session_id == session_id)
        )
        accepted_cancel_receipts = db.scalar(
            select(func.count(ChatflowV34StageEventReceipt.id)).where(
                ChatflowV34StageEventReceipt.business_session_id == session_id,
                ChatflowV34StageEventReceipt.event_type == "selection.cancelled",
                ChatflowV34StageEventReceipt.disposition == "accepted",
            )
        )
        accepted_selection_receipts = db.scalar(
            select(func.count(ChatflowV34StageEventReceipt.id)).where(
                ChatflowV34StageEventReceipt.business_session_id == session_id,
                ChatflowV34StageEventReceipt.event_type == "selection.applied",
                ChatflowV34StageEventReceipt.disposition == "accepted",
            )
        )
    assert state.form_status == "cancelled"
    assert state.selection_status == "not_started"
    assert task_count == 0
    assert accepted_cancel_receipts == 1
    assert accepted_selection_receipts == 0


def test_v34_cancel_receipt_retires_scheduled_recovery_and_allows_restart(
    v34_pg_engine: Engine,
) -> None:
    (
        client,
        _owner_id,
        reference_set_id,
        session_id,
        recommendation_id,
        run_id,
        _form_token,
    ) = _prepare_v34_selection_ready(
        v34_pg_engine,
        idempotency_key=f"v34-cancel-recovery-restart-{uuid4().hex}",
    )
    with Session(v34_pg_engine) as db, db.begin():
        now = db.scalar(select(func.clock_timestamp()))
        old_mappings = (
            db.execute(
                select(ChatflowDifyReferenceFile)
                .where(ChatflowDifyReferenceFile.business_session_id == session_id)
                .order_by(ChatflowDifyReferenceFile.position)
            )
            .scalars()
            .all()
        )
        old_upload_actions = (
            db.execute(
                select(Action).where(
                    Action.business_session_id == session_id,
                    Action.action_kind == CHATFLOW_REFERENCE_UPLOAD_ACTION,
                )
            )
            .scalars()
            .all()
        )
        assert now is not None and old_mappings and old_upload_actions
        for row in old_mappings:
            row.status = "uploaded"
            row.dify_file_id = f"old-run-file-{row.position}"
            row.uploaded_at = now
            row.updated_at = now
        for action in old_upload_actions:
            action.status = "completed"
            action.last_outcome = "accepted"
            action.completed_at = now

    cancel_response = client.post(
        f"/api/v1/business-sessions/{session_id}/cancel",
        headers={"Origin": TRUSTED_ORIGIN},
    )
    assert cancel_response.status_code == 202, cancel_response.text
    assert cancel_response.json()["status"] == "cancelling"

    port = _CancelLifecyclePort(
        cancel_result=ChatflowCancelResult(
            "unknown", safe_reason_code="DIFY_CANCEL_OUTCOME_PENDING"
        ),
        recovery_result=ChatflowCancelResult("unknown"),
    )
    action_registrations = build_chatflow_action_registrations(port)
    processor = ActionProcessor(
        v34_pg_engine,
        QueueOptions(
            broker_url=f"amqp://integration:integration@localhost:5672/v34-cancel-restart-{uuid4().hex}"
        ),
        action_registrations,
    )
    with Session(v34_pg_engine) as db:
        cancel_action = db.execute(
            select(Action).where(
                Action.business_session_id == session_id,
                Action.action_kind == CHATFLOW_CANCEL_ACTION,
                Action.sequence == CHATFLOW_CANCEL_SEQUENCE,
            )
        ).scalar_one()
        cancel_action_id = cancel_action.id
    assert (
        _process_chatflow_action(v34_pg_engine, action_id=cancel_action_id, processor=processor)
        == "completed"
    )
    assert len(port.cancel_calls) == 1

    with Session(v34_pg_engine) as db:
        cancel_action = db.get(Action, cancel_action_id)
        recovery_action = db.execute(
            select(Action).where(
                Action.business_session_id == session_id,
                Action.action_kind == CHATFLOW_CANCEL_RECOVERY_ACTION,
            )
        ).scalar_one()
        assert cancel_action is not None
        assert cancel_action.status == "completed"
        assert cancel_action.last_outcome == "unknown"
        assert recovery_action.sequence == CHATFLOW_CANCEL_RECOVERY_SEQUENCE_BASE
        assert recovery_action.status == "scheduled"
        recovery_action_id = recovery_action.id

    cancelled_event = _v34_selection_cancelled_event(
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
        event_key=f"{run_id}:cancelled-with-pending-recovery-{uuid4().hex}",
    )
    accepted = ChatflowV34StageEventService(v34_pg_engine).accept(cancelled_event)
    assert accepted.status == "accepted"

    with Session(v34_pg_engine) as db, db.begin():
        session = db.get(BusinessSession, session_id)
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        cancel_action = db.execute(
            select(Action).where(
                Action.business_session_id == session_id,
                Action.action_kind == CHATFLOW_CANCEL_ACTION,
                Action.sequence == CHATFLOW_CANCEL_SEQUENCE,
            )
        ).scalar_one()
        recovery_action = db.get(Action, recovery_action_id)
        assert session is not None and recovery_action is not None
        assert session.status == "cancelled"
        assert state.form_status == "cancelled"
        assert cancel_action.status == "completed"
        assert recovery_action.status == "cancelled"
        recovery_event_id = recovery_action.outbox_event_id
        assert recovery_event_id is not None
        recovery_event = db.get(OutboxEvent, recovery_event_id)
        recovery_receipt = db.get(OutboxReceipt, recovery_event_id)
        assert recovery_event is not None and recovery_receipt is not None
        assert recovery_event.status == "pending"
        assert recovery_receipt.status == "pending"
        earliest_pending_at = db.scalar(
            select(func.min(OutboxEvent.available_at)).where(OutboxEvent.status == "pending")
        )
        assert earliest_pending_at is not None
        recovery_event.available_at = earliest_pending_at - timedelta(seconds=1)

    publisher = _RecordingEventPublisher()
    dispatcher = OutboxDispatcher(
        v34_pg_engine,
        QueueOptions(
            broker_url=f"amqp://integration:integration@localhost:5672/v34-cancel-dispatch-{uuid4().hex}",
            claim_batch_size=1,
        ),
        publisher=publisher,
        action_registrations=action_registrations,
    )
    dispatch_report = dispatcher.dispatch_once()
    assert dispatch_report.claimed == 1
    assert dispatch_report.published == 1
    assert dispatch_report.failed == 0
    assert [event.event_id for event in publisher.events] == [recovery_event_id]
    with Session(v34_pg_engine) as db:
        recovery_event = db.get(OutboxEvent, recovery_event_id)
        recovery_receipt = db.get(OutboxReceipt, recovery_event_id)
        assert recovery_event is not None and recovery_receipt is not None
        assert recovery_event.status == "published"
        assert recovery_receipt.status == "pending"

    # Processing either persisted outbox message after the callback must be a
    # no-op: the uncertain POST is settled by the receipt and the GET-only
    # recovery is retired before it can execute.
    assert (
        _process_chatflow_action(v34_pg_engine, action_id=cancel_action_id, processor=processor)
        == "completed"
    )
    assert (
        _process_chatflow_action(v34_pg_engine, action_id=recovery_action_id, processor=processor)
        == "completed"
    )
    assert len(port.cancel_calls) == 1
    assert port.recovery_calls == []
    with Session(v34_pg_engine) as db:
        recovery_event = db.get(OutboxEvent, recovery_event_id)
        recovery_receipt = db.get(OutboxReceipt, recovery_event_id)
        assert recovery_event is not None and recovery_receipt is not None
        assert recovery_event.status == "published"
        assert recovery_receipt.status == "completed"

    restarted = client.post(
        "/api/v1/business-sessions",
        json={"reference_set_id": str(reference_set_id)},
        headers={
            "Origin": TRUSTED_ORIGIN,
            "Idempotency-Key": f"v34-cancel-recovery-restart-new-{uuid4().hex}",
        },
    )
    assert restarted.status_code == 202, restarted.text
    assert UUID(restarted.json()["business_session_id"]) != session_id


def test_v34_unknown_continuation_action_is_completed_by_accepted_receipt(
    v34_pg_engine: Engine,
) -> None:
    (
        client,
        _owner_id,
        _reference_set_id,
        session_id,
        recommendation_id,
        run_id,
        _form_token,
    ) = _prepare_v34_selection_ready(
        v34_pg_engine,
        idempotency_key=f"v34-unknown-selection-receipt-{uuid4().hex}",
    )

    task_id = _accept_v34_selection_evidence(
        v34_pg_engine,
        client=client,
        session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
    )

    # A fresh DB session is the restart-style read of the durable convergence.
    with Session(v34_pg_engine) as db:
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        action = db.get(Action, state.selection_submission_id)
        task = db.get(Task, task_id)
    assert action is not None and task is not None
    assert action.status == "completed"
    assert action.completed_at is not None
    assert state.selection_status == "accepted"
    assert state.selection_evidence_kind == "selection_applied"
    assert task.status == "queued"


def test_v34_cancel_callback_without_durable_cancel_intent_is_rejected(
    v34_pg_engine: Engine,
) -> None:
    (
        client,
        _owner_id,
        _reference_set_id,
        session_id,
        recommendation_id,
        run_id,
        _form_token,
    ) = _prepare_v34_selection_ready(
        v34_pg_engine,
        idempotency_key=f"v34-cancel-no-intent-{uuid4().hex}",
    )
    event = _v34_selection_cancelled_event(
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
        event_key=f"{run_id}:cancel-without-intent-{uuid4().hex}",
    )

    result = ChatflowV34StageEventService(v34_pg_engine).accept(event)

    assert result.status == "rejected"
    public = client.get(f"/api/v1/business-sessions/{session_id}")
    assert public.status_code == 200
    assert public.json()["status"] == "recommendation_ready"
    assert public.json()["selection_ready"] is True
    with Session(v34_pg_engine) as db:
        session = db.get(BusinessSession, session_id)
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        task_count = db.scalar(
            select(func.count(Task.id)).where(Task.business_session_id == session_id)
        )
        cancel_action_count = db.scalar(
            select(func.count(Action.id)).where(
                Action.business_session_id == session_id,
                Action.action_kind == CHATFLOW_CANCEL_ACTION,
            )
        )
        rejected_receipt = db.execute(
            select(ChatflowV34StageEventReceipt).where(
                ChatflowV34StageEventReceipt.business_session_id == session_id,
                ChatflowV34StageEventReceipt.event_key == event.event_key,
            )
        ).scalar_one()
    assert session is not None and session.status == "recommendation_ready"
    assert state.form_status == "ready"
    assert state.selection_status == "not_started"
    assert task_count == 0
    assert cancel_action_count == 0
    assert rejected_receipt.disposition == "rejected"


def test_v34_late_prepare_failure_cannot_overwrite_cancelled_session(
    v34_pg_engine: Engine,
) -> None:
    (
        client,
        _owner_id,
        _reference_set_id,
        session_id,
        recommendation_id,
        run_id,
        _form_token,
    ) = _prepare_v34_selection_ready(
        v34_pg_engine,
        idempotency_key=f"v34-cancel-late-failure-{uuid4().hex}",
    )
    cancel_request = client.post(
        f"/api/v1/business-sessions/{session_id}/cancel",
        headers={"Origin": TRUSTED_ORIGIN},
    )
    assert cancel_request.status_code == 202, cancel_request.text
    _complete_v34_cancel_action(v34_pg_engine, session_id)
    events = ChatflowV34StageEventService(v34_pg_engine)
    cancelled = _v34_selection_cancelled_event(
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
        event_key=f"{run_id}:selection-cancelled-before-late-failure-{uuid4().hex}",
    )
    assert events.accept(cancelled).status == "accepted"

    late_failure = _v34_workflow_failed_event(
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
        event_key=f"{run_id}:prepare-failed-after-cancel-{uuid4().hex}",
        phase="prepare",
    )
    assert events.accept(late_failure).status == "rejected"

    public = client.get(f"/api/v1/business-sessions/{session_id}")
    assert public.status_code == 200
    assert public.json()["status"] == "cancelled"
    assert public.json()["safe_reason_code"] is None
    with Session(v34_pg_engine) as db:
        session = db.get(BusinessSession, session_id)
        recommendation = db.get(Recommendation, recommendation_id)
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        task_count = db.scalar(
            select(func.count(Task.id)).where(Task.business_session_id == session_id)
        )
        late_failure_receipt = db.execute(
            select(ChatflowV34StageEventReceipt).where(
                ChatflowV34StageEventReceipt.business_session_id == session_id,
                ChatflowV34StageEventReceipt.event_key == late_failure.event_key,
            )
        ).scalar_one()
    assert session is not None and recommendation is not None
    assert session.status == "cancelled"
    assert recommendation.status == "ready"
    assert recommendation.failure_code is None
    assert state.form_status == "cancelled"
    assert state.safe_reason_code is None
    assert late_failure_receipt.disposition == "rejected"
    assert task_count == 0


@pytest.mark.parametrize("existing_evidence", ["task", "selection.applied_receipt"])
def test_v34_cancel_callback_rejects_existing_task_or_selection_receipt_even_if_state_reset(
    v34_pg_engine: Engine,
    existing_evidence: str,
) -> None:
    (
        client,
        _owner_id,
        _reference_set_id,
        session_id,
        recommendation_id,
        run_id,
        _form_token,
    ) = _prepare_v34_selection_ready(
        v34_pg_engine,
        idempotency_key=f"v34-cancel-stale-state-{existing_evidence}-{uuid4().hex}",
    )

    if existing_evidence == "task":
        selected = client.post(
            f"/api/v1/business-sessions/{session_id}/selection",
            json=_selection_request(recommendation_id).model_dump(mode="json"),
            headers={
                "Origin": TRUSTED_ORIGIN,
                "Idempotency-Key": f"v34-cancel-stale-state-task-{uuid4().hex}",
            },
        )
        assert selected.status_code == 202, selected.text
        task_id = UUID(selected.json()["task_id"])
        # Simulate stale/corrupt V34 state while retaining the durable Task.
        with Session(v34_pg_engine) as db, db.begin():
            session = db.get(BusinessSession, session_id)
            state = db.execute(
                select(ChatflowV34SessionState).where(
                    ChatflowV34SessionState.business_session_id == session_id
                )
            ).scalar_one()
            assert session is not None
            session.status = "recommendation_ready"
            state.form_status = "ready"
            state.selection_status = "not_started"
            state.selection_submission_id = None
            state.selection_input_digest = None
            state.selection_inputs = null()
            state.selection_evidence_kind = None
            state.selection_accepted_at = None
    else:
        task_id = None
        with Session(v34_pg_engine) as db, db.begin():
            db.add(
                ChatflowV34StageEventReceipt(
                    business_session_id=session_id,
                    flow_version=V34_FLOW_VERSION,
                    recommendation_id=recommendation_id,
                    recommendation_revision=1,
                    dify_run_id=run_id,
                    node_id="parse_0",
                    event_key=f"{run_id}:selection-applied-existing:{uuid4().hex}",
                    event_type="selection.applied",
                    event_digest=hashlib.sha256(uuid4().bytes).digest(),
                    payload={"selection_submission_id": str(uuid4())},
                    disposition="accepted",
                    safe_reason_code=None,
                    processed_at=db.scalar(select(func.clock_timestamp())),
                )
            )

    _force_v34_cancel_intent_completed(v34_pg_engine, session_id)

    event = _v34_selection_cancelled_event(
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
        event_key=f"{run_id}:cancel-after-selection-evidence:{uuid4().hex}",
    )
    result = ChatflowV34StageEventService(v34_pg_engine).accept(event)
    assert result.status == "rejected"

    with Session(v34_pg_engine) as db:
        session = db.get(BusinessSession, session_id)
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        task_count = db.scalar(
            select(func.count(Task.id)).where(Task.business_session_id == session_id)
        )
        accepted_selection_receipts = db.scalar(
            select(func.count(ChatflowV34StageEventReceipt.id)).where(
                ChatflowV34StageEventReceipt.business_session_id == session_id,
                ChatflowV34StageEventReceipt.event_type == "selection.applied",
                ChatflowV34StageEventReceipt.disposition == "accepted",
            )
        )
        rejected_cancel_receipt = db.execute(
            select(ChatflowV34StageEventReceipt).where(
                ChatflowV34StageEventReceipt.business_session_id == session_id,
                ChatflowV34StageEventReceipt.event_key == event.event_key,
            )
        ).scalar_one()

    assert session is not None
    assert session.status == "reconciling"
    assert state.form_status == "cancelling"
    assert state.selection_status == "not_started"
    assert state.selection_submission_id is None
    assert state.selection_input_digest is None
    assert state.selection_inputs is None
    assert rejected_cancel_receipt.disposition == "rejected"
    assert rejected_cancel_receipt.safe_reason_code == "V34_CANCEL_AFTER_SELECTION"
    assert task_count == (1 if task_id is not None else 0)
    assert accepted_selection_receipts == (
        1 if existing_evidence == "selection.applied_receipt" else 0
    )


def test_v34_expiry_callback_is_terminal_without_task_and_exposes_only_safe_reason(
    v34_pg_engine: Engine,
) -> None:
    (
        client,
        owner_id,
        reference_set_id,
        session_id,
        recommendation_id,
        run_id,
        _form_token,
    ) = _prepare_v34_selection_ready(
        v34_pg_engine,
        idempotency_key=f"v34-form-expired-{uuid4().hex}",
    )
    cancel_requested = client.post(
        f"/api/v1/business-sessions/{session_id}/cancel",
        headers={"Origin": TRUSTED_ORIGIN},
    )
    assert cancel_requested.status_code == 202, cancel_requested.text
    with Session(v34_pg_engine) as db, db.begin():
        now = db.scalar(select(func.clock_timestamp()))
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        assert now is not None
        state.form_expires_at = now - timedelta(seconds=1)

    expired = _v34_selection_expired_event(
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
        event_key=f"{run_id}:form-expired",
    )
    service = ChatflowV34StageEventService(v34_pg_engine)
    assert service.accept(expired).status == "accepted"
    assert service.accept(expired).status == "replayed"

    public = client.get(f"/api/v1/business-sessions/{session_id}")
    assert public.status_code == 200
    assert public.json()["status"] == "expired"
    assert public.json()["phase"] == "expired"
    assert public.json()["safe_reason_code"] == "V34_FORM_EXPIRED"
    cancel = client.post(
        f"/api/v1/business-sessions/{session_id}/cancel",
        headers={"Origin": TRUSTED_ORIGIN},
    )
    assert cancel.status_code == 409

    restart = client.post(
        "/api/v1/business-sessions",
        json={"reference_set_id": str(reference_set_id)},
        headers={
            "Origin": TRUSTED_ORIGIN,
            "Idempotency-Key": f"v34-expired-restart-blocked-{uuid4().hex}",
        },
    )
    assert restart.status_code == 409
    assert restart.json()["code"] == "V34_RESTART_REQUIRES_RECONCILIATION"

    with Session(v34_pg_engine) as db:
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        recommendation = db.get(Recommendation, recommendation_id)
        task_count = db.scalar(
            select(func.count(Task.id)).where(Task.business_session_id == session_id)
        )
        cancel_action_count = db.scalar(
            select(func.count(Action.id)).where(
                Action.business_session_id == session_id,
                Action.action_kind == CHATFLOW_CANCEL_ACTION,
            )
        )
        same_reference_session_count = db.scalar(
            select(func.count(BusinessSession.id)).where(
                BusinessSession.owner_user_id == owner_id,
                BusinessSession.reference_set_id == reference_set_id,
            )
        )
    assert recommendation is not None
    assert recommendation.status == "expired"
    assert state.form_status == "expired"
    assert state.selection_status == "not_started"
    assert state.safe_reason_code == "V34_FORM_EXPIRED"
    assert task_count == 0
    assert cancel_action_count == 1
    assert same_reference_session_count == 1


def test_v34_prepare_failure_uses_fixed_safe_reason_and_keeps_no_task(
    v34_pg_engine: Engine,
) -> None:
    client, _owner_id, _reference_set_id, created = _create_session(
        v34_pg_engine,
        flow_version=V34_FLOW_VERSION,
        idempotency_key=f"v34-prepare-failure-{uuid4().hex}",
    )
    session_id = UUID(created["business_session_id"])
    recommendation_id = UUID(created["recommendation_id"])
    run_id = f"v34-prepare-failure-{uuid4().hex}"
    with Session(v34_pg_engine) as db, db.begin():
        session = db.get(BusinessSession, session_id)
        recommendation = db.get(Recommendation, recommendation_id)
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        assert session is not None and recommendation is not None
        session.status = "awaiting_recommendation"
        session.start_outcome = "accepted"
        session.dify_run_id = run_id
        session.run_revision = 1
        recommendation.dify_run_id = run_id
        recommendation.revision = 1
        recommendation.status = "analyzing"
        recommendation.state = {
            "stage": "awaiting_recommendation",
            "provenance_kind": "dify_chatflow",
            "reference_asset_digest": session.reference_asset_digest.hex(),
        }

    event = _v34_workflow_failed_event(
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
        event_key=f"{run_id}:workflow-failed:prepare",
        phase="prepare",
    )
    assert ChatflowV34StageEventService(v34_pg_engine).accept(event).status == "accepted"

    public = client.get(f"/api/v1/business-sessions/{session_id}")
    assert public.status_code == 200
    assert public.json()["status"] == "failed"
    assert public.json()["phase"] == "failed"
    assert public.json()["safe_reason_code"] == "V34_PREPARE_FAILED"
    with Session(v34_pg_engine) as db:
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        recommendation = db.get(Recommendation, recommendation_id)
        task_count = db.scalar(
            select(func.count(Task.id)).where(Task.business_session_id == session_id)
        )
    assert recommendation is not None
    assert recommendation.failure_code == "V34_PREPARE_FAILED"
    assert state.safe_reason_code == "V34_PREPARE_FAILED"
    assert task_count == 0


def test_v34_incomplete_callback_retains_task_items_without_generation_request(
    v34_pg_engine: Engine,
) -> None:
    (
        client,
        _owner_id,
        _reference_set_id,
        session_id,
        recommendation_id,
        run_id,
        _form_token,
    ) = _prepare_v34_selection_ready(
        v34_pg_engine,
        idempotency_key=f"v34-incomplete-no-generation-{uuid4().hex}",
    )
    task_id = _accept_v34_selection_evidence(
        v34_pg_engine,
        client=client,
        session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
    )
    event = _v34_incomplete_event(
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
        event_key=f"{run_id}:ready:incomplete",
        conflicting_field_ids=["3", "4"],
    )
    assert ChatflowV34StageEventService(v34_pg_engine).accept(event).status == "accepted"

    public = client.get(f"/api/v1/business-sessions/{session_id}")
    assert public.status_code == 200
    assert public.json()["status"] == "failed"
    assert public.json()["safe_reason_code"] == "PRODUCT_IDENTITY_CONFLICT"
    with Session(v34_pg_engine) as db:
        task = db.get(Task, task_id)
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
        generation_permit_count = db.scalar(
            select(func.count(ChatflowGenerationPermit.permit_id))
            .join(
                ChatflowGenerationRequest,
                ChatflowGenerationRequest.id == ChatflowGenerationPermit.generation_request_id,
            )
            .where(ChatflowGenerationRequest.business_session_id == session_id)
        )
    assert task is not None
    assert task.status == "failed"
    assert task.terminal_at is not None
    assert task.expires_at is not None
    assert len(items) == task.planned_count
    assert all(item.status == "failed" for item in items)
    assert all(item.error_code == "V34_PRE_GENERATION_FAILED" for item in items)
    assert all(item.image_id is None for item in items)
    assert generation_request_count == 0
    assert generation_permit_count == 0


@pytest.mark.parametrize("add_permit", [False, True])
def test_v34_generation_request_or_permit_blocks_late_terminal_reprojection(
    v34_pg_engine: Engine,
    add_permit: bool,
) -> None:
    (
        client,
        _owner_id,
        _reference_set_id,
        session_id,
        recommendation_id,
        run_id,
        _form_token,
    ) = _prepare_v34_selection_ready(
        v34_pg_engine,
        idempotency_key=f"v34-generation-protection-{uuid4().hex}",
    )
    task_id = _accept_v34_selection_evidence(
        v34_pg_engine,
        client=client,
        session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
    )
    generation_request_id = uuid4()
    package_id = uuid4()
    permit_id = uuid4()
    package_digest = b"p" * 32
    request_digest = b"r" * 32
    now = None
    with Session(v34_pg_engine) as db:
        now = db.scalar(select(func.clock_timestamp()))
        task = db.get(Task, task_id)
    assert now is not None and task is not None
    with Session(v34_pg_engine) as db, db.begin():
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
                package_version="test-generation-protection-v1",
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
                id=generation_request_id,
                package_id=package_id,
                task_id=task.id,
                business_session_id=session_id,
                dify_run_id=run_id,
                package_digest=package_digest,
                request_digest=request_digest,
            )
        )
        if add_permit:
            db.flush()
            db.add(
                ChatflowGenerationPermit(
                    permit_id=permit_id,
                    generation_request_id=generation_request_id,
                    task_id=task.id,
                    package_digest=package_digest,
                    contract_version="chatflow-handoff-v1",
                    permit_version=2,
                    slot_mapping_digest=b"s" * 32,
                    reference_manifest_digest=b"f" * 32,
                    token_digest=b"t" * 32,
                    expires_at=now + timedelta(hours=1),
                )
            )

    event = _v34_workflow_failed_event(
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
        event_key=f"{run_id}:late-workflow-failed:design:{add_permit}",
        phase="design",
    )
    accepted = ChatflowV34StageEventService(v34_pg_engine).accept(event)
    assert accepted.status == "rejected"

    public = client.get(f"/api/v1/business-sessions/{session_id}")
    assert public.status_code == 200
    assert public.json()["status"] == "task_created"
    assert public.json()["safe_reason_code"] is None
    with Session(v34_pg_engine) as db:
        session = db.get(BusinessSession, session_id)
        recommendation = db.get(Recommendation, recommendation_id)
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        task_after = db.get(Task, task_id)
        item_states = (
            db.execute(select(TaskItem.status).where(TaskItem.task_id == task_id)).scalars().all()
        )
        permit_count = db.scalar(
            select(func.count(ChatflowGenerationPermit.permit_id)).where(
                ChatflowGenerationPermit.generation_request_id == generation_request_id
            )
        )
    assert session is not None and recommendation is not None and task_after is not None
    assert session.status == "task_created"
    assert recommendation.status == "ready"
    assert state.selection_status == "accepted"
    assert state.safe_reason_code is None
    assert task_after.status == "queued"
    assert task_after.terminal_at is None
    assert all(status == "queued" for status in item_states)
    assert permit_count == (1 if add_permit else 0)


@pytest.mark.parametrize(
    ("terminal_event", "workflow_status"),
    [
        ("workflow_finished", "failed"),
        ("workflow_failed", "failed"),
        ("workflow_failed", None),
    ],
)
def test_v34_run_observation_fails_same_run_only_for_explicit_failure_terminal(
    v34_pg_engine: Engine,
    terminal_event: str,
    workflow_status: str | None,
) -> None:
    client, session_id, recommendation_id, run_id, task_id, action_id = (
        _prepare_v34_run_observation_case(
            v34_pg_engine,
            idempotency_key=f"v34-run-observe-failure-{terminal_event}-{uuid4().hex}",
        )
    )
    observation = ChatflowOriginalRunObservationResult(
        "received",
        events=(
            ChatflowOriginalRunEvent(
                event=terminal_event,
                dify_run_id=run_id,
                workflow_status=workflow_status,
            ),
        ),
    )

    result, port = _process_v34_run_observation(
        v34_pg_engine, action_id=action_id, observation=observation
    )

    assert result == "completed"
    assert port.observation_calls == [(run_id, f"bs-{session_id.hex}")]
    assert port.calls == []
    public = client.get(f"/api/v1/business-sessions/{session_id}")
    assert public.status_code == 200
    assert public.json()["status"] == "failed"
    with Session(v34_pg_engine) as db:
        session = db.get(BusinessSession, session_id)
        recommendation = db.get(Recommendation, recommendation_id)
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        task = db.get(Task, task_id)
        items = (
            db.execute(
                select(TaskItem).where(TaskItem.task_id == task_id).order_by(TaskItem.position)
            )
            .scalars()
            .all()
        )
        receipts = (
            db.execute(
                select(ChatflowV34StageEventReceipt).where(
                    ChatflowV34StageEventReceipt.business_session_id == session_id,
                    ChatflowV34StageEventReceipt.event_type == "workflow.failed",
                )
            )
            .scalars()
            .all()
        )
        generation_request_count = db.scalar(
            select(func.count(ChatflowGenerationRequest.id)).where(
                ChatflowGenerationRequest.business_session_id == session_id
            )
        )
        generation_permit_count = db.scalar(
            select(func.count(ChatflowGenerationPermit.permit_id))
            .join(
                ChatflowGenerationRequest,
                ChatflowGenerationRequest.id == ChatflowGenerationPermit.generation_request_id,
            )
            .where(ChatflowGenerationRequest.business_session_id == session_id)
        )
        generation_action_count = db.scalar(
            select(func.count(Action.id)).where(
                Action.business_session_id == session_id,
                Action.action_kind.like("generation.%"),
            )
        )
        run_observation_action = db.execute(
            select(Action).where(Action.id == action_id)
        ).scalar_one()

    assert session is not None and recommendation is not None and task is not None
    assert session.status == "failed"
    assert recommendation.status == "failed"
    assert recommendation.failure_code == "V34_DIFY_WORKFLOW_FAILED"
    assert task.status == "failed"
    assert task.terminal_at is not None
    assert task.expires_at is not None
    assert len(items) == task.planned_count
    assert all(item.status == "failed" for item in items)
    assert all(item.error_code == "V34_PRE_GENERATION_FAILED" for item in items)
    assert all(item.image_id is None for item in items)
    assert state.selection_status == "accepted"
    assert state.safe_reason_code == "V34_DIFY_WORKFLOW_FAILED"
    assert run_observation_action.action_kind == CHATFLOW_RUN_OBSERVATION_ACTION
    assert run_observation_action.sequence == CHATFLOW_RUN_OBSERVATION_SEQUENCE_BASE
    assert run_observation_action.status == "completed"
    assert len(receipts) == 1
    assert receipts[0].disposition == "accepted"
    assert receipts[0].node_id == "workflow_finished"
    assert receipts[0].payload["source"] == "dify_sse_terminal_observation_v1"
    assert receipts[0].payload["terminal_event"] == terminal_event
    assert receipts[0].payload["workflow_status"] == "failed"
    assert generation_request_count == 0
    assert generation_permit_count == 0
    assert generation_action_count == 0


@pytest.mark.parametrize(
    ("observed_event", "workflow_status", "wrong_run"),
    [
        ("node_failed", "failed", False),
        ("workflow_paused", "paused", False),
        ("workflow_finished", None, False),
        ("workflow_failed", "succeeded", False),
        ("workflow_finished", "failed", True),
    ],
)
def test_v34_run_observation_reconciles_untrusted_nonterminal_or_other_run(
    v34_pg_engine: Engine,
    observed_event: str,
    workflow_status: str | None,
    wrong_run: bool,
) -> None:
    client, session_id, recommendation_id, run_id, task_id, action_id = (
        _prepare_v34_run_observation_case(
            v34_pg_engine,
            idempotency_key=f"v34-run-observe-nonfailure-{uuid4().hex}",
        )
    )
    observation_run_id = f"other-{run_id}" if wrong_run else run_id
    observation = ChatflowOriginalRunObservationResult(
        "received",
        events=(
            ChatflowOriginalRunEvent(
                event=observed_event,
                dify_run_id=observation_run_id,
                workflow_status=workflow_status,
            ),
        ),
    )

    result, port = _process_v34_run_observation(
        v34_pg_engine, action_id=action_id, observation=observation
    )

    assert result == "completed"
    assert port.observation_calls == [(run_id, f"bs-{session_id.hex}")]
    assert port.calls == []
    with Session(v34_pg_engine) as db:
        session = db.get(BusinessSession, session_id)
        recommendation = db.get(Recommendation, recommendation_id)
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        task = db.get(Task, task_id)
        items = db.execute(select(TaskItem).where(TaskItem.task_id == task_id)).scalars().all()
        failure_receipt_count = db.scalar(
            select(func.count(ChatflowV34StageEventReceipt.id)).where(
                ChatflowV34StageEventReceipt.business_session_id == session_id,
                ChatflowV34StageEventReceipt.event_type == "workflow.failed",
                ChatflowV34StageEventReceipt.disposition == "accepted",
            )
        )
        next_observation = db.execute(
            select(Action).where(
                Action.business_session_id == session_id,
                Action.action_kind == CHATFLOW_RUN_OBSERVATION_ACTION,
                Action.sequence == CHATFLOW_RUN_OBSERVATION_SEQUENCE_BASE + 1,
            )
        ).scalar_one_or_none()

    assert session is not None and recommendation is not None and task is not None
    assert session.status != "failed"
    assert recommendation.status == "ready"
    assert state.selection_status == "accepted"
    assert task.status != "failed"
    assert all(item.status == "queued" and item.image_id is None for item in items)
    assert failure_receipt_count == 0
    # Trusted succeeded/partial-succeeded termination without a generation permit
    # belongs to the S026 closure tests, including its durable/owner-safe evidence.
    assert next_observation is not None
    assert next_observation.status == "scheduled"
    assert session.status == "reconciling"
    assert task.status == "reconciling"


@pytest.mark.parametrize(
    ("reconciliation_reason", "expected_status"),
    [
        ("V34_RUN_TERMINAL_NOT_OBSERVED", 200),
        ("OTHER_RECONCILIATION_REASON", 409),
    ],
)
def test_v34_h4_first_submission_only_passes_observer_reconciling_state(
    v34_pg_engine: Engine,
    reconciliation_reason: str,
    expected_status: int,
) -> None:
    client, session_id, recommendation_id, run_id, task_id, action_id = (
        _prepare_v34_run_observation_case(
            v34_pg_engine,
            idempotency_key=f"v34-h4-observer-reconcile-{uuid4().hex}",
        )
    )
    observation = ChatflowOriginalRunObservationResult(
        "received",
        events=(
            ChatflowOriginalRunEvent(
                event="workflow_paused",
                dify_run_id=run_id,
                workflow_status="paused",
            ),
        ),
    )
    observed, _port = _process_v34_run_observation(
        v34_pg_engine, action_id=action_id, observation=observation
    )
    assert observed == "completed"

    with Session(v34_pg_engine) as db, db.begin():
        session = db.get(BusinessSession, session_id)
        recommendation = db.get(Recommendation, recommendation_id)
        state = db.execute(
            select(ChatflowV34SessionState)
            .where(ChatflowV34SessionState.business_session_id == session_id)
            .with_for_update()
        ).scalar_one()
        task = db.get(Task, task_id)
        assert session is not None and recommendation is not None and task is not None
        assert session.status == "reconciling"
        assert task.status == "reconciling"
        assert state.selection_status == "accepted"
        assert state.safe_reason_code == "V34_RUN_TERMINAL_NOT_OBSERVED"
        if reconciliation_reason != "V34_RUN_TERMINAL_NOT_OBSERVED":
            state.safe_reason_code = reconciliation_reason
        selection_action_id = state.selection_submission_id

    assert selection_action_id is not None
    callback = SimpleNamespace(
        recommendation_id=recommendation_id,
        recommendation_revision=1,
        dify_run_id=run_id,
    )
    submission = _submission_for_task(
        v34_pg_engine,
        task_id=task_id,
        session_id=session_id,
        callback=callback,
    ).model_copy(update={"submission_id": prompt_package_submission_id(selection_action_id)})
    prompt_service = ChatflowPromptPackageService(
        v34_pg_engine,
        permit_secret=SecretStr(TEST_GENERATION_PERMIT_SECRET),
        service_authority_id=TEST_SERVICE_AUTHORITY_ID,
    )
    client.app.include_router(
        create_internal_dify_router(
            engine=v34_pg_engine,
            callback_credential=SecretStr(TEST_INTERNAL_CALLBACK_SECRET),
            prompt_package_service=prompt_service,
        )
    )

    response = _post_submission(client, submission)

    assert response.status_code == expected_status, response.text
    if expected_status == 200:
        assert response.json()["status"] == "accepted"
        assert response.json()["permit"]["permit_version"] == 2
        assert "prompt_package" not in response.json()
    else:
        assert response.json()["code"] == "PROMPT_PACKAGE_LINKAGE_INVALID"

    with Session(v34_pg_engine) as db:
        package_count = db.scalar(
            select(func.count(ChatflowPromptPackage.id)).where(
                ChatflowPromptPackage.task_id == task_id
            )
        )
        request_count = db.scalar(
            select(func.count(ChatflowGenerationRequest.id)).where(
                ChatflowGenerationRequest.task_id == task_id
            )
        )
        permit_count = db.scalar(
            select(func.count(ChatflowGenerationPermit.permit_id)).where(
                ChatflowGenerationPermit.task_id == task_id
            )
        )
        tracking_count = db.scalar(
            select(func.count(ChatflowGenerationTracking.generation_request_id)).where(
                ChatflowGenerationTracking.task_id == task_id
            )
        )
        attempt_count = db.scalar(
            select(func.count(TaskItemAttempt.id))
            .join(TaskItem, TaskItem.id == TaskItemAttempt.task_item_id)
            .where(TaskItem.task_id == task_id)
        )
    expected_count = 1 if expected_status == 200 else 0
    assert (package_count, request_count, permit_count, tracking_count) == (expected_count,) * 4
    assert attempt_count == (len(submission.image_types) if expected_status == 200 else 0)


def test_v34_h4_is_rejected_after_terminal_observer_commits(v34_pg_engine: Engine) -> None:
    (
        client,
        session_id,
        recommendation_id,
        run_id,
        task_id,
        terminal_action_id,
        submission,
    ) = _prepare_v34_observer_reconciling_handoff(
        v34_pg_engine, idempotency_key=f"v34-h4-observer-first-{uuid4().hex}"
    )
    failed = ChatflowOriginalRunObservationResult(
        "received",
        events=(
            ChatflowOriginalRunEvent(
                event="workflow_finished",
                dify_run_id=run_id,
                workflow_status="failed",
            ),
        ),
    )

    result, _port = _process_v34_run_observation(
        v34_pg_engine, action_id=terminal_action_id, observation=failed
    )
    assert result == "completed"
    response = _post_submission(client, submission)

    assert response.status_code == 409
    assert response.json()["code"] == "PROMPT_PACKAGE_LINKAGE_INVALID"
    assert _v34_handoff_row_counts(v34_pg_engine, task_id) == (0, 0, 0, 0, 0)
    with Session(v34_pg_engine) as db:
        session = db.get(BusinessSession, session_id)
        recommendation = db.get(Recommendation, recommendation_id)
        task = db.get(Task, task_id)
        receipt = db.execute(
            select(ChatflowV34StageEventReceipt).where(
                ChatflowV34StageEventReceipt.business_session_id == session_id,
                ChatflowV34StageEventReceipt.event_type == "workflow.failed",
                ChatflowV34StageEventReceipt.disposition == "accepted",
            )
        ).scalar_one()
        assert session is not None and session.status == "failed"
        assert recommendation is not None and recommendation.status == "failed"
        assert task is not None and task.status == "failed" and task.terminal_at is not None
        assert receipt.dify_run_id == run_id
        assert receipt.safe_reason_code == "V34_DIFY_WORKFLOW_FAILED"


def test_v34_h4_commit_before_terminal_observer_preserves_generation_chain(
    v34_pg_engine: Engine,
) -> None:
    (
        client,
        session_id,
        _recommendation_id,
        run_id,
        task_id,
        terminal_action_id,
        submission,
    ) = _prepare_v34_observer_reconciling_handoff(
        v34_pg_engine, idempotency_key=f"v34-h4-commit-first-{uuid4().hex}"
    )
    failed = ChatflowOriginalRunObservationResult(
        "received",
        events=(
            ChatflowOriginalRunEvent(
                event="workflow_finished",
                dify_run_id=run_id,
                workflow_status="failed",
            ),
        ),
    )
    permit_inserted = threading.Event()
    release_h4_commit = threading.Event()
    observer_lock_attempt = threading.Event()
    observer_thread_id: list[int] = []
    backend_pids: dict[str, int] = {}
    backend_connections: dict[str, int] = {}
    results: dict[str, object] = {}
    errors: dict[str, BaseException] = {}
    observer_engine = create_engine(v34_pg_engine.url, pool_pre_ping=True)

    def backend_pid(connection) -> int:
        return connection.connection.driver_connection.info.backend_pid

    def after_cursor_execute(connection, _cursor, statement, _parameters, _context, _many) -> None:
        # TestClient executes the ASGI handler on its portal thread, so identify
        # the H4 transaction by its permit insert while the observer has not started.
        if "insert into chatflow_generation_permits" in statement.lower():
            backend_pids["h4"] = backend_pid(connection)
            backend_connections["h4"] = id(connection.connection.driver_connection)
            permit_inserted.set()
            if not release_h4_commit.wait(20):
                raise TimeoutError("test synchronization did not release the H4 transaction")

    def before_cursor_execute(connection, _cursor, statement, _parameters, _context, _many) -> None:
        if (
            observer_thread_id
            and threading.get_ident() == observer_thread_id[0]
            and "from users" in statement.lower()
            and "for update" in statement.lower()
        ):
            backend_pids["observer"] = backend_pid(connection)
            backend_connections["observer"] = id(connection.connection.driver_connection)
            observer_lock_attempt.set()

    def submit_h4() -> None:
        try:
            results["h4"] = _post_submission(client, submission)
        except BaseException as exc:  # surfaced in the owning test thread
            errors["h4"] = exc

    def observe_terminal_failure() -> None:
        observer_thread_id.append(threading.get_ident())
        try:
            results["observer"] = _process_v34_run_observation(
                observer_engine, action_id=terminal_action_id, observation=failed
            )[0]
        except BaseException as exc:  # surfaced in the owning test thread
            errors["observer"] = exc

    event.listen(v34_pg_engine, "after_cursor_execute", after_cursor_execute)
    event.listen(observer_engine, "before_cursor_execute", before_cursor_execute)
    h4_thread = threading.Thread(target=submit_h4, name="v34-h4-submit", daemon=True)
    observer_thread: threading.Thread | None = None
    h4_thread.start()
    try:
        assert permit_inserted.wait(15), f"H4 did not flush its generation permit: {errors!r}"
        observer_thread = threading.Thread(
            target=observe_terminal_failure, name="v34-terminal-observer", daemon=True
        )
        observer_thread.start()
        assert observer_lock_attempt.wait(15), "terminal observer did not contend for session locks"
        observer_pid = backend_pids.get("observer")
        assert observer_pid is not None
        _wait_for_postgres_lock_wait(v34_pg_engine, observer_pid)
    finally:
        release_h4_commit.set()
        h4_thread.join(timeout=20)
        if observer_thread is not None:
            observer_thread.join(timeout=20)
        event.remove(v34_pg_engine, "after_cursor_execute", after_cursor_execute)
        event.remove(observer_engine, "before_cursor_execute", before_cursor_execute)
        observer_engine.dispose()

    assert not h4_thread.is_alive(), "H4 request deadlocked past the test deadline"
    assert observer_thread is not None and not observer_thread.is_alive(), (
        "terminal observer deadlocked past the test deadline"
    )
    assert errors == {}, repr(errors)
    assert backend_pids.get("h4") is not None
    assert backend_pids["h4"] != backend_pids["observer"], (
        f"H4 and observer did not use distinct PostgreSQL backends: "
        f"pids={backend_pids!r}, connections={backend_connections!r}"
    )
    response = results["h4"]
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "accepted"
    assert response.json()["permit"]["permit_version"] == 2
    assert results["observer"] == "completed"
    assert _v34_handoff_row_counts(v34_pg_engine, task_id) == (
        1,
        1,
        1,
        1,
        len(submission.image_types),
    )
    with Session(v34_pg_engine) as db:
        session = db.get(BusinessSession, session_id)
        task = db.get(Task, task_id)
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        items = db.execute(
            select(TaskItem).where(TaskItem.task_id == task_id).order_by(TaskItem.position)
        ).scalars().all()
        failed_receipts = db.scalar(
            select(func.count(ChatflowV34StageEventReceipt.id)).where(
                ChatflowV34StageEventReceipt.business_session_id == session_id,
                ChatflowV34StageEventReceipt.event_type == "workflow.failed",
                ChatflowV34StageEventReceipt.disposition == "accepted",
            )
        )
        request = db.execute(
            select(ChatflowGenerationRequest).where(ChatflowGenerationRequest.task_id == task_id)
        ).scalar_one()
        permit = db.execute(
            select(ChatflowGenerationPermit).where(
                ChatflowGenerationPermit.generation_request_id == request.id
            )
        ).scalar_one()
        assert session is not None and session.status == "reconciling"
        assert task is not None and task.status == "reconciling" and task.terminal_at is None
        assert state.safe_reason_code == "V34_RUN_TERMINAL_NOT_OBSERVED"
        assert failed_receipts == 0
        assert all(item.status == "queued" and item.image_id is None for item in items)
        assert str(request.id) == response.json()["generation_request_id"]
        assert str(permit.permit_id) == response.json()["permit"]["permit_id"]


def test_v34_repeated_h4_replays_one_request_and_permit(v34_pg_engine: Engine) -> None:
    (
        _client,
        session_id,
        _recommendation_id,
        _run_id,
        task_id,
        _terminal_action_id,
        submission,
    ) = _prepare_v34_observer_reconciling_handoff(
        v34_pg_engine, idempotency_key=f"v34-h4-replay-{uuid4().hex}"
    )
    first = _post_submission(_client, submission)
    replay = _post_submission(_client, submission)

    assert first.status_code == replay.status_code == 200
    assert first.json()["status"] == "accepted"
    assert replay.json()["status"] == "replayed"
    assert replay.json()["generation_request_id"] == first.json()["generation_request_id"]
    assert replay.json()["permit"] == first.json()["permit"]
    assert _v34_handoff_row_counts(v34_pg_engine, task_id) == (
        1,
        1,
        1,
        1,
        len(submission.image_types),
    )
    with Session(v34_pg_engine) as db:
        session = db.get(BusinessSession, session_id)
        task = db.get(Task, task_id)
        assert session is not None and session.status == "reconciling"
        assert task is not None and task.status == "reconciling" and task.terminal_at is None


@pytest.mark.parametrize("rejection", ["wrong_run", "expired", "non_target_reason"])
def test_v34_observer_exception_rejects_wrong_run_expiry_and_other_reasons(
    v34_pg_engine: Engine,
    rejection: str,
) -> None:
    (
        _client,
        session_id,
        recommendation_id,
        run_id,
        task_id,
        _terminal_action_id,
        submission,
    ) = _prepare_v34_observer_reconciling_handoff(
        v34_pg_engine, idempotency_key=f"v34-h4-reject-{rejection}-{uuid4().hex}"
    )
    expected_code = "PROMPT_PACKAGE_LINKAGE_INVALID"
    if rejection == "wrong_run":
        submission = submission.model_copy(update={"dify_run_id": f"{run_id}-different"})
    elif rejection == "expired":
        with Session(v34_pg_engine) as db, db.begin():
            recommendation = db.get(Recommendation, recommendation_id)
            now = db.scalar(select(func.clock_timestamp()))
            assert recommendation is not None and now is not None
            recommendation.valid_until = now - timedelta(seconds=1)
        expected_code = "GENERATION_PERMIT_EXPIRED"
    else:
        with Session(v34_pg_engine) as db, db.begin():
            state = db.execute(
                select(ChatflowV34SessionState)
                .where(ChatflowV34SessionState.business_session_id == session_id)
                .with_for_update()
            ).scalar_one()
            state.safe_reason_code = "OTHER_RECONCILIATION_REASON"

    response = _post_submission(_client, submission)
    expected_status = 410 if rejection == "expired" else 409
    assert response.status_code == expected_status
    assert response.json()["code"] == expected_code
    assert _v34_handoff_row_counts(v34_pg_engine, task_id) == (0, 0, 0, 0, 0)


@pytest.mark.parametrize(
    ("terminal_failure", "reconciliation_reason", "expected_status"),
    [
        (False, "V34_RUN_TERMINAL_NOT_OBSERVED", 200),
        (False, "OTHER_RECONCILIATION_REASON", 409),
        (True, None, 409),
    ],
)
def test_v34_prompt_context_only_passes_same_run_unobserved_state(
    v34_pg_engine: Engine,
    terminal_failure: bool,
    reconciliation_reason: str | None,
    expected_status: int,
) -> None:
    client, session_id, _recommendation_id, run_id, task_id, action_id = (
        _prepare_v34_run_observation_case(
            v34_pg_engine,
            idempotency_key=f"v34-prompt-context-observer-{uuid4().hex}",
        )
    )
    observation = ChatflowOriginalRunObservationResult(
        "received",
        events=(
            ChatflowOriginalRunEvent(
                event="workflow_finished" if terminal_failure else "workflow_paused",
                dify_run_id=run_id,
                workflow_status="failed" if terminal_failure else "paused",
            ),
        ),
    )
    observed, _port = _process_v34_run_observation(
        v34_pg_engine, action_id=action_id, observation=observation
    )
    assert observed == "completed"

    if (
        reconciliation_reason is not None
        and reconciliation_reason != "V34_RUN_TERMINAL_NOT_OBSERVED"
    ):
        with Session(v34_pg_engine) as db, db.begin():
            state = db.execute(
                select(ChatflowV34SessionState)
                .where(ChatflowV34SessionState.business_session_id == session_id)
                .with_for_update()
            ).scalar_one()
            state.safe_reason_code = reconciliation_reason

    context_service = ChatflowV34ContextService(
        v34_pg_engine,
        dify_app_id="test-v34-app",
        dify_workflow_id="test-v34-workflow-release",
    )
    client.app.include_router(
        create_internal_dify_router(
            engine=v34_pg_engine,
            callback_credential=SecretStr(TEST_INTERNAL_CALLBACK_SECRET),
            v34_context_service=context_service,
        )
    )
    path = f"/internal/v1/dify/business-sessions/{session_id}/context"
    headers = {
        "Authorization": f"Bearer {TEST_INTERNAL_CALLBACK_SECRET}",
        "Content-Type": "application/json",
    }

    with Session(v34_pg_engine) as db:
        action_count_before = db.scalar(
            select(func.count(Action.id)).where(Action.business_session_id == session_id)
        )
    response = client.get(
        path,
        params={"workflow_run_id": run_id, "phase": "prompt"},
        headers=headers,
    )

    assert response.status_code == expected_status, response.text
    if expected_status == 200:
        body = response.json()
        assert body["phase"] == "prompt"
        assert body["selection_status"] == "accepted"
        assert body["task_id"] == str(task_id)
        wrong_run = client.get(
            path,
            params={"workflow_run_id": f"other-{run_id}", "phase": "prompt"},
            headers=headers,
        )
        assert wrong_run.status_code == 409
    else:
        assert response.json()["code"] == "V34_CONTEXT_NOT_READY"

    with Session(v34_pg_engine) as db:
        task = db.get(Task, task_id)
        session = db.get(BusinessSession, session_id)
        assert task is not None and session is not None
        assert task.status == ("failed" if terminal_failure else "reconciling")
        assert session.status == ("failed" if terminal_failure else "reconciling")
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
                select(func.count(ChatflowGenerationRequest.id)).where(
                    ChatflowGenerationRequest.task_id == task_id
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
                select(func.count(TaskItemAttempt.id))
                .join(TaskItem, TaskItem.id == TaskItemAttempt.task_item_id)
                .where(TaskItem.task_id == task_id)
            )
            == 0
        )
        action_count_after = db.scalar(
            select(func.count(Action.id)).where(Action.business_session_id == session_id)
        )
    assert action_count_after == action_count_before


def test_v34_h4_first_submission_stays_blocked_after_observed_run_failure(
    v34_pg_engine: Engine,
) -> None:
    client, session_id, recommendation_id, run_id, task_id, action_id = (
        _prepare_v34_run_observation_case(
            v34_pg_engine,
            idempotency_key=f"v34-h4-terminal-failure-{uuid4().hex}",
        )
    )
    observation = ChatflowOriginalRunObservationResult(
        "received",
        events=(
            ChatflowOriginalRunEvent(
                event="workflow_finished",
                dify_run_id=run_id,
                workflow_status="failed",
            ),
        ),
    )
    observed, _port = _process_v34_run_observation(
        v34_pg_engine, action_id=action_id, observation=observation
    )
    assert observed == "completed"

    with Session(v34_pg_engine) as db:
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        assert state.selection_submission_id is not None
        selection_action_id = state.selection_submission_id
        callback = SimpleNamespace(
            recommendation_id=recommendation_id,
            recommendation_revision=1,
            dify_run_id=run_id,
        )
    submission = _submission_for_task(
        v34_pg_engine,
        task_id=task_id,
        session_id=session_id,
        callback=callback,
    ).model_copy(update={"submission_id": prompt_package_submission_id(selection_action_id)})
    client.app.include_router(
        create_internal_dify_router(
            engine=v34_pg_engine,
            callback_credential=SecretStr(TEST_INTERNAL_CALLBACK_SECRET),
            prompt_package_service=ChatflowPromptPackageService(
                v34_pg_engine,
                permit_secret=SecretStr(TEST_GENERATION_PERMIT_SECRET),
                service_authority_id=TEST_SERVICE_AUTHORITY_ID,
            ),
        )
    )

    response = _post_submission(client, submission)

    assert response.status_code == 409
    assert response.json()["code"] == "PROMPT_PACKAGE_LINKAGE_INVALID"
    with Session(v34_pg_engine) as db:
        session = db.get(BusinessSession, session_id)
        task = db.get(Task, task_id)
        assert session is not None and session.status == "failed"
        assert task is not None and task.status == "failed" and task.terminal_at is not None
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
                select(func.count(ChatflowGenerationRequest.id)).where(
                    ChatflowGenerationRequest.task_id == task_id
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
                select(func.count(TaskItemAttempt.id))
                .join(TaskItem, TaskItem.id == TaskItemAttempt.task_item_id)
                .where(TaskItem.task_id == task_id)
            )
            == 0
        )


@pytest.mark.parametrize("guard", ["generation_request", "successful_image"])
def test_v34_run_observation_failure_does_not_override_generation_or_success(
    v34_pg_engine: Engine,
    guard: str,
) -> None:
    client, session_id, recommendation_id, run_id, task_id, action_id = (
        _prepare_v34_run_observation_case(
            v34_pg_engine,
            idempotency_key=f"v34-run-observe-guard-{guard}-{uuid4().hex}",
        )
    )
    generation_request_id = uuid4()
    if guard == "generation_request":
        with Session(v34_pg_engine) as db:
            now = db.scalar(select(func.clock_timestamp()))
            task = db.get(Task, task_id)
        assert now is not None and task is not None
        package_id = uuid4()
        package_digest = b"p" * 32
        request_digest = b"r" * 32
        with Session(v34_pg_engine) as db, db.begin():
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
                    package_version="test-run-observation-protection-v1",
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
                    id=generation_request_id,
                    package_id=package_id,
                    task_id=task.id,
                    business_session_id=session_id,
                    dify_run_id=run_id,
                    package_digest=package_digest,
                    request_digest=request_digest,
                )
            )
    else:
        with Session(v34_pg_engine) as db, db.begin():
            task = db.get(Task, task_id)
            assert task is not None
            task.status = "partially_succeeded"
            successful_item = (
                db.execute(
                    select(TaskItem).where(TaskItem.task_id == task_id).order_by(TaskItem.position)
                )
                .scalars()
                .first()
            )
            assert successful_item is not None
            successful_item.status = "succeeded"
            successful_item.image_id = uuid4()

    observation = ChatflowOriginalRunObservationResult(
        "received",
        events=(
            ChatflowOriginalRunEvent(
                event="workflow_finished",
                dify_run_id=run_id,
                workflow_status="failed",
            ),
        ),
    )
    result, port = _process_v34_run_observation(
        v34_pg_engine, action_id=action_id, observation=observation
    )

    assert result == "completed"
    assert port.calls == []
    assert port.observation_calls == (
        [] if guard == "generation_request" else [(run_id, f"bs-{session_id.hex}")]
    )
    with Session(v34_pg_engine) as db:
        session = db.get(BusinessSession, session_id)
        recommendation = db.get(Recommendation, recommendation_id)
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        task = db.get(Task, task_id)
        items = (
            db.execute(
                select(TaskItem).where(TaskItem.task_id == task_id).order_by(TaskItem.position)
            )
            .scalars()
            .all()
        )
        failure_receipt_count = db.scalar(
            select(func.count(ChatflowV34StageEventReceipt.id)).where(
                ChatflowV34StageEventReceipt.business_session_id == session_id,
                ChatflowV34StageEventReceipt.event_type == "workflow.failed",
            )
        )
        generation_request_count = db.scalar(
            select(func.count(ChatflowGenerationRequest.id)).where(
                ChatflowGenerationRequest.business_session_id == session_id
            )
        )

    assert session is not None and recommendation is not None and task is not None
    assert session.status != "failed"
    assert recommendation.status == "ready"
    assert state.selection_status == "accepted"
    assert state.safe_reason_code is None
    assert task.status == ("queued" if guard == "generation_request" else "partially_succeeded")
    assert failure_receipt_count == 0
    assert generation_request_count == (1 if guard == "generation_request" else 0)
    if guard == "generation_request":
        assert all(item.status == "queued" and item.image_id is None for item in items)
    else:
        assert items[0].status == "succeeded"
        assert items[0].image_id is not None
        assert all(item.status == "queued" for item in items[1:])


def test_v34_cancel_requires_exact_original_run_terminal_evidence_and_recovery_never_reposts(
    v34_pg_engine: Engine,
) -> None:
    (
        client,
        _owner_id,
        _reference_set_id,
        session_id,
        _recommendation_id,
        run_id,
        _form_token,
    ) = _prepare_v34_selection_ready(
        v34_pg_engine,
        idempotency_key=f"v34-cancel-recovery-{uuid4().hex}",
    )
    response = client.post(
        f"/api/v1/business-sessions/{session_id}/cancel",
        headers={"Origin": TRUSTED_ORIGIN},
    )
    assert response.status_code == 202, response.text

    # A terminal-looking sequence for another Dify run cannot close this
    # session. The only recovery call is a bounded observation of the bound run.
    other_run = f"other-{uuid4().hex}"
    port = _CancelLifecyclePort(
        cancel_result=ChatflowCancelResult(
            "accepted",
            events=(
                ChatflowOriginalRunEvent(
                    event="node_finished",
                    dify_run_id=other_run,
                    node_id="cancelled",
                ),
                ChatflowOriginalRunEvent(
                    event="workflow_finished",
                    dify_run_id=other_run,
                    workflow_status="succeeded",
                ),
            ),
        ),
        recovery_result=ChatflowCancelResult(
            "unknown",
            safe_reason_code="DIFY_CANCEL_RECOVERY_REQUIRES_ROOT_EVIDENCE",
            events=(
                ChatflowOriginalRunEvent(
                    event="human_input_required",
                    dify_run_id=run_id,
                    node_id="form_0",
                ),
            ),
        ),
    )
    processor = ActionProcessor(
        v34_pg_engine,
        QueueOptions(
            broker_url=f"amqp://integration:integration@localhost:5672/v34-cancel-{uuid4().hex}"
        ),
        build_chatflow_action_registrations(port),
    )
    with Session(v34_pg_engine) as db:
        cancel_action = db.execute(
            select(Action).where(
                Action.business_session_id == session_id,
                Action.action_kind == CHATFLOW_CANCEL_ACTION,
                Action.sequence == CHATFLOW_CANCEL_SEQUENCE,
            )
        ).scalar_one()
    assert (
        _process_chatflow_action(v34_pg_engine, action_id=cancel_action.id, processor=processor)
        == "completed"
    )

    with Session(v34_pg_engine) as db:
        recovery_action = db.execute(
            select(Action).where(
                Action.business_session_id == session_id,
                Action.action_kind == "dify.chatflow.cancel.recover.v1",
            )
        ).scalar_one()
        assert recovery_action.sequence == 16
    assert (
        _process_chatflow_action(v34_pg_engine, action_id=recovery_action.id, processor=processor)
        == "completed"
    )

    with Session(v34_pg_engine) as db:
        session = db.get(BusinessSession, session_id)
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        task_count = db.scalar(
            select(func.count(Task.id)).where(Task.business_session_id == session_id)
        )
        assert session is not None
        assert session.dify_run_id == run_id
        assert session.status == "reconciling"
        assert state.form_status == "cancelling"
        assert state.selection_status == "not_started"
        assert recovery_action.sequence == 16
    assert len(port.cancel_calls) == 1
    assert len(port.recovery_calls) == 1
    assert port.recovery_calls[0].dify_run_id == run_id
    assert task_count == 0


@pytest.mark.parametrize("workflow_status", [None, "failed"])
def test_v34_cancel_terminal_snapshot_without_durable_callback_stays_reconciling(
    v34_pg_engine: Engine,
    workflow_status: str | None,
) -> None:
    (
        client,
        _owner_id,
        _reference_set_id,
        session_id,
        recommendation_id,
        run_id,
        _form_token,
    ) = _prepare_v34_selection_ready(
        v34_pg_engine,
        idempotency_key=f"v34-cancel-snapshot-no-receipt-{uuid4().hex}",
    )
    cancel_request = client.post(
        f"/api/v1/business-sessions/{session_id}/cancel",
        headers={"Origin": TRUSTED_ORIGIN},
    )
    assert cancel_request.status_code == 202, cancel_request.text

    # Even an exact-run terminal event snapshot cannot replace the durable
    # selection.cancelled callback receipt required to close the business state.
    port = _CancelLifecyclePort(
        cancel_result=ChatflowCancelResult(
            "accepted",
            events=(
                ChatflowOriginalRunEvent(
                    event="node_finished",
                    dify_run_id=run_id,
                    node_id="selection_cancelled_callback",
                ),
                ChatflowOriginalRunEvent(
                    event="node_finished", dify_run_id=run_id, node_id="cancelled"
                ),
                ChatflowOriginalRunEvent(
                    event="workflow_finished",
                    dify_run_id=run_id,
                    workflow_status=workflow_status,
                ),
            ),
        ),
        recovery_result=ChatflowCancelResult(
            "unknown", safe_reason_code="DIFY_CANCEL_RECOVERY_REQUIRES_ROOT_EVIDENCE"
        ),
    )
    processor = ActionProcessor(
        v34_pg_engine,
        QueueOptions(
            broker_url=f"amqp://integration:integration@localhost:5672/v34-cancel-no-receipt-{uuid4().hex}"
        ),
        build_chatflow_action_registrations(port),
    )
    with Session(v34_pg_engine) as db:
        cancel_action = db.execute(
            select(Action).where(
                Action.business_session_id == session_id,
                Action.action_kind == CHATFLOW_CANCEL_ACTION,
                Action.sequence == CHATFLOW_CANCEL_SEQUENCE,
            )
        ).scalar_one()

    assert (
        _process_chatflow_action(v34_pg_engine, action_id=cancel_action.id, processor=processor)
        == "completed"
    )

    public = client.get(f"/api/v1/business-sessions/{session_id}")
    assert public.status_code == 200
    assert public.json()["status"] == "reconciling"
    with Session(v34_pg_engine) as db:
        session = db.get(BusinessSession, session_id)
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        task_count = db.scalar(
            select(func.count(Task.id)).where(Task.business_session_id == session_id)
        )
        accepted_cancel_receipts = db.scalar(
            select(func.count(ChatflowV34StageEventReceipt.id)).where(
                ChatflowV34StageEventReceipt.business_session_id == session_id,
                ChatflowV34StageEventReceipt.event_type == "selection.cancelled",
                ChatflowV34StageEventReceipt.disposition == "accepted",
            )
        )
    assert session is not None and session.status == "reconciling"
    assert state.form_status == "cancelling"
    assert state.selection_status == "not_started"
    assert task_count == 0
    assert accepted_cancel_receipts == 0


@pytest.mark.parametrize("accepted_evidence", ["selection.cancelled", "selection.applied"])
def test_v34_accepted_stage_receipt_wins_expired_claimed_action_lease(
    v34_pg_engine: Engine,
    accepted_evidence: str,
) -> None:
    (
        client,
        _owner_id,
        _reference_set_id,
        session_id,
        recommendation_id,
        run_id,
        _form_token,
    ) = _prepare_v34_selection_ready(
        v34_pg_engine,
        idempotency_key=f"v34-stage-receipt-wins-lease-{accepted_evidence}-{uuid4().hex}",
    )

    if accepted_evidence == "selection.cancelled":
        cancel_request = client.post(
            f"/api/v1/business-sessions/{session_id}/cancel",
            headers={"Origin": TRUSTED_ORIGIN},
        )
        assert cancel_request.status_code == 202, cancel_request.text
        with Session(v34_pg_engine) as db:
            action = db.execute(
                select(Action).where(
                    Action.business_session_id == session_id,
                    Action.action_kind == CHATFLOW_CANCEL_ACTION,
                    Action.sequence == CHATFLOW_CANCEL_SEQUENCE,
                )
            ).scalar_one()
        event_id = _expire_claimed_action_lease(v34_pg_engine, action.id)
        event = _v34_selection_cancelled_event(
            business_session_id=session_id,
            recommendation_id=recommendation_id,
            run_id=run_id,
            event_key=f"{run_id}:lease-race-cancel-{uuid4().hex}",
        )
        assert ChatflowV34StageEventService(v34_pg_engine).accept(event).status == "accepted"
    else:
        request = _selection_request(recommendation_id)
        selected = client.post(
            f"/api/v1/business-sessions/{session_id}/selection",
            json=request.model_dump(mode="json"),
            headers={
                "Origin": TRUSTED_ORIGIN,
                "Idempotency-Key": f"v34-stage-receipt-wins-selection-{uuid4().hex}",
            },
        )
        assert selected.status_code == 202, selected.text
        task_id = UUID(selected.json()["task_id"])
        with Session(v34_pg_engine) as db:
            recommendation = db.get(Recommendation, recommendation_id)
            action = db.execute(
                select(Action).where(
                    Action.business_session_id == session_id,
                    Action.action_kind == CHATFLOW_CONTINUE_ACTION,
                    Action.sequence == 2,
                )
            ).scalar_one()
            assert recommendation is not None
            original = DifyOriginalRecommendation.model_validate(
                recommendation.original_recommendation, strict=True
            )
            action_id = action.id
        conversion = selection_to_form_0(request, original)
        event_id = _expire_claimed_action_lease(v34_pg_engine, action_id)
        event = _v34_selection_applied_event(
            business_session_id=session_id,
            recommendation_id=recommendation_id,
            run_id=run_id,
            submission_id=action_id,
            parse_state=V34ParsedSelection(
                preferences=conversion.normalized_preferences,
                image_types=request.image_types,
            ),
            event_key=f"{run_id}:lease-race-selection-applied-{uuid4().hex}",
        )
        assert ChatflowV34StageEventService(v34_pg_engine).accept(event).status == "accepted"

    assert _sweep_expired_action_lease(v34_pg_engine, event_id) is True

    with Session(v34_pg_engine) as db:
        session = db.get(BusinessSession, session_id)
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        action = db.execute(select(Action).where(Action.outbox_event_id == event_id)).scalar_one()
        receipt = db.get(OutboxReceipt, event_id)
        assert session is not None and receipt is not None
        task = db.get(Task, task_id) if accepted_evidence == "selection.applied" else None

    assert action.status == "completed"
    assert receipt.status == "completed"
    if accepted_evidence == "selection.cancelled":
        assert session.status == "cancelled"
        assert state.form_status == "cancelled"
        assert state.selection_status == "not_started"
        assert task is None
    else:
        assert session.status == "task_created"
        assert state.selection_status == "accepted"
        assert state.selection_evidence_kind == "selection_applied"
        assert task is not None and task.status == "queued"


def test_v34_explicit_restart_after_cancel_creates_new_session_and_s019_rows(
    v34_pg_engine: Engine,
) -> None:
    (
        client,
        owner_id,
        reference_set_id,
        old_session_id,
        old_recommendation_id,
        run_id,
        _form_token,
    ) = _prepare_v34_selection_ready(
        v34_pg_engine,
        idempotency_key=f"v34-explicit-restart-old-{uuid4().hex}",
    )
    with Session(v34_pg_engine) as db, db.begin():
        now = db.scalar(select(func.clock_timestamp()))
        old_mappings = (
            db.execute(
                select(ChatflowDifyReferenceFile)
                .where(ChatflowDifyReferenceFile.business_session_id == old_session_id)
                .order_by(ChatflowDifyReferenceFile.position)
            )
            .scalars()
            .all()
        )
        assert now is not None
        for row in old_mappings:
            row.status = "uploaded"
            row.dify_file_id = f"old-run-file-{row.position}"
            row.uploaded_at = now
            row.updated_at = now
        old_mapping_ids = {row.id for row in old_mappings}
        old_positions = tuple(row.position for row in old_mappings)
        old_upload_action_ids = {row.upload_action_id for row in old_mappings}
        old_upload_actions = (
            db.execute(
                select(Action).where(
                    Action.business_session_id == old_session_id,
                    Action.action_kind == CHATFLOW_REFERENCE_UPLOAD_ACTION,
                )
            )
            .scalars()
            .all()
        )
        for action in old_upload_actions:
            action.status = "completed"
            action.last_outcome = "accepted"
            action.completed_at = now

    cancel = client.post(
        f"/api/v1/business-sessions/{old_session_id}/cancel",
        headers={"Origin": TRUSTED_ORIGIN},
    )
    assert cancel.status_code == 202, cancel.text
    cancel_event = _v34_selection_cancelled_event(
        business_session_id=old_session_id,
        recommendation_id=old_recommendation_id,
        run_id=run_id,
        event_key=f"{run_id}:cancelled-during-action-{uuid4().hex}",
    )
    port = _CancelWithCallbackPort(
        cancel_result=ChatflowCancelResult(
            "accepted",
            events=(
                ChatflowOriginalRunEvent(
                    event="node_finished",
                    dify_run_id=run_id,
                    node_id="selection_cancelled_callback",
                ),
                ChatflowOriginalRunEvent(
                    event="node_finished", dify_run_id=run_id, node_id="cancelled"
                ),
                ChatflowOriginalRunEvent(
                    event="workflow_finished",
                    dify_run_id=run_id,
                    workflow_status="succeeded",
                ),
            ),
        ),
        recovery_result=ChatflowCancelResult("unknown"),
        cancel_event=cancel_event,
        event_service=ChatflowV34StageEventService(v34_pg_engine),
    )
    processor = ActionProcessor(
        v34_pg_engine,
        QueueOptions(
            broker_url=f"amqp://integration:integration@localhost:5672/v34-restart-{uuid4().hex}"
        ),
        build_chatflow_action_registrations(port),
    )
    with Session(v34_pg_engine) as db:
        cancel_action = db.execute(
            select(Action).where(
                Action.business_session_id == old_session_id,
                Action.action_kind == CHATFLOW_CANCEL_ACTION,
                Action.sequence == CHATFLOW_CANCEL_SEQUENCE,
            )
        ).scalar_one()
    assert (
        _process_chatflow_action(v34_pg_engine, action_id=cancel_action.id, processor=processor)
        == "completed"
    )
    assert port.callback_status == "accepted"

    restarted = client.post(
        "/api/v1/business-sessions",
        json={"reference_set_id": str(reference_set_id)},
        headers={
            "Origin": TRUSTED_ORIGIN,
            "Idempotency-Key": f"v34-explicit-restart-new-{uuid4().hex}",
        },
    )
    assert restarted.status_code == 202, restarted.text
    new_session_id = UUID(restarted.json()["business_session_id"])
    new_recommendation_id = UUID(restarted.json()["recommendation_id"])
    assert new_session_id != old_session_id
    assert new_recommendation_id != old_recommendation_id

    with Session(v34_pg_engine) as db:
        old_session = db.get(BusinessSession, old_session_id)
        new_session = db.get(BusinessSession, new_session_id)
        new_mappings = (
            db.execute(
                select(ChatflowDifyReferenceFile)
                .where(ChatflowDifyReferenceFile.business_session_id == new_session_id)
                .order_by(ChatflowDifyReferenceFile.position)
            )
            .scalars()
            .all()
        )
        new_upload_actions = (
            db.execute(
                select(Action).where(
                    Action.business_session_id == new_session_id,
                    Action.action_kind == CHATFLOW_REFERENCE_UPLOAD_ACTION,
                )
            )
            .scalars()
            .all()
        )
        assert old_session is not None and new_session is not None
        assert old_session.status == "cancelled"
        assert old_session.dify_run_id == run_id
        assert new_session.dify_run_id is None
        assert new_session.start_outcome == "pending"
        assert len(new_mappings) == len(old_mappings)
        assert not old_mapping_ids.intersection({row.id for row in new_mappings})
        assert all(row.status == "pending" for row in new_mappings)
        assert all(row.dify_file_id is None and row.uploaded_at is None for row in new_mappings)
        assert tuple(row.position for row in new_mappings) == old_positions
        new_mapping_action_ids = {row.upload_action_id for row in new_mappings}
        assert new_mapping_action_ids.isdisjoint(old_upload_action_ids)
        assert len(new_upload_actions) == 1
        assert new_upload_actions[0].id == new_mappings[0].upload_action_id
        assert new_upload_actions[0].status == "scheduled"
    assert len(port.cancel_calls) == 1
    assert port.recovery_calls == []


def test_v34_selection_is_unique_and_requires_bound_acceptance_evidence(
    v34_pg_engine: Engine,
) -> None:
    (
        client,
        _owner_id,
        _reference_set_id,
        session_id,
        recommendation_id,
        run_id,
        form_token,
    ) = _prepare_v34_selection_ready(
        v34_pg_engine,
        idempotency_key=f"v34-selection-evidence-{uuid4().hex}",
    )
    request = _selection_request(recommendation_id)
    selection_key = f"v34-selection-{uuid4().hex}"
    response = client.post(
        f"/api/v1/business-sessions/{session_id}/selection",
        json=request.model_dump(mode="json"),
        headers={"Origin": TRUSTED_ORIGIN, "Idempotency-Key": selection_key},
    )
    assert response.status_code == 202, response.text
    selected = response.json()
    assert selected["status"] == "queued"

    replay = client.post(
        f"/api/v1/business-sessions/{session_id}/selection",
        json=request.model_dump(mode="json"),
        headers={"Origin": TRUSTED_ORIGIN, "Idempotency-Key": selection_key},
    )
    assert replay.status_code == 202, replay.text
    assert replay.json()["status"] == "replayed"
    assert replay.json()["task_id"] == selected["task_id"]

    changed_request = request.model_copy(update={"image_types": [IMAGE_TYPES[2]]})
    idempotency_conflict = client.post(
        f"/api/v1/business-sessions/{session_id}/selection",
        json=changed_request.model_dump(mode="json"),
        headers={"Origin": TRUSTED_ORIGIN, "Idempotency-Key": selection_key},
    )
    assert idempotency_conflict.status_code == 409
    session_conflict = client.post(
        f"/api/v1/business-sessions/{session_id}/selection",
        json=changed_request.model_dump(mode="json"),
        headers={"Origin": TRUSTED_ORIGIN, "Idempotency-Key": f"other-{uuid4().hex}"},
    )
    assert session_conflict.status_code == 409

    task_id = UUID(selected["task_id"])
    with Session(v34_pg_engine) as db:
        task = db.get(Task, task_id)
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        action = db.execute(
            select(Action).where(
                Action.business_session_id == session_id,
                Action.action_kind == "dify.chatflow.continue.v1",
            )
        ).scalar_one()
        assert task is not None
        assert task.status == "queued"
        assert action.task_id == task.id
        assert action.sequence == 2
        assert action.status == "scheduled"
        assert state.selection_status == "pending"
        assert state.selection_submission_id == action.id
        assert state.form_status == "consumed"
        assert state.selection_evidence_kind is None
        assert state.selection_inputs is not None
        assert state.selection_input_digest is not None
        assert (
            db.scalar(
                select(func.count(Action.id)).where(
                    Action.business_session_id == session_id,
                    Action.action_kind.like("generation.%"),
                )
            )
            == 0
        )
        action_id = action.id

    events = ChatflowV34StageEventService(v34_pg_engine)
    with Session(v34_pg_engine) as db:
        recommendation = db.get(Recommendation, recommendation_id)
        assert recommendation is not None
        original_recommendation = DifyOriginalRecommendation.model_validate(
            recommendation.original_recommendation, strict=True
        )
    conversion = selection_to_form_0(request, original_recommendation)
    parsed_state = V34ParsedSelection(
        preferences=conversion.normalized_preferences,
        image_types=request.image_types,
    )
    premature = _v34_selection_applied_event(
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
        submission_id=action_id,
        parse_state=parsed_state,
        event_key=f"{run_id}:parse:premature",
    )
    assert events.accept(premature).status == "rejected"

    port = _UnknownSelectionContinuationPort()
    with Session(v34_pg_engine) as db:
        pending_action = db.get(Action, action_id)
        assert pending_action is not None
        prepared_input = prepare_chatflow_continuation(db, pending_action)
        assert isinstance(prepared_input.payload["form_token"], SecretStr)
        assert form_token not in repr(prepared_input)
    processor = ActionProcessor(
        v34_pg_engine,
        QueueOptions(
            broker_url=f"amqp://integration:integration@localhost:5672/v34-selection-{uuid4().hex}"
        ),
        build_chatflow_action_registrations(port),
    )
    with Session(v34_pg_engine) as db:
        pending_action = db.get(Action, action_id)
        assert pending_action is not None
        outbox_event = db.get(OutboxEvent, pending_action.outbox_event_id)
        assert outbox_event is not None
        action_message = OutboxMessage(
            event_id=outbox_event.id,
            aggregate_id=outbox_event.aggregate_id,
            operation_version=outbox_event.operation_version,
            schema_version=outbox_event.schema_version,
            payload={key: UUID(value) for key, value in outbox_event.payload.items()},
        ).model_dump(mode="json")
    processed = processor.process_payload(action_message)
    assert processed.status == "unknown", processed.error_code
    assert len(port.calls) == 1
    continuation = port.calls[0]
    assert continuation.selection_submission_id == action_id
    assert continuation.form_node_id == "form_0"
    assert continuation.form_token == form_token
    assert tuple(continuation.form_inputs or {}) == (
        "choice_1",
        "custom_1",
        "choice_2",
        "choice_3",
        "custom_3",
        "choice_4",
        "custom_4",
        "choice_5",
        "custom_5",
        "image_types",
    )
    assert form_token not in repr(continuation)

    context_service = ChatflowV34ContextService(v34_pg_engine)
    context = context_service.get_selection_context(
        business_session_id=session_id,
        workflow_run_id=run_id,
        phase="selection",
    )
    assert context.selection_status == "unknown"
    assert context.task_id == task_id
    assert context.selection_submission_id == action_id
    assert form_token not in repr(context)

    prompt_context_service = ChatflowV34ContextService(
        v34_pg_engine,
        dify_app_id="test-v34-app",
        dify_workflow_id="test-v34-workflow-release",
    )
    prompt_package_service = ChatflowPromptPackageService(
        v34_pg_engine,
        permit_secret=SecretStr(TEST_GENERATION_PERMIT_SECRET),
        service_authority_id=TEST_SERVICE_AUTHORITY_ID,
    )
    client.app.include_router(
        create_internal_dify_router(
            engine=v34_pg_engine,
            callback_credential=SecretStr(TEST_INTERNAL_CALLBACK_SECRET),
            v34_context_service=prompt_context_service,
            prompt_package_service=prompt_package_service,
        )
    )
    context_path = f"/internal/v1/dify/business-sessions/{session_id}/context"
    pending_prompt = client.get(
        context_path,
        params={"workflow_run_id": run_id, "phase": "prompt"},
        headers={
            "Authorization": f"Bearer {TEST_INTERNAL_CALLBACK_SECRET}",
            "Content-Type": "application/json",
        },
    )
    assert pending_prompt.status_code == 409
    wrong_run_prompt = client.get(
        context_path,
        params={"workflow_run_id": f"other-{run_id}", "phase": "prompt"},
        headers={
            "Authorization": f"Bearer {TEST_INTERNAL_CALLBACK_SECRET}",
            "Content-Type": "application/json",
        },
    )
    assert wrong_run_prompt.status_code == 409

    malformed = parsed_state.model_copy(update={"image_types": [IMAGE_TYPES[2]]})
    wrong_parse = _v34_selection_applied_event(
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
        submission_id=action_id,
        parse_state=malformed,
        event_key=f"{run_id}:parse:wrong-state",
    )
    assert events.accept(wrong_parse).status == "rejected"

    applied = _v34_selection_applied_event(
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
        submission_id=action_id,
        parse_state=parsed_state,
        event_key=f"{run_id}:parse:applied",
    )
    applied_result = events.accept(applied)
    if applied_result.status != "accepted":
        with Session(v34_pg_engine) as db:
            receipt = db.execute(
                select(ChatflowV34StageEventReceipt).where(
                    ChatflowV34StageEventReceipt.business_session_id == session_id,
                    ChatflowV34StageEventReceipt.event_key == applied.event_key,
                )
            ).scalar_one()
            failure_reason = receipt.safe_reason_code
    else:
        failure_reason = None
    assert applied_result.status == "accepted", failure_reason
    accepted_context = context_service.get_selection_context(
        business_session_id=session_id,
        workflow_run_id=run_id,
        phase="selection",
    )
    assert accepted_context.selection_status == "accepted"

    prompt_response = client.get(
        context_path,
        params={"workflow_run_id": run_id, "phase": "prompt"},
        headers={
            "Authorization": f"Bearer {TEST_INTERNAL_CALLBACK_SECRET}",
            "Content-Type": "application/json",
        },
    )
    assert prompt_response.status_code == 200, prompt_response.text
    assert prompt_response.headers["cache-control"] == "no-store"
    prompt_context = prompt_response.json()
    assert prompt_context["phase"] == "prompt"
    assert prompt_context["selection_status"] == "accepted"
    assert prompt_context["selection_evidence_kind"] == "selection_applied"
    assert prompt_context["task_id"] == str(task_id)
    assert prompt_context["selection_submission_id"] == str(action_id)
    assert prompt_context["prompt_submission_id"] == str(prompt_package_submission_id(action_id))
    assert [item["ref_id"] for item in prompt_context["references"]] == [
        "PRIMARY",
        "AUX01",
        "AUX02",
    ]
    assert [item["order"] for item in prompt_context["references"]] == [1, 2, 3]
    assert form_token not in prompt_response.text

    with Session(v34_pg_engine) as db:
        recommendation = db.get(Recommendation, recommendation_id)
        assert recommendation is not None
        callback = SimpleNamespace(
            recommendation_id=recommendation_id,
            recommendation_revision=recommendation.revision,
            dify_run_id=run_id,
        )
    generic_submission = _submission_for_task(
        v34_pg_engine,
        task_id=task_id,
        session_id=session_id,
        callback=callback,
    )
    submission_values = generic_submission.model_dump(mode="python")
    submission_values["submission_id"] = prompt_package_submission_id(action_id)
    submission = ChatflowPromptPackageSubmission.model_validate(submission_values, strict=True)
    h4_first = _post_submission(client, submission)
    assert h4_first.status_code == 200, h4_first.text
    assert h4_first.json()["status"] == "accepted"
    h4_replayed = _post_submission(client, submission)
    assert h4_replayed.status_code == 200, h4_replayed.text
    assert h4_replayed.json()["status"] == "replayed"
    assert h4_replayed.json()["generation_request_id"] == h4_first.json()["generation_request_id"]
    assert h4_replayed.json()["slots"] == h4_first.json()["slots"]
    assert h4_replayed.json()["permit"] == h4_first.json()["permit"]
    assert h4_first.json()["permit"]["permit_version"] == 2

    with Session(v34_pg_engine) as db, db.begin():
        action = db.get(Action, action_id)
        assert action is not None
        action_input = prepare_chatflow_continuation(db, action)
        finalize_chatflow_continuation(
            db,
            action,
            action_input,
            ActionExecutionResult("unknown", safe_reason_code="DIFY_SELECTION_OUTCOME_UNKNOWN"),
        )
    with Session(v34_pg_engine) as db:
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        assert state.selection_status == "accepted"
        assert state.selection_evidence_kind == "selection_applied"
        assert state.selection_accepted_at is not None


def test_v34_early_selection_callback_survives_unknown_action_finalization(
    v34_pg_engine: Engine,
) -> None:
    (
        client,
        _owner_id,
        _reference_set_id,
        session_id,
        recommendation_id,
        run_id,
        _form_token,
    ) = _prepare_v34_selection_ready(
        v34_pg_engine,
        idempotency_key=f"v34-selection-early-callback-{uuid4().hex}",
    )
    request = _selection_request(recommendation_id)
    response = client.post(
        f"/api/v1/business-sessions/{session_id}/selection",
        json=request.model_dump(mode="json"),
        headers={
            "Origin": TRUSTED_ORIGIN,
            "Idempotency-Key": f"v34-selection-early-{uuid4().hex}",
        },
    )
    assert response.status_code == 202, response.text
    task_id = UUID(response.json()["task_id"])
    with Session(v34_pg_engine) as db:
        recommendation = db.get(Recommendation, recommendation_id)
        action = db.execute(
            select(Action).where(
                Action.business_session_id == session_id,
                Action.action_kind == "dify.chatflow.continue.v1",
            )
        ).scalar_one()
        assert recommendation is not None
        original = DifyOriginalRecommendation.model_validate(
            recommendation.original_recommendation, strict=True
        )
        action_id = action.id
    conversion = selection_to_form_0(request, original)
    event = _v34_selection_applied_event(
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
        submission_id=action_id,
        parse_state=V34ParsedSelection(
            preferences=conversion.normalized_preferences,
            image_types=request.image_types,
        ),
        event_key=f"{run_id}:parse:early-applied",
    )
    port = _EarlySelectionAppliedPort(event, ChatflowV34StageEventService(v34_pg_engine))
    processor = ActionProcessor(
        v34_pg_engine,
        QueueOptions(
            broker_url=f"amqp://integration:integration@localhost:5672/v34-selection-early-{uuid4().hex}"
        ),
        build_chatflow_action_registrations(port),
    )
    assert (
        _process_chatflow_action(v34_pg_engine, action_id=action_id, processor=processor)
        == "completed"
    )
    assert port.callback_status == "accepted"
    with Session(v34_pg_engine) as db:
        task = db.get(Task, task_id)
        session = db.get(BusinessSession, session_id)
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        action = db.get(Action, action_id)
        assert task is not None and session is not None and action is not None
        assert task.status == "queued"
        assert session.status == "task_created"
        assert state.selection_status == "accepted"
        assert state.selection_evidence_kind == "selection_applied"
        assert action.status == "completed"


def test_v34_late_accepted_continuation_does_not_reopen_pre_generation_failure(
    v34_pg_engine: Engine,
) -> None:
    (
        client,
        _owner_id,
        _reference_set_id,
        session_id,
        recommendation_id,
        run_id,
        _form_token,
    ) = _prepare_v34_selection_ready(
        v34_pg_engine,
        idempotency_key=f"v34-late-accepted-after-incomplete-{uuid4().hex}",
    )
    request = _selection_request(recommendation_id)
    response = client.post(
        f"/api/v1/business-sessions/{session_id}/selection",
        json=request.model_dump(mode="json"),
        headers={
            "Origin": TRUSTED_ORIGIN,
            "Idempotency-Key": f"v34-late-accepted-{uuid4().hex}",
        },
    )
    assert response.status_code == 202, response.text
    task_id = UUID(response.json()["task_id"])

    with Session(v34_pg_engine) as db:
        recommendation = db.get(Recommendation, recommendation_id)
        action = db.execute(
            select(Action).where(
                Action.business_session_id == session_id,
                Action.action_kind == CHATFLOW_CONTINUE_ACTION,
                Action.sequence == 2,
            )
        ).scalar_one()
        assert recommendation is not None
        original = DifyOriginalRecommendation.model_validate(
            recommendation.original_recommendation, strict=True
        )
        action_id = action.id

    conversion = selection_to_form_0(request, original)
    selection_applied = _v34_selection_applied_event(
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
        submission_id=action_id,
        parse_state=V34ParsedSelection(
            preferences=conversion.normalized_preferences,
            image_types=request.image_types,
        ),
        event_key=f"{run_id}:parse:before-late-accepted-{uuid4().hex}",
    )
    incomplete = _v34_incomplete_event(
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
        event_key=f"{run_id}:ready:before-late-accepted-{uuid4().hex}",
        conflicting_field_ids=["3"],
    )
    port = _TerminalThenAcceptedContinuationPort(
        (selection_applied, incomplete), ChatflowV34StageEventService(v34_pg_engine)
    )
    processor = ActionProcessor(
        v34_pg_engine,
        QueueOptions(
            broker_url=f"amqp://integration:integration@localhost:5672/v34-late-accepted-{uuid4().hex}"
        ),
        build_chatflow_action_registrations(port),
    )

    assert (
        _process_chatflow_action(v34_pg_engine, action_id=action_id, processor=processor)
        == "completed"
    )
    assert port.callback_statuses == ["accepted", "accepted"]
    assert len(port.calls) == 1

    with Session(v34_pg_engine) as db:
        session = db.get(BusinessSession, session_id)
        recommendation = db.get(Recommendation, recommendation_id)
        task = db.get(Task, task_id)
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        action = db.get(Action, action_id)
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
        terminal_receipts = (
            db.execute(
                select(ChatflowV34StageEventReceipt.event_type).where(
                    ChatflowV34StageEventReceipt.business_session_id == session_id,
                    ChatflowV34StageEventReceipt.disposition == "accepted",
                )
            )
            .scalars()
            .all()
        )

    assert session is not None and recommendation is not None and task is not None
    assert action is not None
    assert session.status == "failed"
    assert recommendation.status == "failed"
    assert recommendation.failure_code == "PRODUCT_IDENTITY_CONFLICT"
    assert task.status == "failed"
    assert task.terminal_at is not None
    assert all(item.status == "failed" for item in items)
    assert all(item.error_code == "V34_PRE_GENERATION_FAILED" for item in items)
    assert state.selection_status == "accepted"
    assert state.safe_reason_code == "PRODUCT_IDENTITY_CONFLICT"
    assert action.status == "completed"
    assert action.last_outcome == "accepted"
    assert generation_request_count == 0
    assert terminal_receipts.count("selection.applied") == 1
    assert terminal_receipts.count("pre_generation.incomplete") == 1


class _EarlyStageCallbackStartPort:
    def __init__(
        self,
        event_service: ChatflowV34StageEventService,
        events: tuple[ChatflowV34StageEvent, ...],
        *,
        run_id: str,
    ) -> None:
        self.event_service = event_service
        self.events = events
        self.run_id = run_id
        self.start_calls: list[ChatflowStartCommand] = []
        self.callback_statuses: list[str] = []

    def start_recommendation(self, command: ChatflowStartCommand) -> ChatflowStartResult:
        self.start_calls.append(command)
        self.callback_statuses.extend(
            self.event_service.accept(event).status for event in self.events
        )
        return ChatflowStartResult(
            "unknown",
            dify_run_id=self.run_id,
            safe_reason_code="DIFY_START_OUTCOME_UNKNOWN",
        )

    def upload_reference(self, _command: DifyReferenceUploadCommand) -> DifyReferenceUploadResult:
        raise AssertionError("reference uploads are not part of this start reconciliation test")

    def continue_after_selection(
        self, _command: ChatflowContinuationCommand
    ) -> ChatflowContinuationResult:
        raise AssertionError("continuation is not part of this start reconciliation test")

    def recover_after_selection(
        self, _command: ChatflowContinuationCommand
    ) -> ChatflowContinuationResult:
        raise AssertionError("recovery is not part of this start reconciliation test")

    def submit_human_input(self, _command: ChatflowHumanInputCommand) -> ChatflowHumanInputResult:
        raise AssertionError("human input is not part of this start reconciliation test")

    def recover_human_input(self, _command: ChatflowHumanInputCommand) -> ChatflowHumanInputResult:
        raise AssertionError("human input recovery is not part of this start reconciliation test")


class _FirstFormBindingStartPort(_EarlyStageCallbackStartPort):
    def __init__(
        self,
        *,
        run_id: str,
        form_definition: ChatflowFirstFormDefinitionResult,
        first_form: ChatflowFirstFormBinding | None = None,
    ) -> None:
        self.run_id = run_id
        self.form_definition = form_definition
        self.first_form = first_form or ChatflowFirstFormBinding(
            form_node_id="form_0",
            form_id="ab1f4152-bdd4-4ea1-8133-535051ae2d80",
            form_token="private-v34-form-token",
        )
        self.start_calls: list[ChatflowStartCommand] = []
        self.form_binding_calls: list[ChatflowFirstFormDefinitionCommand] = []

    def start_recommendation(self, command: ChatflowStartCommand) -> ChatflowStartResult:
        self.start_calls.append(command)
        return ChatflowStartResult(
            "started",
            dify_run_id=self.run_id,
            first_form=self.first_form,
        )

    def get_first_form_definition(
        self, command: ChatflowFirstFormDefinitionCommand
    ) -> ChatflowFirstFormDefinitionResult:
        self.form_binding_calls.append(command)
        return self.form_definition


def test_v34_session_persists_version_private_state_and_omits_legacy_question(
    v34_pg_engine: Engine,
) -> None:
    idempotency_key = f"v34-session-{uuid4().hex}"
    client, owner_id, _reference_set_id, created = _create_session(
        v34_pg_engine,
        flow_version=V34_FLOW_VERSION,
        idempotency_key=idempotency_key,
    )
    session_id = UUID(created["business_session_id"])
    recommendation_id = UUID(created["recommendation_id"])
    assert created["flow_version"] == V34_FLOW_VERSION

    replay = client.post(
        "/api/v1/business-sessions",
        json={"reference_set_id": str(_reference_set_id)},
        headers={"Origin": TRUSTED_ORIGIN, "Idempotency-Key": idempotency_key},
    )
    assert replay.status_code == 202
    assert replay.json()["status"] == "replayed"
    assert replay.json()["business_session_id"] == str(session_id)
    assert replay.json()["recommendation_id"] == str(recommendation_id)

    with Session(v34_pg_engine) as db:
        session = db.get(BusinessSession, session_id)
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        state_count = db.scalar(
            select(func.count(ChatflowV34SessionState.id)).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        )
    assert session is not None
    assert session.flow_version == V34_FLOW_VERSION
    assert state.flow_version == V34_FLOW_VERSION
    assert state.dify_app_id == "test-v34-app"
    assert state.dify_workflow_id == "test-v34-workflow-release"
    assert state.form_status == "awaiting_event"
    assert state.selection_status == "not_started"
    assert state_count == 1

    public = client.get(f"/api/v1/business-sessions/{session_id}")
    assert public.status_code == 200
    assert "question" not in public.json()
    assert public.json()["selection_ready"] is False

    legacy_client, legacy_owner_id, _legacy_ref_id, legacy_created = _create_session(
        v34_pg_engine,
        flow_version="legacy",
        idempotency_key=f"legacy-session-{uuid4().hex}",
    )
    legacy_id = UUID(legacy_created["business_session_id"])
    with Session(v34_pg_engine) as db:
        legacy = db.get(BusinessSession, legacy_id)
        legacy_state_count = db.scalar(
            select(func.count(ChatflowV34SessionState.id)).where(
                ChatflowV34SessionState.business_session_id == legacy_id
            )
        )
    assert legacy is not None
    assert legacy.owner_user_id == legacy_owner_id
    assert legacy.flow_version == "legacy"
    assert legacy_state_count == 0
    legacy_public = legacy_client.get(f"/api/v1/business-sessions/{legacy_id}")
    assert legacy_public.status_code == 200
    assert legacy_public.json()["question"] is None
    assert legacy_public.json().get("flow_version") is None


def test_business_session_flow_version_is_database_immutable(v34_pg_engine: Engine) -> None:
    for original, replacement in (
        (V34_FLOW_VERSION, "legacy"),
        ("legacy", V34_FLOW_VERSION),
    ):
        _client, _owner_id, _reference_set_id, created = _create_session(
            v34_pg_engine,
            flow_version=original,
            idempotency_key=f"flow-version-immutable-{uuid4().hex}",
        )
        session_id = UUID(created["business_session_id"])
        with v34_pg_engine.connect() as connection:
            transaction = connection.begin()
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "UPDATE business_sessions SET flow_version = :replacement "
                        "WHERE id = :session_id"
                    ),
                    {"replacement": replacement, "session_id": session_id},
                )
            transaction.rollback()

        with Session(v34_pg_engine) as db:
            saved = db.get(BusinessSession, session_id)
        assert saved is not None
        assert saved.flow_version == original


def test_v34_legacy_selection_h3_answer_and_callbacks_fail_closed_without_task(
    v34_pg_engine: Engine,
) -> None:
    client, owner_id, _reference_set_id, created = _create_session(
        v34_pg_engine,
        flow_version=V34_FLOW_VERSION,
        idempotency_key=f"v34-reject-{uuid4().hex}",
    )
    client.app.state.business_question_service = (
        client.app.state.business_session_service.question_service
    )
    session_id = UUID(created["business_session_id"])
    recommendation_id = UUID(created["recommendation_id"])

    selected = client.post(
        f"/api/v1/business-sessions/{session_id}/selection",
        json=_selection_request(recommendation_id).model_dump(mode="json"),
        headers={"Origin": TRUSTED_ORIGIN, "Idempotency-Key": f"v34-select-{uuid4().hex}"},
    )
    assert selected.status_code == 409
    assert selected.json()["code"] == "V34_SELECTION_NOT_READY"

    answer = client.post(
        f"/api/v1/business-sessions/{session_id}/questions/{uuid4()}/answers",
        json={"answers": {"color": "blue"}},
        headers={"Origin": TRUSTED_ORIGIN, "Idempotency-Key": f"v34-answer-{uuid4().hex}"},
    )
    assert answer.status_code == 409
    assert answer.json()["code"] == "V34_H3_EVENT_REJECTED"

    for event_type in ("recommendation.ready", "human_input_required"):
        event = _legacy_callback(
            business_session_id=session_id,
            recommendation_id=recommendation_id,
            event_type=event_type,
        )
        callback = client.post(
            "/internal/v1/dify/business-events",
            json=event.model_dump(mode="json"),
            headers={"Authorization": f"Bearer {TEST_INTERNAL_CALLBACK_SECRET}"},
        )
        assert callback.status_code == 409
        assert callback.json()["code"] == "V34_LEGACY_CALLBACK_REJECTED"

    with Session(v34_pg_engine) as db:
        task_count = db.scalar(
            select(func.count(Task.id)).where(Task.business_session_id == session_id)
        )
        continuation_count = db.scalar(
            select(func.count(Action.id)).where(
                Action.business_session_id == session_id,
                Action.action_kind == CHATFLOW_CONTINUE_ACTION,
            )
        )
        owner_count = db.scalar(
            select(func.count())
            .select_from(BusinessSession)
            .where(
                BusinessSession.id == session_id,
                BusinessSession.owner_user_id == owner_id,
            )
        )
    assert owner_count == 1
    assert task_count == 0
    assert continuation_count == 0


def test_v34_selection_ready_requires_ready_recommendation_and_live_form_zero(
    v34_pg_engine: Engine,
) -> None:
    client, _owner_id, _reference_set_id, created = _create_session(
        v34_pg_engine,
        flow_version=V34_FLOW_VERSION,
        idempotency_key=f"v34-readiness-{uuid4().hex}",
    )
    session_id = UUID(created["business_session_id"])
    options = build_mock_options()
    field_texts = {
        name: [choice["text"] for choice in options[name]["choices"]]
        for name in ("language", "usage_scene", "visual_style", "selling_points")
    }
    token = "private-v34-form-token"

    with Session(v34_pg_engine) as db, db.begin():
        now = db.scalar(select(func.clock_timestamp()))
        assert now is not None
        session = db.get(BusinessSession, session_id)
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        recommendation = db.execute(
            select(Recommendation).where(Recommendation.business_session_id == session_id)
        ).scalar_one()
        assert session is not None
        session.status = "recommendation_ready"
        state.form_status = "ready"
        state.form_node_id = "form_0"
        state.form_token = token
        state.form_token_digest = hashlib.sha256(token.encode()).digest()
        state.form_definition_digest = hashlib.sha256(b"form-0-definition").digest()
        state.form_expires_at = now + timedelta(hours=1)

    # A ready form alone is insufficient while its recommendation is not ready.
    pending = client.get(f"/api/v1/business-sessions/{session_id}")
    assert pending.status_code == 200
    assert pending.json()["selection_ready"] is False

    with Session(v34_pg_engine) as db, db.begin():
        now = db.scalar(select(func.clock_timestamp()))
        recommendation = db.execute(
            select(Recommendation).where(Recommendation.business_session_id == session_id)
        ).scalar_one()
        assert now is not None
        recommendation.status = "ready"
        recommendation.original_recommendation = {
            "category_snapshot": "测试品类",
            "field_texts": field_texts,
            "image_types": list(IMAGE_TYPES),
        }
        recommendation.public_options = options
        recommendation.image_types = list(IMAGE_TYPES)
        recommendation.ready_at = now
        recommendation.valid_until = now + timedelta(hours=1)

    ready = client.get(f"/api/v1/business-sessions/{session_id}")
    assert ready.status_code == 200
    assert ready.json()["selection_ready"] is True
    assert token not in ready.text
    assert "question" not in ready.json()

    with Session(v34_pg_engine) as db, db.begin():
        now = db.scalar(select(func.clock_timestamp()))
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        assert now is not None
        state.form_expires_at = now - timedelta(seconds=1)

    expired_form = client.get(f"/api/v1/business-sessions/{session_id}")
    assert expired_form.status_code == 200
    assert expired_form.json()["selection_ready"] is False


def test_v34_start_persists_run_before_form_definition_get(v34_pg_engine: Engine) -> None:
    client, _owner_id, _reference_set_id, created = _create_session(
        v34_pg_engine,
        flow_version=V34_FLOW_VERSION,
        idempotency_key=f"v34-form-binding-{uuid4().hex}",
    )
    session_id = UUID(created["business_session_id"])
    recommendation_id = UUID(created["recommendation_id"])
    run_id = f"v34-form-binding-run-{uuid4().hex}"
    expected_options = {
        "choice_1": ["A|||语言 A", "B|||语言 B", "C|||语言 C"],
        "choice_2": [f"PLATFORM|||{platform}" for platform in PLATFORMS],
        "choice_3": ["A|||场景 A", "B|||场景 B", "C|||场景 C"],
        "choice_4": ["A|||风格 A", "B|||风格 B", "C|||风格 C"],
        "choice_5": ["A|||卖点 A", "B|||卖点 B", "C|||卖点 C"],
    }
    custom_options = ("D|||由视觉审核 Agent 自由设计", "custom|||自定义")
    fields = tuple(
        ChatflowFirstFormField(
            output_variable_name=name,
            input_type=input_type,
            option_values=tuple(
                expected_options[name] + list(custom_options)
                if name in {"choice_1", "choice_3", "choice_4", "choice_5"}
                else expected_options[name]
                if name == "choice_2"
                else []
            ),
        )
        for name, input_type in (
            ("choice_1", "select"),
            ("custom_1", "paragraph"),
            ("choice_2", "select"),
            ("choice_3", "select"),
            ("custom_3", "paragraph"),
            ("choice_4", "select"),
            ("custom_4", "paragraph"),
            ("choice_5", "select"),
            ("custom_5", "paragraph"),
            ("image_types", "paragraph"),
        )
    )
    with Session(v34_pg_engine) as db:
        now = db.scalar(select(func.clock_timestamp()))
    assert now is not None
    port = _FirstFormBindingStartPort(
        run_id=run_id,
        form_definition=ChatflowFirstFormDefinitionResult(
            "available",
            fields=fields,
            action_ids=("submit", "cancel"),
            expiration_time=int(now.timestamp()) + 3600,
        ),
    )

    with Session(v34_pg_engine) as db, db.begin():
        session = db.get(BusinessSession, session_id)
        assert session is not None
        mappings = (
            db.execute(
                select(ChatflowDifyReferenceFile)
                .where(ChatflowDifyReferenceFile.business_session_id == session_id)
                .order_by(ChatflowDifyReferenceFile.position)
            )
            .scalars()
            .all()
        )
        for row in mappings:
            row.status = "uploaded"
            row.dify_file_id = f"uploaded-v34-bind-{row.position}"
            row.uploaded_at = now
            row.updated_at = now
        bound_digest = hashlib.sha256(
            session.request_digest + session.reference_asset_digest
        ).digest()
        schedule_action_in_transaction(
            db,
            action_id=chatflow_action_id(
                business_session_id=session_id,
                action_kind=CHATFLOW_START_ACTION,
            ),
            task_id=None,
            scope_kind="business_session",
            scope_id=session_id,
            business_session_id=session_id,
            action_kind=CHATFLOW_START_ACTION,
            sequence=CHATFLOW_START_SEQUENCE,
            due_at=now,
            input_digest_version=1,
            input_digest=chatflow_action_digest(
                action_kind=CHATFLOW_START_ACTION,
                business_session_id=session_id,
                task_id=None,
                request_digest=bound_digest,
            ),
        )

    processor = ActionProcessor(
        v34_pg_engine,
        QueueOptions(broker_url="amqp://integration:integration@localhost:5672/v34-form-bind"),
        build_chatflow_action_registrations(port),
    )
    with Session(v34_pg_engine) as db:
        start_action_id = db.execute(
            select(Action.id).where(
                Action.business_session_id == session_id,
                Action.action_kind == CHATFLOW_START_ACTION,
            )
        ).scalar_one()
    assert _process_chatflow_action(
        v34_pg_engine, action_id=start_action_id, processor=processor
    ) == ("completed")
    assert len(port.start_calls) == 1
    assert port.form_binding_calls == []

    with Session(v34_pg_engine) as db:
        session = db.get(BusinessSession, session_id)
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        form_action = db.execute(
            select(Action).where(
                Action.business_session_id == session_id,
                Action.action_kind == CHATFLOW_FORM_BIND_ACTION,
            )
        ).scalar_one()
        assert session is not None
        assert session.dify_run_id == run_id
        assert session.start_outcome == "accepted"
        assert state.form_status == "binding"
        assert state.form_token == "private-v34-form-token"
        assert state.form_definition_digest is None
        assert form_action.status == "scheduled"
        form_action_id = form_action.id

    public_while_binding = client.get(f"/api/v1/business-sessions/{session_id}")
    assert public_while_binding.status_code == 200
    assert public_while_binding.json()["selection_ready"] is False
    assert "private-v34-form-token" not in public_while_binding.text

    assert _process_chatflow_action(
        v34_pg_engine, action_id=form_action_id, processor=processor
    ) == ("completed")
    assert port.form_binding_calls == [
        ChatflowFirstFormDefinitionCommand(
            business_session_id=session_id,
            recommendation_id=recommendation_id,
            recommendation_revision=1,
            dify_run_id=run_id,
            dify_user_id=f"bs-{session_id.hex}",
            form_node_id="form_0",
            form_token="private-v34-form-token",
        )
    ]

    event = _v34_recommendation_ready_event(
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
        event_key=f"{run_id}:prepare:ready",
    )
    assert ChatflowV34StageEventService(v34_pg_engine).accept(event).status == "accepted"
    public_ready = client.get(f"/api/v1/business-sessions/{session_id}")
    assert public_ready.status_code == 200
    assert public_ready.json()["selection_ready"] is True
    assert "private-v34-form-token" not in public_ready.text


def test_v34_first_form_identity_conflict_fails_recommendation_and_drains_pending_event(
    v34_pg_engine: Engine,
) -> None:
    client, _owner_id, _reference_set_id, created = _create_session(
        v34_pg_engine,
        flow_version=V34_FLOW_VERSION,
        idempotency_key=f"v34-form-identity-conflict-{uuid4().hex}",
    )
    session_id = UUID(created["business_session_id"])
    recommendation_id = UUID(created["recommendation_id"])
    run_id = f"v34-form-identity-conflict-run-{uuid4().hex}"
    event = _v34_recommendation_ready_event(
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=run_id,
        event_key=f"{run_id}:prepare:ready",
    )
    event_service = ChatflowV34StageEventService(v34_pg_engine)
    assert event_service.accept(event).status == "received_pending"
    with Session(v34_pg_engine) as db:
        now = db.scalar(select(func.clock_timestamp()))
    assert now is not None
    port = _FirstFormBindingStartPort(
        run_id=run_id,
        form_definition=ChatflowFirstFormDefinitionResult("available"),
        first_form=ChatflowFirstFormBinding(
            form_node_id="form_0",
            form_id="not-a-uuid",
            form_token="private-v34-invalid-form-token",
        ),
    )

    with Session(v34_pg_engine) as db, db.begin():
        session = db.get(BusinessSession, session_id)
        assert session is not None
        mappings = (
            db.execute(
                select(ChatflowDifyReferenceFile)
                .where(ChatflowDifyReferenceFile.business_session_id == session_id)
                .order_by(ChatflowDifyReferenceFile.position)
            )
            .scalars()
            .all()
        )
        for row in mappings:
            row.status = "uploaded"
            row.dify_file_id = f"uploaded-v34-conflict-{row.position}"
            row.uploaded_at = now
            row.updated_at = now
        bound_digest = hashlib.sha256(
            session.request_digest + session.reference_asset_digest
        ).digest()
        schedule_action_in_transaction(
            db,
            action_id=chatflow_action_id(
                business_session_id=session_id,
                action_kind=CHATFLOW_START_ACTION,
            ),
            task_id=None,
            scope_kind="business_session",
            scope_id=session_id,
            business_session_id=session_id,
            action_kind=CHATFLOW_START_ACTION,
            sequence=CHATFLOW_START_SEQUENCE,
            due_at=now,
            input_digest_version=1,
            input_digest=chatflow_action_digest(
                action_kind=CHATFLOW_START_ACTION,
                business_session_id=session_id,
                task_id=None,
                request_digest=bound_digest,
            ),
        )

    processor = ActionProcessor(
        v34_pg_engine,
        QueueOptions(broker_url="amqp://integration:integration@localhost:5672/v34-form-conflict"),
        build_chatflow_action_registrations(port),
    )
    with Session(v34_pg_engine) as db:
        start_action_id = db.execute(
            select(Action.id).where(
                Action.business_session_id == session_id,
                Action.action_kind == CHATFLOW_START_ACTION,
            )
        ).scalar_one()
    assert (
        _process_chatflow_action(v34_pg_engine, action_id=start_action_id, processor=processor)
        == "completed"
    )

    public_failed = client.get(f"/api/v1/business-sessions/{session_id}")
    assert public_failed.status_code == 200
    assert public_failed.json()["status"] == "failed"
    assert public_failed.json()["phase"] == "failed"
    assert public_failed.json()["selection_ready"] is False
    assert public_failed.json()["safe_reason_code"] == "V34_FIRST_FORM_IDENTITY_MISMATCH"
    with Session(v34_pg_engine) as db:
        recommendation = db.get(Recommendation, recommendation_id)
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        receipt = db.execute(
            select(ChatflowV34StageEventReceipt).where(
                ChatflowV34StageEventReceipt.business_session_id == session_id
            )
        ).scalar_one()
        assert recommendation is not None
        assert recommendation.status == "failed"
        assert recommendation.failure_code == "V34_FIRST_FORM_IDENTITY_MISMATCH"
        assert state.form_status == "rejected"
        assert state.safe_reason_code == "V34_FIRST_FORM_IDENTITY_MISMATCH"
        assert receipt.disposition == "rejected"
        assert receipt.safe_reason_code == "V34_RECOMMENDATION_ALREADY_ACCEPTED"
        assert (
            db.execute(
                select(Action.id).where(
                    Action.business_session_id == session_id,
                    Action.action_kind == CHATFLOW_FORM_BIND_ACTION,
                )
            ).first()
            is None
        )


def test_v34_unknown_start_binds_run_and_drains_early_matching_recommendation_callback(
    v34_pg_engine: Engine,
) -> None:
    _client, _owner_id, _reference_set_id, created = _create_session(
        v34_pg_engine,
        flow_version=V34_FLOW_VERSION,
        idempotency_key=f"v34-early-callback-{uuid4().hex}",
    )
    session_id = UUID(created["business_session_id"])
    recommendation_id = UUID(created["recommendation_id"])
    original_run_id = "observed-v34-run-001"
    mismatched_run_id = "observed-v34-run-other"

    matching_event = _v34_recommendation_ready_event(
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=original_run_id,
        event_key=f"{original_run_id}:prepare:ready",
    )
    mismatched_event = _v34_recommendation_ready_event(
        business_session_id=session_id,
        recommendation_id=recommendation_id,
        run_id=mismatched_run_id,
        event_key=f"{mismatched_run_id}:prepare:ready",
    )

    with Session(v34_pg_engine) as db, db.begin():
        now = db.scalar(select(func.clock_timestamp()))
        assert now is not None
        session = db.get(BusinessSession, session_id)
        assert session is not None
        mappings = (
            db.execute(
                select(ChatflowDifyReferenceFile)
                .where(ChatflowDifyReferenceFile.business_session_id == session_id)
                .order_by(ChatflowDifyReferenceFile.position)
            )
            .scalars()
            .all()
        )
        assert mappings
        for row in mappings:
            row.status = "uploaded"
            row.dify_file_id = f"uploaded-v34-{row.position}"
            row.uploaded_at = now
            row.updated_at = now

        bound_digest = hashlib.sha256(
            session.request_digest + session.reference_asset_digest
        ).digest()
        schedule_action_in_transaction(
            db,
            action_id=chatflow_action_id(
                business_session_id=session_id,
                action_kind=CHATFLOW_START_ACTION,
            ),
            task_id=None,
            scope_kind="business_session",
            scope_id=session_id,
            business_session_id=session_id,
            action_kind=CHATFLOW_START_ACTION,
            sequence=CHATFLOW_START_SEQUENCE,
            due_at=now,
            input_digest_version=1,
            input_digest=chatflow_action_digest(
                action_kind=CHATFLOW_START_ACTION,
                business_session_id=session_id,
                task_id=None,
                request_digest=bound_digest,
            ),
        )

    stage_event_service = ChatflowV34StageEventService(v34_pg_engine)
    start_port = _EarlyStageCallbackStartPort(
        stage_event_service,
        (matching_event, mismatched_event),
        run_id=original_run_id,
    )
    processor = ActionProcessor(
        v34_pg_engine,
        QueueOptions(broker_url="amqp://integration:integration@localhost:5672/v34-early"),
        build_chatflow_action_registrations(start_port),
    )
    with Session(v34_pg_engine) as db:
        start_action = db.execute(
            select(Action).where(
                Action.business_session_id == session_id,
                Action.action_kind == CHATFLOW_START_ACTION,
            )
        ).scalar_one()
        start_action_id = start_action.id

    assert (
        _process_chatflow_action(
            v34_pg_engine,
            action_id=start_action_id,
            processor=processor,
        )
        == "unknown"
    )
    assert start_port.callback_statuses == ["received_pending", "received_pending"]
    assert len(start_port.start_calls) == 1

    assert stage_event_service.accept(matching_event).status == "replayed"
    conflicting_event = matching_event.model_copy(
        update={
            "payload": {"audit": str(matching_event.payload["audit"]).replace("语言 A", "语言 X")}
        }
    )
    conflicting_event = conflicting_event.model_copy(
        update={"digest": chatflow_v34_stage_event_digest(conflicting_event).hex()}
    )
    with pytest.raises(DomainError) as conflict:
        stage_event_service.accept(conflicting_event)
    assert conflict.value.code == "EVENT_CONTENT_CONFLICT"

    with Session(v34_pg_engine) as db:
        session = db.get(BusinessSession, session_id)
        recommendation = db.get(Recommendation, recommendation_id)
        state = db.execute(
            select(ChatflowV34SessionState).where(
                ChatflowV34SessionState.business_session_id == session_id
            )
        ).scalar_one()
        receipts = (
            db.execute(
                select(ChatflowV34StageEventReceipt).where(
                    ChatflowV34StageEventReceipt.business_session_id == session_id
                )
            )
            .scalars()
            .all()
        )
        start_action_count = db.scalar(
            select(func.count(Action.id)).where(
                Action.business_session_id == session_id,
                Action.action_kind == CHATFLOW_START_ACTION,
            )
        )

    assert session is not None
    assert recommendation is not None
    assert session.dify_run_id == original_run_id
    assert recommendation.dify_run_id == original_run_id
    assert session.start_outcome == "unknown"
    assert session.status == "reconciling", {
        "session_status": session.status,
        "recommendation_status": recommendation.status,
        "safe_reason_code": state.safe_reason_code,
        "receipts": [
            (receipt.event_type, receipt.disposition, receipt.safe_reason_code)
            for receipt in receipts
        ],
    }
    assert recommendation.status == "ready"
    assert state.form_status == "unknown"
    assert state.form_token is None
    receipt_states = {
        receipt.event_key: (receipt.disposition, receipt.safe_reason_code) for receipt in receipts
    }
    assert receipt_states[matching_event.event_key] == ("accepted", None)
    assert receipt_states[mismatched_event.event_key] == (
        "rejected",
        "DIFY_RUN_ID_MISMATCH",
    )
    assert start_action_count == 1

    # Re-delivery of the durable Action message must not execute another start.
    assert (
        _process_chatflow_action(
            v34_pg_engine,
            action_id=start_action_id,
            processor=processor,
        )
        == "completed"
    )
    assert len(start_port.start_calls) == 1
