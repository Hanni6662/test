from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import UUID

import pytest
from app.core.domain_errors import DomainError
from app.db.action_models import Action
from app.db.outbox_models import OutboxEvent
from app.db.prompt_models import MockPromptSubmission, PromptSubmission, TaskPromptWorkflow
from app.db.recommendation_models import Recommendation
from app.db.reference_models import ReferenceSet
from app.db.task_models import Task, TaskItem
from app.integrations.dify.contracts import DifyPromptWorkflowPort
from app.integrations.dify.factory import create_test_prompt_workflow_adapter
from app.schemas.prompt_preferences import (
    PromptPreferenceAccepted,
    PromptPreferenceRequest,
    RecommendationResultCallback,
    recommendation_callback_digest,
)
from app.schemas.prompt_workflow import (
    PROMPT_RESULT_DIGEST_VERSION,
    PromptCallback,
    canonical_sha256,
    prompt_callback_digest_payload,
)
from app.services.prompt_callbacks import PromptCallbackService
from app.services.prompt_preferences import PromptPreferenceService
from app.services.prompt_workflows import (
    GENERATION_START_EVENT_TYPE,
    PROMPT_MOCK_RESULT_QUERY_ACTION_KIND,
)
from app.services.recommendation_callbacks import RecommendationCallbackService
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.integration.actions.test_submission_action_processor_postgres import (
    _action_row,
)
from tests.integration.actions.test_submission_action_processor_postgres import (
    _process_submission_action as _process_submission_action_with_processor,
)
from tests.integration.prompt_workflows.test_prompt_workflows_postgres import (
    _create_submission,
    _create_task,
)
from tests.integration.r3_callbacks.conftest import _PGContext


def _process_submission_action(
    context: _PGContext,
    *,
    task_id: UUID,
    submission_id: UUID,
    sequence: int,
    adapter: DifyPromptWorkflowPort,
    mock_result_callback_service: PromptCallbackService | None = None,
) -> str:
    """Run the registered submit/recover handler for either prompt phase."""
    action = _action_row(context.engine, submission_id, sequence)
    assert action.task_id == task_id
    processor_status = _process_submission_action_with_processor(
        context.engine,
        action=action,
        port=adapter,
        mock_result_callback_service=mock_result_callback_service,
    )
    assert processor_status == "completed"
    with Session(context.engine) as db:
        processed_action = db.get(Action, action.id)
        assert processed_action is not None and processed_action.last_outcome is not None
        return processed_action.last_outcome


def _database_now(context: _PGContext):
    with Session(context.engine) as db:
        return db.scalar(select(func.clock_timestamp()))


def _accept_needs_input(
    context: _PGContext,
    *,
    task_id: UUID,
    submission_id: UUID,
    round_number: int = 1,
    allowed: tuple[str, ...] = ("language",),
    locked: tuple[str, ...] = ("platform", "usage_scene", "visual_style", "selling_points"),
    valid_until=None,
) -> PromptCallback:
    now = _database_now(context)
    with Session(context.engine) as db:
        task = db.get(Task, task_id)
        submission = db.get(PromptSubmission, submission_id)
        workflow = db.execute(
            select(TaskPromptWorkflow).where(TaskPromptWorkflow.task_id == task_id)
        ).scalar_one()
        assert task is not None and submission is not None
        callback = PromptCallback(
            task_id=task.id,
            workflow_run_id=workflow.workflow_run_id,
            workflow_revision=workflow.workflow_revision,
            submission_id=submission.submission_id,
            submission_content_digest_version=submission.content_digest_version,
            submission_content_digest=submission.content_digest,
            result_digest_version=PROMPT_RESULT_DIGEST_VERSION,
            result_digest=b"0" * 32,
            outcome="needs_input",
            next_question_round=round_number,
            allowed_fields=allowed,
            locked_fields=locked,
            valid_until=valid_until or now + timedelta(hours=2),
        )
    fields = callback.model_dump(mode="python")
    fields["result_digest"] = canonical_sha256(prompt_callback_digest_payload(callback))
    return PromptCallback.model_validate(fields, strict=True)


