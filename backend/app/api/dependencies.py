from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import Request

from app.core.domain_errors import DomainError
from app.services.auth import _canonical_origin


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: UUID
    identity_kind: str
    session_id: UUID


async def require_empty_body(request: Request) -> None:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) != 0:
                raise DomainError(400, "REQUEST_INVALID", "请求参数无效")
        except ValueError:
            raise DomainError(400, "REQUEST_INVALID", "请求参数无效") from None
        return None
    async for chunk in request.stream():
        if chunk:
            raise DomainError(400, "REQUEST_INVALID", "请求参数无效")


def _auth_service(request: Request):
    service = getattr(request.app.state, "auth_service", None)
    if service is None:
        raise DomainError(503, "SERVICE_UNAVAILABLE", "身份服务暂不可用")
    return service


def require_principal(request: Request) -> Principal:
    service = _auth_service(request)
    credential = request.cookies.get(service.cookie_name)
    identity = service.authenticate(credential)
    if identity is None:
        raise DomainError(401, "AUTH_REQUIRED", "需要有效身份")
    return Principal(
        user_id=identity.user_id,
        identity_kind=identity.identity_kind,
        session_id=identity.session_id,
    )


def require_same_origin(request: Request) -> None:
    service = _auth_service(request)
    trusted_origins = service.options.trusted_origins
    origin = request.headers.get("origin")
    if origin is not None:
        if origin != "null" and origin in trusted_origins:
            return None
        raise DomainError(403, "ORIGIN_NOT_ALLOWED", "请求来源不受信任")

    referer = request.headers.get("referer")
    if referer is None:
        raise DomainError(403, "ORIGIN_REQUIRED", "缺少可信请求来源")
    candidate = _canonical_origin(referer, allow_referer_path=True)
    if candidate and candidate in trusted_origins:
        return None
    raise DomainError(403, "ORIGIN_NOT_ALLOWED", "请求来源不受信任")
