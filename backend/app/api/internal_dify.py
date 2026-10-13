from __future__ import annotations

import json
import re
import secrets
from typing import Protocol
from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic import SecretStr, ValidationError
from sqlalchemy.engine import Engine
from starlette.concurrency import run_in_threadpool

from app.core.domain_errors import DomainError
from app.schemas.business_sessions import (
    ChatflowBusinessEvent,
    ChatflowBusinessEventAccepted,
)
from app.schemas.chatflow_generation import (
    ChatflowGenerationEvent,
    ChatflowGenerationEventAccepted,
)
from app.schemas.chatflow_generation_results import (
    ChatflowGenerationManifestAccepted,
    ChatflowGenerationResultManifest,
)
from app.schemas.chatflow_prompt_packages import (
    ChatflowPromptPackageAccepted,
    ChatflowPromptPackageSubmission,
)
from app.schemas.chatflow_v34 import (
    ChatflowV34StageEvent,
    ChatflowV34StageEventAccepted,
    V34PromptContextResponse,
    V34SelectionContextResponse,
)
from app.schemas.prompt_preferences import (
    InternalCallbackAccepted,
    PromptCallbackRequest,
    RecommendationResultCallback,
)
from app.schemas.prompt_workflow import PromptCallback, PromptPhase
from app.services.prompt_callbacks import PromptCallbackService
from app.services.recommendation_callbacks import RecommendationCallbackService

CALLBACK_BODY_MAX_BYTES = 2 * 1024 * 1024
_CONTENT_LENGTH = re.compile(r"^[0-9]+$")


class _PromptCallbackPort(Protocol):
    def accept(self, callback: PromptCallback, *, phase: PromptPhase): ...


class _RecommendationCallbackPort(Protocol):
    def accept(self, callback: RecommendationResultCallback) -> str: ...


class _BusinessEventPort(Protocol):
    def accept_business_event(
        self, event: ChatflowBusinessEvent
    ) -> ChatflowBusinessEventAccepted: ...


class _ChatflowPromptPackagePort(Protocol):
    def accept(
        self, submission: ChatflowPromptPackageSubmission
    ) -> ChatflowPromptPackageAccepted: ...


class _ChatflowGenerationEventPort(Protocol):
    def accept(self, event: ChatflowGenerationEvent) -> ChatflowGenerationEventAccepted: ...


class _ChatflowGenerationResultPort(Protocol):
    def accept(
        self, manifest: ChatflowGenerationResultManifest
    ) -> ChatflowGenerationManifestAccepted: ...


class _ChatflowV34StageEventPort(Protocol):
    def accept(self, event: ChatflowV34StageEvent) -> ChatflowV34StageEventAccepted: ...


class _ChatflowV34ContextPort(Protocol):
    def get_selection_context(
        self, *, business_session_id, workflow_run_id: str, phase: str
    ) -> V34SelectionContextResponse: ...

    def get_prompt_context(
        self, *, business_session_id, workflow_run_id: str, phase: str
    ) -> V34PromptContextResponse: ...


