from __future__ import annotations

import copy
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.domain_errors import DomainError
from app.db.action_models import Action
from app.db.prompt_models import PromptSubmission, TaskPromptWorkflow
from app.db.task_models import Task
from app.schemas.prompt_preferences import (
    PromptPreferenceAccepted,
    PromptPreferenceRequest,
    valid_idempotency_key,
)
from app.schemas.prompt_workflow import (
    PROMPT_CONTENT_DIGEST_VERSION,
    PromptSubmissionCommand,
    json_safe_value,
)
from app.schemas.recommendations import RecommendationPublicOptions
from app.schemas.tasks import CustomPreference, PreferenceMode
from app.services.prompt_workflows import (
    PROMPT_FOLLOWUP_SUBMIT_ACTION_KIND,
    PromptWorkflowUnavailable,
    _command_from_submission,
    _db_now,
    _fail_prompt_task_items,
    _load_task_pinned_analysis,
    _lock_task_parent_chain,
    _submission_digest,
)
from app.workers.action_processor import schedule_action_in_transaction

_FIELD_TO_RESOLVED = {
    "language": "language",
    "platform": "platform",
    "usage_scene": "usage_scene",
    "visual_style": "visual_style",
    "selling_points": "selling_points",
}
_AGENT_DESIGN_INSTRUCTION = "由智能体自由设计，不得将未提供的信息表述为事实。"


