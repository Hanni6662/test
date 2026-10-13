from __future__ import annotations

import json
import re
import secrets
from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic import SecretStr, ValidationError
from starlette.concurrency import run_in_threadpool

from app.core.domain_errors import DomainError
from app.schemas.operations import (
    OperationsDiagnosticsResponse,
    RecoveryOperationRequest,
    RecoveryOperationResponse,
)
from app.schemas.retention import (
    CleanupPreviewResponse,
    CleanupRunAccepted,
    CleanupRunRequest,
    CleanupRunResponse,
    RetentionPolicyResponse,
    RetentionPolicyUpdateRequest,
)
from app.services.operations import OperationsDiagnosticsService
from app.services.retention_cleanup import RetentionCleanupService

MAINTENANCE_BODY_MAX_BYTES = 64 * 1024
_CONTENT_LENGTH = re.compile(r"^[0-9]+$")
_IDEMPOTENCY_KEY = re.compile(r"^[!-~]{1,200}$")


def create_internal_maintenance_router(
    *,
    service: RetentionCleanupService,
    operations_service: OperationsDiagnosticsService | None = None,
    bearer_secret: SecretStr,
    schedule_timezone: str,
    schedule_daily_time: str,
) -> APIRouter:
    """Build private maintenance routes with a credential independent of cookies/Dify."""
    credential = bearer_secret.get_secret_value()
    if (
        len(credential) < 32
        or not credential.isascii()
        or any(character.isspace() for character in credential)
    ):
        raise ValueError(
            "maintenance bearer secret must contain 32 non-whitespace ASCII characters"
        )

    router = APIRouter(prefix="/internal/v1/maintenance", tags=["internal-maintenance"])

    @router.get("/retention-policy", response_model=RetentionPolicyResponse)
    async def get_retention_policy(request: Request, response: Response) -> RetentionPolicyResponse:
        await _authorize(request, credential)
        result = await run_in_threadpool(service.get_policy)
        result = result.model_copy(
            update={
                "schedule_timezone": schedule_timezone,
                "schedule_daily_time": schedule_daily_time,
            }
        )
        response.headers["Cache-Control"] = "private, no-store"
        return result

    @router.put("/retention-policy", response_model=RetentionPolicyResponse)
    async def update_retention_policy(
        request: Request, response: Response
    ) -> RetentionPolicyResponse:
        await _authorize_and_check_json_headers(request, credential)
        raw = await _read_json_bytes(request)
        try:
            payload = RetentionPolicyUpdateRequest.model_validate_json(raw, strict=True)
        except (ValidationError, ValueError):
            raise DomainError(422, "MAINTENANCE_REQUEST_INVALID", "维护请求无效") from None
        result = await run_in_threadpool(service.update_policy, payload)
        result = result.model_copy(
            update={
                "schedule_timezone": schedule_timezone,
                "schedule_daily_time": schedule_daily_time,
            }
        )
        response.headers["Cache-Control"] = "private, no-store"
        return result

    @router.post("/cleanup/preview", response_model=CleanupPreviewResponse)
    async def preview_cleanup(request: Request, response: Response) -> CleanupPreviewResponse:
        await _authorize(request, credential)
        result = await run_in_threadpool(service.preview)
        response.headers["Cache-Control"] = "private, no-store"
        return result

    @router.post(
        "/cleanup/runs",
        response_model=CleanupRunAccepted,
        status_code=202,
    )
    async def create_cleanup_run(request: Request, response: Response) -> CleanupRunAccepted:
        await _authorize_and_check_json_headers(request, credential)
        key = request.headers.get("idempotency-key", "")
        if _IDEMPOTENCY_KEY.fullmatch(key) is None:
            raise DomainError(400, "IDEMPOTENCY_KEY_INVALID", "幂等键格式无效")
        raw = await _read_json_bytes(request)
        try:
            payload = CleanupRunRequest.model_validate_json(raw, strict=True)
        except (ValidationError, ValueError):
            raise DomainError(422, "MAINTENANCE_REQUEST_INVALID", "维护请求无效") from None
        result = await run_in_threadpool(
            service.create_run,
            payload,
            idempotency_key=key,
        )
        response.headers["Cache-Control"] = "private, no-store"
        return result

    @router.get("/cleanup/runs/{run_id}", response_model=CleanupRunResponse)
    async def get_cleanup_run(
        run_id: str, request: Request, response: Response
    ) -> CleanupRunResponse:
        await _authorize(request, credential)
        result = await run_in_threadpool(service.get_run, run_id)
        response.headers["Cache-Control"] = "private, no-store"
        return result

    if operations_service is not None:

        @router.get("/operations", response_model=OperationsDiagnosticsResponse)
        async def get_operations_diagnostics(
            request: Request, response: Response
        ) -> OperationsDiagnosticsResponse:
            await _authorize(request, credential)
            result = await run_in_threadpool(operations_service.snapshot)
            response.headers["Cache-Control"] = "private, no-store"
            return result

        @router.post(
            "/operations/tasks/{task_id}/expire-followup",
            response_model=RecoveryOperationResponse,
        )
        async def expire_followup(
            task_id: UUID, request: Request, response: Response
        ) -> RecoveryOperationResponse:
            await _authorize_and_check_json_headers(request, credential)
            _validate_empty_recovery_request(await _read_json_bytes(request))
            key = _validated_idempotency_key(request)
            result = await run_in_threadpool(
                operations_service.expire_followup, task_id, idempotency_key=key
            )
            response.headers["Cache-Control"] = "private, no-store"
            return result

        @router.post(
            "/operations/actions/{action_id}/retry-save",
            response_model=RecoveryOperationResponse,
        )
        async def retry_same_attempt_save(
            action_id: UUID, request: Request, response: Response
        ) -> RecoveryOperationResponse:
            await _authorize_and_check_json_headers(request, credential)
            _validate_empty_recovery_request(await _read_json_bytes(request))
            key = _validated_idempotency_key(request)
            result = await run_in_threadpool(
                operations_service.expedite_same_attempt_save,
                action_id,
                idempotency_key=key,
            )
            response.headers["Cache-Control"] = "private, no-store"
            return result

    return router


