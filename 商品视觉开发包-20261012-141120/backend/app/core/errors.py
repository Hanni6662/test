from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.domain_errors import DomainError

logger = logging.getLogger("image_backend.errors")


class ErrorResponse(BaseModel):
    code: str
    message: str
    request_id: str


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else str(uuid4())


def _error_response(request: Request, *, status_code: int, message: str) -> JSONResponse:
    payload = ErrorResponse(
        code="HTTP_ERROR",
        message=message,
        request_id=_request_id(request),
    )
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(),
        headers={"X-Request-ID": payload.request_id, "Cache-Control": "no-store"},
    )


async def validation_error_handler(
    request: Request, _exc: RequestValidationError
) -> JSONResponse:
    return _error_response(request, status_code=422, message="请求参数无效")


async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    messages = {
        400: "请求无效",
        401: "需要有效身份",
        403: "无权执行此操作",
        404: "未找到请求的资源",
        409: "请求与当前状态冲突",
        429: "请求过于频繁",
    }
    message = messages.get(exc.status_code, "请求无法处理")
    return _error_response(request, status_code=exc.status_code, message=message)


async def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "unhandled_error request_id=%s exception_type=%s",
        _request_id(request),
        type(exc).__name__,
    )
    return _error_response(request, status_code=500, message="服务暂时不可用")


async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    request_id = _request_id(request)
    payload = ErrorResponse(
        code=exc.code,
        message=exc.message,
        request_id=request_id,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=payload.model_dump(),
        headers={"X-Request-ID": request_id, "Cache-Control": "no-store"},
    )


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(Exception, internal_error_handler)
