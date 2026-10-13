from __future__ import annotations

from collections import Counter
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.api.dependencies import Principal, require_principal, require_same_origin
from app.core.domain_errors import DomainError
from app.schemas.references import ReferenceSetResponse
from app.services.references import ReferenceService, ReferenceUploadFile

router = APIRouter(prefix="/api/v1/reference-sets", tags=["references"])


def _service(request: Request) -> ReferenceService:
    service = getattr(request.app.state, "reference_service", None)
    if service is None:
        raise DomainError(503, "SERVICE_UNAVAILABLE", "参考图片服务暂不可用")
    return service


@router.post("", response_model=ReferenceSetResponse, status_code=201)
async def upload_reference_set(
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    _same_origin: Annotated[None, Depends(require_same_origin)],
) -> ReferenceSetResponse:
    service = _service(request)
    try:
        form = await request.form(max_files=5, max_fields=8, max_part_size=4096)
    except Exception:
        raise DomainError(422, "REQUEST_INVALID", "上传表单格式无效") from None

    try:
        allowed_names = {
            "product_category",
            "primary_product_image",
            "supplementary_product_images",
        }
        counts = Counter(name for name, _value in form.multi_items())
        if any(name not in allowed_names for name in counts):
            raise DomainError(422, "REQUEST_INVALID", "上传表单包含未知字段")

        categories = form.getlist("product_category")
        primary_values = form.getlist("primary_product_image")
        supplementary_values = form.getlist("supplementary_product_images")
        if len(categories) != 1 or not isinstance(categories[0], str):
            raise DomainError(422, "REQUEST_INVALID", "商品品类必须且只能提交一次")
        if len(primary_values) != 1 or not isinstance(primary_values[0], StarletteUploadFile):
            raise DomainError(422, "REQUEST_INVALID", "必须且只能上传一张主图")
        if len(supplementary_values) > 3 or any(
            not isinstance(item, StarletteUploadFile) for item in supplementary_values
        ):
            raise DomainError(422, "REQUEST_INVALID", "辅助图片最多上传 3 张")

        primary = ReferenceUploadFile(
            stream=primary_values[0].file,
            declared_mime_type=primary_values[0].content_type,
        )
        supplementary = [
            ReferenceUploadFile(stream=item.file, declared_mime_type=item.content_type)
            for item in supplementary_values
        ]
        return await run_in_threadpool(
            service.upload_reference_set,
            owner_user_id=principal.user_id,
            product_category=categories[0],
            primary_file=primary,
            supplementary_files=supplementary,
        )
    finally:
        await form.close()


@router.get("/{reference_set_id}", response_model=ReferenceSetResponse)
def get_reference_set(
    reference_set_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
) -> ReferenceSetResponse:
    return _service(request).get_reference_set(
        owner_user_id=principal.user_id,
        reference_set_id=reference_set_id,
    )