class PromptPreferenceService:
    """Persist one allowed follow-up answer and its submission Action atomically."""

    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

    def submit(
        self,
        *,
        owner_user_id: UUID,
        task_id: UUID,
        idempotency_key: str,
        request: PromptPreferenceRequest,
    ) -> PromptPreferenceAccepted:
        if not valid_idempotency_key(idempotency_key):
            raise DomainError(400, "IDEMPOTENCY_KEY_INVALID", "幂等键格式无效")

        response: PromptPreferenceAccepted | None = None
        expired = False
        try:
            with self._sessions() as db, db.begin():
                task = _lock_task_parent_chain(db, task_id)
                if task is None or task.owner_user_id != owner_user_id:
                    raise DomainError(404, "TASK_NOT_FOUND", "任务不可用")

                workflow = db.execute(
                    select(TaskPromptWorkflow)
                    .where(TaskPromptWorkflow.task_id == task.id)
                    .order_by(TaskPromptWorkflow.workflow_revision.desc())
                    .limit(1)
                    .with_for_update()
                ).scalar_one_or_none()
                if workflow is None:
                    raise DomainError(409, "PROMPT_WORKFLOW_UNAVAILABLE", "当前问题不可处理")

                submission_id = uuid5(task.id, f"prompt-preferences:{idempotency_key}")
                existing_by_key = db.execute(
                    select(PromptSubmission)
                    .where(
                        PromptSubmission.submission_id == submission_id,
                        PromptSubmission.task_id == task.id,
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                answers = _normalized_answers(request)
                if existing_by_key is not None:
                    if (
                        existing_by_key.workflow_run_id != request.workflow_run_id
                        or existing_by_key.workflow_revision != request.workflow_revision
                        or existing_by_key.phase != "followup_answer"
                        or existing_by_key.followup_round != request.question_round
                        or not isinstance(existing_by_key.private_payload, dict)
                        or existing_by_key.private_payload.get("followup_answers") != answers
                    ):
                        raise DomainError(
                            409,
                            "PROMPT_PREFERENCE_CONFLICT",
                            "偏好提交与已保存请求冲突",
                        )
                    _assert_saved_followup_submission(db, existing_by_key)
                    response = _preference_response(existing_by_key, status="replayed")
                elif (
                    workflow.workflow_run_id != request.workflow_run_id
                    or workflow.workflow_revision != request.workflow_revision
                ):
                    raise DomainError(409, "PROMPT_WORKFLOW_STALE", "当前问题版本已变化")
                else:
                    now = _db_now(db)
                    if (
                        task.terminal_at is not None
                        or task.expires_at is not None
                        or task.status != "needs_input"
                        or workflow.status != "needs_input"
                    ):
                        raise DomainError(409, "PROMPT_WORKFLOW_STALE", "当前问题不可处理")

                    current_submission = db.execute(
                        select(PromptSubmission)
                        .where(
                            PromptSubmission.workflow_id == workflow.id,
                            PromptSubmission.callback_outcome == "needs_input",
                            PromptSubmission.status == "callback_received",
                        )
                        .order_by(PromptSubmission.followup_round.desc())
                        .limit(1)
                        .with_for_update()
                    ).scalar_one_or_none()
                    if (
                        current_submission is None
                        or current_submission.workflow_run_id != workflow.workflow_run_id
                        or current_submission.workflow_revision != workflow.workflow_revision
                        or current_submission.callback_round != request.question_round
                    ):
                        raise DomainError(409, "PROMPT_WORKFLOW_STALE", "当前问题版本已变化")
                    if (
                        current_submission.callback_valid_until is None
                        or current_submission.callback_valid_until <= now
                    ):
                        self._expire_question(db, task, workflow, current_submission, now)
                        expired = True
                    else:
                        opened = set(current_submission.callback_allowed_fields or [])
                        locked = set(current_submission.callback_locked_fields or [])
                        if (
                            not opened
                            or opened & locked
                            or not set(answers).issubset(opened - locked)
                        ):
                            raise DomainError(
                                409,
                                "PROMPT_FIELDS_NOT_ALLOWED",
                                "本轮问题不允许修改这些字段",
                            )
                        try:
                            snapshot = _load_task_pinned_analysis(task)
                            if (
                                workflow.trusted_analysis_snapshot_id != snapshot.snapshot_id
                                or workflow.trusted_analysis_snapshot_digest
                                != bytes.fromhex(snapshot.snapshot_digest)
                            ):
                                raise PromptWorkflowUnavailable
                            payload = self._build_followup_payload(task, answers)
                            digest = _submission_digest(
                                "followup_answer", request.question_round, payload
                            )
                            workflow_id = workflow.id
                            command = PromptSubmissionCommand(
                                task_id=task.id,
                                workflow_run_id=workflow.workflow_run_id,
                                workflow_revision=workflow.workflow_revision,
                                submission_id=submission_id,
                                phase="followup_answer",
                                followup_round=request.question_round,
                                content_digest_version=PROMPT_CONTENT_DIGEST_VERSION,
                                content_digest=digest,
                                private_payload=payload,
                            )
                        except (PromptWorkflowUnavailable, TypeError, ValueError):
                            raise DomainError(
                                409, "PROMPT_WORKFLOW_UNAVAILABLE", "当前问题不可处理"
                            ) from None

                        occupied_round = db.execute(
                            select(PromptSubmission)
                            .where(
                                PromptSubmission.workflow_id == workflow.id,
                                PromptSubmission.phase == "followup_answer",
                                PromptSubmission.followup_round == request.question_round,
                            )
                            .with_for_update()
                        ).scalar_one_or_none()
                        if occupied_round is not None:
                            raise DomainError(
                                409,
                                "PROMPT_PREFERENCE_CONFLICT",
                                "本轮偏好已提交",
                            )

                        saved = PromptSubmission(
                            submission_id=command.submission_id,
                            workflow_id=workflow_id,
                            task_id=task.id,
                            workflow_run_id=workflow.workflow_run_id,
                            workflow_revision=workflow.workflow_revision,
                            phase="followup_answer",
                            followup_round=request.question_round,
                            content_digest_version=command.content_digest_version,
                            content_digest=command.content_digest,
                            private_payload=payload,
                            status="prepared",
                            created_at=now,
                            updated_at=now,
                        )
                        db.add(saved)
                        db.flush()
                        schedule_action_in_transaction(
                            db,
                            action_id=uuid5(submission_id, "action:1"),
                            task_id=task.id,
                            scope_kind="submission",
                            scope_id=submission_id,
                            action_kind=PROMPT_FOLLOWUP_SUBMIT_ACTION_KIND,
                            sequence=1,
                            due_at=now,
                            input_digest_version=command.content_digest_version,
                            input_digest=command.content_digest,
                        )
                        task.resolved_preferences = payload["resolved_preferences"]
                        task.status = "prompting"
                        task.updated_at = now
                        workflow.status = "pending_submission"
                        workflow.updated_at = now
                        response = _preference_response(saved, status="accepted")
        except DomainError:
            raise
        except IntegrityError:
            raise DomainError(
                409, "PROMPT_PREFERENCE_CONFLICT", "偏好提交状态冲突，请重新读取"
            ) from None
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "偏好提交暂时无法保存") from None

        if expired:
            raise DomainError(410, "PROMPT_QUESTION_EXPIRED", "本轮问题已过期")
        if response is None:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "偏好提交暂时无法保存")
        return response

    @staticmethod
    def _build_followup_payload(task: Task, answers: dict[str, object]) -> dict[str, object]:
        saved = task.recommendation_snapshot
        if not isinstance(saved, dict):
            raise PromptWorkflowUnavailable
        original = saved.get("original_recommendation")
        options_value = saved.get("public_options")
        if not isinstance(original, dict) or not isinstance(task.resolved_preferences, dict):
            raise PromptWorkflowUnavailable
        try:
            options = RecommendationPublicOptions.model_validate(options_value)
        except Exception:
            raise PromptWorkflowUnavailable from None

        updated = copy.deepcopy(task.resolved_preferences)
        for field_name, answer in answers.items():
            if field_name == "platform":
                if not isinstance(answer, str) or answer not in options.platform:
                    raise DomainError(422, "PLATFORM_NOT_ALLOWED", "所选平台不在当前推荐范围内")
                updated["platform"] = {"mode": "platform", "value": answer}
                continue

            option = getattr(options, field_name, None)
            if option is None:
                raise PromptWorkflowUnavailable
            if isinstance(answer, dict):
                mode = answer.get("mode")
                if mode in {"A", "B", "C"}:
                    index = ord(mode) - ord("A")
                    original_fields = original.get("field_texts")
                    texts = (
                        original_fields.get(field_name)
                        if isinstance(original_fields, dict)
                        else None
                    )
                    if (
                        not isinstance(texts, list)
                        or len(texts) != 3
                        or any(not isinstance(text, str) or not text for text in texts)
                        or option.choices[index].text != texts[index]
                    ):
                        raise PromptWorkflowUnavailable
                    updated[field_name] = {
                        "mode": mode,
                        "text": texts[index],
                        "source": "recommendation_original",
                        "recommendation_revision": task.recommendation_revision,
                    }
                elif mode == "D" and option.allow_agent_design:
                    updated[field_name] = {
                        "mode": "D",
                        "kind": "agent_design",
                        "instruction": _AGENT_DESIGN_INSTRUCTION,
                    }
                elif mode == "custom" and option.allow_custom:
                    text = answer.get("text")
                    if not isinstance(text, str) or not text.strip():
                        raise PromptWorkflowUnavailable
                    updated[field_name] = {"mode": "custom", "text": text}
                else:
                    raise DomainError(422, "PREFERENCE_NOT_ALLOWED", "当前推荐不支持此偏好选项")
            else:
                raise PromptWorkflowUnavailable

        snapshot = saved.get("trusted_analysis_snapshot")
        if not isinstance(snapshot, dict):
            raise PromptWorkflowUnavailable
        return {
            "category_snapshot": task.category_snapshot,
            "image_types": list(task.image_types),
            "resolved_preferences": updated,
            "trusted_analysis_snapshot": json_safe_value(snapshot),
            "followup_answers": answers,
        }

    @staticmethod
    def _expire_question(
        db: Session,
        task: Task,
        workflow: TaskPromptWorkflow,
        submission: PromptSubmission,
        now,
    ) -> None:
        submission.status = "failed"
        submission.callback_reason_code = "PROMPT_QUESTION_EXPIRED"
        submission.callback_round = None
        submission.callback_allowed_fields = None
        submission.callback_locked_fields = None
        submission.callback_valid_until = None
        submission.updated_at = now
        workflow.status = "failed"
        _fail_prompt_task_items(db, task, error_code="PROMPT_WORKFLOW_FAILED")
        workflow.updated_at = now