def _rebind_prompt_callback(callback: PromptCallback, **changes: object) -> PromptCallback:
    fields = callback.model_dump(mode="python")
    fields.update(changes)
    fields["result_digest"] = b"0" * 32
    changed = PromptCallback.model_validate(fields, strict=True)
    fields = changed.model_dump(mode="python")
    fields["result_digest"] = canonical_sha256(prompt_callback_digest_payload(changed))
    return PromptCallback.model_validate(fields, strict=True)


def _prepare_submitted_initial(
    context: _PGContext,
    *,
    reference_count: int = 1,
) -> tuple[UUID, UUID, UUID]:
    owner_id, task_id = _create_task(context, reference_count=reference_count)
    submission_id = _create_submission(context, task_id)
    adapter = create_test_prompt_workflow_adapter(
        session_factory=lambda: Session(context.engine),
    )
    assert (
        _process_submission_action(
            context,
            task_id=task_id,
            submission_id=submission_id,
            sequence=1,
            adapter=adapter,
        )
        == "accepted"
    )
    return owner_id, task_id, submission_id


def _ready_recommendation_callback(
    recommendation: Recommendation,
    *,
    valid_until,
) -> RecommendationResultCallback:
    callback = RecommendationResultCallback(
        recommendation_id=recommendation.id,
        reference_set_id=recommendation.reference_set_id,
        recommendation_revision=recommendation.revision,
        run_ref=recommendation.run_ref,
        status="ready",
        original_recommendation=recommendation.original_recommendation,
        public_options=recommendation.public_options,
        image_types=tuple(recommendation.image_types),
        audit={"source": "synthetic_mock", "image_analysis_performed": False},
        state=recommendation.state,
        form_ref=recommendation.form_ref,
        valid_until=valid_until,
        result_digest_version=1,
        result_digest="00" * 32,
    )
    fields = callback.model_dump(mode="python")
    fields["result_digest"] = recommendation_callback_digest(callback)
    return RecommendationResultCallback.model_validate(fields, strict=True)


def test_postgres_recommendation_callback_is_run_bound_replay_safe_and_preserves_state(
    pg_context: _PGContext,
) -> None:
    _, task_id = _create_task(pg_context, reference_count=2)
    with Session(pg_context.engine) as db, db.begin():
        task = db.get(Task, task_id)
        assert task is not None
        recommendation = db.get(Recommendation, task.recommendation_id)
        assert recommendation is not None
        recommendation.status = "analyzing"
        recommendation.state = {**recommendation.state, "existing_private_key": "preserved"}
        reference_set = db.get(ReferenceSet, recommendation.reference_set_id)
        assert reference_set is not None
        valid_until = min(
            recommendation.data_expires_at,
            reference_set.expires_at,
        ) - timedelta(hours=1)
        callback = _ready_recommendation_callback(recommendation, valid_until=valid_until)

    service = RecommendationCallbackService(pg_context.engine)
    assert service.accept(callback) == "accepted"
    assert service.accept(callback) == "replayed"
    changed_fields = callback.model_dump(mode="python")
    changed_fields["state"] = {**callback.state, "changed_synthetic_field": True}
    changed = RecommendationResultCallback.model_validate(
        {**changed_fields, "result_digest": "00" * 32}, strict=True
    )
    changed_fields = changed.model_dump(mode="python")
    changed_fields["result_digest"] = recommendation_callback_digest(changed)
    conflicting = RecommendationResultCallback.model_validate(changed_fields, strict=True)
    with pytest.raises(DomainError) as conflict:
        service.accept(conflicting)
    assert conflict.value.status_code == 409
    with Session(pg_context.engine) as db:
        saved = db.get(Recommendation, callback.recommendation_id)
        assert saved is not None and saved.status == "ready"
        assert saved.state["existing_private_key"] == "preserved"
        assert (
            saved.state["trusted_analysis_snapshot"] == callback.state["trusted_analysis_snapshot"]
        )
        assert saved.state["r3_recommendation_result_callback"]["result_digest"] == (
            callback.result_digest
        )


