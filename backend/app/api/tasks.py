from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.dependencies import (
    Principal,
    require_empty_body,
    require_principal,
    require_same_origin,
)
from app.core.domain_errors import DomainError
from app.integrations.storage.protocol import Storage
from app.schemas.task_history import TaskHistoryPage, TaskRetryAccepted, TaskRetryRequest
from app.schemas.tasks import TaskAccepted, TaskCreateRequest, TaskResponse
from app.services.original_downloads import DownloadGate, OriginalDownloadResponse
from app.services.tasks import PreviewStream, TaskService

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


def _service(request: Request) -> TaskService:
    service = getattr(request.app.state, "task_service", None)
    if service is None:
        raise DomainError(503, "SERVICE_UNAVAILABLE", "任务服务暂不可用")
    return service


def _storage(request: Request) -> Storage:
    storage = getattr(request.app.state, "storage", None)
    if storage is None:
        raise DomainError(503, "SERVICE_UNAVAILABLE", "私有存储暂不可用")
    return storage


def _download_gate(request: Request) -> DownloadGate:
    gate = getattr(request.app.state, "original_download_gate", None)
    if gate is None:
        raise DomainError(503, "SERVICE_UNAVAILABLE", "原图下载暂不可用")
    return gate


async def _validate_original_request(request: Request) -> None:
    if request.query_params:
        raise DomainError(400, "REQUEST_INVALID", "请求参数无效")
    await require_empty_body(request)


def _read_chunks(preview: PreviewStream, chunk_size: int = 128 * 1024) -> Iterator[bytes]:
    try:
        while chunk := preview.body.read(chunk_size):
            yield chunk
    finally:
        preview.body.close()


@router.post("", response_model=TaskAccepted, status_code=202)
def create_task(
    body: TaskCreateRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    principal: Annotated[Principal, Depends(require_principal)],
    _same_origin: Annotated[None, Depends(require_same_origin)],
) -> TaskAccepted:
    response.headers["Cache-Control"] = "no-store"
    return _service(request).create_task(
        owner_user_id=principal.user_id,
        idempotency_key=idempotency_key,
        request=body,
    )


@router.get("", response_model=TaskHistoryPage)
def list_tasks(
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(require_principal)],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
) -> TaskHistoryPage:
    response.headers["Cache-Control"] = "no-store"
    return _service(request).list_tasks(
        owner_user_id=principal.user_id,
        limit=limit,
        cursor=cursor,
    )


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(
    task_id: UUID,
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(require_principal)],
) -> TaskResponse:
    response.headers["Cache-Control"] = "no-store"
    return _service(request).get_task(owner_user_id=principal.user_id, task_id=task_id)


@router.get("/{task_id}/images/{image_id}/preview")
def get_task_preview(
    task_id: UUID,
    image_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
) -> StreamingResponse:
    preview = _service(request).open_preview(
        owner_user_id=principal.user_id,
        task_id=task_id,
        image_id=image_id,
        storage=_storage(request),
    )
    return StreamingResponse(
        _read_chunks(preview),
        media_type=preview.mime_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Length": str(preview.size_bytes),
        },
    )


@router.get(
    "/{task_id}/images/{image_id}/original",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "已校验的原图二进制下载",
            "content": {"image/jpeg": {}, "image/png": {}, "image/webp": {}},
            "headers": {
                "Content-Disposition": {"schema": {"type": "string"}},
                "Content-Length": {"schema": {"type": "integer"}},
                "Cache-Control": {"schema": {"type": "string"}},
                "X-Content-Type-Options": {"schema": {"type": "string"}},
                "X-Request-ID": {"schema": {"type": "string"}},
                "Accept-Ranges": {"schema": {"type": "string"}},
            },
        },
        401: {"description": "身份无效"},
        404: {"description": "原图不可用"},
        503: {"description": "原图暂不可用或下载额度已满"},
    },
)
def get_task_original(
    task_id: UUID,
    image_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    _empty_request: Annotated[None, Depends(_validate_original_request)],
) -> Response:
    gate = _download_gate(request)
    lease = gate.try_acquire()
    request_id = getattr(request.state, "request_id", "")
    if lease is None:
        return JSONResponse(
            status_code=503,
            content={
                "code": "DOWNLOAD_BUSY",
                "message": "当前下载较多，请稍后重试",
                "request_id": request_id,
            },
            headers={
                "Cache-Control": "no-store",
                "Retry-After": "5",
                "X-Request-ID": request_id,
            },
        )

    auth_service = getattr(request.app.state, "auth_service", None)
    cookie_name = getattr(auth_service, "cookie_name", None)
    credential = request.cookies.get(cookie_name) if cookie_name else None

    def identity_recheck() -> bool:
        if auth_service is None:
            return False
        identity = auth_service.authenticate(credential)
        return bool(
            identity is not None
            and identity.user_id == principal.user_id
            and identity.session_id == principal.session_id
            and identity.identity_kind == principal.identity_kind
        )

    try:
        download = _service(request).open_original(
            owner_user_id=principal.user_id,
            task_id=task_id,
            image_id=image_id,
            storage=_storage(request),
            lease=lease,
            identity_recheck=identity_recheck,
        )
        return OriginalDownloadResponse(download, request_id=request_id)
    except BaseException:
        lease.release()
        raise


@router.post("/{task_id}/retry", response_model=TaskRetryAccepted, status_code=202)
def retry_failed_items(
    task_id: UUID,
    body: TaskRetryRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    principal: Annotated[Principal, Depends(require_principal)],
    _same_origin: Annotated[None, Depends(require_same_origin)],
) -> TaskRetryAccepted:
    response.headers["Cache-Control"] = "no-store"
    return _service(request).retry_failed_items(
        owner_user_id=principal.user_id,
        task_id=task_id,
        idempotency_key=idempotency_key,
        request=body,
    )
