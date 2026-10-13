from __future__ import annotations

import hmac
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.domain_errors import DomainError
from app.db.business_session_models import BusinessSession
from app.db.chatflow_generation_models import (
    ChatflowGenerationEventReceipt,
    ChatflowGenerationPermit,
    ChatflowGenerationRequest,
    ChatflowGenerationRequestSlot,
    ChatflowGenerationSlotState,
    ChatflowGenerationTracking,
    ChatflowPromptReference,
)
from app.db.models import User
from app.db.recommendation_models import Recommendation
from app.db.reference_models import ReferenceSet
from app.db.task_models import Task, TaskItem, TaskItemAttempt
from app.schemas.chatflow_generation import (
    ChatflowGenerationEvent,
    ChatflowGenerationEventAccepted,
    ChatflowGenerationSlotEvent,
    aggregate_generation_status,
    chatflow_generation_event_digest,
    generation_snapshot_digest,
)
from app.schemas.prompt_workflow import canonical_sha256
from app.services.chatflow_generation_actions import ensure_generation_recovery_action


class ChatflowGenerationEventService:
    """Persist authenticated generation snapshots against the H4 immutable mapping."""

    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

    def accept(self, event: ChatflowGenerationEvent) -> ChatflowGenerationEventAccepted:
        digest = chatflow_generation_event_digest(event)
        snapshot_digest = generation_snapshot_digest(
            batch_id=event.batch_id,
            source_sequence=event.source_sequence,
            slots=event.slots,
        )
        if not hmac.compare_digest(digest, event.digest):
            raise DomainError(422, "GENERATION_EVENT_DIGEST_INVALID", "状态事件摘要无效")
        try:
            with self._sessions() as db, db.begin():
                parents = _lock_generation_event_parents(db, event)
                request, task, tracking, request_slots, permit, references, slot_states = parents
                if (
                    event.task_id != task.id
                    or event.business_session_id != task.business_session_id
                    or event.dify_run_id != request.dify_run_id
                    or event.package_digest != request.package_digest.hex()
                    or event.permit_version != permit.permit_version
                    or permit.permit_version != 2
                    or event.permit_contract_version != "chatflow-generation-permit-v2"
                    or event.slot_mapping_digest != permit.slot_mapping_digest.hex()
                    or event.reference_manifest_digest != permit.reference_manifest_digest.hex()
                    or (tracking.batch_id is not None and event.batch_id != tracking.batch_id)
                ):
                    raise _conflict()
                expected_mapping = [
                    {
                        "slot_id": slot.slot_id,
                        "position": slot.position,
                        "image_type": slot.image_type,
                        "task_item_id": str(slot.task_item_id),
                        "attempt_id": str(slot.attempt_id),
                    }
                    for slot in request_slots
                ]
                expected_references = [
                    {
                        "asset_id": str(reference.asset_id),
                        "position": reference.position,
                        "content_sha256": reference.content_sha256,
                        "ref_id": reference.ref_id,
                    }
                    for reference in references
                ]
                if (
                    canonical_sha256(expected_mapping) != permit.slot_mapping_digest
                    or canonical_sha256(expected_references) != permit.reference_manifest_digest
                    or len(event.slots) != len(request_slots)
                ):
                    raise _conflict()
                mapping_by_id = {slot.slot_id: slot for slot in request_slots}
                for slot_event in event.slots:
                    saved = mapping_by_id.get(slot_event.slot_id)
                    if saved is None or (
                        saved.task_id != task.id
                        or saved.task_item_id != slot_event.task_item_id
                        or saved.attempt_id != slot_event.attempt_id
                        or saved.position != slot_event.position
                        or saved.image_type != slot_event.image_type
                    ):
                        raise _conflict()

                by_event = db.execute(
                    select(ChatflowGenerationEventReceipt).where(
                        ChatflowGenerationEventReceipt.event_id == event.event_id
                    )
                ).scalar_one_or_none()
                if by_event is not None:
                    if (
                        by_event.generation_request_id != request.id
                        or by_event.event_digest != bytes.fromhex(digest)
                    ):
                        raise _conflict()
                    return ChatflowGenerationEventAccepted(
                        status="replayed",
                        generation_request_id=request.id,
                        batch_id=event.batch_id,
                    )
                by_sequence = db.execute(
                    select(ChatflowGenerationEventReceipt).where(
                        ChatflowGenerationEventReceipt.generation_request_id == request.id,
                        ChatflowGenerationEventReceipt.source_sequence == event.source_sequence,
                    )
                ).scalars().all()
                if any(
                    prior.snapshot_digest != snapshot_digest for prior in by_sequence
                ):
                    raise _conflict()

                if tracking.source_sequence == event.source_sequence:
                    if tracking.status_digest is None or tracking.status_digest != snapshot_digest:
                        raise _conflict()
                    replayed = True
                else:
                    replayed = bool(by_sequence)

                receipt = ChatflowGenerationEventReceipt(
                    event_id=event.event_id,
                    generation_request_id=request.id,
                    task_id=task.id,
                    source_sequence=event.source_sequence,
                    event_digest=bytes.fromhex(digest),
                    snapshot_digest=snapshot_digest,
                )
                db.add(receipt)
                db.flush()
                if replayed:
                    return ChatflowGenerationEventAccepted(
                        status="replayed",
                        generation_request_id=request.id,
                        batch_id=event.batch_id,
                    )
                if (
                    tracking.source_sequence is not None
                    and event.source_sequence < tracking.source_sequence
                ):
                    return ChatflowGenerationEventAccepted(
                        status="ignored",
                        generation_request_id=request.id,
                        batch_id=event.batch_id,
                    )

                states_by_id = {state.slot_id: state for state in slot_states}
                for slot_event in event.slots:
                    slot_digest = canonical_sha256(slot_event.model_dump(mode="json"))
                    old = states_by_id.get(slot_event.slot_id)
                    if old is not None:
                        if slot_event.status_version < old.status_version:
                            continue
                        if slot_event.status_version == old.status_version:
                            if old.status_digest != slot_digest:
                                raise _conflict()
                            continue
                        if _is_terminal(old.status) and not _is_terminal(slot_event.status):
                            continue
                        if _is_terminal(old.status) and old.status != slot_event.status:
                            continue
                    retry_count = (
                        None
                        if slot_event.status in {"submitting", "submission_unknown"}
                        else slot_event.known_retry_count
                    )
                    if old is None:
                        db.add(
                            ChatflowGenerationSlotState(
                                generation_request_id=request.id,
                                slot_id=slot_event.slot_id,
                                status=slot_event.status,
                                status_version=slot_event.status_version,
                                status_digest=slot_digest,
                                error_code=slot_event.error_code,
                                known_retry_count=retry_count,
                            )
                        )
                    else:
                        old.status = slot_event.status
                        old.status_version = slot_event.status_version
                        old.status_digest = slot_digest
                        old.error_code = slot_event.error_code
                        old.known_retry_count = retry_count

                db.flush()
                current_states = db.execute(
                    select(ChatflowGenerationSlotState)
                    .where(ChatflowGenerationSlotState.generation_request_id == request.id)
                    .order_by(ChatflowGenerationSlotState.slot_id)
                ).scalars().all()
                status_slots = [
                    _slot_state_projection(state, mapping_by_id[state.slot_id])
                    for state in current_states
                ]
                if tracking.batch_id is None:
                    tracking.batch_id = event.batch_id
                tracking.status = aggregate_generation_status(status_slots)
                tracking.source_sequence = max(
                    tracking.source_sequence or 0, event.source_sequence
                )
                tracking.status_digest = snapshot_digest
                tracking.reconcile_state = (
                    "complete"
                    if all(_is_terminal(state.status) for state in current_states)
                    else "active"
                )
                tracking.last_error_code = None
                now = db.scalar(select(func.clock_timestamp()))
                tracking.updated_at = now
                if tracking.reconcile_state == "active":
                    ensure_generation_recovery_action(
                        db,
                        request=request,
                        task_id=task.id,
                        session_id=task.business_session_id,
                        tracking=tracking,
                        due_at=now + timedelta(seconds=10),
                    )
                return ChatflowGenerationEventAccepted(
                    status="accepted",
                    generation_request_id=request.id,
                    batch_id=event.batch_id,
                )
        except DomainError:
            raise
        except IntegrityError:
            # Integrity errors racing the same event are retried through the exact
            # event identity; no status write is acknowledged before commit.
            with self._sessions() as db:
                receipt = db.execute(
                    select(ChatflowGenerationEventReceipt).where(
                        ChatflowGenerationEventReceipt.event_id == event.event_id
                    )
                ).scalar_one_or_none()
                if receipt is not None and receipt.event_digest == bytes.fromhex(digest):
                    return ChatflowGenerationEventAccepted(
                        status="replayed",
                        generation_request_id=event.generation_request_id,
                        batch_id=event.batch_id,
                    )
            raise DomainError(409, "GENERATION_EVENT_CONFLICT", "状态事件已发生并发冲突") from None
        except SQLAlchemyError:
            raise DomainError(503, "GENERATION_EVENT_UNAVAILABLE", "状态事件暂时无法保存") from None