def create_internal_dify_router(
    *,
    engine: Engine,
    callback_credential: SecretStr,
    prompt_service: _PromptCallbackPort | None = None,
    recommendation_service: _RecommendationCallbackPort | None = None,
    business_event_service: _BusinessEventPort | None = None,
    v34_stage_event_service: _ChatflowV34StageEventPort | None = None,
    v34_context_service: _ChatflowV34ContextPort | None = None,
    prompt_package_service: _ChatflowPromptPackagePort | None = None,
    generation_event_service: _ChatflowGenerationEventPort | None = None,
    generation_result_service: _ChatflowGenerationResultPort | None = None,
) -> APIRouter:
    """Create the isolated internal routes with an injected, independent secret.

    The returned router is not included by the public application factory. The
    integrator must mount it only on the intended internal API surface and make
    the reverse proxy reject `/internal/*` on public listeners.
    """
    credential = callback_credential.get_secret_value()
    if len(credential) < 32 or not credential.isascii():
        raise ValueError("Dify callback credential must contain at least 32 ASCII characters")
    callback_service = prompt_service or PromptCallbackService(engine)
    recommendation_callback_service = recommendation_service or RecommendationCallbackService(
        engine
    )
    router = APIRouter(prefix="/internal/v1/dify", tags=["internal-dify"])

    @router.post(
        "/recommendation-results",
        response_model=InternalCallbackAccepted,
        status_code=200,
    )
    async def recommendation_results(
        request: Request, response: Response
    ) -> InternalCallbackAccepted:
        await _authorize_and_check_headers(request, credential)
        raw = await _read_json_bytes(request)
        try:
            callback = RecommendationResultCallback.model_validate_json(raw, strict=True)
        except (ValidationError, ValueError):
            raise DomainError(422, "CALLBACK_INVALID", "回传内容无效") from None
        status = await run_in_threadpool(recommendation_callback_service.accept, callback)
        response.headers["Cache-Control"] = "no-store"
        return InternalCallbackAccepted(status=status, business_id=callback.recommendation_id)

    @router.post(
        "/prompt-results",
        response_model=InternalCallbackAccepted,
        status_code=200,
    )
    async def prompt_results(request: Request, response: Response) -> InternalCallbackAccepted:
        await _authorize_and_check_headers(request, credential)
        raw = await _read_json_bytes(request)
        try:
            envelope = PromptCallbackRequest.model_validate_json(raw, strict=True)
            # Wire digests are lowercase hex. Decode them explicitly instead of
            # letting Pydantic coerce the strings to UTF-8 bytes.
            callback = envelope.to_domain()
        except (ValidationError, ValueError):
            raise DomainError(422, "CALLBACK_INVALID", "回传内容无效") from None
        accepted = await run_in_threadpool(
            callback_service.accept,
            callback,
            phase=envelope.phase,
        )
        response.headers["Cache-Control"] = "no-store"
        return InternalCallbackAccepted(
            status=accepted.status,
            business_id=callback.task_id,
            workflow_run_id=accepted.workflow_run_id,
            workflow_revision=accepted.workflow_revision,
            safe_reason_code=accepted.safe_reason_code,
        )

    if business_event_service is not None:

        @router.post(
            "/business-events",
            response_model=ChatflowBusinessEventAccepted,
            status_code=200,
        )
        async def business_events(
            request: Request, response: Response
        ) -> ChatflowBusinessEventAccepted:
            await _authorize_and_check_headers(request, credential)
            raw = await _read_json_bytes(request)
            try:
                event = ChatflowBusinessEvent.model_validate_json(raw, strict=True)
            except (ValidationError, ValueError):
                raise DomainError(422, "CALLBACK_INVALID", "回传内容无效") from None
            accepted = await run_in_threadpool(business_event_service.accept_business_event, event)
            response.headers["Cache-Control"] = "no-store"
            return accepted

    if v34_stage_event_service is not None:

        @router.post(
            "/stage-events",
            response_model=ChatflowV34StageEventAccepted,
            status_code=200,
        )
        async def v34_stage_events(
            request: Request, response: Response
        ) -> ChatflowV34StageEventAccepted:
            await _authorize_and_check_headers(request, credential)
            raw = await _read_json_bytes(request)
            try:
                event = ChatflowV34StageEvent.model_validate_json(raw, strict=True)
            except (ValidationError, ValueError):
                raise DomainError(422, "CALLBACK_INVALID", "回传内容无效") from None
            accepted = await run_in_threadpool(v34_stage_event_service.accept, event)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            return accepted

    if v34_context_service is not None:

        @router.get(
            "/business-sessions/{business_session_id}/context",
            response_model=V34SelectionContextResponse | V34PromptContextResponse,
        )
        async def v34_selection_context(
            business_session_id: UUID,
            request: Request,
            response: Response,
        ) -> V34SelectionContextResponse | V34PromptContextResponse:
            await _authorize_and_check_headers(request, credential)
            workflow_run_id = request.query_params.get("workflow_run_id", "")
            phase = request.query_params.get("phase", "")
            context_method = (
                v34_context_service.get_prompt_context
                if phase == "prompt"
                else v34_context_service.get_selection_context
            )
            context = await run_in_threadpool(
                context_method,
                business_session_id=business_session_id,
                workflow_run_id=workflow_run_id,
                phase=phase,
            )
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            return context

    if prompt_package_service is not None:

        @router.post(
            "/prompt-packages",
            response_model=ChatflowPromptPackageAccepted,
            status_code=200,
        )
        async def prompt_packages(
            request: Request, response: Response
        ) -> ChatflowPromptPackageAccepted:
            await _authorize_and_check_headers(request, credential)
            raw = await _read_json_bytes(request)
            try:
                submission = ChatflowPromptPackageSubmission.model_validate_json(raw, strict=True)
            except (ValidationError, ValueError):
                raise DomainError(422, "PROMPT_PACKAGE_INVALID", "提示词包内容无效") from None
            accepted = await run_in_threadpool(prompt_package_service.accept, submission)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            return accepted

    if generation_event_service is not None:

        @router.post(
            "/generation-events",
            response_model=ChatflowGenerationEventAccepted,
            status_code=200,
        )
        async def generation_events(
            request: Request, response: Response
        ) -> ChatflowGenerationEventAccepted:
            await _authorize_and_check_headers(request, credential)
            raw = await _read_json_bytes(request)
            try:
                event = ChatflowGenerationEvent.model_validate_json(raw, strict=True)
            except (ValidationError, ValueError):
                raise DomainError(422, "GENERATION_EVENT_INVALID", "状态事件内容无效") from None
            accepted = await run_in_threadpool(generation_event_service.accept, event)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            return accepted

    if generation_result_service is not None:

        @router.post(
            "/generation-results",
            response_model=ChatflowGenerationManifestAccepted,
            status_code=200,
        )
        async def generation_results(
            request: Request, response: Response
        ) -> ChatflowGenerationManifestAccepted:
            await _authorize_and_check_headers(request, credential)
            raw = await _read_json_bytes(request)
            try:
                manifest = ChatflowGenerationResultManifest.model_validate_json(raw, strict=True)
            except (ValidationError, ValueError):
                raise DomainError(422, "GENERATION_RESULT_INVALID", "结果清单内容无效") from None
            accepted = await run_in_threadpool(generation_result_service.accept, manifest)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            return accepted

    return router