def test_postgres_recommendation_callback_rejects_expired_validity_and_wrong_run(
    pg_context: _PGContext,
) -> None:
    _, task_id = _create_task(pg_context, reference_count=1)
    now = _database_now(pg_context)
    with Session(pg_context.engine) as db, db.begin():
        task = db.get(Task, task_id)
        assert task is not None
        recommendation = db.get(Recommendation, task.recommendation_id)
        reference_set = db.get(ReferenceSet, task.reference_set_id)
        assert recommendation is not None and reference_set is not None
        recommendation.status = "analyzing"
        callback = _ready_recommendation_callback(
            recommendation,
            valid_until=min(recommendation.data_expires_at, reference_set.expires_at)
            + timedelta(days=1),
        )

    with pytest.raises(DomainError) as expired:
        RecommendationCallbackService(pg_context.engine).accept(callback)
    assert expired.value.status_code == 422

    fields = callback.model_dump(mode="python")
    fields["valid_until"] = now + timedelta(hours=1)
    fields["run_ref"] = "different-synthetic-run-ref"
    changed = RecommendationResultCallback.model_validate(
        {**fields, "result_digest": "00" * 32}, strict=True
    )
    fields = changed.model_dump(mode="python")
    fields["result_digest"] = recommendation_callback_digest(changed)
    wrong_run = RecommendationResultCallback.model_validate(fields, strict=True)
    with pytest.raises(DomainError) as stale:
        RecommendationCallbackService(pg_context.engine).accept(wrong_run)
    assert stale.value.status_code == 409
    with Session(pg_context.engine) as db:
        saved = db.get(Recommendation, wrong_run.recommendation_id)
        assert saved is not None and saved.status == "analyzing"
        assert "r3_recommendation_result_callback" not in saved.state


def test_postgres_prompt_callback_rejects_wrong_phase_without_mutation(
    pg_context: _PGContext,
) -> None:
    _, task_id, submission_id = _prepare_submitted_initial(pg_context)
    callback = _accept_needs_input(
        pg_context,
        task_id=task_id,
        submission_id=submission_id,
    )

    with pytest.raises(DomainError) as mismatch:
        PromptCallbackService(pg_context.engine).accept(callback, phase="followup_answer")
    assert mismatch.value.status_code == 409
    with Session(pg_context.engine) as db:
        task = db.get(Task, task_id)
        submission = db.get(PromptSubmission, submission_id)
        workflow = db.execute(
            select(TaskPromptWorkflow).where(TaskPromptWorkflow.task_id == task_id)
        ).scalar_one()
        assert task is not None and task.status == "prompting"
        assert submission is not None and submission.callback_result_digest is None
        assert workflow.status == "submitted"


@pytest.mark.parametrize("identity_field", ["task_id", "workflow_run_id", "workflow_revision"])
def test_postgres_prompt_callback_rejects_cross_task_or_run_binding(
    pg_context: _PGContext,
    identity_field: str,
) -> None:
    _, task_id, submission_id = _prepare_submitted_initial(pg_context)
    callback = _accept_needs_input(pg_context, task_id=task_id, submission_id=submission_id)
    _, other_task_id = _create_task(pg_context, reference_count=1)
    substitutions = {
        "task_id": other_task_id,
        "workflow_run_id": UUID(int=0),
        "workflow_revision": callback.workflow_revision + 1,
    }
    forged = _rebind_prompt_callback(
        callback,
        **{identity_field: substitutions[identity_field]},
    )

    with pytest.raises(DomainError) as rejected:
        PromptCallbackService(pg_context.engine).accept(forged, phase="initial_preferences")
    assert rejected.value.status_code == 409
    with Session(pg_context.engine) as db:
        saved = db.get(PromptSubmission, submission_id)
        task = db.get(Task, task_id)
        assert saved is not None and saved.callback_result_digest is None
        assert task is not None and task.status == "prompting"