def _lock_generation_event_parents(db: Session, event: ChatflowGenerationEvent):
    hint = db.execute(
        select(
            ChatflowGenerationRequest.task_id,
            ChatflowGenerationRequest.business_session_id,
        ).where(ChatflowGenerationRequest.id == event.generation_request_id)
    ).one_or_none()
    if hint is None:
        raise DomainError(404, "GENERATION_REQUEST_NOT_FOUND", "生成请求不可用")
    task_id, session_id = hint
    task_hint = db.execute(
        select(Task.owner_user_id, Task.reference_set_id, Task.recommendation_id).where(
            Task.id == task_id
        )
    ).one_or_none()
    if task_hint is None:
        raise DomainError(404, "GENERATION_REQUEST_NOT_FOUND", "生成请求不可用")
    owner_id, reference_set_id, recommendation_id = task_hint
    owner = db.execute(
        select(User).where(User.id == owner_id, User.status == "active").with_for_update()
    ).scalar_one_or_none()
    reference_set = db.execute(
        select(ReferenceSet)
        .where(ReferenceSet.id == reference_set_id, ReferenceSet.owner_user_id == owner_id)
        .with_for_update()
    ).scalar_one_or_none()
    business_session = db.execute(
        select(BusinessSession)
        .where(
            BusinessSession.id == session_id,
            BusinessSession.owner_user_id == owner_id,
            BusinessSession.reference_set_id == reference_set_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    recommendation = db.execute(
        select(Recommendation)
        .where(
            Recommendation.id == recommendation_id,
            Recommendation.business_session_id == session_id,
            Recommendation.owner_user_id == owner_id,
            Recommendation.reference_set_id == reference_set_id,
            Recommendation.source_kind == "dify_chatflow",
        )
        .with_for_update()
    ).scalar_one_or_none()
    task = db.execute(
        select(Task)
        .where(
            Task.id == task_id,
            Task.business_session_id == session_id,
            Task.owner_user_id == owner_id,
            Task.reference_set_id == reference_set_id,
            Task.recommendation_id == recommendation_id,
            Task.provenance_kind == "dify_chatflow",
        )
        .with_for_update()
    ).scalar_one_or_none()
    if any(row is None for row in (owner, reference_set, business_session, recommendation, task)):
        raise DomainError(404, "GENERATION_REQUEST_NOT_FOUND", "生成请求不可用")
    if (
        event.task_id != task.id
        or event.business_session_id != business_session.id
        or event.dify_run_id != business_session.dify_run_id
    ):
        raise _conflict()
    task_items = db.execute(
        select(TaskItem)
        .where(TaskItem.task_id == task.id)
        .order_by(TaskItem.position, TaskItem.id)
        .with_for_update()
    ).scalars().all()
    if not task_items:
        raise _conflict()
    db.execute(
        select(TaskItemAttempt)
        .where(TaskItemAttempt.task_item_id.in_([item.id for item in task_items]))
        .order_by(TaskItemAttempt.task_item_id, TaskItemAttempt.attempt_no, TaskItemAttempt.id)
        .with_for_update()
    ).scalars().all()
    request = db.execute(
        select(ChatflowGenerationRequest)
        .where(
            ChatflowGenerationRequest.id == event.generation_request_id,
            ChatflowGenerationRequest.task_id == task.id,
            ChatflowGenerationRequest.business_session_id == business_session.id,
            ChatflowGenerationRequest.dify_run_id == business_session.dify_run_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if request is None:
        raise _conflict()
    permit = db.execute(
        select(ChatflowGenerationPermit)
        .where(ChatflowGenerationPermit.generation_request_id == request.id)
        .with_for_update()
    ).scalar_one_or_none()
    if permit is None:
        raise _conflict()
    slots = db.execute(
        select(ChatflowGenerationRequestSlot)
        .where(ChatflowGenerationRequestSlot.generation_request_id == request.id)
        .order_by(ChatflowGenerationRequestSlot.position, ChatflowGenerationRequestSlot.slot_id)
        .with_for_update()
    ).scalars().all()
    references = db.execute(
        select(ChatflowPromptReference)
        .where(ChatflowPromptReference.package_id == request.package_id)
        .order_by(ChatflowPromptReference.position, ChatflowPromptReference.ref_id)
        .with_for_update()
    ).scalars().all()
    tracking = db.execute(
        select(ChatflowGenerationTracking)
        .where(ChatflowGenerationTracking.generation_request_id == request.id)
        .with_for_update()
    ).scalar_one_or_none()
    if tracking is None or len(slots) != len(task_items):
        raise _conflict()
    slot_states = db.execute(
        select(ChatflowGenerationSlotState)
        .where(ChatflowGenerationSlotState.generation_request_id == request.id)
        .order_by(ChatflowGenerationSlotState.slot_id)
        .with_for_update()
    ).scalars().all()
    return request, task, tracking, slots, permit, references, slot_states


def _slot_state_projection(
    state: ChatflowGenerationSlotState, mapping: ChatflowGenerationRequestSlot
) -> ChatflowGenerationSlotEvent:
    return ChatflowGenerationSlotEvent(
        slot_id=state.slot_id,
        task_item_id=mapping.task_item_id,
        attempt_id=mapping.attempt_id,
        position=mapping.position,
        image_type=mapping.image_type,
        status=state.status,
        status_version=state.status_version,
        error_code=state.error_code,
        known_retry_count=state.known_retry_count,
    )


def _is_terminal(status: str) -> bool:
    return status in {"completed", "failed", "cancelled", "protocol_error"}


def _conflict() -> DomainError:
    return DomainError(409, "GENERATION_EVENT_CONFLICT", "状态事件与已保存关联不一致")