def _normalized_answers(request: PromptPreferenceRequest) -> dict[str, object]:
    result: dict[str, object] = {}
    for field, answer in request.answers.items():
        if isinstance(answer, (PreferenceMode, CustomPreference)):
            result[field] = answer.model_dump(mode="json")
        elif isinstance(answer, str):
            result[field] = answer
        else:
            raise DomainError(422, "PREFERENCE_INVALID", "偏好选项无效")
    return result


def _assert_saved_followup_submission(db: Session, submission: PromptSubmission) -> None:
    try:
        command = _command_from_submission(submission)
    except PromptWorkflowUnavailable:
        raise DomainError(409, "PROMPT_PREFERENCE_CONFLICT", "偏好提交状态冲突") from None
    if (
        command.phase != "followup_answer"
        or command.followup_round not in {1, 2}
        or command.private_payload.get("followup_answers") is None
    ):
        raise DomainError(409, "PROMPT_PREFERENCE_CONFLICT", "偏好提交状态冲突")
    action = db.execute(
        select(Action).where(
            Action.scope_kind == "submission",
            Action.scope_id == submission.submission_id,
            Action.sequence == 1,
        )
    ).scalar_one_or_none()
    if (
        action is None
        or action.task_id != submission.task_id
        or action.action_kind != PROMPT_FOLLOWUP_SUBMIT_ACTION_KIND
        or action.input_digest_version != submission.content_digest_version
        or action.input_digest != submission.content_digest
    ):
        raise DomainError(409, "PROMPT_PREFERENCE_CONFLICT", "偏好提交状态冲突")


def _preference_response(
    submission: PromptSubmission,
    *,
    status: str,
) -> PromptPreferenceAccepted:
    return PromptPreferenceAccepted(
        status=status,  # type: ignore[arg-type]
        task_id=submission.task_id,
        submission_id=submission.submission_id,
        workflow_run_id=submission.workflow_run_id,
        workflow_revision=submission.workflow_revision,
        question_round=submission.followup_round,
    )
