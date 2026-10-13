from __future__ import annotations

import hashlib
import io
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import BinaryIO, Literal
from uuid import UUID, uuid4

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, aliased

from app.core.domain_errors import DomainError
from app.core.upload_limits import (
    IMAGE_MAX_BYTES,
    IMAGE_MAX_PIXELS,
    REFERENCE_SET_MAX_BYTES,
)
from app.db.reference_models import (
    ImageAsset,
    ReferenceSet,
    ReferenceSetImage,
    ReferenceUploadObject,
)
from app.integrations.storage.protocol import ObjectStat, Storage, StorageError
from app.schemas.references import (
    ReferenceAuxiliaryImageResponse,
    ReferencePrimaryImageResponse,
    ReferenceSetResponse,
)
from app.services.retention_policy import (
    RetentionPolicyUnavailable,
    load_retention_policy_in_transaction,
)

ImageSlot = Literal["PRIMARY", "AUX01", "AUX02", "AUX03"]
ImageAssetKind = Literal["source", "model_input"]


@dataclass(frozen=True, slots=True)
class ReferenceOptions:
    upload_lease_seconds: int = 1800
    orphan_grace_hours: int = 24

    def __post_init__(self) -> None:
        if not 60 <= self.upload_lease_seconds <= 86_400:
            raise ValueError("upload_lease_seconds must be between 60 and 86400")
        if not 24 <= self.orphan_grace_hours <= 8760:
            raise ValueError("orphan_grace_hours must be between 24 and 8760")


@dataclass(frozen=True, slots=True)
class ReferenceUploadFile:
    stream: BinaryIO
    declared_mime_type: str | None


@dataclass(frozen=True, slots=True)
class _PreparedImage:
    image_id: UUID
    slot: ImageSlot
    mime_type: str
    source_bytes: bytes
    source_sha256: str
    source_width: int
    source_height: int
    model_bytes: bytes
    model_sha256: str
    model_width: int
    model_height: int


@dataclass(frozen=True, slots=True)
class _ReservedObject:
    reservation_id: UUID
    asset_id: UUID
    slot: ImageSlot
    asset_kind: ImageAssetKind
    object_key: str
    sha256: str
    size_bytes: int
    mime_type: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class ReferenceModelInput:
    image_id: UUID
    slot: ImageSlot
    mime_type: str
    size_bytes: int
    width: int
    height: int
    content: bytes


