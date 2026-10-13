from __future__ import annotations

import hmac
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Response
from sqlalchemy.engine import Engine

from app.core.domain_errors import DomainError
from app.integrations.storage.protocol import Storage
from app.services.chatflow_reference_reads import ChatflowReferenceReadService


def create_internal_image_generation_router(
    *, engine: Engine, storage: Storage, service_credential: str
) -> APIRouter:
    if len(service_credential) < 16 or not service_credential.isascii():
        raise ValueError("image-generation service credential is invalid")

    router = APIRouter(prefix="/internal/v1/image-generation", tags=["internal-image-generation"])
    service = ChatflowReferenceReadService(engine, storage)

    @router.get(
        "/generation-requests/{generation_request_id}/references/{position}/content"
    )
    def read_generation_reference(
        generation_request_id: UUID,
        position: int,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        expected_task_id: Annotated[UUID | None, Header(alias="X-Expected-Task-ID")] = None,
        expected_asset_id: Annotated[UUID | None, Header(alias="X-Expected-Asset-ID")] = None,
        expected_ref_id: Annotated[str | None, Header(alias="X-Expected-Ref-ID")] = None,
        expected_sha256: Annotated[str | None, Header(alias="X-Expected-SHA256")] = None,
    ) -> Response:
        if not authorization or not hmac.compare_digest(
            authorization, f"Bearer {service_credential}"
        ):
            raise DomainError(401, "UNAUTHORIZED", "服务认证失败")
        if (
            expected_task_id is None
            or expected_asset_id is None
            or expected_ref_id is None
            or expected_sha256 is None
        ):
            raise DomainError(404, "REFERENCE_NOT_FOUND", "参考图片不可用")
        item = service.read_reference(
            generation_request_id=generation_request_id,
            position=position,
            expected_task_id=expected_task_id,
            expected_asset_id=expected_asset_id,
            expected_ref_id=expected_ref_id,
            expected_sha256=expected_sha256,
        )
        return Response(
            content=item.content,
            media_type=item.mime_type,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Generation-Request-ID": str(item.generation_request_id),
                "X-Task-ID": str(item.task_id),
                "X-Asset-ID": str(item.asset_id),
                "X-Reference-Position": str(item.position),
                "X-Reference-ID": item.ref_id,
                "X-Content-SHA256": item.content_sha256,
            },
        )

    return router