async def _authorize_and_check_headers(request: Request, credential: str) -> None:
    authorization = request.headers.get("authorization", "")
    prefix = "Bearer "
    supplied = authorization[len(prefix) :] if authorization.startswith(prefix) else ""
    try:
        valid = secrets.compare_digest(supplied.encode("ascii"), credential.encode("ascii"))
    except UnicodeEncodeError:
        valid = False
    if not valid:
        raise DomainError(401, "CALLBACK_AUTH_REQUIRED", "回传凭证无效")

    content_length = request.headers.get("content-length")
    if content_length is not None:
        if not _CONTENT_LENGTH.fullmatch(content_length):
            raise DomainError(400, "CALLBACK_INVALID", "回传内容无效")
        if int(content_length) > CALLBACK_BODY_MAX_BYTES:
            raise DomainError(413, "CALLBACK_TOO_LARGE", "回传内容超过允许大小")
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise DomainError(415, "CALLBACK_ENCODING_UNSUPPORTED", "回传编码不受支持")
    media_type = request.headers.get("content-type", "").split(";", maxsplit=1)[0].strip().lower()
    if media_type != "application/json":
        raise DomainError(415, "CALLBACK_CONTENT_TYPE_INVALID", "回传内容类型无效")


async def _read_json_bytes(request: Request) -> bytes:
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > CALLBACK_BODY_MAX_BYTES:
            raise DomainError(413, "CALLBACK_TOO_LARGE", "回传内容超过允许大小")
        data.extend(chunk)
    raw = bytes(data)
    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise DomainError(400, "CALLBACK_INVALID", "回传内容无效") from None
    if not isinstance(decoded, dict):
        raise DomainError(400, "CALLBACK_INVALID", "回传内容无效")
    return raw


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")
