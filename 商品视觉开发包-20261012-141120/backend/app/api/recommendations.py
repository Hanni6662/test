from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response

from app.api.dependencies import Principal, require_principal, require_same_origin
from app.core.domain_errors import DomainError
from app.schemas.recommendations import (
    RecommendationAccepted,
    RecommendationCreateRequest,
    RecommendationResponse,
)
from app.services.recommendations import RecommendationService

router = APIRouter(prefix="/api/v1/recommendations", tags=["recommendations"])


def _service(request: Request) -> RecommendationService:
    service = getattr(request.app.state, "recommendation_service", None)
    if service is None:
        raise DomainError(503, "SERVICE_UNAVAILABLE", "推荐服务暂不可用")
    return service


@router.post("", response_model=RecommendationAccepted, status_code=202)
def create_recommendation(
    body: RecommendationCreateRequest,
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(require_principal)],
    _same_origin: Annotated[None, Depends(require_same_origin)],
) -> RecommendationAccepted:
    recommendation_id = _service(request).create_recommendation(
        owner_user_id=principal.user_id,
        reference_set_id=body.reference_set_id,
    )
    response.headers["Cache-Control"] = "no-store"
    return RecommendationAccepted(recommendation_id=recommendation_id)


@router.get("/{recommendation_id}", response_model=RecommendationResponse)
def get_recommendation(
    recommendation_id: UUID,
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(require_principal)],
) -> RecommendationResponse:
    response.headers["Cache-Control"] = "no-store"
    return _service(request).get_recommendation(
        owner_user_id=principal.user_id,
        recommendation_id=recommendation_id,
    )