def test_postgres_prompt_callback_replay_is_idempotent_and_changed_digest_conflicts(
    pg_context: _PGContext,
) -> None:
    _, task_id, submission_id = _prepare_submitted_initial(pg_context)
    callback = _accept_needs_input(pg_context, task_id=task_id, submission_id=submission_id)
    service = PromptCallbackService(pg_context.engine)

    first = service.accept(callback, phase="initial_preferences")
    replay = service.accept(callback, phase="initial_preferences")
    changed = _accept_needs_input(
        pg_context,
        task_id=task_id,
        submission_id=submission_id,
        valid_until=callback.valid_until + timedelta(minutes=1),
    )
    with pytest.raises(DomainError) as conflict:
        service.accept(changed, phase="initial_preferences")

    assert first.status == "needs_input"
    assert replay.status == "needs_input"
    assert conflict.value.status_code == 409
    with Session(pg_context.engine) as db:
        saved = db.get(PromptSubmission, submission_id)
        assert saved is not None and saved.callback_result_digest == callback.result_digest


def test_postgres_late_prompt_callback_cannot_reopen_terminal_task(
    pg_context: _PGContext,
) -> None:
    _, task_id, submission_id = _prepare_submitted_initial(pg_context)
    callback = _accept_needs_input(pg_context, task_id=task_id, submission_id=submission_id)
    with Session(pg_context.engine) as db, db.begin():
        task = db.get(Task, task_id)
        workflow = db.execute(
            select(TaskPromptWorkflow).where(TaskPromptWorkflow.task_id == task_id)
        ).scalar_one()
        assert task is not None
        now = db.scalar(select(func.clock_timestamp()))
        task.status = "failed"
        task.terminal_at = now
        task.expires_at = now + timedelta(days=task.retention_days)
        workflow.status = "failed"

    with pytest.raises(DomainError) as terminal:
        PromptCallbackService(pg_context.engine).accept(callback, phase="initial_preferences")
    assert terminal.value.status_code == 409
    with Session(pg_context.engine) as db:
        task = db.get(Task, task_id)
        submission = db.get(PromptSubmission, submission_id)
        workflow = db.execute(
            select(TaskPromptWorkflow).where(TaskPromptWorkflow.task_id == task_id)
        ).scalar_one()
        assert task is not None and task.status == "failed"
        assert submission is not None and submission.callback_result_digest is None
        assert workflow.status == "failed"


def test_postgres_expired_open_question_terminally_fails_without_submit_action(
    pg_context: _PGContext,
) -> None:
    owner_id, task_id, initial_submission_id = _prepare_submitted_initial(pg_context)
    callback = _accept_needs_input(pg_context, task_id=task_id, submission_id=initial_submission_id)
    PromptCallbackService(pg_context.engine).accept(callback, phase="initial_preferences")
    with Session(pg_context.engine) as db, db.begin():
        submission = db.get(PromptSubmission, initial_submission_id)
        assert submission is not None
        now = db.scalar(select(func.clock_timestamp()))
        submission.callback_valid_until = now - timedelta(seconds=1)

    request = PromptPreferenceRequest.model_validate(
        {
            "workflow_run_id": callback.workflow_run_id,
            "workflow_revision": callback.workflow_revision,
            "question_round": 1,
            "answers": {"language": {"mode": "C"}},
        },
        strict=True,
    )
    with pytest.raises(DomainError) as expired:
        PromptPreferenceService(pg_context.engine).submit(
            owner_user_id=owner_id,
            task_id=task_id,
            idempotency_key="r3-expired-question-0001",
            request=request,
        )
    assert expired.value.status_code == 410
    with Session(pg_context.engine) as db:
        task = db.get(Task, task_id)
        submission = db.get(PromptSubmission, initial_submission_id)
        items = db.scalars(select(TaskItem).where(TaskItem.task_id == task_id)).all()
        assert task is not None and task.status == "failed"
        assert submission is not None and submission.status == "failed"
        assert submission.callback_reason_code == "PROMPT_QUESTION_EXPIRED"
        assert items and all(item.status == "failed" for item in items)
        assert (
            db.scalar(
                select(func.count(PromptSubmission.submission_id)).where(
                    PromptSubmission.task_id == task_id,
                    PromptSubmission.phase == "followup_answer",
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(Action.id)).where(
                    Action.scope_kind == "submission",
                    Action.task_id == task_id,
                    Action.action_kind == "prompt.followup.submit",
                )
            )
            == 0
        )


