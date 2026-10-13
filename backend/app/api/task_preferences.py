from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response

from app.api.dependencies import Principal, require_principal, require_same_origin
from app.core.domain_errors import DomainError
from app.schemas.prompt_preferences import PromptPreferenceAccepted, PromptPreferenceRequest
from app.services.prompt_preferences import PromptPreferenceService

router = APIRouter(prefix="/api/v1/tasks", tags=["task-preferences"])


def _service(request: Request) -> PromptPreferenceService:
    service = getattr(request.app.state, "prompt_preference_service", None)
    if service is None:
        raise DomainError(503, "SERVICE_UNAVAILABLE", "偏好服务暂不可用")
    return service


@router.post(
    "/{task_id}/preferences",
    response_model=PromptPreferenceAccepted,
    status_code=202,
)
def submit_preferences(
    task_id: UUID,
    body: PromptPreferenceRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    principal: Annotated[Principal, Depends(require_principal)],
    _same_origin: Annotated[None, Depends(require_same_origin)],
) -> PromptPreferenceAccepted:
    response.headers["Cache-Control"] = "no-store"
    return _service(request).submit(
        owner_user_id=principal.user_id,
        task_id=task_id,
        idempotency_key=idempotency_key,
        request=body,
    )
