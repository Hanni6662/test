from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.domain_errors import DomainError
from app.db.action_models import Action
from app.db.business_question_models import (
    BusinessAnswerSubmission,
    BusinessQuestion,
    BusinessQuestionEvent,
)
from app.db.business_session_models import BusinessSession
from app.db.models import User
from app.db.recommendation_models import Recommendation
from app.db.reference_models import ReferenceSet
from app.db.task_models import Task, TaskItem
from app.schemas.business_questions import (
    BusinessQuestionAnswerAccepted,
    BusinessQuestionAnswerRequest,
    BusinessQuestionPublic,
    HumanInputField,
    HumanInputRequiredPayload,
)
from app.schemas.business_sessions import (
    ChatflowBusinessEvent,
    ChatflowBusinessEventAccepted,
    chatflow_business_event_digest,
)
from app.services.chatflow_actions import (
    CHATFLOW_ANSWER_ACTION,
    CHATFLOW_ANSWER_FIRST_SEQUENCE,
    chatflow_answer_action_digest,
    chatflow_answer_action_id,
)
from app.services.tasks import idempotency_key_hash, validate_dify_chatflow_task_snapshot
from app.workers.action_processor import schedule_action_in_transaction

_DIGEST_VERSION = 1
_RECOVERY_WINDOW = timedelta(minutes=5)


