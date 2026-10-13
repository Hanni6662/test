from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal
from uuid import UUID

from app.db.prompt_models import MockPromptSubmission
from app.db.recommendation_models import MockRecommendationRun
from app.integrations.dify.contracts import (
    DifyBusinessConflict,
    DifyNotConfigured,
    DifyPromptSubmissionConflict,
    DifyPromptWorkflowPort,
    DifyRecommendationPort,
    PreferenceSubmissionResult,
    PromptResult,
    RecommendationRun,
    ReferenceAssetSummary,
)
from app.schemas.prompt_packages import PromptPackage
from app.schemas.prompt_workflow import (
    PROMPT_RESULT_DIGEST_VERSION,
    PromptCallback,
    PromptRecoveryResult,
    PromptSubmissionCommand,
    PromptSubmitResult,
    canonical_sha256,
    json_safe_value,
    prompt_callback_digest_payload,
)
from app.schemas.recommendations import IMAGE_TYPES, PLATFORMS
from app.services.trusted_analysis import (
    TRUSTED_ANALYSIS_STATE_KEY,
    TrustedAnalysisSnapshotError,
    build_trusted_analysis_snapshot,
    load_trusted_analysis_snapshot,
    reference_asset_payload,
    trusted_analysis_state,
)
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

MockOutcome = Literal["analyzing", "ready", "failed"]

_MOCK_CHOICES: dict[str, tuple[str, str, str]] = {
    "language": (
        "使用简体中文，文案准确简洁。",
        "Use concise English copy with clear wording.",
        "不添加新文案，以纯视觉展示商品。",
    ),
    "usage_scene": (
        "在日常使用场景中展示商品与使用动作。",
        "以简洁中性场景呈现商品的典型用途。",
        "使用清晰背景突出商品外观与摆放方式。",
    ),
    "visual_style": (
        "采用明亮、克制且清楚易读的商业视觉。",
        "采用自然生活方式风格与柔和光线。",
        "采用简洁的信息分区和明确视觉层级。",
    ),
    "selling_points": (
        "突出可核验的外观与结构，不添加未证实信息。",
        "围绕已知用途和可见细节组织卖点表达。",
        "避免未提供的性能、认证、材质和尺寸描述。",
    ),
}


def build_mock_options() -> dict[str, object]:
    fields: dict[str, object] = {}
    for field_name, texts in _MOCK_CHOICES.items():
        fields[field_name] = {
            "choices": [
                {"code": code, "text": text}
                for code, text in zip(("A", "B", "C"), texts, strict=True)
            ],
            "allow_agent_design": True,
            "allow_custom": True,
        }
    fields["platform"] = list(PLATFORMS)
    return fields


def build_mock_original(category: str, options: dict[str, object]) -> dict[str, object]:
    """Return the complete fixed fixture text, not a claim of image analysis."""
    original_fields: dict[str, list[str]] = {}
    for name, value in options.items():
        if isinstance(value, dict):
            choices = value.get("choices")
            if isinstance(choices, list):
                original_fields[name] = [
                    choice["text"]
                    for choice in choices
                    if isinstance(choice, dict) and isinstance(choice.get("text"), str)
                ]
        elif isinstance(value, list):
            original_fields[name] = [item for item in value if isinstance(item, str)]
    return {
        "fixture_id": "synthetic-recommendation-v1",
        "category_snapshot": category,
        "field_texts": original_fields,
        "image_types": list(IMAGE_TYPES),
    }


