from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response

from app.api.dependencies import Principal, require_principal, require_same_origin
from app.core.domain_errors import DomainError
from app.schemas.business_questions import (
    BusinessQuestionAnswerAccepted,
    BusinessQuestionAnswerRequest,
)
from app.schemas.business_sessions import (
    BusinessSelectionAccepted,
    BusinessSessionCancelResponse,
    BusinessSessionCreated,
    BusinessSessionCreateRequest,
    BusinessSessionHistoryPage,
    BusinessSessionResponse,
    BusinessSessionSelectionRequest,
    BusinessSessionV34Created,
    BusinessSessionV34Response,
)
from app.services.business_questions import BusinessQuestionService
from app.services.business_sessions import BusinessSessionService
from app.services.creation_readiness import ensure_creation_ready

router = APIRouter(prefix="/api/v1/business-sessions", tags=["business-sessions"])


def _service(request: Request) -> BusinessSessionService:
    service = getattr(request.app.state, "business_session_service", None)
    if service is None:
        raise DomainError(503, "SERVICE_UNAVAILABLE", "流程服务暂不可用")
    return service


def _question_service(request: Request) -> BusinessQuestionService:
    service = getattr(request.app.state, "business_question_service", None)
    if service is None:
        raise DomainError(503, "SERVICE_UNAVAILABLE", "流程服务暂不可用")
    return service


@router.post(
    "",
    response_model=BusinessSessionCreated | BusinessSessionV34Created,
    status_code=202,
)
def create_business_session(
    body: BusinessSessionCreateRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    principal: Annotated[Principal, Depends(require_principal)],
    _same_origin: Annotated[None, Depends(require_same_origin)],
) -> BusinessSessionCreated | BusinessSessionV34Created:
    response.headers["Cache-Control"] = "no-store"
    ensure_creation_ready(getattr(request.app.state, "settings", None))
    return _service(request).create_session(
        owner_user_id=principal.user_id,
        reference_set_id=body.reference_set_id,
        idempotency_key=idempotency_key,
    )


@router.get("", response_model=BusinessSessionHistoryPage)
def list_business_sessions(
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(require_principal)],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
) -> BusinessSessionHistoryPage:
    response.headers["Cache-Control"] = "no-store"
    return _service(request).list_sessions(
        owner_user_id=principal.user_id, limit=limit, cursor=cursor
    )


@router.get("/readiness")
def creation_readiness(
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(require_principal)],
) -> dict[str, str]:
    response.headers["Cache-Control"] = "no-store"
    ensure_creation_ready(getattr(request.app.state, "settings", None))
    return {"status": "ready"}


@router.post("/{business_session_id}/end-unstarted", response_model=BusinessSessionV34Response)
def end_unstarted_business_session(
    business_session_id: UUID,
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(require_principal)],
    _same_origin: Annotated[None, Depends(require_same_origin)],
) -> BusinessSessionV34Response:
    response.headers["Cache-Control"] = "no-store"
    return _service(request).end_unstarted(
        owner_user_id=principal.user_id, business_session_id=business_session_id
    )


@router.get(
    "/{business_session_id}",
    response_model=BusinessSessionResponse | BusinessSessionV34Response,
)
def get_business_session(
    business_session_id: UUID,
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(require_principal)],
) -> BusinessSessionResponse | BusinessSessionV34Response:
    response.headers["Cache-Control"] = "no-store"
    return _service(request).get_session(
        owner_user_id=principal.user_id,
        business_session_id=business_session_id,
    )


@router.post(
    "/{business_session_id}/selection",
    response_model=BusinessSelectionAccepted,
    status_code=202,
)
def create_selection(
    business_session_id: UUID,
    body: BusinessSessionSelectionRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    principal: Annotated[Principal, Depends(require_principal)],
    _same_origin: Annotated[None, Depends(require_same_origin)],
) -> BusinessSelectionAccepted:
    response.headers["Cache-Control"] = "no-store"
    return _service(request).create_selection(
        owner_user_id=principal.user_id,
        business_session_id=business_session_id,
        idempotency_key=idempotency_key,
        request=body,
    )


@router.post(
    "/{business_session_id}/cancel",
    response_model=BusinessSessionCancelResponse,
    status_code=202,
)
def cancel_business_session_before_selection(
    business_session_id: UUID,
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(require_principal)],
    _same_origin: Annotated[None, Depends(require_same_origin)],
) -> BusinessSessionCancelResponse:
    response.headers["Cache-Control"] = "no-store"
    return _service(request).cancel_before_selection(
        owner_user_id=principal.user_id,
        business_session_id=business_session_id,
    )


@router.post(
    "/{business_session_id}/questions/{question_id}/answers",
    response_model=BusinessQuestionAnswerAccepted,
    status_code=202,
)
def submit_business_question_answer(
    business_session_id: UUID,
    question_id: UUID,
    body: BusinessQuestionAnswerRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    principal: Annotated[Principal, Depends(require_principal)],
    _same_origin: Annotated[None, Depends(require_same_origin)],
) -> BusinessQuestionAnswerAccepted:
    response.headers["Cache-Control"] = "no-store"
    _service(request).ensure_h3_allowed(
        owner_user_id=principal.user_id,
        business_session_id=business_session_id,
    )
    return _question_service(request).submit_answer(
        owner_user_id=principal.user_id,
        business_session_id=business_session_id,
        question_id=question_id,
        idempotency_key=idempotency_key,
        request=body,
    )
