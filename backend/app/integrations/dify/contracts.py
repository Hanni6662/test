from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from app.schemas.prompt_workflow import (
    PromptCallback,
    PromptRecoveryResult,
    PromptSubmissionCommand,
    PromptSubmitResult,
)
from sqlalchemy.orm import Session

RunStatus = Literal["analyzing", "ready", "failed"]


class DifyNotConfigured(RuntimeError):
    """Raised when a real Dify operation is requested without a real adapter."""


class DifyBusinessConflict(RuntimeError):
    """A stable mock business id was reused with different reference inputs."""


class DifyPromptSubmissionConflict(RuntimeError):
    """A prompt submission id was reused with different immutable inputs."""


@dataclass(frozen=True, slots=True)
class ReferenceAssetSummary:
    reference_set_image_id: UUID
    slot: str
    position: int
    model_asset_id: UUID
    source_asset_id: UUID
    model_mime_type: str
    source_mime_type: str
    model_size_bytes: int
    source_size_bytes: int
    model_width: int
    model_height: int
    source_width: int
    source_height: int
    model_sha256: str
    source_sha256: str


@dataclass(frozen=True, slots=True)
class RecommendationRun:
    status: RunStatus
    run_ref: str
    original_recommendation: dict[str, object] | None
    public_options: dict[str, object] | None
    image_types: tuple[str, ...]
    audit: dict[str, object] | None
    state: dict[str, object]
    form_ref: str | None = None
    valid_until: datetime | None = None
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class PreferenceSubmissionResult:
    status: Literal["accepted", "needs_input", "unknown"]
    state: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class PromptResult:
    status: Literal["ready", "needs_input"]
    package: dict[str, object] | None = None


class DifyRecommendationPort(Protocol):
    """Business-domain port; these signatures are not Dify REST endpoints."""

    def start_recommendation(
        self,
        db: Session,
        *,
        business_id: UUID,
        recommendation_revision: int,
        reference_set_id: UUID,
        reference_assets: Sequence[ReferenceAssetSummary],
        category: str,
    ) -> str: ...

    def read_recommendation(self, db: Session, *, run_ref: str) -> RecommendationRun: ...

    def submit_preferences(
        self, run_ref: str, submission_id: UUID, resolved_snapshot: dict[str, object]
    ) -> PreferenceSubmissionResult: ...

    def read_prompt_result(self, run_ref: str) -> PromptResult: ...


class DifyPromptWorkflowPort(Protocol):
    """Private workflow port; all methods are one-shot and receive no caller Session."""

    def submit_preferences(self, command: PromptSubmissionCommand) -> PromptSubmitResult: ...

    def recover_submission(self, command: PromptSubmissionCommand) -> PromptRecoveryResult: ...

    def read_prompt_result(self, submission_id: UUID) -> PromptCallback: ...
