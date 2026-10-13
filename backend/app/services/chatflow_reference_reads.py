from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.domain_errors import DomainError
from app.core.upload_limits import IMAGE_MAX_BYTES
from app.db.business_session_models import BusinessSession
from app.db.chatflow_generation_models import (
    ChatflowGenerationPermit,
    ChatflowGenerationRequest,
    ChatflowPromptPackage,
    ChatflowPromptReference,
)
from app.db.reference_models import ImageAsset, ReferenceSetImage
from app.db.task_models import Task
from app.integrations.storage.protocol import Storage

_REFERENCE_ALIASES = ("PRIMARY", "AUX01", "AUX02", "AUX03")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}


@dataclass(frozen=True, slots=True)
class ChatflowReferenceContent:
    content: bytes
    mime_type: str
    generation_request_id: UUID
    task_id: UUID
    asset_id: UUID
    ref_id: str
    position: int
    content_sha256: str


class ChatflowReferenceReadService:
    """Serve only reference bytes pinned by a live H4 request and permit."""

    def __init__(self, engine: Engine, storage: Storage) -> None:
        self._engine = engine
        self._storage = storage

    def read_reference(
        self,
        *,
        generation_request_id: UUID,
        position: int,
        expected_task_id: UUID,
        expected_asset_id: UUID,
        expected_ref_id: str,
        expected_sha256: str,
    ) -> ChatflowReferenceContent:
        if (
            not 1 <= position <= 4
            or expected_ref_id not in _REFERENCE_ALIASES
            or _SHA256.fullmatch(expected_sha256) is None
        ):
            raise DomainError(404, "REFERENCE_NOT_FOUND", "参考图片不可用")

        # Copy the immutable locator and identity under a short read transaction;
        # storage I/O happens after the Session is closed.
        with Session(self._engine) as db:
            request = db.get(ChatflowGenerationRequest, generation_request_id)
            if request is None or request.task_id != expected_task_id:
                raise DomainError(404, "REFERENCE_NOT_FOUND", "参考图片不可用")
            package = db.get(ChatflowPromptPackage, request.package_id)
            permit = db.execute(
                select(ChatflowGenerationPermit).where(
                    ChatflowGenerationPermit.generation_request_id == request.id,
                    ChatflowGenerationPermit.task_id == request.task_id,
                )
            ).scalar_one_or_none()
            task = db.get(Task, request.task_id)
            business_session = db.get(BusinessSession, request.business_session_id)
            references = db.execute(
                select(ChatflowPromptReference)
                .where(
                    ChatflowPromptReference.package_id == request.package_id,
                    ChatflowPromptReference.task_id == request.task_id,
                )
                .order_by(ChatflowPromptReference.position, ChatflowPromptReference.ref_id)
            ).scalars().all()
            if (
                package is None
                or permit is None
                or task is None
                or business_session is None
                or request.package_id != package.id
                or request.task_id != package.task_id
                or request.business_session_id != package.business_session_id
                or task.business_session_id != business_session.id
                or task.owner_user_id != package.owner_user_id
                or task.reference_set_id != package.reference_set_id
                or task.recommendation_id != package.recommendation_id
                or business_session.owner_user_id != task.owner_user_id
                or business_session.reference_set_id != task.reference_set_id
                or permit.permit_version != 2
                or permit.slot_mapping_digest is None
                or permit.reference_manifest_digest is None
                or permit.package_digest != request.package_digest
                or permit.package_digest != package.package_digest
                or permit.expires_at <= db.scalar(select(func.clock_timestamp()))
                or not 1 <= len(references) <= 4
            ):
                raise DomainError(404, "REFERENCE_NOT_FOUND", "参考图片不可用")

            manifest = [
                {
                    "asset_id": str(reference.asset_id),
                    "position": reference.position,
                    "content_sha256": reference.content_sha256,
                    "ref_id": reference.ref_id,
                }
                for reference in references
            ]
            encoded = json.dumps(
                manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
            if hashlib.sha256(encoded).digest() != permit.reference_manifest_digest:
                raise DomainError(404, "REFERENCE_NOT_FOUND", "参考图片不可用")

            selected = references[position - 1]
            if (
                selected.position != position
                or selected.ref_id != _REFERENCE_ALIASES[position - 1]
                or selected.asset_id != expected_asset_id
                or selected.ref_id != expected_ref_id
                or selected.content_sha256 != expected_sha256
            ):
                raise DomainError(404, "REFERENCE_NOT_FOUND", "参考图片不可用")

            asset = db.execute(
                select(ImageAsset)
                .join(ReferenceSetImage, ReferenceSetImage.model_asset_id == ImageAsset.id)
                .where(
                    ReferenceSetImage.reference_set_id == task.reference_set_id,
                    ReferenceSetImage.owner_user_id == task.owner_user_id,
                    ReferenceSetImage.model_asset_id == selected.asset_id,
                    ReferenceSetImage.position == position - 1,
                    ReferenceSetImage.slot == _REFERENCE_ALIASES[position - 1],
                    ReferenceSetImage.content_sha256 == selected.content_sha256,
                    ImageAsset.id == selected.asset_id,
                    ImageAsset.owner_user_id == task.owner_user_id,
                    ImageAsset.asset_kind == "reference_model_input",
                    ImageAsset.content_sha256 == selected.content_sha256,
                )
            ).scalar_one_or_none()
            if (
                asset is None
                or asset.mime_type not in _ALLOWED_MIME
                or asset.size_bytes > IMAGE_MAX_BYTES
            ):
                raise DomainError(404, "REFERENCE_NOT_FOUND", "参考图片不可用")
            object_key = asset.object_key
            mime_type = asset.mime_type
            expected_size = asset.size_bytes
            actual_identity = ChatflowReferenceContent(
                content=b"",
                mime_type=mime_type,
                generation_request_id=request.id,
                task_id=task.id,
                asset_id=asset.id,
                ref_id=selected.ref_id,
                position=selected.position,
                content_sha256=selected.content_sha256,
            )

        try:
            with self._storage.open_reader(object_key) as reader:
                content = reader.read(IMAGE_MAX_BYTES + 1)
        except Exception:
            raise DomainError(
                503, "REFERENCE_STORAGE_UNAVAILABLE", "参考图片暂时无法读取"
            ) from None
        if (
            len(content) != expected_size
            or len(content) > IMAGE_MAX_BYTES
            or hashlib.sha256(content).hexdigest() != actual_identity.content_sha256
        ):
            raise DomainError(503, "REFERENCE_STORAGE_INTEGRITY", "参考图片完整性校验失败")
        return ChatflowReferenceContent(
            content=content,
            mime_type=actual_identity.mime_type,
            generation_request_id=actual_identity.generation_request_id,
            task_id=actual_identity.task_id,
            asset_id=actual_identity.asset_id,
            ref_id=actual_identity.ref_id,
            position=actual_identity.position,
            content_sha256=actual_identity.content_sha256,
        )
