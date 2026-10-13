from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from uuid import UUID, uuid4

from sqlalchemy import Engine, and_, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, aliased, sessionmaker

from app.core.domain_errors import DomainError
from app.db.models import User
from app.db.outbox_models import OutboxEvent
from app.db.recommendation_models import Recommendation
from app.db.reference_models import ImageAsset, ReferenceSet, ReferenceSetImage
from app.integrations.dify.contracts import (
    DifyBusinessConflict,
    DifyNotConfigured,
    DifyRecommendationPort,
    RecommendationRun,
    ReferenceAssetSummary,
)
from app.schemas.recommendations import (
    IMAGE_TYPES,
    RecommendationPublicError,
    RecommendationPublicOptions,
    RecommendationResponse,
)
from app.services.outbox import OutboxActionConflict, enqueue_in_transaction
from app.services.retention_policy import (
    RetentionPolicyUnavailable,
    load_retention_policy_in_transaction,
)
from app.services.trusted_analysis import (
    TRUSTED_ANALYSIS_STATE_KEY,
    TrustedAnalysisSnapshotError,
    load_trusted_analysis_snapshot,
    reference_asset_payload,
    trusted_analysis_state,
)
from app.workers.messages import OutboxMessage
from app.workers.processor import HandlerRegistration

RECOMMENDATION_EVENT_TYPE = "recommendation.analyze.v1"
PUBLIC_FAILURE = RecommendationPublicError(
    code="RECOMMENDATION_FAILED", message="推荐分析暂时不可用，请稍后重试"
)


@dataclass(frozen=True, slots=True)
class RecommendationOptions:
    max_active_per_owner: int = 1
    simulated_form_ttl_days: int = 3
    event_operation_version: int = 1
    event_schema_version: int = 1

    def __post_init__(self) -> None:
        if not 1 <= self.max_active_per_owner <= 100:
            raise ValueError("max_active_per_owner must be between 1 and 100")
        if self.simulated_form_ttl_days < 1:
            raise ValueError("recommendation retention periods must be positive")
        if self.event_operation_version != 1 or self.event_schema_version != 1:
            raise ValueError("only recommendation event version 1 is implemented")