def test_postgres_third_needs_input_terminates_without_generation_intents(
    pg_context: _PGContext,
) -> None:
    owner_id, task_id, initial_submission_id = _prepare_submitted_initial(pg_context)
    initial_callback = _accept_needs_input(
        pg_context, task_id=task_id, submission_id=initial_submission_id
    )
    PromptCallbackService(pg_context.engine).accept(initial_callback, phase="initial_preferences")

    first_request = PromptPreferenceRequest.model_validate(
        {
            "workflow_run_id": initial_callback.workflow_run_id,
            "workflow_revision": initial_callback.workflow_revision,
            "question_round": 1,
            "answers": {"language": {"mode": "B"}},
        },
        strict=True,
    )
    first_submission = PromptPreferenceService(pg_context.engine).submit(
        owner_user_id=owner_id,
        task_id=task_id,
        idempotency_key="r3-round-one-answer",
        request=first_request,
    )
    adapter = create_test_prompt_workflow_adapter(
        session_factory=lambda: Session(pg_context.engine)
    )
    assert (
        _process_submission_action(
            pg_context,
            task_id=task_id,
            submission_id=first_submission.submission_id,
            sequence=1,
            adapter=adapter,
        )
        == "accepted"
    )
    second_callback = _accept_needs_input(
        pg_context,
        task_id=task_id,
        submission_id=first_submission.submission_id,
        round_number=2,
        allowed=("usage_scene",),
        locked=("language", "platform", "visual_style", "selling_points"),
    )
    PromptCallbackService(pg_context.engine).accept(second_callback, phase="followup_answer")

    second_request = PromptPreferenceRequest.model_validate(
        {
            "workflow_run_id": second_callback.workflow_run_id,
            "workflow_revision": second_callback.workflow_revision,
            "question_round": 2,
            "answers": {"usage_scene": {"mode": "C"}},
        },
        strict=True,
    )
    second_submission = PromptPreferenceService(pg_context.engine).submit(
        owner_user_id=owner_id,
        task_id=task_id,
        idempotency_key="r3-round-two-answer",
        request=second_request,
    )
    assert (
        _process_submission_action(
            pg_context,
            task_id=task_id,
            submission_id=second_submission.submission_id,
            sequence=1,
            adapter=adapter,
        )
        == "accepted"
    )
    callback = _accept_needs_input(
        pg_context,
        task_id=task_id,
        submission_id=second_submission.submission_id,
        round_number=3,
        allowed=("visual_style",),
        locked=("language", "platform", "usage_scene", "selling_points"),
    )

    accepted = PromptCallbackService(pg_context.engine).accept(callback, phase="followup_answer")

    assert accepted.status == "rejected"
    assert accepted.safe_reason_code == "NEEDS_INPUT_LIMIT"
    with Session(pg_context.engine) as db:
        task = db.get(Task, task_id)
        submission = db.get(PromptSubmission, second_submission.submission_id)
        items = db.scalars(select(TaskItem).where(TaskItem.task_id == task_id)).all()
        workflow = db.execute(
            select(TaskPromptWorkflow).where(TaskPromptWorkflow.task_id == task_id)
        ).scalar_one()
        assert task is not None and task.status == "failed"
        assert task.terminal_at is not None and task.expires_at is not None
        assert submission is not None and submission.status == "failed"
        assert submission.callback_reason_code == "NEEDS_INPUT_LIMIT"
        assert submission.callback_round is None
        assert workflow.status == "failed"
        assert items and all(item.status == "failed" for item in items)
        assert all(item.error_code == "PROMPT_WORKFLOW_FAILED" for item in items)
        assert (
            db.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.event_type == GENERATION_START_EVENT_TYPE,
                    OutboxEvent.aggregate_id.in_(
                        select(TaskItem.id).where(TaskItem.task_id == task_id)
                    ),
                )
            )
            == 0
        )