class MockDifyAdapter(DifyRecommendationPort):
    """PostgreSQL-persisted sample adapter; it never reads image bytes or calls a model."""

    mode = "mock"

    def __init__(
        self,
        *,
        form_ttl_days: int = 3,
        outcome: MockOutcome = "ready",
        _test_relation_overrides: Mapping[str, str] | None = None,
    ) -> None:
        if form_ttl_days < 1:
            raise ValueError("form_ttl_days must be positive")
        self._form_ttl = timedelta(days=form_ttl_days)
        self._outcome = outcome
        self._test_relation_overrides = dict(_test_relation_overrides or {})

    @classmethod
    def for_testing(
        cls,
        *,
        auxiliary_relation_overrides: Mapping[str, str],
        form_ttl_days: int = 3,
        outcome: MockOutcome = "ready",
    ) -> MockDifyAdapter:
        """Create a test-only adapter with explicit synthetic relation overrides."""
        return cls(
            form_ttl_days=form_ttl_days,
            outcome=outcome,
            _test_relation_overrides=auxiliary_relation_overrides,
        )

    def start_recommendation(
        self,
        db: Session,
        *,
        business_id: UUID,
        recommendation_revision: int,
        reference_set_id: UUID,
        reference_assets: Sequence[ReferenceAssetSummary],
        category: str,
    ) -> str:
        run_ref = f"mock:{business_id}"
        asset_snapshot = [reference_asset_payload(asset) for asset in reference_assets]
        options = build_mock_options() if self._outcome == "ready" else None
        original = build_mock_original(category, options) if options is not None else None
        now = db.scalar(select(func.clock_timestamp()))
        if now is None:
            raise DifyBusinessConflict("database clock is unavailable")
        trusted_snapshot = None
        if self._outcome == "ready":
            try:
                trusted_snapshot = build_trusted_analysis_snapshot(
                    reference_set_id=reference_set_id,
                    recommendation_id=business_id,
                    recommendation_revision=recommendation_revision,
                    category=category,
                    references=asset_snapshot,
                    created_at=now,
                    relation_overrides=self._test_relation_overrides,
                )
            except TrustedAnalysisSnapshotError:
                raise DifyBusinessConflict("trusted synthetic analysis input is invalid") from None
        ready_until = now + self._form_ttl if self._outcome == "ready" else None
        run_state: dict[str, object] = {
            "status": self._outcome,
            "fixture_id": "synthetic-recommendation-v1",
        }
        if trusted_snapshot is not None:
            run_state[TRUSTED_ANALYSIS_STATE_KEY] = trusted_analysis_state(trusted_snapshot)
        run = MockRecommendationRun(
            business_id=business_id,
            reference_set_id=reference_set_id,
            run_ref=run_ref,
            category=category,
            reference_assets=asset_snapshot,
            status=self._outcome,
            original_recommendation=original,
            public_options=options,
            image_types=list(IMAGE_TYPES),
            audit={
                "source": "synthetic_mock",
                "image_analysis_performed": False,
                "category_snapshot": category,
                "reference_metadata": asset_snapshot,
                "trusted_analysis_snapshot_id": (
                    str(trusted_snapshot.snapshot_id) if trusted_snapshot is not None else None
                ),
                "trusted_analysis_snapshot_digest": (
                    trusted_snapshot.snapshot_digest if trusted_snapshot is not None else None
                ),
            },
            state=run_state,
            form_ref=f"mock-form:{business_id}" if self._outcome == "ready" else None,
            valid_until=ready_until,
            failure_code="MOCK_RECOMMENDATION_FAILED" if self._outcome == "failed" else None,
        )
        insert_values = {
            column.name: getattr(run, column.name)
            for column in MockRecommendationRun.__table__.columns
            if column.name not in {"id", "created_at", "updated_at"}
        }
        db.execute(
            pg_insert(MockRecommendationRun)
            .values(**insert_values)
            .on_conflict_do_nothing(index_elements=[MockRecommendationRun.business_id])
        )
        persisted = db.execute(
            select(MockRecommendationRun)
            .where(MockRecommendationRun.business_id == business_id)
            .with_for_update()
        ).scalar_one()
        if (
            persisted.run_ref != run_ref
            or persisted.reference_set_id != reference_set_id
            or persisted.category != category
            or persisted.reference_assets != asset_snapshot
        ):
            raise DifyBusinessConflict("mock business id already uses different reference input")
        if self._outcome == "ready":
            try:
                load_trusted_analysis_snapshot(
                    persisted.state.get(TRUSTED_ANALYSIS_STATE_KEY),
                    reference_set_id=reference_set_id,
                    recommendation_id=business_id,
                    recommendation_revision=recommendation_revision,
                    category=category,
                    expected_references=asset_snapshot,
                )
            except TrustedAnalysisSnapshotError:
                raise DifyBusinessConflict(
                    "persisted trusted synthetic analysis is unavailable"
                ) from None
        return persisted.run_ref

    def read_recommendation(self, db: Session, *, run_ref: str) -> RecommendationRun:
        run = db.execute(
            select(MockRecommendationRun)
            .where(MockRecommendationRun.run_ref == run_ref)
            .with_for_update()
        ).scalar_one_or_none()
        if run is None:
            raise DifyBusinessConflict("mock run reference was not found")
        return RecommendationRun(
            status=run.status,  # type: ignore[arg-type]
            run_ref=run.run_ref,
            original_recommendation=run.original_recommendation,
            public_options=run.public_options,
            image_types=tuple(run.image_types),
            audit=run.audit,
            state=run.state,
            form_ref=run.form_ref,
            valid_until=run.valid_until,
            failure_code=run.failure_code,
        )

    def submit_preferences(
        self, run_ref: str, submission_id: UUID, resolved_snapshot: dict[str, object]
    ) -> PreferenceSubmissionResult:
        del run_ref, submission_id, resolved_snapshot
        raise DifyNotConfigured("the S5 mock adapter does not implement preference submission")

    def read_prompt_result(self, run_ref: str) -> PromptResult:
        del run_ref
        raise DifyNotConfigured("prompt results are not configured in S5")