class RecommendationService:
    """Owner-scoped recommendation API service; each method owns its PG Session."""

    def __init__(
        self,
        engine: Engine,
        options: RecommendationOptions,
        dify_port: DifyRecommendationPort,
    ) -> None:
        self.options = options
        self.dify_port = dify_port
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

    def create_recommendation(self, *, owner_user_id: UUID, reference_set_id: UUID) -> UUID:
        try:
            with self._sessions() as db, db.begin():
                owner = db.execute(
                    select(User)
                    .where(User.id == owner_user_id, User.status == "active")
                    .with_for_update()
                ).scalar_one_or_none()
                if owner is None:
                    raise DomainError(404, "IDENTITY_NOT_FOUND", "身份不可用")

                retention_policy = load_retention_policy_in_transaction(db)
                now = db.scalar(select(func.clock_timestamp()))
                reference_set = db.execute(
                    select(ReferenceSet)
                    .where(
                        ReferenceSet.id == reference_set_id,
                        ReferenceSet.owner_user_id == owner_user_id,
                        ReferenceSet.status == "ready",
                        ReferenceSet.expires_at > now,
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if reference_set is None:
                    raise DomainError(404, "REFERENCE_SET_NOT_FOUND", "参考组不可用")

                reference_assets = self._reference_asset_summary(
                    db, owner_user_id=owner_user_id, reference_set_id=reference_set_id
                )
                if not reference_assets:
                    raise DomainError(404, "REFERENCE_SET_NOT_FOUND", "参考组不可用")

                active_count = db.scalar(
                    select(func.count(Recommendation.id)).where(
                        Recommendation.owner_user_id == owner_user_id,
                        Recommendation.data_expires_at > now,
                        Recommendation.status == "analyzing",
                    )
                )
                if (active_count or 0) >= self.options.max_active_per_owner:
                    raise DomainError(
                        429,
                        "RECOMMENDATION_LIMIT_REACHED",
                        "已有推荐正在处理或等待确认",
                    )

                recommendation_id = uuid4()
                recommendation = Recommendation(
                    id=recommendation_id,
                    owner_user_id=owner_user_id,
                    reference_set_id=reference_set_id,
                    status="analyzing",
                    revision=1,
                    image_types=list(IMAGE_TYPES),
                    state={"stage": "queued", "schema_version": 1},
                    data_expires_at=now
                    + timedelta(days=retention_policy.recommendation_retention_days),
                    retention_days=retention_policy.recommendation_retention_days,
                    retention_policy_revision=retention_policy.revision,
                    created_at=now,
                    updated_at=now,
                )
                db.add(recommendation)
                db.flush()
                try:
                    enqueue_in_transaction(
                        db,
                        event_type=RECOMMENDATION_EVENT_TYPE,
                        aggregate_id=recommendation_id,
                        operation_version=self.options.event_operation_version,
                        schema_version=self.options.event_schema_version,
                        payload={"reference_set_id": reference_set_id},
                    )
                except OutboxActionConflict:
                    raise DomainError(
                        409,
                        "RECOMMENDATION_CONFLICT",
                        "推荐作业状态冲突，请重新读取",
                    ) from None
                return recommendation_id
        except DomainError:
            raise
        except RetentionPolicyUnavailable:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "推荐服务暂不可用") from None
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "推荐服务暂时不可用") from None

    def get_recommendation(
        self, *, owner_user_id: UUID, recommendation_id: UUID
    ) -> RecommendationResponse:
        try:
            with self._sessions() as db:
                now = db.scalar(select(func.clock_timestamp()))
                recommendation = db.execute(
                    select(Recommendation).where(
                        Recommendation.id == recommendation_id,
                        Recommendation.owner_user_id == owner_user_id,
                        Recommendation.data_expires_at > now,
                    )
                ).scalar_one_or_none()
                if recommendation is None:
                    raise DomainError(404, "RECOMMENDATION_NOT_FOUND", "推荐记录不可用")
                return self._to_public_response(recommendation, now=now)
        except DomainError:
            raise
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "推荐信息暂时无法读取") from None

    @staticmethod
    def _reference_asset_summary(
        db: Session, *, owner_user_id: UUID, reference_set_id: UUID
    ) -> tuple[ReferenceAssetSummary, ...]:
        source_asset = aliased(ImageAsset)
        model_asset = aliased(ImageAsset)
        rows = db.execute(
            select(ReferenceSetImage, source_asset, model_asset)
            .join(
                source_asset,
                and_(
                    source_asset.id == ReferenceSetImage.source_asset_id,
                    source_asset.owner_user_id == ReferenceSetImage.owner_user_id,
                    source_asset.asset_kind == "reference_source",
                ),
            )
            .join(
                model_asset,
                and_(
                    model_asset.id == ReferenceSetImage.model_asset_id,
                    model_asset.owner_user_id == ReferenceSetImage.owner_user_id,
                    model_asset.asset_kind == "reference_model_input",
                ),
            )
            .where(
                ReferenceSetImage.reference_set_id == reference_set_id,
                ReferenceSetImage.owner_user_id == owner_user_id,
            )
            .order_by(ReferenceSetImage.position)
        ).all()
        if not 1 <= len(rows) <= 4:
            return ()
        if any(
            reference_image.content_sha256 != model.content_sha256
            for reference_image, _, model in rows
        ):
            # ReferenceSetImage pins the digest of the normalized model input.
            # A drifted/cross-wired asset must never seed trusted analysis state.
            return ()
        expected_slots = ["PRIMARY", *(f"AUX{index:02d}" for index in range(1, len(rows)))]
        if [row[0].slot for row in rows] != expected_slots:
            return ()
        if [row[0].position for row in rows] != list(range(len(rows))):
            return ()
        return tuple(
            ReferenceAssetSummary(
                reference_set_image_id=reference_image.id,
                slot=reference_image.slot,
                position=reference_image.position,
                model_asset_id=model.id,
                source_asset_id=source.id,
                model_mime_type=model.mime_type,
                source_mime_type=source.mime_type,
                model_size_bytes=model.size_bytes,
                source_size_bytes=source.size_bytes,
                model_width=model.width,
                model_height=model.height,
                source_width=source.width,
                source_height=source.height,
                model_sha256=model.content_sha256,
                source_sha256=source.content_sha256,
            )
            for reference_image, source, model in rows
        )

    @staticmethod
    def _to_public_response(
        recommendation: Recommendation, *, now: datetime
    ) -> RecommendationResponse:
        status = recommendation.status
        if status == "ready" and recommendation.valid_until is not None:
            if recommendation.valid_until <= now:
                status = "expired"

        options: RecommendationPublicOptions | None = None
        if status == "ready" and recommendation.public_options is not None:
            try:
                options = RecommendationPublicOptions.model_validate(recommendation.public_options)
            except Exception:
                raise DomainError(
                    503, "RECOMMENDATION_UNAVAILABLE", "推荐信息暂时无法读取"
                ) from None

        error = PUBLIC_FAILURE if status == "failed" else None
        image_types = recommendation.image_types or list(IMAGE_TYPES)
        return RecommendationResponse(
            recommendation_id=recommendation.id,
            reference_set_id=recommendation.reference_set_id,
            status=status,  # type: ignore[arg-type]
            revision=recommendation.revision,
            options=options,
            image_types=image_types,
            valid_until=recommendation.valid_until,
            error=error,
        )