def test_postgres_preference_submission_is_idempotent_and_creates_followup_action(
    pg_context: _PGContext,
) -> None:
    owner_id, task_id, submission_id = _prepare_submitted_initial(pg_context)
    callback = _accept_needs_input(
        pg_context,
        task_id=task_id,
        submission_id=submission_id,
    )
    accepted_callback = PromptCallbackService(pg_context.engine).accept(
        callback, phase="initial_preferences"
    )
    assert accepted_callback.status == "needs_input"

    request = PromptPreferenceRequest.model_validate(
        {
            "workflow_run_id": callback.workflow_run_id,
            "workflow_revision": callback.workflow_revision,
            "question_round": 1,
            "answers": {"language": {"mode": "B"}},
        },
        strict=True,
    )
    service = PromptPreferenceService(pg_context.engine)
    first = service.submit(
        owner_user_id=owner_id,
        task_id=task_id,
        idempotency_key="r3-preference-answer-0001",
        request=request,
    )
    replay = service.submit(
        owner_user_id=owner_id,
        task_id=task_id,
        idempotency_key="r3-preference-answer-0001",
        request=request,
    )
    changed_request = PromptPreferenceRequest.model_validate(
        {
            "workflow_run_id": callback.workflow_run_id,
            "workflow_revision": callback.workflow_revision,
            "question_round": 1,
            "answers": {"language": {"mode": "C"}},
        },
        strict=True,
    )
    with pytest.raises(DomainError) as changed_key:
        service.submit(
            owner_user_id=owner_id,
            task_id=task_id,
            idempotency_key="r3-preference-answer-0001",
            request=changed_request,
        )

    assert first.status == "accepted"
    assert replay.status == "replayed"
    assert changed_key.value.status_code == 409
    assert first.submission_id == replay.submission_id
    with Session(pg_context.engine) as db:
        task = db.get(Task, task_id)
        workflow = db.execute(
            select(TaskPromptWorkflow).where(TaskPromptWorkflow.task_id == task_id)
        ).scalar_one()
        submission = db.get(PromptSubmission, first.submission_id)
        assert task is not None and task.status == "prompting"
        assert task.resolved_preferences["language"]["mode"] == "B"
        assert workflow.status == "pending_submission"
        assert submission is not None and submission.phase == "followup_answer"
        assert submission.private_payload["followup_answers"] == {"language": {"mode": "B"}}
        action = db.execute(
            select(Action).where(
                Action.scope_id == first.submission_id,
                Action.sequence == 1,
            )
        ).scalar_one()
        assert action.action_kind == "prompt.followup.submit"
        assert action.input_digest == submission.content_digest


def test_postgres_concurrent_preference_keys_create_only_one_round_submission(
    pg_context: _PGContext,
) -> None:
    owner_id, task_id, initial_submission_id = _prepare_submitted_initial(pg_context)
    callback = _accept_needs_input(pg_context, task_id=task_id, submission_id=initial_submission_id)
    PromptCallbackService(pg_context.engine).accept(callback, phase="initial_preferences")
    request = PromptPreferenceRequest.model_validate(
        {
            "workflow_run_id": callback.workflow_run_id,
            "workflow_revision": callback.workflow_revision,
            "question_round": 1,
            "answers": {"language": {"mode": "B"}},
        },
        strict=True,
    )
    barrier = Barrier(2)

    def submit_with_key(key: str):
        barrier.wait(timeout=5)
        try:
            return PromptPreferenceService(pg_context.engine).submit(
                owner_user_id=owner_id,
                task_id=task_id,
                idempotency_key=key,
                request=request,
            )
        except DomainError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit_with_key, ("r3-concurrent-one", "r3-concurrent-two")))

    accepted = [result for result in results if isinstance(result, PromptPreferenceAccepted)]
    conflicts = [result for result in results if isinstance(result, DomainError)]
    assert len(accepted) == 1
    assert len(conflicts) == 1 and conflicts[0].status_code == 409
    with Session(pg_context.engine) as db:
        assert (
            db.scalar(
                select(func.count(PromptSubmission.submission_id)).where(
                    PromptSubmission.task_id == task_id,
                    PromptSubmission.phase == "followup_answer",
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(Action.id)).where(
                    Action.task_id == task_id,
                    Action.action_kind == "prompt.followup.submit",
                )
            )
            == 1
        )