@dataclass(slots=True)
class _PromptTestFaults:
    """Constructor-only fault controls; request JSON and application settings cannot set them."""

    reject_before_accept_once: bool = False
    accepted_response_lost_once: bool = False
    recovery_unknown_once: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def consume(self, name: str) -> bool:
        with self._lock:
            value = getattr(self, name)
            if value:
                setattr(self, name, False)
            return value


class MockPromptWorkflowAdapter(DifyPromptWorkflowPort):
    """Durable synthetic prompt submit/callback store; never reads images or calls a model."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        _test_faults: _PromptTestFaults | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._test_faults = _test_faults or _PromptTestFaults()

    @classmethod
    def for_testing(
        cls,
        session_factory: Callable[[], Session],
        *,
        reject_before_accept_once: bool = False,
        accepted_response_lost_once: bool = False,
        recovery_unknown_once: bool = False,
    ) -> MockPromptWorkflowAdapter:
        return cls(
            session_factory,
            _test_faults=_PromptTestFaults(
                reject_before_accept_once=reject_before_accept_once,
                accepted_response_lost_once=accepted_response_lost_once,
                recovery_unknown_once=recovery_unknown_once,
            ),
        )

    def submit_preferences(self, command: PromptSubmissionCommand) -> PromptSubmitResult:
        if not isinstance(command, PromptSubmissionCommand):
            raise TypeError("prompt submit requires a validated PromptSubmissionCommand")
        try:
            with self._session_factory() as db, db.begin():
                self._lock_submission_id(db, command.submission_id)
                row = db.execute(
                    select(MockPromptSubmission)
                    .where(MockPromptSubmission.submission_id == command.submission_id)
                    .with_for_update()
                ).scalar_one_or_none()
                if row is not None:
                    self._assert_same_submission(row, command)
                else:
                    rejected = self._test_faults.consume("reject_before_accept_once")
                    if rejected:
                        row = MockPromptSubmission(
                            submission_id=command.submission_id,
                            task_id=command.task_id,
                            workflow_run_id=command.workflow_run_id,
                            workflow_revision=command.workflow_revision,
                            content_digest_version=command.content_digest_version,
                            content_digest=command.content_digest,
                            outcome="definitely_rejected",
                            safe_reason_code="MOCK_REJECTED_BEFORE_ACCEPT",
                        )
                    else:
                        package = _build_synthetic_prompt_package(command)
                        provider_submission_ref = f"mock-prompt-submission:{command.submission_id}"
                        provider_run_ref = f"mock-prompt-run:{command.workflow_run_id}"
                        callback_fields: dict[str, object] = {
                            "task_id": str(command.task_id),
                            "workflow_run_id": str(command.workflow_run_id),
                            "workflow_revision": command.workflow_revision,
                            "submission_id": str(command.submission_id),
                            "submission_content_digest_version": command.content_digest_version,
                            "submission_content_digest": command.content_digest.hex(),
                            "outcome": "completed",
                            "package": package,
                            "next_question_round": None,
                            "allowed_fields": None,
                            "locked_fields": None,
                            "valid_until": None,
                            "safe_reason_code": None,
                        }
                        row = MockPromptSubmission(
                            submission_id=command.submission_id,
                            task_id=command.task_id,
                            workflow_run_id=command.workflow_run_id,
                            workflow_revision=command.workflow_revision,
                            content_digest_version=command.content_digest_version,
                            content_digest=command.content_digest,
                            outcome="accepted",
                            provider_submission_ref=provider_submission_ref,
                            provider_run_ref=provider_run_ref,
                            result_package=package,
                            result_digest_version=PROMPT_RESULT_DIGEST_VERSION,
                            result_digest=canonical_sha256(callback_fields),
                        )
                    db.add(row)
                    db.flush()
                accepted = row.outcome == "accepted"
                result = self._submit_result(row)
        except DifyPromptSubmissionConflict:
            raise
        except SQLAlchemyError:
            return PromptSubmitResult(
                submission_id=command.submission_id,
                task_id=command.task_id,
                workflow_run_id=command.workflow_run_id,
                workflow_revision=command.workflow_revision,
                outcome="unknown",
                safe_reason_code="MOCK_SUBMIT_STORE_OUTCOME_UNKNOWN",
            )

        if accepted and self._test_faults.consume("accepted_response_lost_once"):
            return PromptSubmitResult(
                submission_id=command.submission_id,
                task_id=command.task_id,
                workflow_run_id=command.workflow_run_id,
                workflow_revision=command.workflow_revision,
                outcome="unknown",
                safe_reason_code="MOCK_ACCEPTED_RESPONSE_LOST",
            )
        return result

    def recover_submission(self, command: PromptSubmissionCommand) -> PromptRecoveryResult:
        if self._test_faults.consume("recovery_unknown_once"):
            return PromptRecoveryResult(
                submission_id=command.submission_id,
                task_id=command.task_id,
                workflow_run_id=command.workflow_run_id,
                workflow_revision=command.workflow_revision,
                outcome="unknown",
                safe_reason_code="MOCK_RECOVERY_OUTCOME_UNKNOWN",
            )
        try:
            with self._session_factory() as db:
                row = db.execute(
                    select(MockPromptSubmission).where(
                        MockPromptSubmission.submission_id == command.submission_id
                    )
                ).scalar_one_or_none()
        except SQLAlchemyError:
            return PromptRecoveryResult(
                submission_id=command.submission_id,
                task_id=command.task_id,
                workflow_run_id=command.workflow_run_id,
                workflow_revision=command.workflow_revision,
                outcome="unknown",
                safe_reason_code="MOCK_SUBMISSION_RECOVERY_UNKNOWN",
            )
        if row is None:
            return PromptRecoveryResult(
                submission_id=command.submission_id,
                task_id=command.task_id,
                workflow_run_id=command.workflow_run_id,
                workflow_revision=command.workflow_revision,
                outcome="definitely_not_found",
                safe_reason_code="MOCK_SUBMISSION_NOT_FOUND",
            )
        self._assert_same_submission(row, command)
        outcome = "accepted" if row.outcome == "accepted" else "definitely_rejected"
        return PromptRecoveryResult(
            submission_id=command.submission_id,
            task_id=command.task_id,
            workflow_run_id=command.workflow_run_id,
            workflow_revision=command.workflow_revision,
            outcome=outcome,  # type: ignore[arg-type]
            provider_submission_ref=row.provider_submission_ref,
            provider_run_ref=row.provider_run_ref,
            safe_reason_code=row.safe_reason_code,
        )

    def read_prompt_result(self, submission_id: UUID) -> PromptCallback:
        try:
            with self._session_factory() as db:
                row = db.execute(
                    select(MockPromptSubmission).where(
                        MockPromptSubmission.submission_id == submission_id
                    )
                ).scalar_one_or_none()
        except SQLAlchemyError:
            raise DifyNotConfigured("mock prompt result is temporarily unavailable") from None
        if row is None or row.outcome != "accepted" or row.result_package is None:
            raise DifyNotConfigured("mock prompt result is not available")
        callback = PromptCallback(
            task_id=row.task_id,
            workflow_run_id=row.workflow_run_id,
            workflow_revision=row.workflow_revision,
            submission_id=row.submission_id,
            submission_content_digest_version=row.content_digest_version,
            submission_content_digest=row.content_digest,
            result_digest_version=row.result_digest_version,
            result_digest=row.result_digest,
            outcome="completed",
            package=row.result_package,
        )
        if canonical_sha256(prompt_callback_digest_payload(callback)) != row.result_digest:
            raise DifyPromptSubmissionConflict("mock callback result digest is inconsistent")
        return callback

    @staticmethod
    def _assert_same_submission(
        row: MockPromptSubmission, command: PromptSubmissionCommand
    ) -> None:
        if (
            row.task_id != command.task_id
            or row.workflow_run_id != command.workflow_run_id
            or row.workflow_revision != command.workflow_revision
            or row.content_digest_version != command.content_digest_version
            or row.content_digest != command.content_digest
        ):
            raise DifyPromptSubmissionConflict("prompt submission id has different immutable input")

    @staticmethod
    def _submit_result(row: MockPromptSubmission) -> PromptSubmitResult:
        return PromptSubmitResult(
            submission_id=row.submission_id,
            task_id=row.task_id,
            workflow_run_id=row.workflow_run_id,
            workflow_revision=row.workflow_revision,
            outcome=("accepted" if row.outcome == "accepted" else "definitely_rejected"),
            provider_submission_ref=row.provider_submission_ref,
            provider_run_ref=row.provider_run_ref,
            safe_reason_code=row.safe_reason_code,
        )

    @staticmethod
    def _lock_submission_id(db: Session, submission_id: UUID) -> None:
        if db.get_bind().dialect.name != "postgresql":
            return
        digest = hashlib.sha256(submission_id.bytes).digest()
        first = int.from_bytes(digest[:4], "big", signed=True)
        second = int.from_bytes(digest[4:8], "big", signed=True)
        db.execute(select(func.pg_advisory_xact_lock(first, second)))


def _build_synthetic_prompt_package(command: PromptSubmissionCommand) -> dict[str, object]:
    context = command.private_payload
    snapshot = context.get("trusted_analysis_snapshot")
    resolved = context.get("resolved_preferences")
    category = context.get("category_snapshot")
    image_types = context.get("image_types")
    if (
        not isinstance(snapshot, dict)
        or not isinstance(resolved, dict)
        or not isinstance(category, str)
        or not isinstance(image_types, list)
        or any(not isinstance(item, str) for item in image_types)
    ):
        raise DifyPromptSubmissionConflict("synthetic prompt input is incomplete")
    try:
        analysis = json_safe_value(snapshot)
        references = analysis["references"]
        relations = analysis["auxiliary_relations"]
        if not isinstance(references, list) or not isinstance(relations, list):
            raise ValueError
        aliases = [str(reference["alias"]) for reference in references]
        auxiliary = aliases[1:]
        relation_rows = []
        for entry in relations:
            relation_rows.append(
                {
                    "id": entry["alias"],
                    "view": entry["view"],
                    "relation": entry["relation"],
                    "scope": entry["scope"],
                    "evidence": entry["evidence"],
                }
            )
        platform_entry = resolved["platform"]
        platform = platform_entry["value"]
        if not isinstance(platform, str):
            raise ValueError
        preference_form = _mock_preference_form(resolved, platform)
        from app.services.prompt_validation import PLATFORM_RULES

        identity_prompt = (
            "Synthetic mock instruction: preserve the identity of the PRIMARY reference; "
            "no uploaded image was analyzed."
        )
        synthetic_note = "Synthetic metadata fixture only; no image bytes were read or recognized."
        basis: dict[str, object] = {
            "product": f"User-provided category context: {category}",
            "declared_category": category,
            "primary_view": "PRIMARY is the identity anchor; visual analysis was not performed.",
            "physical_profile": {
                "silhouette_proportions": synthetic_note,
                "parts_count": synthetic_note,
                "openings_connections": synthetic_note,
                "material_appearance": synthetic_note,
                "color_zones": synthetic_note,
                "pattern_logo_placement": synthetic_note,
                "visible_texture": synthetic_note,
            },
            "auxiliary_relations": relation_rows,
            "asset_ids": {"primary": "PRIMARY", "auxiliary": auxiliary},
            "identity_constraints": [
                "Keep unknown visual facts unknown.",
                "Use PRIMARY as the identity anchor.",
                "Do not infer unseen product properties.",
            ],
            "identity_prompt": identity_prompt,
            "unknowns": ["Visual details not encoded in saved metadata remain unknown."],
            "preference_form": preference_form,
            "image_types": list(image_types),
            "platform_profile": {
                "platform": platform,
                "rule": PLATFORM_RULES[platform],
                "canvases": [
                    {
                        "type": image_type,
                        "canvas": _mock_canvas_for(platform, image_type),
                    }
                    for image_type in image_types
                ],
            },
        }
        view_aliases = list(auxiliary)
        images = []
        for image_type in image_types:
            images.append(
                {
                    "type": image_type,
                    "canvas": _mock_canvas_for(platform, image_type),
                    "design_prompt": (
                        f"Synthetic mock prompt for {image_type}; follow submitted preferences "
                        "and do not state unverified facts."
                    ),
                    "identity_prompt": identity_prompt,
                    "resources": [
                        {"field": key, "use": f"Use the saved preference for field {key}."}
                        for key in ("1", "2", "3", "4", "5")
                    ],
                    "reference_plan": {
                        "identity_anchor_id": "PRIMARY",
                        "view_reference_ids": view_aliases,
                        "excluded_reference_ids": [],
                        "target_view": "Use only trusted saved view relationships.",
                    },
                }
            )
        return PromptPackage.model_validate({"basis": basis, "images": images}).model_dump(
            mode="json"
        )
    except (KeyError, TypeError, ValueError):
        raise DifyPromptSubmissionConflict("synthetic prompt context is invalid") from None


def _mock_preference_form(resolved: Mapping[str, object], platform: str) -> dict[str, object]:
    names = {
        "language": ("1", "语言种类"),
        "usage_scene": ("3", "使用场景"),
        "visual_style": ("4", "视觉风格"),
        "selling_points": ("5", "商品卖点"),
    }
    result: dict[str, object] = {
        "2": {"name": "使用的电商平台", "source": "PLATFORM", "value": platform}
    }
    for field_name, (key, label) in names.items():
        entry = resolved.get(field_name)
        if not isinstance(entry, dict):
            raise DifyPromptSubmissionConflict("saved preference is invalid")
        mode = entry.get("mode")
        if mode in {"A", "B", "C"}:
            source = mode
            value = entry.get("text")
        elif mode == "D" or entry.get("kind") == "agent_design":
            source = "D"
            value = entry.get("instruction")
        elif mode == "custom":
            source = "custom"
            value = entry.get("text")
        else:
            raise DifyPromptSubmissionConflict("saved preference mode is invalid")
        if not isinstance(value, str) or not value.strip():
            raise DifyPromptSubmissionConflict("saved preference text is invalid")
        result[key] = {"name": label, "source": source, "value": value}
    return result


def _mock_canvas_for(platform: str, image_type: str) -> str:
    if platform == "亚马逊高级A+":
        return "1464 x 1800 px" if image_type == IMAGE_TYPES[0] else "1464 x 600 px"
    if platform == "TEMU服饰类":
        return "3:4"
    if platform in {"淘宝", "京东", "拼多多", "1688"}:
        return "800 x 800 px"
    return "1:1"
