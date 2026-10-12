from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.dependencies import Principal, require_principal, require_same_origin
from app.billing.disabled import DisabledBillingService
from app.core.domain_errors import DomainError
from app.schemas.billing import DisabledFeatureResponse

router = APIRouter(prefix="/api/v1", tags=["billing"])
_disabled_billing_service = DisabledBillingService()


def _billing_service(request: Request) -> DisabledBillingService:
    service = getattr(request.app.state, "billing_service", _disabled_billing_service)
    if service is None:
        raise DomainError(503, "SERVICE_UNAVAILABLE", "计费服务暂不可用")
    return service


@router.get("/quota", response_model=DisabledFeatureResponse)
def quota(
    request: Request,
    _principal: Annotated[Principal, Depends(require_principal)],
) -> DisabledFeatureResponse:
    return DisabledFeatureResponse(**_billing_service(request).quota_status())


@router.get("/billing/balance", response_model=DisabledFeatureResponse)
def balance(
    request: Request,
    _principal: Annotated[Principal, Depends(require_principal)],
) -> DisabledFeatureResponse:
    return DisabledFeatureResponse(**_billing_service(request).balance_status())


@router.post("/billing/recharge-orders", status_code=503, response_model=None)
def create_recharge_order(
    request: Request,
    _origin: Annotated[None, Depends(require_same_origin)],
    _principal: Annotated[Principal, Depends(require_principal)],
) -> None:
    _billing_service(request).create_recharge_order()