def process_recommendation_event(
    db: Session,
    event: OutboxEvent,
    message: OutboxMessage,
    *,
    options: RecommendationOptions,
    dify_port: DifyRecommendationPort,
) -> None:
    """Worker handler logic; caller owns the short PG transaction and Session."""
    if (
        event.event_type != RECOMMENDATION_EVENT_TYPE
        or event.id != message.event_id
        or event.aggregate_id != message.aggregate_id
        or event.operation_version != options.event_operation_version
        or event.schema_version != options.event_schema_version
        or message.operation_version != options.event_operation_version
        or message.schema_version != options.event_schema_version
    ):
        raise DifyBusinessConflict("recommendation event envelope does not match its contract")
    reference_set_id = message.payload.get("reference_set_id")
    if reference_set_id is None:
        raise DifyBusinessConflict("recommendation event has no reference set id")

    recommendation = db.execute(
        select(Recommendation).where(Recommendation.id == event.aggregate_id).with_for_update()
    ).scalar_one_or_none()
    if recommendation is None:
        raise DifyBusinessConflict("recommendation business record was not found")
    if recommendation.status != "analyzing":
        return

    now = db.scalar(select(func.clock_timestamp()))
    if recommendation.data_expires_at <= now or recommendation.reference_set_id != reference_set_id:
        _mark_failed(recommendation, now=now, failure_code="RECOMMENDATION_UNAVAILABLE")
        return

    reference_set = db.execute(
        select(ReferenceSet).where(
            ReferenceSet.id == reference_set_id,
            ReferenceSet.owner_user_id == recommendation.owner_user_id,
            ReferenceSet.status == "ready",
            ReferenceSet.expires_at > now,
        )
    ).scalar_one_or_none()
    if reference_set is None:
        _mark_failed(recommendation, now=now, failure_code="REFERENCE_SET_UNAVAILABLE")
        return
    reference_assets = RecommendationService._reference_asset_summary(
        db,
        owner_user_id=recommendation.owner_user_id,
        reference_set_id=reference_set_id,
    )
    if not reference_assets:
        _mark_failed(recommendation, now=now, failure_code="REFERENCE_SET_UNAVAILABLE")
        return

    try:
        run_ref = dify_port.start_recommendation(
            db,
            business_id=recommendation.id,
            recommendation_revision=recommendation.revision,
            reference_set_id=reference_set_id,
            reference_assets=reference_assets,
            category=reference_set.product_category,
        )
        result: RecommendationRun = dify_port.read_recommendation(db, run_ref=run_ref)
    except DifyNotConfigured:
        _mark_failed(recommendation, now=now, failure_code="DIFY_NOT_CONFIGURED")
        return
    except DifyBusinessConflict:
        _mark_failed(recommendation, now=now, failure_code="DIFY_RESULT_CONFLICT")
        return

    recommendation.run_ref = result.run_ref
    recommendation.form_ref = result.form_ref
    recommendation.audit = result.audit
    recommendation.state = result.state
    recommendation.updated_at = now
    if result.status == "analyzing":
        return
    if result.status == "failed":
        _mark_failed(recommendation, now=now, failure_code="RECOMMENDATION_FAILED")
        return

    try:
        public_options = RecommendationPublicOptions.model_validate(result.public_options)
    except Exception:
        _mark_failed(recommendation, now=now, failure_code="RECOMMENDATION_FAILED")
        return
    if (
        result.original_recommendation is None
        or result.audit is None
        or result.form_ref is None
        or result.valid_until is None
        or result.valid_until <= now
        or tuple(result.image_types) != IMAGE_TYPES
    ):
        _mark_failed(recommendation, now=now, failure_code="RECOMMENDATION_FAILED")
        return

    if (
        result.audit.get("source") != "synthetic_mock"
        or result.audit.get("image_analysis_performed") is not False
    ):
        _mark_failed(recommendation, now=now, failure_code="RECOMMENDATION_FAILED")
        return
    try:
        trusted_snapshot = load_trusted_analysis_snapshot(
            result.state.get(TRUSTED_ANALYSIS_STATE_KEY),
            reference_set_id=reference_set.id,
            recommendation_id=recommendation.id,
            recommendation_revision=recommendation.revision,
            category=reference_set.product_category,
            expected_references=[reference_asset_payload(asset) for asset in reference_assets],
        )
    except TrustedAnalysisSnapshotError:
        _mark_failed(recommendation, now=now, failure_code="RECOMMENDATION_FAILED")
        return

    recommendation.status = "ready"
    recommendation.ready_at = now
    recommendation.valid_until = result.valid_until
    recommendation.original_recommendation = result.original_recommendation
    recommendation.public_options = public_options.model_dump(mode="json")
    recommendation.image_types = list(result.image_types)
    recommendation.state = dict(result.state)
    recommendation.state[TRUSTED_ANALYSIS_STATE_KEY] = trusted_analysis_state(trusted_snapshot)
    recommendation.failure_code = None
    recommendation.updated_at = now


def _mark_failed(recommendation: Recommendation, *, now: datetime, failure_code: str) -> None:
    recommendation.status = "failed"
    recommendation.failure_code = failure_code
    recommendation.state = {"stage": "failed", "failure_code": failure_code}
    recommendation.updated_at = now


def recommendation_handler_registration(
    options: RecommendationOptions, dify_port: DifyRecommendationPort
) -> HandlerRegistration:
    return HandlerRegistration(
        handler=partial(
            process_recommendation_event,
            options=options,
            dify_port=dify_port,
        ),
        operation_versions=frozenset({options.event_operation_version}),
    )


def recommendation_handlers(
    options: RecommendationOptions, dify_port: DifyRecommendationPort
) -> Mapping[str, HandlerRegistration]:
    return {RECOMMENDATION_EVENT_TYPE: recommendation_handler_registration(options, dify_port)}
