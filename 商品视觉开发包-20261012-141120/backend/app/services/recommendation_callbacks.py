from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.domain_errors import DomainError
from app.db.models import User
from app.db.recommendation_models import Recommendation
from app.db.reference_models import ReferenceSet
from app.integrations.dify.contracts import ReferenceAssetSummary
from app.schemas.prompt_preferences import (
    RecommendationResultCallback,
    recommendation_callback_digest,
)
from app.schemas.recommendations import IMAGE_TYPES, RecommendationPublicOptions
from app.services.recommendations import RecommendationService
from app.services.trusted_analysis import (
    TRUSTED_ANALYSIS_STATE_KEY,
    TrustedAnalysisSnapshotError,
    load_trusted_analysis_snapshot,
    reference_asset_payload,
)

_CALLBACK_RECEIPT_KEY = "r3_recommendation_result_callback"


class RecommendationCallbackService:
    """Accept the R3 synthetic recommendation-result contract on the current S5 run.

    S5 currently completes recommendation work synchronously in its worker. This
    receiver is an independently testable callback contract, not evidence that a
    deployed Dify workflow calls this endpoint.
    """

    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

    def accept(self, callback: RecommendationResultCallback) -> str:
        if not isinstance(callback, RecommendationResultCallback):
            raise DomainError(422, "CALLBACK_INVALID", "回传内容无效")
        if recommendation_callback_digest(callback) != callback.result_digest:
            raise DomainError(422, "CALLBACK_INVALID", "回传内容无效")
        if callback.status == "ready":
            self._validate_synthetic_ready_payload(callback)

        try:
            with self._sessions() as db, db.begin():
                locked = self._lock_recommendation_chain(db, callback.recommendation_id)
                if locked is None:
                    raise DomainError(404, "RECOMMENDATION_NOT_FOUND", "推荐记录不可用")
                recommendation, reference_set = locked
                state = recommendation.state if isinstance(recommendation.state, dict) else {}
                receipt = state.get(_CALLBACK_RECEIPT_KEY)
                if isinstance(receipt, dict):
                    if (
                        receipt.get("recommendation_id") == str(callback.recommendation_id)
                        and receipt.get("recommendation_revision")
                        == callback.recommendation_revision
                        and receipt.get("run_ref") == callback.run_ref
                    ):
                        if (
                            receipt.get("result_digest_version") == callback.result_digest_version
                            and receipt.get("result_digest") == callback.result_digest
                        ):
                            return "replayed"
                        raise DomainError(
                            409,
                            "RECOMMENDATION_CALLBACK_CONFLICT",
                            "推荐回传与已保存结果冲突",
                        )

                now = self._db_now(db)
                if (
                    recommendation.reference_set_id != callback.reference_set_id
                    or recommendation.revision != callback.recommendation_revision
                    or recommendation.run_ref != callback.run_ref
                    or recommendation.status != "analyzing"
                    or recommendation.data_expires_at <= now
                    or reference_set.status != "ready"
                    or reference_set.expires_at <= now
                ):
                    raise DomainError(409, "RECOMMENDATION_CALLBACK_STALE", "推荐回传已过期")

                if callback.status == "failed":
                    next_state = (
                        dict(recommendation.state) if isinstance(recommendation.state, dict) else {}
                    )
                    next_state.update(
                        {
                            "stage": "failed",
                            "failure_code": callback.failure_code,
                            _CALLBACK_RECEIPT_KEY: self._receipt_payload(callback),
                        }
                    )
                    recommendation.status = "failed"
                    recommendation.failure_code = callback.failure_code
                    recommendation.state = next_state
                    recommendation.updated_at = now
                    return "accepted"

                reference_assets = RecommendationService._reference_asset_summary(
                    db,
                    owner_user_id=recommendation.owner_user_id,
                    reference_set_id=reference_set.id,
                )
                if not reference_assets:
                    raise DomainError(409, "RECOMMENDATION_CALLBACK_STALE", "推荐回传已过期")
                self._validate_reference_snapshot(
                    callback,
                    reference_set=reference_set,
                    recommendation=recommendation,
                    references=reference_assets,
                )
                # A ready result must remain usable for a positive interval and
                # cannot outlive either persisted parent object's retention.
                if (
                    callback.valid_until is None
                    or callback.valid_until <= now
                    or callback.valid_until > recommendation.data_expires_at
                    or callback.valid_until > reference_set.expires_at
                ):
                    raise DomainError(422, "CALLBACK_INVALID", "回传内容无效")
                try:
                    public_options = RecommendationPublicOptions.model_validate(
                        callback.public_options
                    )
                except Exception:
                    raise DomainError(422, "CALLBACK_INVALID", "回传内容无效") from None

                next_state = (
                    dict(recommendation.state) if isinstance(recommendation.state, dict) else {}
                )
                next_state.update(callback.state)
                next_state[_CALLBACK_RECEIPT_KEY] = self._receipt_payload(callback)
                recommendation.status = "ready"
                recommendation.ready_at = now
                recommendation.valid_until = callback.valid_until
                recommendation.original_recommendation = callback.original_recommendation
                recommendation.public_options = public_options.model_dump(mode="json")
                recommendation.image_types = list(callback.image_types)
                recommendation.audit = dict(callback.audit or {})
                recommendation.state = next_state
                recommendation.failure_code = None
                recommendation.updated_at = now
                return "accepted"
        except DomainError:
            raise
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "推荐回传暂时无法保存") from None

    @staticmethod
    def _validate_synthetic_ready_payload(callback: RecommendationResultCallback) -> None:
        audit = callback.audit
        if (
            not isinstance(audit, dict)
            or audit.get("source") != "synthetic_mock"
            or audit.get("image_analysis_performed") is not False
        ):
            raise DomainError(422, "CALLBACK_INVALID", "回传内容无效")
        if callback.original_recommendation is None:
            raise DomainError(422, "CALLBACK_INVALID", "回传内容无效")

    @staticmethod
    def _validate_reference_snapshot(
        callback: RecommendationResultCallback,
        *,
        reference_set: ReferenceSet,
        recommendation: Recommendation,
        references: tuple[ReferenceAssetSummary, ...],
    ) -> None:
        if tuple(callback.image_types) != IMAGE_TYPES:
            raise DomainError(422, "CALLBACK_INVALID", "回传内容无效")
        original = callback.original_recommendation
        if (
            not isinstance(original, dict)
            or original.get("category_snapshot") != reference_set.product_category
            or tuple(original.get("image_types", ())) != IMAGE_TYPES
        ):
            raise DomainError(422, "CALLBACK_INVALID", "回传内容无效")
        raw_snapshot = callback.state.get(TRUSTED_ANALYSIS_STATE_KEY)
        try:
            load_trusted_analysis_snapshot(
                raw_snapshot,
                reference_set_id=reference_set.id,
                recommendation_id=recommendation.id,
                recommendation_revision=recommendation.revision,
                category=reference_set.product_category,
                expected_references=[reference_asset_payload(asset) for asset in references],
            )
        except TrustedAnalysisSnapshotError:
            raise DomainError(422, "CALLBACK_INVALID", "回传内容无效") from None

    @staticmethod
    def _receipt_payload(callback: RecommendationResultCallback) -> dict[str, object]:
        return {
            "recommendation_id": str(callback.recommendation_id),
            "recommendation_revision": callback.recommendation_revision,
            "run_ref": callback.run_ref,
            "result_digest_version": callback.result_digest_version,
            "result_digest": callback.result_digest,
            "status": callback.status,
        }

    @staticmethod
    def _lock_recommendation_chain(
        db: Session,
        recommendation_id: UUID,
    ) -> tuple[Recommendation, ReferenceSet] | None:
        hint = db.execute(
            select(Recommendation.owner_user_id, Recommendation.reference_set_id).where(
                Recommendation.id == recommendation_id
            )
        ).one_or_none()
        if hint is None:
            return None
        owner_id, reference_set_id = hint
        owner = db.execute(
            select(User).where(User.id == owner_id, User.status == "active").with_for_update()
        ).scalar_one_or_none()
        reference_set = db.execute(
            select(ReferenceSet)
            .where(
                ReferenceSet.id == reference_set_id,
                ReferenceSet.owner_user_id == owner_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        recommendation = db.execute(
            select(Recommendation)
            .where(
                Recommendation.id == recommendation_id,
                Recommendation.owner_user_id == owner_id,
                Recommendation.reference_set_id == reference_set_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if owner is None or reference_set is None or recommendation is None:
            return None
        return recommendation, reference_set

    @staticmethod
    def _db_now(db: Session) -> datetime:
        value = db.scalar(select(func.clock_timestamp()))
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() != timedelta(0)
        ):
            raise DomainError(503, "DATABASE_UNAVAILABLE", "推荐回传暂时无法保存")
        return value.astimezone(UTC)