class BusinessQuestionService:
    """Persist trusted Dify question events and user answers before worker I/O."""

    def __init__(self, engine: Engine, *, idempotency_key_max_length: int = 200) -> None:
        if not 1 <= idempotency_key_max_length <= 200:
            raise ValueError("idempotency key max length must be between 1 and 200")
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        self._idempotency_key_max_length = idempotency_key_max_length

    def accept_human_input_event(
        self, event: ChatflowBusinessEvent
    ) -> ChatflowBusinessEventAccepted:
        if (
            not isinstance(event, ChatflowBusinessEvent)
            or event.event_type != "human_input_required"
            or event.source != "dify"
            or event.contract_version != "chatflow-handoff-v1"
            or chatflow_business_event_digest(event) != event.digest
        ):
            raise DomainError(422, "CALLBACK_INVALID", "回传内容无效")
        try:
            payload = HumanInputRequiredPayload.model_validate_json(
                json.dumps(event.payload, ensure_ascii=False), strict=True
            )
        except Exception:
            raise DomainError(422, "CALLBACK_INVALID", "回传内容无效") from None
        question_digest = _question_digest(event, payload)
        try:
            with self._sessions() as db, db.begin():
                locked = _lock_question_parents(
                    db,
                    owner_session_id=event.business_session_id,
                    recommendation_id=event.recommendation_id,
                    task_id=payload.task_id,
                )
                if locked is None:
                    raise DomainError(404, "BUSINESS_SESSION_NOT_FOUND", "流程不可用")
                owner, reference_set, session, recommendation, task, task_items = locked
                questions = _lock_questions(db, session.id, task.id if task else None)
                question_ids = [item.id for item in questions]
                answers = _lock_answers(db, session.id, task.id if task else None, question_ids)
                existing = db.execute(
                    select(BusinessQuestionEvent)
                    .where(
                        BusinessQuestionEvent.business_session_id == session.id,
                        BusinessQuestionEvent.source_event_id == event.event_id,
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if existing is not None:
                    if (
                        existing.digest_version == event.digest_version
                        and existing.event_digest == bytes.fromhex(event.digest)
                    ):
                        return self._event_accepted(event, "replayed", existing.question_id)
                    raise DomainError(409, "EVENT_CONTENT_CONFLICT", "回传内容与已保存事件冲突")
                same_sequence = db.execute(
                    select(BusinessQuestionEvent)
                    .where(
                        BusinessQuestionEvent.business_session_id == session.id,
                        BusinessQuestionEvent.dify_run_id == event.dify_run_id,
                        BusinessQuestionEvent.source_sequence == event.source_sequence,
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if same_sequence is not None:
                    raise DomainError(409, "EVENT_SEQUENCE_CONFLICT", "回传顺序冲突")

                now = _db_now(db)
                current_task = (
                    task is not None
                    and task.provenance_kind == "dify_chatflow"
                    and task.business_session_id == session.id
                    and task.recommendation_id == recommendation.id
                    and task.recommendation_revision == event.recommendation_revision
                )
                valid_session_binding = (
                    event.recommendation_id == recommendation.id
                    and event.recommendation_revision == recommendation.revision
                )
                if not valid_session_binding:
                    raise DomainError(409, "CALLBACK_STALE", "问题回传已过期")

                if (
                    event.dify_run_id != session.dify_run_id
                    or event.dify_run_id != recommendation.dify_run_id
                ):
                    self._record_event(
                        db,
                        event,
                        task_id=task.id if current_task else None,
                        question_id=None,
                        disposition="reconciling",
                    )
                    return self._event_accepted(event, "ignored")

                if not current_task or not task_items:
                    self._record_event(
                        db,
                        event,
                        task_id=None,
                        question_id=None,
                        disposition="reconciling",
                    )
                    return self._event_accepted(event, "ignored")

                if reference_set.expires_at is None:
                    self._record_event(
                        db,
                        event,
                        task_id=task.id,
                        question_id=None,
                        disposition="expired",
                    )
                    return self._event_accepted(event, "ignored")
                expected_expiry = min(
                    payload.expires_at.astimezone(UTC),
                    session.expires_at,
                    reference_set.expires_at,
                    recommendation.valid_until or session.expires_at,
                    task.expires_at or session.expires_at,
                )
                expired = (
                    session.expires_at <= now
                    or reference_set.status != "ready"
                    or reference_set.expires_at <= now
                    or recommendation.status != "ready"
                    or recommendation.valid_until is None
                    or recommendation.valid_until <= now
                    or expected_expiry <= now
                )
                if expired:
                    self._record_event(
                        db,
                        event,
                        task_id=task.id,
                        question_id=None,
                        disposition="expired",
                    )
                    return self._event_accepted(event, "ignored")

                continuation = db.execute(
                    select(Action)
                    .where(
                        Action.scope_kind == "business_session",
                        Action.scope_id == session.id,
                        Action.business_session_id == session.id,
                        Action.task_id == task.id,
                        Action.action_kind == "dify.chatflow.continue.v1",
                        Action.sequence == 2,
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                continuation_ready = (
                    continuation is not None
                    and continuation.status == "completed"
                    and continuation.last_outcome == "accepted"
                )
                if (
                    session.status != "task_created"
                    or session.start_outcome != "accepted"
                    or recommendation.status != "ready"
                    or task.status not in {"queued", "needs_input", "prompting"}
                    or not continuation_ready
                ):
                    self._record_event(
                        db,
                        event,
                        task_id=task.id,
                        question_id=None,
                        disposition="reconciling",
                    )
                    return self._event_accepted(event, "ignored")

                minimum_sequence = _recommendation_source_sequence(recommendation.state)
                max_received_sequence = max(
                    [
                        item.source_sequence
                        for item in db.execute(
                            select(BusinessQuestionEvent).where(
                                BusinessQuestionEvent.business_session_id == session.id,
                                BusinessQuestionEvent.dify_run_id == event.dify_run_id,
                            )
                        ).scalars()
                    ],
                    default=minimum_sequence,
                )
                if event.source_sequence <= max(minimum_sequence, max_received_sequence):
                    self._record_event(
                        db,
                        event,
                        task_id=task.id,
                        question_id=None,
                        disposition="stale",
                    )
                    return self._event_accepted(event, "ignored")

                try:
                    _validate_complete_task_snapshot(task, session, task_items)
                except DomainError:
                    self._record_event(
                        db,
                        event,
                        task_id=task.id,
                        question_id=None,
                        disposition="reconciling",
                    )
                    return self._event_accepted(event, "ignored")

                existing_round = next(
                    (item for item in questions if item.round_number == payload.round_number), None
                )
                if existing_round is not None:
                    if (
                        existing_round.status == "awaiting_form_binding"
                        and existing_round.question_digest == bytes.fromhex(question_digest)
                        and payload.form_token is not None
                    ):
                        existing_round.form_token = payload.form_token
                        existing_round.form_token_digest = hashlib.sha256(
                            payload.form_token.encode("utf-8")
                        ).digest()
                        existing_round.status = "awaiting_answer"
                        existing_round.updated_at = now
                        self._record_event(
                            db,
                            event,
                            task_id=task.id,
                            question_id=existing_round.id,
                            disposition="form_bound",
                        )
                        return self._event_accepted(event, "accepted", existing_round.id)
                    self._record_event(
                        db,
                        event,
                        task_id=task.id,
                        question_id=None,
                        disposition="stale",
                    )
                    return self._event_accepted(event, "ignored")

                expected_round = 1
                if questions:
                    prior = max(questions, key=lambda item: item.round_number)
                    prior_answer = next(
                        (item for item in answers if item.business_question_id == prior.id), None
                    )
                    if (
                        prior.round_number == 1
                        and prior.status == "answered"
                        and prior_answer is not None
                        and prior_answer.status == "accepted"
                    ):
                        expected_round = 2
                    else:
                        expected_round = 3
                if payload.round_number != expected_round:
                    self._record_event(
                        db,
                        event,
                        task_id=task.id,
                        question_id=None,
                        disposition="stale",
                    )
                    return self._event_accepted(event, "ignored")

                question_id = uuid4()
                form_token = payload.form_token
                question = BusinessQuestion(
                    id=question_id,
                    business_session_id=session.id,
                    task_id=task.id,
                    recommendation_id=recommendation.id,
                    owner_user_id=owner.id,
                    reference_set_id=reference_set.id,
                    recommendation_revision=recommendation.revision,
                    dify_run_id=event.dify_run_id,
                    source_event_id=event.event_id,
                    source_sequence=event.source_sequence,
                    digest_version=event.digest_version,
                    event_digest=bytes.fromhex(event.digest),
                    question_digest=bytes.fromhex(question_digest),
                    round_number=payload.round_number,
                    question_text=payload.question_text,
                    fields_snapshot=[item.model_dump(mode="json") for item in payload.fields],
                    form_token=form_token,
                    form_token_digest=(
                        hashlib.sha256(form_token.encode("utf-8")).digest()
                        if form_token is not None
                        else None
                    ),
                    status="awaiting_answer" if form_token is not None else "awaiting_form_binding",
                    expires_at=expected_expiry,
                    created_at=now,
                    updated_at=now,
                )
                db.add(question)
                db.flush()
                self._record_event(
                    db,
                    event,
                    task_id=task.id,
                    question_id=question.id,
                    disposition=(
                        "question_created" if form_token is not None else "awaiting_form_binding"
                    ),
                )
                task.status = "needs_input"
                task.updated_at = now
                return self._event_accepted(event, "accepted", question.id)
        except DomainError:
            raise
        except IntegrityError:
            raise DomainError(409, "EVENT_CONTENT_CONFLICT", "回传事件发生冲突") from None
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "问题回传暂时无法保存") from None

    def submit_answer(
        self,
        *,
        owner_user_id: UUID,
        business_session_id: UUID,
        question_id: UUID,
        idempotency_key: str,
        request: BusinessQuestionAnswerRequest,
    ) -> BusinessQuestionAnswerAccepted:
        key_hash = idempotency_key_hash(
            idempotency_key, maximum_length=self._idempotency_key_max_length
        )
        try:
            with self._sessions() as db, db.begin():
                locked = _lock_question_parents(
                    db,
                    owner_session_id=business_session_id,
                    recommendation_id=None,
                    task_id=None,
                    owner_user_id=owner_user_id,
                )
                if locked is None:
                    raise DomainError(404, "BUSINESS_SESSION_NOT_FOUND", "流程不可用")
                owner, reference_set, session, recommendation, task, task_items = locked
                if task is None:
                    raise DomainError(404, "BUSINESS_SESSION_NOT_FOUND", "流程不可用")
                questions = _lock_questions(db, session.id, task.id)
                question = next((item for item in questions if item.id == question_id), None)
                if question is None:
                    raise DomainError(404, "QUESTION_NOT_FOUND", "问题不可用")
                answers = _lock_answers(db, session.id, task.id, [item.id for item in questions])
                now = _db_now(db)
                normalized_answers = request.model_dump(mode="json")["answers"]
                request_digest = _answer_digest(
                    business_session_id=session.id,
                    question_id=question.id,
                    answers=normalized_answers,
                )
                existing = next(
                    (item for item in answers if item.business_question_id == question.id), None
                )
                if existing is not None:
                    if existing.idempotency_key_hash != key_hash:
                        raise DomainError(409, "QUESTION_ALREADY_ANSWERED", "该问题已提交答案")
                    if existing.request_digest != request_digest:
                        raise DomainError(409, "IDEMPOTENCY_CONFLICT", "相同幂等键不能用于不同答案")
                    action = (
                        db.execute(
                            select(Action)
                            .where(
                                Action.scope_kind == "business_session",
                                Action.scope_id == session.id,
                                Action.business_answer_submission_id == existing.id,
                                Action.business_question_id == question.id,
                            )
                            .order_by(Action.sequence.desc(), Action.id)
                            .with_for_update()
                        )
                        .scalars()
                        .first()
                    )
                    if action is None:
                        raise DomainError(409, "ANSWER_STATE_INVALID", "答案状态暂不可用")
                    replay_status = (
                        existing.status
                        if existing.status in {"accepted", "rejected"}
                        else "replayed"
                    )
                    return BusinessQuestionAnswerAccepted(
                        business_session_id=session.id,
                        question_id=question.id,
                        answer_submission_id=existing.id,
                        status=replay_status,  # type: ignore[arg-type]
                    )

                if (
                    session.status != "task_created"
                    or session.expires_at <= now
                    or reference_set.status != "ready"
                    or reference_set.expires_at is None
                    or reference_set.expires_at <= now
                    or recommendation.status != "ready"
                    or recommendation.valid_until is None
                    or recommendation.valid_until <= now
                    or question.status != "awaiting_answer"
                    or question.form_token is None
                    or question.form_token_digest is None
                    or hashlib.sha256(question.form_token.encode("utf-8")).digest()
                    != question.form_token_digest
                    or question.expires_at <= now
                    or question.dify_run_id != session.dify_run_id
                    or question.recommendation_revision != recommendation.revision
                    or task.status != "needs_input"
                ):
                    raise DomainError(409, "QUESTION_NOT_SUBMITTABLE", "问题暂不可回答")
                _validate_answer_fields(question.fields_snapshot, normalized_answers)
                _validate_complete_task_snapshot(task, session, task_items)

                answer_id = uuid4()
                recovery_deadline = min(now + _RECOVERY_WINDOW, session.expires_at)
                answer = BusinessAnswerSubmission(
                    id=answer_id,
                    business_session_id=session.id,
                    task_id=task.id,
                    business_question_id=question.id,
                    owner_user_id=owner.id,
                    reference_set_id=reference_set.id,
                    idempotency_key_hash=key_hash,
                    request_digest=request_digest,
                    digest_version=_DIGEST_VERSION,
                    answers=normalized_answers,
                    status="confirming",
                    recovery_attempts=0,
                    recovery_deadline=recovery_deadline,
                    created_at=now,
                    updated_at=now,
                )
                db.add(answer)
                db.flush()
                sequence = _next_action_sequence(db, session.id)
                action_digest = chatflow_answer_action_digest(
                    action_kind=CHATFLOW_ANSWER_ACTION,
                    question=question,
                    answer=answer,
                )
                schedule_action_in_transaction(
                    db,
                    action_id=chatflow_answer_action_id(
                        answer_submission_id=answer.id,
                        sequence=sequence,
                        recovery=False,
                    ),
                    task_id=task.id,
                    business_session_id=session.id,
                    business_question_id=question.id,
                    business_answer_submission_id=answer.id,
                    scope_kind="business_session",
                    scope_id=session.id,
                    action_kind=CHATFLOW_ANSWER_ACTION,
                    sequence=sequence,
                    due_at=now,
                    input_digest_version=1,
                    input_digest=action_digest,
                )
                question.status = "confirming"
                question.updated_at = now
                return BusinessQuestionAnswerAccepted(
                    business_session_id=session.id,
                    question_id=question.id,
                    answer_submission_id=answer.id,
                    status="confirming",
                )
        except DomainError:
            raise
        except IntegrityError:
            return self._recover_answer_race(
                owner_user_id,
                business_session_id,
                question_id,
                key_hash,
                request_digest,
            )
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "答案暂时无法保存") from None

    def _recover_answer_race(
        self,
        owner_user_id: UUID,
        business_session_id: UUID,
        question_id: UUID,
        key_hash: bytes,
        request_digest: bytes,
    ) -> BusinessQuestionAnswerAccepted:
        try:
            with self._sessions() as db:
                answer = db.execute(
                    select(BusinessAnswerSubmission).where(
                        BusinessAnswerSubmission.owner_user_id == owner_user_id,
                        BusinessAnswerSubmission.business_session_id == business_session_id,
                        BusinessAnswerSubmission.business_question_id == question_id,
                    )
                ).scalar_one_or_none()
                if (
                    answer is not None
                    and answer.idempotency_key_hash == key_hash
                    and answer.request_digest == request_digest
                ):
                    return BusinessQuestionAnswerAccepted(
                        business_session_id=business_session_id,
                        question_id=question_id,
                        answer_submission_id=answer.id,
                        status=(
                            answer.status
                            if answer.status in {"accepted", "rejected"}
                            else "replayed"
                        ),  # type: ignore[arg-type]
                    )
        except SQLAlchemyError:
            pass
        raise DomainError(409, "ANSWER_CONFLICT", "答案提交状态冲突，请重新读取") from None

    @staticmethod
    def _record_event(
        db: Session,
        event: ChatflowBusinessEvent,
        *,
        task_id: UUID | None,
        question_id: UUID | None,
        disposition: str,
    ) -> None:
        db.add(
            BusinessQuestionEvent(
                business_session_id=event.business_session_id,
                task_id=task_id,
                question_id=question_id,
                source_event_id=event.event_id,
                dify_run_id=event.dify_run_id,
                recommendation_revision=event.recommendation_revision,
                source_sequence=event.source_sequence,
                digest_version=event.digest_version,
                event_digest=bytes.fromhex(event.digest),
                disposition=disposition,
            )
        )

    @staticmethod
    def _event_accepted(
        event: ChatflowBusinessEvent,
        status: str,
        question_id: UUID | None = None,
    ) -> ChatflowBusinessEventAccepted:
        return ChatflowBusinessEventAccepted(
            status=status,  # type: ignore[arg-type]
            business_session_id=event.business_session_id,
            recommendation_id=event.recommendation_id,
            recommendation_revision=event.recommendation_revision,
            question_id=question_id,
        )


def project_business_question(
    db: Session,
    *,
    session: BusinessSession,
    recommendation: Recommendation | None,
    task: Task | None,
    now: datetime,
) -> BusinessQuestionPublic | None:
    if task is None or recommendation is None or session.dify_run_id is None:
        return None
    question = db.execute(
        select(BusinessQuestion)
        .where(
            BusinessQuestion.business_session_id == session.id,
            BusinessQuestion.task_id == task.id,
            BusinessQuestion.recommendation_id == recommendation.id,
            BusinessQuestion.recommendation_revision == recommendation.revision,
            BusinessQuestion.dify_run_id == session.dify_run_id,
        )
        .order_by(BusinessQuestion.round_number.desc(), BusinessQuestion.id)
        .limit(1)
    ).scalar_one_or_none()
    if question is None:
        return None
    answer = db.execute(
        select(BusinessAnswerSubmission).where(
            BusinessAnswerSubmission.business_question_id == question.id
        )
    ).scalar_one_or_none()
    answer_status = answer.status if answer is not None else None
    try:
        fields = [
            HumanInputField.model_validate(item, strict=True) for item in question.fields_snapshot
        ]
    except Exception:
        raise DomainError(503, "QUESTION_UNAVAILABLE", "问题暂时无法读取") from None
    expired = question.expires_at <= now or session.expires_at <= now
    status = (
        "expired"
        if expired and question.status in {"awaiting_answer", "awaiting_form_binding"}
        else question.status
    )
    can_answer = (
        not expired
        and status == "awaiting_answer"
        and question.form_token is not None
        and answer is None
        and session.status == "task_created"
    )
    return BusinessQuestionPublic(
        question_id=question.id,
        question_text=question.question_text,
        fields=fields,
        round_number=question.round_number,
        expires_at=question.expires_at,
        status=status,  # type: ignore[arg-type]
        answer_status=answer_status,  # type: ignore[arg-type]
        can_answer=can_answer,
    )


def _lock_question_parents(
    db: Session,
    *,
    owner_session_id: UUID,
    recommendation_id: UUID | None,
    task_id: UUID | None,
    owner_user_id: UUID | None = None,
) -> (
    tuple[User, ReferenceSet, BusinessSession, Recommendation, Task | None, tuple[TaskItem, ...]]
    | None
):
    hint = db.execute(
        select(BusinessSession.owner_user_id, BusinessSession.reference_set_id).where(
            BusinessSession.id == owner_session_id,
            *([BusinessSession.owner_user_id == owner_user_id] if owner_user_id else []),
        )
    ).one_or_none()
    if hint is None:
        return None
    owner_id, reference_id = hint
    owner = db.execute(
        select(User).where(User.id == owner_id, User.status == "active").with_for_update()
    ).scalar_one_or_none()
    reference_set = db.execute(
        select(ReferenceSet)
        .where(ReferenceSet.id == reference_id, ReferenceSet.owner_user_id == owner_id)
        .with_for_update()
    ).scalar_one_or_none()
    session = db.execute(
        select(BusinessSession)
        .where(
            BusinessSession.id == owner_session_id,
            BusinessSession.owner_user_id == owner_id,
            BusinessSession.reference_set_id == reference_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    rec_statement = select(Recommendation).where(
        Recommendation.business_session_id == owner_session_id,
        Recommendation.owner_user_id == owner_id,
        Recommendation.reference_set_id == reference_id,
        Recommendation.source_kind == "dify_chatflow",
    )
    if recommendation_id is not None:
        rec_statement = rec_statement.where(Recommendation.id == recommendation_id)
    recommendation = db.execute(rec_statement.with_for_update()).scalar_one_or_none()
    if owner is None or reference_set is None or session is None or recommendation is None:
        return None
    task: Task | None = None
    items: tuple[TaskItem, ...] = ()
    if task_id is not None:
        task = db.execute(
            select(Task)
            .where(
                Task.id == task_id,
                Task.business_session_id == session.id,
                Task.owner_user_id == owner_id,
                Task.reference_set_id == reference_id,
                Task.recommendation_id == recommendation.id,
                Task.provenance_kind == "dify_chatflow",
            )
            .with_for_update()
        ).scalar_one_or_none()
        if task is not None:
            items = tuple(
                db.execute(
                    select(TaskItem)
                    .where(TaskItem.task_id == task.id)
                    .order_by(TaskItem.position, TaskItem.id)
                    .with_for_update()
                )
                .scalars()
                .all()
            )
    else:
        task = db.execute(
            select(Task)
            .where(Task.business_session_id == session.id, Task.owner_user_id == owner_id)
            .with_for_update()
        ).scalar_one_or_none()
        if task is not None:
            task = db.execute(
                select(Task)
                .where(
                    Task.id == task.id,
                    Task.business_session_id == session.id,
                    Task.recommendation_id == recommendation.id,
                    Task.provenance_kind == "dify_chatflow",
                )
                .with_for_update()
            ).scalar_one_or_none()
            if task is not None:
                items = tuple(
                    db.execute(
                        select(TaskItem)
                        .where(TaskItem.task_id == task.id)
                        .order_by(TaskItem.position, TaskItem.id)
                        .with_for_update()
                    )
                    .scalars()
                    .all()
                )
    return owner, reference_set, session, recommendation, task, items


def _lock_questions(
    db: Session, session_id: UUID, task_id: UUID | None
) -> tuple[BusinessQuestion, ...]:
    if task_id is None:
        return ()
    return tuple(
        db.execute(
            select(BusinessQuestion)
            .where(
                BusinessQuestion.business_session_id == session_id,
                BusinessQuestion.task_id == task_id,
            )
            .order_by(BusinessQuestion.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )


def _lock_answers(
    db: Session, session_id: UUID, task_id: UUID | None, question_ids: list[UUID]
) -> tuple[BusinessAnswerSubmission, ...]:
    if task_id is None or not question_ids:
        return ()
    return tuple(
        db.execute(
            select(BusinessAnswerSubmission)
            .where(
                BusinessAnswerSubmission.business_session_id == session_id,
                BusinessAnswerSubmission.task_id == task_id,
                BusinessAnswerSubmission.business_question_id.in_(question_ids),
            )
            .order_by(BusinessAnswerSubmission.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )


def _question_digest(event: ChatflowBusinessEvent, payload: HumanInputRequiredPayload) -> str:
    safe_payload = payload.model_dump(mode="json", exclude={"form_token"})
    canonical = json.dumps(
        {
            "business_session_id": str(event.business_session_id),
            "recommendation_id": str(event.recommendation_id),
            "recommendation_revision": event.recommendation_revision,
            "dify_run_id": event.dify_run_id,
            "payload": safe_payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _answer_digest(
    *, business_session_id: UUID, question_id: UUID, answers: dict[str, object]
) -> bytes:
    canonical = json.dumps(
        {
            "business_session_id": str(business_session_id),
            "question_id": str(question_id),
            "answers": answers,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).digest()


def _validate_complete_task_snapshot(
    task: Task, session: BusinessSession, task_items: tuple[TaskItem, ...]
) -> None:
    validate_dify_chatflow_task_snapshot(task, session)
    if (
        len(task_items) != task.planned_count
        or [item.position for item in task_items] != list(range(1, task.planned_count + 1))
        or [item.image_type for item in task_items] != task.image_types
    ):
        raise DomainError(409, "REFERENCE_SET_UNAVAILABLE", "任务引用组暂不可用")


def _validate_answer_fields(
    fields_snapshot: list[dict[str, object]], answers: dict[str, object]
) -> None:
    try:
        fields = [HumanInputField.model_validate(item, strict=True) for item in fields_snapshot]
    except Exception:
        raise DomainError(409, "QUESTION_UNAVAILABLE", "问题暂不可回答") from None
    by_id = {item.field_id: item for item in fields}
    if not set(answers).issubset(by_id):
        raise DomainError(422, "ANSWER_INVALID", "答案内容无效")
    if any(item.required and item.field_id not in answers for item in fields):
        raise DomainError(422, "ANSWER_INVALID", "答案内容无效")
    for field_id, answer in answers.items():
        field = by_id[field_id]
        if field.kind == "text":
            if (
                not isinstance(answer, str)
                or len(answer) > 4000
                or (field.required and not answer.strip())
            ):
                raise DomainError(422, "ANSWER_INVALID", "答案内容无效")
        elif field.kind == "single_choice":
            if not isinstance(answer, str) or answer not in field.options:
                raise DomainError(422, "ANSWER_INVALID", "答案内容无效")
        elif (
            not isinstance(answer, list)
            or len(answer) != len(set(answer))
            or any(not isinstance(item, str) or item not in field.options for item in answer)
        ):
            raise DomainError(422, "ANSWER_INVALID", "答案内容无效")


def _next_action_sequence(db: Session, session_id: UUID) -> int:
    maximum = db.scalar(
        select(func.coalesce(func.max(Action.sequence), 0)).where(
            Action.scope_kind == "business_session", Action.scope_id == session_id
        )
    )
    return max(CHATFLOW_ANSWER_FIRST_SEQUENCE, int(maximum or 0) + 1)


def _recommendation_source_sequence(state: object) -> int:
    if not isinstance(state, dict):
        return 0
    sequence = state.get("callback_source_sequence")
    return sequence if isinstance(sequence, int) and not isinstance(sequence, bool) else 0


def _db_now(db: Session) -> datetime:
    value = db.scalar(select(func.clock_timestamp()))
    if value is None:
        raise DomainError(503, "DATABASE_UNAVAILABLE", "流程暂时无法读取")
    return value.astimezone(UTC)