def test_postgres_followup_action_preserves_saved_answers_and_round(
    pg_context: _PGContext,
) -> None:
    owner_id, task_id, initial_submission_id = _prepare_submitted_initial(pg_context)
    callback = _accept_needs_input(pg_context, task_id=task_id, submission_id=initial_submission_id)
    PromptCallbackService(pg_context.engine).accept(callback, phase="initial_preferences")
    request = PromptPreferenceRequest.model_validate(
        {
            "workflow_run_id": callback.workflow_run_id,
            "workflow_revision": callback.workflow_revision,
            "question_round": 1,
            "answers": {"language": {"mode": "C"}},
        },
        strict=True,
    )
    followup = PromptPreferenceService(pg_context.engine).submit(
        owner_user_id=owner_id,
        task_id=task_id,
        idempotency_key="r3-followup-mock-round-0001",
        request=request,
    )
    adapter = create_test_prompt_workflow_adapter(
        session_factory=lambda: Session(pg_context.engine)
    )

    outcome = _process_submission_action(
        pg_context,
        task_id=task_id,
        submission_id=followup.submission_id,
        sequence=1,
        adapter=adapter,
        mock_result_callback_service=PromptCallbackService(pg_context.engine),
    )

    assert outcome == "accepted"
    with Session(pg_context.engine) as db:
        submission = db.get(PromptSubmission, followup.submission_id)
        mock = db.get(MockPromptSubmission, followup.submission_id)
        assert submission is not None and submission.followup_round == 1
        assert submission.private_payload["followup_answers"] == {"language": {"mode": "C"}}
        assert submission.private_payload["resolved_preferences"]["language"]["mode"] == "C"
        assert mock is not None and mock.content_digest == submission.content_digest
        assert mock.result_package is not None
        query_action = db.execute(
            select(Action).where(
                Action.scope_kind == "submission",
                Action.scope_id == followup.submission_id,
                Action.sequence == 2,
            )
        ).scalar_one()
        assert query_action.action_kind == PROMPT_MOCK_RESULT_QUERY_ACTION_KIND
        assert query_action.status == "scheduled"
        assert query_action.input_digest == submission.content_digest


def test_postgres_preference_submission_fails_closed_for_locked_fields(
    pg_context: _PGContext,
) -> None:
    owner_id, task_id, submission_id = _prepare_submitted_initial(pg_context)
    callback = _accept_needs_input(pg_context, task_id=task_id, submission_id=submission_id)
    PromptCallbackService(pg_context.engine).accept(callback, phase="initial_preferences")
    request = PromptPreferenceRequest.model_validate(
        {
            "workflow_run_id": callback.workflow_run_id,
            "workflow_revision": callback.workflow_revision,
            "question_round": 1,
            "answers": {"platform": "TEMU"},
        },
        strict=True,
    )
    with pytest.raises(DomainError) as locked:
        PromptPreferenceService(pg_context.engine).submit(
            owner_user_id=owner_id,
            task_id=task_id,
            idempotency_key="r3-locked-preference-0001",
            request=request,
        )
    assert locked.value.status_code == 409
    with Session(pg_context.engine) as db:
        assert (
            db.scalar(
                select(func.count(PromptSubmission.submission_id)).where(
                    PromptSubmission.task_id == task_id,
                    PromptSubmission.phase == "followup_answer",
                )
            )
            == 0
        )


def test_postgres_preference_request_from_other_owner_is_not_revealed(
    pg_context: _PGContext,
) -> None:
    owner_id, task_id, submission_id = _prepare_submitted_initial(pg_context)
    callback = _accept_needs_input(pg_context, task_id=task_id, submission_id=submission_id)
    PromptCallbackService(pg_context.engine).accept(callback, phase="initial_preferences")
    request = PromptPreferenceRequest.model_validate(
        {
            "workflow_run_id": callback.workflow_run_id,
            "workflow_revision": callback.workflow_revision,
            "question_round": 1,
            "answers": {"language": {"mode": "B"}},
        },
        strict=True,
    )
    with pytest.raises(DomainError) as missing:
        PromptPreferenceService(pg_context.engine).submit(
            owner_user_id=UUID(int=0),
            task_id=task_id,
            idempotency_key="r3-other-owner-0001",
            request=request,
        )
    assert missing.value.status_code == 404
    assert owner_id != UUID(int=0)