class ReferenceService:
    """Owns short database sessions and all reference upload state transitions."""

    def __init__(self, engine: Engine, storage: Storage, options: ReferenceOptions) -> None:
        self._engine = engine
        self._storage = storage
        self._options = options

    def upload_reference_set(
        self,
        *,
        owner_user_id: UUID,
        product_category: str,
        primary_file: ReferenceUploadFile,
        supplementary_files: list[ReferenceUploadFile],
    ) -> ReferenceSetResponse:
        category = self._validate_category(product_category)
        if len(supplementary_files) > 3:
            raise DomainError(422, "REQUEST_INVALID", "辅助图片最多上传 3 张")

        prepared_images = self._prepare_batch(primary_file, supplementary_files)
        reference_set_id = uuid4()
        lease_token = uuid4()
        reserved_objects = self._make_reservations(owner_user_id, reference_set_id, prepared_images)
        current_slot: str | None = None
        objects_were_started = False

        try:
            self._begin_upload(
                owner_user_id=owner_user_id,
                reference_set_id=reference_set_id,
                lease_token=lease_token,
                product_category=category,
                objects=reserved_objects,
            )
            objects_were_started = True

            object_by_slot_kind = {(item.slot, item.asset_kind): item for item in reserved_objects}
            for prepared in prepared_images:
                for kind, body, sha, size, width, height in (
                    (
                        "source",
                        prepared.source_bytes,
                        prepared.source_sha256,
                        len(prepared.source_bytes),
                        prepared.source_width,
                        prepared.source_height,
                    ),
                    (
                        "model_input",
                        prepared.model_bytes,
                        prepared.model_sha256,
                        len(prepared.model_bytes),
                        prepared.model_width,
                        prepared.model_height,
                    ),
                ):
                    current_slot = prepared.slot
                    reserved = object_by_slot_kind[(prepared.slot, kind)]
                    stat = self._storage.put_immutable(
                        reserved.object_key,
                        io.BytesIO(body),
                        {
                            "sha256": sha,
                            "size_bytes": size,
                            "mime_type": prepared.mime_type,
                            "width": width,
                            "height": height,
                            "asset_kind": kind,
                        },
                    )
                    self._verify_stored_object(reserved, stat)
                    self._mark_object_stored(reserved.reservation_id)

            current_slot = None
            self._commit_ready_group(
                owner_user_id=owner_user_id,
                reference_set_id=reference_set_id,
                lease_token=lease_token,
                category=category,
                prepared_images=prepared_images,
                objects=reserved_objects,
            )
            return self._response(reference_set_id, category, prepared_images)
        except Exception as exc:
            outcome = self._resolve_failed_upload(
                reference_set_id=reference_set_id,
                lease_token=lease_token,
                objects=reserved_objects,
                storage_may_contain_objects=objects_were_started,
                failure_code="REFERENCE_UPLOAD_FAILED",
            )
            if outcome == "ready":
                return self._response(reference_set_id, category, prepared_images)
            if isinstance(exc, DomainError):
                raise exc
            if current_slot:
                raise DomainError(
                    503,
                    "REFERENCE_STORAGE_FAILED",
                    f"{current_slot} 图片保存失败，未创建可用参考组",
                ) from None
            raise DomainError(
                503,
                "REFERENCE_UPLOAD_FAILED",
                "参考图片暂时无法保存，未创建可用参考组",
            ) from None

    def get_reference_set(
        self, *, owner_user_id: UUID, reference_set_id: UUID
    ) -> ReferenceSetResponse:
        source_asset = aliased(ImageAsset)
        model_asset = aliased(ImageAsset)
        try:
            with Session(self._engine) as session:
                reference_set = session.scalar(
                    select(ReferenceSet).where(
                        ReferenceSet.id == reference_set_id,
                        ReferenceSet.owner_user_id == owner_user_id,
                        ReferenceSet.status == "ready",
                        ReferenceSet.expires_at > func.current_timestamp(),
                    )
                )
                if reference_set is None:
                    raise DomainError(404, "REFERENCE_NOT_FOUND", "未找到请求的参考组")

                records = session.execute(
                    select(ReferenceSetImage, source_asset, model_asset)
                    .join(source_asset, ReferenceSetImage.source_asset_id == source_asset.id)
                    .join(model_asset, ReferenceSetImage.model_asset_id == model_asset.id)
                    .where(
                        ReferenceSetImage.reference_set_id == reference_set_id,
                        ReferenceSetImage.owner_user_id == owner_user_id,
                    )
                    .order_by(ReferenceSetImage.position)
                ).all()
                primary_records = [record for record in records if record[0].slot == "PRIMARY"]
                auxiliary_records = [record for record in records if record[0].slot != "PRIMARY"]
                if len(primary_records) != 1 or len(auxiliary_records) > 3:
                    raise DomainError(503, "REFERENCE_UNAVAILABLE", "参考组暂时不可用")

                primary = self._image_response(*primary_records[0])
                auxiliary = [self._image_response(*record) for record in auxiliary_records]
                return ReferenceSetResponse(
                    reference_set_id=reference_set.id,
                    product_category=reference_set.product_category,
                    status="ready",
                    primary_image=primary,
                    auxiliary_images=auxiliary,
                )
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "参考组暂时无法读取") from None

    def read_model_inputs(
        self, *, owner_user_id: UUID, reference_set_id: UUID
    ) -> tuple[ReferenceModelInput, ...]:
        """Read the normalized images in slot order for trusted server-side callers."""
        try:
            with Session(self._engine) as session:
                records = session.execute(
                    select(ReferenceSetImage, ImageAsset)
                    .join(ImageAsset, ReferenceSetImage.model_asset_id == ImageAsset.id)
                    .join(ReferenceSet, ReferenceSet.id == ReferenceSetImage.reference_set_id)
                    .where(
                        ReferenceSet.id == reference_set_id,
                        ReferenceSet.owner_user_id == owner_user_id,
                        ReferenceSet.status == "ready",
                        ReferenceSet.expires_at > func.current_timestamp(),
                        ReferenceSetImage.owner_user_id == owner_user_id,
                    )
                    .order_by(ReferenceSetImage.position)
                ).all()
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "参考图片暂时无法读取") from None

        if not records or len(records) > 4 or sum(row[0].slot == "PRIMARY" for row in records) != 1:
            raise DomainError(404, "REFERENCE_NOT_FOUND", "未找到请求的参考组")

        images: list[ReferenceModelInput] = []
        for reference_image, asset in records:
            try:
                with self._storage.open_reader(asset.object_key) as reader:
                    content = reader.read(IMAGE_MAX_BYTES + 1)
            except (OSError, StorageError):
                raise DomainError(
                    503, "REFERENCE_STORAGE_UNAVAILABLE", "参考图片暂时无法读取"
                ) from None
            if (
                len(content) != asset.size_bytes
                or len(content) > IMAGE_MAX_BYTES
                or hashlib.sha256(content).hexdigest() != asset.content_sha256
            ):
                raise DomainError(503, "REFERENCE_STORAGE_INTEGRITY", "参考图片完整性校验失败")
            images.append(
                ReferenceModelInput(
                    image_id=reference_image.id,
                    slot=reference_image.slot,
                    mime_type=asset.mime_type,
                    size_bytes=asset.size_bytes,
                    width=asset.width,
                    height=asset.height,
                    content=content,
                )
            )
        return tuple(images)

    def _prepare_batch(
        self,
        primary_file: ReferenceUploadFile,
        supplementary_files: list[ReferenceUploadFile],
    ) -> list[_PreparedImage]:
        entries: list[tuple[ImageSlot, ReferenceUploadFile]] = [("PRIMARY", primary_file)]
        entries.extend(
            (f"AUX{index:02d}", item) for index, item in enumerate(supplementary_files, start=1)
        )
        prepared: list[_PreparedImage] = []
        total_bytes = 0
        seen_content: set[str] = set()
        for slot, upload in entries:
            raw = self._read_limited(upload.stream, slot)
            total_bytes += len(raw)
            if total_bytes > REFERENCE_SET_MAX_BYTES:
                raise DomainError(413, "UPLOAD_TOO_LARGE", "参考组图片总大小不能超过 40 MiB")
            item = self._decode_image(slot, upload.declared_mime_type, raw)
            if item.model_sha256 in seen_content:
                raise DomainError(
                    422, "DUPLICATE_REFERENCE_IMAGE", f"{slot} 图片与组内其他图片内容重复"
                )
            seen_content.add(item.model_sha256)
            prepared.append(item)
        return prepared

    @staticmethod
    def _validate_category(value: str) -> str:
        if not isinstance(value, str):
            raise DomainError(422, "REQUEST_INVALID", "商品品类必须是文本")
        category = value.strip()
        if not 1 <= len(category) <= 100:
            raise DomainError(422, "REQUEST_INVALID", "商品品类长度须为 1 至 100 个字符")
        return category

    @staticmethod
    def _read_limited(stream: BinaryIO, slot: str) -> bytes:
        chunks: list[bytes] = []
        byte_count = 0
        try:
            stream.seek(0)
            while True:
                chunk = stream.read(min(64 * 1024, IMAGE_MAX_BYTES + 1 - byte_count))
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise TypeError
                chunks.append(chunk)
                byte_count += len(chunk)
                if byte_count > IMAGE_MAX_BYTES:
                    raise DomainError(413, "UPLOAD_TOO_LARGE", f"{slot} 图片不能超过 10 MiB")
        except DomainError:
            raise
        except (OSError, ValueError, TypeError):
            raise DomainError(422, "REFERENCE_IMAGE_INVALID", f"{slot} 图片无法读取") from None
        if byte_count == 0:
            raise DomainError(422, "REFERENCE_IMAGE_INVALID", f"{slot} 图片不能为空")
        return b"".join(chunks)

    @staticmethod
    def _decode_image(
        slot: ImageSlot, declared_mime_type: str | None, raw: bytes
    ) -> _PreparedImage:
        signature_format = ReferenceService._signature_format(raw)
        if signature_format is None:
            raise DomainError(422, "REFERENCE_IMAGE_INVALID", f"{slot} 图片格式不受支持")
        mime_by_format = {
            "JPEG": "image/jpeg",
            "PNG": "image/png",
            "WEBP": "image/webp",
        }
        mime_type = mime_by_format[signature_format]
        if declared_mime_type != mime_type:
            raise DomainError(
                422, "REFERENCE_IMAGE_INVALID", f"{slot} 图片声明类型与文件内容不一致"
            )

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as decoded:
                    if decoded.format != signature_format:
                        raise ValueError
                    source_width, source_height = decoded.size
                    if (
                        source_width < 1
                        or source_height < 1
                        or source_width * source_height > IMAGE_MAX_PIXELS
                    ):
                        raise DomainError(
                            422,
                            "REFERENCE_IMAGE_TOO_MANY_PIXELS",
                            f"{slot} 图片像素不能超过 2500 万",
                        )
                    if getattr(decoded, "n_frames", 1) != 1:
                        raise DomainError(
                            422, "REFERENCE_IMAGE_ANIMATED", f"{slot} 不支持动画或多帧图片"
                        )
                    decoded.verify()

                with Image.open(io.BytesIO(raw)) as decoded:
                    decoded.load()
                    source_width, source_height = decoded.size
                    oriented = ImageOps.exif_transpose(decoded)
                    model_width, model_height = oriented.size
                    if model_width * model_height > IMAGE_MAX_PIXELS:
                        raise DomainError(
                            422,
                            "REFERENCE_IMAGE_TOO_MANY_PIXELS",
                            f"{slot} 图片像素不能超过 2500 万",
                        )
                    mode = ReferenceService._normalized_mode(oriented, signature_format)
                    pixels = oriented.convert(mode)
                    clean = Image.new(mode, pixels.size)
                    clean.paste(pixels)
                    output = io.BytesIO()
                    save_options: dict[str, object] = {}
                    if signature_format == "JPEG":
                        save_options = {"quality": 95, "optimize": False}
                    elif signature_format == "PNG":
                        save_options = {"compress_level": 6, "optimize": False}
                    elif signature_format == "WEBP":
                        save_options = {"quality": 95, "method": 4}
                    clean.save(output, format=signature_format, **save_options)
                    normalized = output.getvalue()
        except DomainError:
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise DomainError(
                422, "REFERENCE_IMAGE_TOO_MANY_PIXELS", f"{slot} 图片像素不能超过 2500 万"
            ) from None
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError):
            raise DomainError(
                422, "REFERENCE_IMAGE_INVALID", f"{slot} 图片损坏或无法解码"
            ) from None

        if not normalized or len(normalized) > IMAGE_MAX_BYTES:
            raise DomainError(
                422, "REFERENCE_IMAGE_NORMALIZED_TOO_LARGE", f"{slot} 标准化图片不能超过 10 MiB"
            )
        return _PreparedImage(
            image_id=uuid4(),
            slot=slot,
            mime_type=mime_type,
            source_bytes=raw,
            source_sha256=hashlib.sha256(raw).hexdigest(),
            source_width=source_width,
            source_height=source_height,
            model_bytes=normalized,
            model_sha256=hashlib.sha256(normalized).hexdigest(),
            model_width=model_width,
            model_height=model_height,
        )

    @staticmethod
    def _signature_format(raw: bytes) -> str | None:
        if raw.startswith(b"\xff\xd8\xff"):
            return "JPEG"
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return "PNG"
        if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            return "WEBP"
        return None

    @staticmethod
    def _normalized_mode(image: Image.Image, image_format: str) -> str:
        if image_format == "JPEG" and image.mode in {"L", "RGB"}:
            return image.mode
        if image.mode in {"RGB", "RGBA", "L", "LA"}:
            return image.mode
        has_alpha = "A" in image.getbands() or "transparency" in image.info
        return "RGBA" if has_alpha else "RGB"

    @staticmethod
    def _make_reservations(
        owner_user_id: UUID, reference_set_id: UUID, images: list[_PreparedImage]
    ) -> list[_ReservedObject]:
        extension_by_mime = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
        records: list[_ReservedObject] = []
        for image in images:
            for kind, body, sha, width, height in (
                (
                    "source",
                    image.source_bytes,
                    image.source_sha256,
                    image.source_width,
                    image.source_height,
                ),
                (
                    "model_input",
                    image.model_bytes,
                    image.model_sha256,
                    image.model_width,
                    image.model_height,
                ),
            ):
                asset_id = uuid4()
                key = (
                    f"users/{owner_user_id}/reference-sets/{reference_set_id}/"
                    f"{image.slot}/{asset_id}-{kind}.{extension_by_mime[image.mime_type]}"
                )
                records.append(
                    _ReservedObject(
                        reservation_id=uuid4(),
                        asset_id=asset_id,
                        slot=image.slot,
                        asset_kind=kind,
                        object_key=key,
                        sha256=sha,
                        size_bytes=len(body),
                        mime_type=image.mime_type,
                        width=width,
                        height=height,
                    )
                )
        return records

    def _begin_upload(
        self,
        *,
        owner_user_id: UUID,
        reference_set_id: UUID,
        lease_token: UUID,
        product_category: str,
        objects: list[_ReservedObject],
    ) -> None:
        try:
            with Session(self._engine) as session, session.begin():
                now = session.scalar(select(func.current_timestamp()))
                assert isinstance(now, datetime)
                policy = load_retention_policy_in_transaction(session)
                reference_set = ReferenceSet(
                    id=reference_set_id,
                    owner_user_id=owner_user_id,
                    product_category=product_category,
                    status="uploading",
                    lease_token=lease_token,
                    upload_lease_until=now + timedelta(seconds=self._options.upload_lease_seconds),
                    retention_days=policy.reference_retention_days,
                    retention_policy_revision=policy.revision,
                    created_at=now,
                    last_activity_at=now,
                )
                session.add(reference_set)
                session.add_all(
                    ReferenceUploadObject(
                        id=item.reservation_id,
                        reference_set_id=reference_set_id,
                        owner_user_id=owner_user_id,
                        slot=item.slot,
                        asset_kind=item.asset_kind,
                        object_key=item.object_key,
                        expected_sha256=item.sha256,
                        expected_size_bytes=item.size_bytes,
                        status="planned",
                    )
                    for item in objects
                )
        except (SQLAlchemyError, RetentionPolicyUnavailable):
            raise DomainError(503, "DATABASE_UNAVAILABLE", "无法建立参考图片上传记录") from None

    @staticmethod
    def _verify_stored_object(reservation: _ReservedObject, stat: ObjectStat) -> None:
        if (
            stat.key != reservation.object_key
            or stat.size_bytes != reservation.size_bytes
            or stat.sha256 != reservation.sha256
        ):
            raise StorageError("stored object did not match its upload record")

    def _mark_object_stored(self, reservation_id: UUID) -> None:
        try:
            with Session(self._engine) as session, session.begin():
                result = session.execute(
                    update(ReferenceUploadObject)
                    .where(
                        ReferenceUploadObject.id == reservation_id,
                        ReferenceUploadObject.status == "planned",
                    )
                    .values(status="stored", updated_at=func.current_timestamp())
                )
                if result.rowcount != 1:
                    raise DomainError(409, "UPLOAD_LEASE_LOST", "参考图片上传状态已变化")
        except DomainError:
            raise
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "无法确认参考图片保存状态") from None

    def _commit_ready_group(
        self,
        *,
        owner_user_id: UUID,
        reference_set_id: UUID,
        lease_token: UUID,
        category: str,
        prepared_images: list[_PreparedImage],
        objects: list[_ReservedObject],
    ) -> None:
        by_slot_kind = {(item.slot, item.asset_kind): item for item in objects}
        try:
            with Session(self._engine) as session, session.begin():
                now = session.scalar(select(func.current_timestamp()))
                assert isinstance(now, datetime)
                reference_set = session.execute(
                    select(ReferenceSet)
                    .where(
                        ReferenceSet.id == reference_set_id,
                        ReferenceSet.owner_user_id == owner_user_id,
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if (
                    reference_set is None
                    or reference_set.status != "uploading"
                    or reference_set.lease_token != lease_token
                    or reference_set.upload_lease_until is None
                    or reference_set.upload_lease_until <= now
                ):
                    raise DomainError(409, "UPLOAD_LEASE_LOST", "参考图片上传租约已失效")
                reference_set.status = "ready"
                reference_set.lease_token = None
                reference_set.upload_lease_until = None
                reference_set.ready_at = now
                reference_set.expires_at = now + timedelta(days=reference_set.retention_days)
                reference_set.last_activity_at = now

                for item in objects:
                    stored = session.execute(
                        update(ReferenceUploadObject)
                        .where(
                            ReferenceUploadObject.id == item.reservation_id,
                            ReferenceUploadObject.status == "stored",
                        )
                        .values(status="attached", updated_at=now)
                    )
                    if stored.rowcount != 1:
                        raise DomainError(409, "UPLOAD_OBJECT_INCOMPLETE", "参考图片尚未全部保存")
                    session.add(
                        ImageAsset(
                            id=item.asset_id,
                            owner_user_id=owner_user_id,
                            asset_kind=(
                                "reference_source"
                                if item.asset_kind == "source"
                                else "reference_model_input"
                            ),
                            object_key=item.object_key,
                            content_sha256=item.sha256,
                            mime_type=item.mime_type,
                            size_bytes=item.size_bytes,
                            width=item.width,
                            height=item.height,
                            created_at=now,
                        )
                    )

                for position, image in enumerate(prepared_images):
                    source = by_slot_kind[(image.slot, "source")]
                    model_input = by_slot_kind[(image.slot, "model_input")]
                    session.add(
                        ReferenceSetImage(
                            id=image.image_id,
                            reference_set_id=reference_set_id,
                            owner_user_id=owner_user_id,
                            slot=image.slot,
                            position=position,
                            source_asset_id=source.asset_id,
                            model_asset_id=model_input.asset_id,
                            content_sha256=image.model_sha256,
                            created_at=now,
                        )
                    )
        except DomainError:
            raise
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "无法提交完整参考组") from None

    def _resolve_failed_upload(
        self,
        *,
        reference_set_id: UUID,
        lease_token: UUID,
        objects: list[_ReservedObject],
        storage_may_contain_objects: bool,
        failure_code: str,
    ) -> Literal["ready", "failed", "unknown"]:
        state = self._read_set_state(reference_set_id)
        if state == "ready":
            return "ready"
        if state == "unknown":
            return "unknown"

        failed = self._mark_failed(reference_set_id, lease_token, failure_code)
        if not failed:
            return "unknown"
        if storage_may_contain_objects:
            self._cleanup_reserved_objects(objects)
        return "failed"

    def _read_set_state(self, reference_set_id: UUID) -> str | None:
        try:
            with Session(self._engine) as session:
                return session.scalar(
                    select(ReferenceSet.status).where(ReferenceSet.id == reference_set_id)
                )
        except SQLAlchemyError:
            return "unknown"

    def _mark_failed(self, reference_set_id: UUID, lease_token: UUID, failure_code: str) -> bool:
        try:
            with Session(self._engine) as session, session.begin():
                now = session.scalar(select(func.current_timestamp()))
                state = session.scalar(
                    select(ReferenceSet.status).where(ReferenceSet.id == reference_set_id)
                )
                if state == "ready":
                    return False
                if state == "uploading":
                    changed = session.execute(
                        update(ReferenceSet)
                        .where(
                            ReferenceSet.id == reference_set_id,
                            ReferenceSet.status == "uploading",
                            ReferenceSet.lease_token == lease_token,
                        )
                        .values(
                            status="failed",
                            lease_token=None,
                            upload_lease_until=None,
                            failed_at=now,
                            failure_code=failure_code,
                            last_activity_at=now,
                        )
                    )
                    if changed.rowcount != 1:
                        return False
                elif state is None:
                    return True
                session.execute(
                    update(ReferenceUploadObject)
                    .where(
                        ReferenceUploadObject.reference_set_id == reference_set_id,
                        ReferenceUploadObject.status.in_(("planned", "stored", "cleanup_pending")),
                    )
                    .values(status="cleanup_pending", updated_at=now)
                )
            return True
        except SQLAlchemyError:
            return False

    def _cleanup_reserved_objects(self, objects: list[_ReservedObject]) -> None:
        for item in objects:
            try:
                self._storage.delete(item.object_key)
            except StorageError:
                continue
            try:
                with Session(self._engine) as session, session.begin():
                    session.execute(
                        update(ReferenceUploadObject)
                        .where(
                            ReferenceUploadObject.id == item.reservation_id,
                            ReferenceUploadObject.status == "cleanup_pending",
                        )
                        .values(status="deleted", updated_at=func.current_timestamp())
                    )
            except SQLAlchemyError:
                continue

    @staticmethod
    def _response(
        reference_set_id: UUID, category: str, images: list[_PreparedImage]
    ) -> ReferenceSetResponse:
        public_images = [ReferenceService._prepared_image_response(image) for image in images]
        return ReferenceSetResponse(
            reference_set_id=reference_set_id,
            product_category=category,
            status="ready",
            primary_image=public_images[0],
            auxiliary_images=public_images[1:],
        )

    @staticmethod
    def _prepared_image_response(
        image: _PreparedImage,
    ) -> ReferencePrimaryImageResponse | ReferenceAuxiliaryImageResponse:
        response_model = (
            ReferencePrimaryImageResponse
            if image.slot == "PRIMARY"
            else ReferenceAuxiliaryImageResponse
        )
        return response_model(
            image_id=image.image_id,
            slot=image.slot,
            mime_type=image.mime_type,
            size_bytes=len(image.source_bytes),
            width=image.model_width,
            height=image.model_height,
        )

    @staticmethod
    def _image_response(
        reference_image: ReferenceSetImage,
        source_asset: ImageAsset,
        model_asset: ImageAsset,
    ) -> ReferencePrimaryImageResponse | ReferenceAuxiliaryImageResponse:
        response_model = (
            ReferencePrimaryImageResponse
            if reference_image.slot == "PRIMARY"
            else ReferenceAuxiliaryImageResponse
        )
        return response_model(
            image_id=reference_image.id,
            slot=reference_image.slot,
            mime_type=source_asset.mime_type,
            size_bytes=source_asset.size_bytes,
            width=model_asset.width,
            height=model_asset.height,
        )
