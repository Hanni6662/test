from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.api.dependencies import (
    Principal,
    require_empty_body,
    require_principal,
    require_same_origin,
)
from app.billing.disabled import DisabledAccountService
from app.core.domain_errors import DomainError
from app.schemas.auth import IdentityResponse
from app.services.auth import AuthService, GuestSessionResult

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
_disabled_account_service = DisabledAccountService()


def _service(request: Request) -> AuthService:
    service = getattr(request.app.state, "auth_service", None)
    if service is None:
        raise DomainError(503, "SERVICE_UNAVAILABLE", "身份服务暂不可用")
    return service


def _response(result: GuestSessionResult) -> IdentityResponse:
    return IdentityResponse(
        user_id=result.identity.user_id,
        identity_kind=result.identity.identity_kind,
    )


def _set_cookie(response: Response, service: AuthService, result: GuestSessionResult) -> None:
    if result.credential is None:
        return
    remaining_seconds = int((result.expires_at - datetime.now(UTC)).total_seconds())
    if remaining_seconds <= 0:
        return
    response.set_cookie(
        key=service.cookie_name,
        value=result.credential,
        max_age=remaining_seconds,
        # psycopg can return ZoneInfo("Etc/UTC"); Starlette's HTTP-date
        # formatter requires the canonical datetime.timezone.utc instance.
        expires=result.expires_at.astimezone(UTC),
        path="/",
        secure=service.options.cookie_secure,
        httponly=True,
        samesite="lax",
    )


@router.post("/guest", response_model=IdentityResponse, status_code=200)
def enter_guest(
    request: Request,
    response: Response,
    _origin: Annotated[None, Depends(require_same_origin)],
    _empty_body: Annotated[None, Depends(require_empty_body)],
) -> IdentityResponse:
    service = _service(request)
    result = service.enter_guest(request.cookies.get(service.cookie_name))
    _set_cookie(response, service, result)
    response.headers["Cache-Control"] = "no-store"
    return _response(result)


@router.get("/me", response_model=IdentityResponse)
def me(
    response: Response,
    principal: Annotated[Principal, Depends(require_principal)],
) -> IdentityResponse:
    response.headers["Cache-Control"] = "no-store"
    return IdentityResponse(
        user_id=principal.user_id,
        identity_kind=principal.identity_kind,
    )


@router.post("/guest/refresh", status_code=204, response_model=None)
def refresh_guest(
    request: Request,
    _origin: Annotated[None, Depends(require_same_origin)],
    _empty_body: Annotated[None, Depends(require_empty_body)],
) -> Response:
    service = _service(request)
    result = service.refresh_guest(request.cookies.get(service.cookie_name))
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    _set_cookie(response, service, result)
    return response


@router.post("/guest/reset", response_model=IdentityResponse)
def reset_guest(
    request: Request,
    response: Response,
    _origin: Annotated[None, Depends(require_same_origin)],
    _empty_body: Annotated[None, Depends(require_empty_body)],
) -> IdentityResponse:
    service = _service(request)
    result = service.reset_guest(request.cookies.get(service.cookie_name))
    _set_cookie(response, service, result)
    response.headers["Cache-Control"] = "no-store"
    return _response(result)


@router.post("/login", status_code=503, response_model=None)
def login(_origin: Annotated[None, Depends(require_same_origin)]) -> None:
    _disabled_account_service.login()


@router.post("/logout", status_code=503, response_model=None)
def logout(_origin: Annotated[None, Depends(require_same_origin)]) -> None:
    _disabled_account_service.logout()