def _validated_idempotency_key(request: Request) -> str:
    key = request.headers.get("idempotency-key", "")
    if _IDEMPOTENCY_KEY.fullmatch(key) is None:
        raise DomainError(400, "IDEMPOTENCY_KEY_INVALID", "幂等键格式无效")
    return key


def _validate_empty_recovery_request(raw: bytes) -> None:
    try:
        RecoveryOperationRequest.model_validate_json(raw, strict=True)
    except (ValidationError, ValueError):
        raise DomainError(422, "MAINTENANCE_REQUEST_INVALID", "维护请求无效") from None


async def _authorize(request: Request, credential: str) -> None:
    authorization = request.headers.get("authorization", "")
    prefix = "Bearer "
    supplied = authorization[len(prefix) :] if authorization.startswith(prefix) else ""
    try:
        valid = secrets.compare_digest(supplied.encode("ascii"), credential.encode("ascii"))
    except UnicodeEncodeError:
        valid = False
    if not valid:
        raise DomainError(401, "MAINTENANCE_AUTH_REQUIRED", "维护凭证无效")


async def _authorize_and_check_json_headers(request: Request, credential: str) -> None:
    await _authorize(request, credential)
    content_length = request.headers.get("content-length")
    if content_length is not None:
        if _CONTENT_LENGTH.fullmatch(content_length) is None:
            raise DomainError(400, "MAINTENANCE_REQUEST_INVALID", "维护请求无效")
        if int(content_length) > MAINTENANCE_BODY_MAX_BYTES:
            raise DomainError(413, "MAINTENANCE_REQUEST_TOO_LARGE", "维护请求过大")
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise DomainError(415, "MAINTENANCE_ENCODING_UNSUPPORTED", "维护请求编码不受支持")
    media_type = request.headers.get("content-type", "").split(";", maxsplit=1)[0].strip().lower()
    if media_type != "application/json":
        raise DomainError(415, "MAINTENANCE_CONTENT_TYPE_INVALID", "维护请求类型无效")


async def _read_json_bytes(request: Request) -> bytes:
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAINTENANCE_BODY_MAX_BYTES:
            raise DomainError(413, "MAINTENANCE_REQUEST_TOO_LARGE", "维护请求过大")
        data.extend(chunk)
    raw = bytes(data)
    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise DomainError(400, "MAINTENANCE_REQUEST_INVALID", "维护请求无效") from None
    if not isinstance(parsed, dict):
        raise DomainError(400, "MAINTENANCE_REQUEST_INVALID", "维护请求无效")
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
