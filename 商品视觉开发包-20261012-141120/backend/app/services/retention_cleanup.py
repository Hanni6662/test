from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, and_, delete, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, aliased, sessionmaker

from app.core.config import get_settings
from app.core.domain_errors import DomainError
from app.db.action_models import Action
from app.db.business_question_models import (
    BusinessAnswerSubmission,
    BusinessQuestion,
    BusinessQuestionEvent,
)
from app.db.business_session_models import BusinessSession
from app.db.chatflow_generation_models import (
    ChatflowGenerationAcceptanceEvidence,
    ChatflowGenerationAcceptedBatchFollowup,
    ChatflowGenerationDelivery,
    ChatflowGenerationRequest,
    ChatflowGenerationTracking,
)
from app.db.generation_models import (
    GenerationAttemptContext,
    GenerationCapacityControl,
    GenerationCapacityReservation,
    GenerationResult,
    MockGenerationInvocation,
    MockGenerationJob,
)
from app.db.models import User
from app.db.outbox_models import (
    OutboxDemoEffect,
    OutboxEvent,
    OutboxReceipt,
    OutboxRejectedMessage,
)
from app.db.prompt_models import (
    MockPromptSubmission,
    PromptSubmission,
    TaskPromptItem,
    TaskPromptPackage,
    TaskPromptWorkflow,
)
from app.db.recommendation_models import MockRecommendationRun, Recommendation
from app.db.reference_models import (
    ImageAsset,
    ReferenceSet,
    ReferenceSetImage,
    ReferenceUploadObject,
)
from app.db.retention_models import CleanupJob, CleanupRun, CleanupTarget, RetentionPolicy
from app.db.task_models import (
    Task,
    TaskIdempotencyRecord,
    TaskItem,
    TaskItemAttempt,
    TaskRecommendationConsumption,
)
from app.integrations.storage.protocol import (
    Storage,
    StorageError,
    StorageInventory,
    StorageInventoryEntry,
    StorageInventoryPage,
)
from app.schemas.prompt_workflow import PROMPT_RESULT_DIGEST_VERSION
from app.schemas.retention import (
    CleanupCounters,
    CleanupPreviewResponse,
    CleanupReasonCount,
    CleanupRunAccepted,
    CleanupRunRequest,
    CleanupRunResponse,
    RetentionPolicyResponse,
    RetentionPolicyUpdateRequest,
)
from app.services.retention_policy import (
    RetentionPolicyUnavailable,
    load_retention_policy_in_transaction,
)

LOGGER = logging.getLogger(__name__)

_TASK_TERMINAL = ("succeeded", "partially_succeeded", "failed")
_TASK_ITEM_TERMINAL = ("succeeded", "failed")
_ATTEMPT_TERMINAL = ("completed", "failed")
_ACTIVE_ACTIONS = ("scheduled", "claimed")
_ACTIVE_PROMPT_SUBMISSIONS = ("prepared", "accepted", "unknown")
_PROMPT_CALLBACK_TERMINAL = ("package_ready", "failed")
_RECOMMENDATION_EVENT = "recommendation.analyze.v1"
_TASK_PROMPT_EVENT = "task.prompt.v1"
_GENERATION_START_EVENT = "task.generation.start.v1"
_CLEANUP_NAMESPACE = UUID("49b2814e-1dd7-4e17-a5ad-0df114d37819")
_MANUAL_REQUEST_DIGEST = hashlib.sha256(b'{"version":1}').digest()
_JOB_LEASE = timedelta(minutes=5)
_RETRY_BASE_SECONDS = 30
_RETRY_MAX_SECONDS = 3600
_DISCOVERY_BATCH_MAX = 100


class _StaleInventoryPage(RuntimeError):
    """Roll back page inserts when the scanner lease/cursor CAS was lost."""


@dataclass(frozen=True, slots=True)
class CleanupWorkReport:
    scanned: int = 0
    claimed: int = 0
    completed: int = 0
    retried: int = 0
    blocked: int = 0
    objects_deleted: int = 0
    safe_error_code: str | None = None


class RetentionCleanupService:
    """Persistent, restart-safe expiry cleanup for business data and private objects."""

    def __init__(self, engine: Engine, storage: Storage) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        self._storage = storage
        self._grace = timedelta(hours=get_settings().reference_orphan_grace_hours)

    def get_policy(self) -> RetentionPolicyResponse:
        with self._sessions() as db:
            policy = db.get(RetentionPolicy, 1)
            if policy is None:
                raise DomainError(503, "RETENTION_POLICY_UNAVAILABLE", "保留策略暂不可用")
            return _policy_response(policy)

    def update_policy(self, request: RetentionPolicyUpdateRequest) -> RetentionPolicyResponse:
        if request.apply_to_existing:
            raise DomainError(
                422,
                "RETENTION_UPDATE_EXISTING_UNSUPPORTED",
                "保留策略只影响之后创建的数据",
            )
        try:
            with self._sessions() as db, db.begin():
                policy = db.execute(
                    select(RetentionPolicy).where(RetentionPolicy.id == 1).with_for_update()
                ).scalar_one_or_none()
                if policy is None:
                    raise DomainError(503, "RETENTION_POLICY_UNAVAILABLE", "保留策略暂不可用")
                if policy.revision != request.expected_revision:
                    raise DomainError(
                        409,
                        "RETENTION_POLICY_REVISION_CONFLICT",
                        "保留策略已更新，请重新读取后提交",
                    )
                policy.task_retention_days = request.task_retention_days
                policy.reference_retention_days = request.reference_retention_days
                policy.recommendation_retention_days = request.recommendation_retention_days
                policy.revision += 1
                policy.updated_at = func.clock_timestamp()
                db.flush()
                return _policy_response(policy)
        except DomainError:
            raise
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "保留策略暂时无法更新") from None

    def preview(self) -> CleanupPreviewResponse:
        try:
            with self._sessions() as db:
                now = _db_now(db)
                cutoff = now - self._grace
                task_ids = _eligible_task_ids(db, now)
                recommendation_ids = _eligible_recommendation_ids(db, now)
                reference_ids = _eligible_reference_ids(db, now)
                upload_ids = _eligible_upload_group_ids(db, now, cutoff)
                task_count = len(task_ids)
                recommendation_count = len(recommendation_ids)
                reference_count = len(reference_ids)
                upload_count = len(upload_ids)
                business_files, business_bytes = _estimate_business_objects(
                    db,
                    task_ids=task_ids,
                    reference_ids=reference_ids,
                    upload_group_ids=upload_ids,
                )
                task_due_count = int(
                    db.scalar(select(func.count(Task.id)).where(Task.expires_at <= now)) or 0
                )
                recommendation_due_count = int(
                    db.scalar(
                        select(func.count(Recommendation.id)).where(
                            Recommendation.status.in_(("ready", "failed", "expired")),
                            Recommendation.data_expires_at <= now,
                        )
                    )
                    or 0
                )
                active_or_unresolved_tasks = int(
                    db.scalar(select(func.count(Task.id)).where(Task.status.not_in(_TASK_TERMINAL)))
                    or 0
                )
                analyzing_recommendations = int(
                    db.scalar(
                        select(func.count(Recommendation.id)).where(
                            Recommendation.status == "analyzing"
                        )
                    )
                    or 0
                )
                reference_due_count = int(
                    db.scalar(
                        select(func.count(ReferenceSet.id)).where(
                            ReferenceSet.status == "ready",
                            ReferenceSet.expires_at.is_not(None),
                            ReferenceSet.expires_at <= now,
                        )
                    )
                    or 0
                )
                active_upload_leases = int(
                    db.scalar(
                        select(func.count(ReferenceSet.id)).where(
                            ReferenceSet.status == "uploading",
                            ReferenceSet.upload_lease_until > now,
                        )
                    )
                    or 0
                )
                active_capacity_reservations = int(
                    db.scalar(
                        select(func.count(GenerationCapacityReservation.id))
                        .join(Task, Task.id == GenerationCapacityReservation.task_id)
                        .where(
                            Task.expires_at <= now,
                            GenerationCapacityReservation.status == "active",
                        )
                    )
                    or 0
                )
                pending_jobs = int(
                    db.scalar(
                        select(func.count(CleanupJob.id)).where(
                            CleanupJob.status.in_(("pending", "claimed", "retry"))
                        )
                    )
                    or 0
                )
            inventory_available = isinstance(self._storage, StorageInventory)
            inventory_count = inventory_bytes = protected_inventory = 0
            safe_error = None
            if inventory_available:
                try:
                    (
                        inventory_count,
                        inventory_bytes,
                        protected_inventory,
                    ) = self._summarize_inventory(cutoff)
                except Exception:
                    LOGGER.warning("retention preview inventory scan failed")
                    safe_error = "INVENTORY_SCAN_FAILED"
            else:
                safe_error = "INVENTORY_UNAVAILABLE"
            reason_counts = {
                "TASK_NOT_SAFE_TO_DELETE": max(0, task_due_count - task_count),
                "ACTIVE_OR_UNRESOLVED_TASK": active_or_unresolved_tasks,
                "RECOMMENDATION_ANALYZING": analyzing_recommendations,
                "RECOMMENDATION_STILL_REFERENCED": max(
                    0, recommendation_due_count - recommendation_count
                ),
                "REFERENCE_STILL_REFERENCED": max(0, reference_due_count - reference_count),
                "ACTIVE_UPLOAD_LEASE": active_upload_leases,
                "GENERATION_SLOT_ACTIVE": active_capacity_reservations,
                "INVENTORY_OBJECT_REFERENCED": protected_inventory,
            }
            if safe_error is not None:
                reason_counts[safe_error] = 1
            return CleanupPreviewResponse(
                as_of=now,
                eligible_tasks=task_count,
                eligible_recommendations=recommendation_count,
                eligible_reference_sets=reference_count,
                eligible_upload_groups=upload_count,
                eligible_inventory_entries=inventory_count,
                estimated_files=business_files + inventory_count,
                estimated_bytes=business_bytes + inventory_bytes,
                skip_reasons=[
                    CleanupReasonCount(code=code, count=count)  # type: ignore[arg-type]
                    for code, count in sorted(reason_counts.items())
                    if count > 0
                ],
                pending_jobs=pending_jobs,
                inventory_available=inventory_available,
                safe_error_code=safe_error,
            )
        except DomainError:
            raise
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "清理预览暂时无法读取") from None

    def create_run(
        self,
        request: CleanupRunRequest,
        *,
        idempotency_key: str,
    ) -> CleanupRunAccepted:
        del request  # The frozen manual operation always scans every supported aggregate.
        try:
            key_hash = hashlib.sha256(idempotency_key.encode("ascii")).digest()
        except UnicodeEncodeError:
            raise DomainError(400, "IDEMPOTENCY_KEY_INVALID", "幂等键格式无效") from None
        try:
            with self._sessions() as db, db.begin():
                existing = db.execute(
                    select(CleanupRun).where(CleanupRun.idempotency_key_hash == key_hash)
                ).scalar_one_or_none()
                if existing is not None:
                    return self._manual_replay(existing, key_hash)
                snapshot = load_retention_policy_in_transaction(db)
                run = CleanupRun(
                    trigger_kind="manual",
                    trigger_key=f"manual:{key_hash.hex()}",
                    idempotency_key_hash=key_hash,
                    request_digest=_MANUAL_REQUEST_DIGEST,
                    policy_revision=snapshot.revision,
                    status="queued",
                    counts={},
                )
                db.add(run)
                db.flush()
                return CleanupRunAccepted(run_id=run.id, status=run.status)
        except DomainError:
            raise
        except IntegrityError:
            return self._recover_manual_replay(key_hash)
        except RetentionPolicyUnavailable:
            raise DomainError(503, "RETENTION_POLICY_UNAVAILABLE", "保留策略暂不可用") from None
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "清理任务暂时无法创建") from None

    def get_run(self, run_id: str) -> CleanupRunResponse:
        try:
            parsed = UUID(run_id)
        except (TypeError, ValueError):
            raise DomainError(404, "CLEANUP_RUN_NOT_FOUND", "清理记录不可用") from None
        try:
            with self._sessions() as db:
                run = db.get(CleanupRun, parsed)
                if run is None:
                    raise DomainError(404, "CLEANUP_RUN_NOT_FOUND", "清理记录不可用")
                pending = int(
                    db.scalar(
                        select(func.count(CleanupJob.id)).where(
                            CleanupJob.run_id == run.id,
                            CleanupJob.status.in_(("pending", "claimed", "retry")),
                        )
                    )
                    or 0
                )
                return _run_response(run, pending_jobs=pending)
        except DomainError:
            raise
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "清理记录暂时无法读取") from None

    def schedule_due_runs(
        self,
        *,
        now: datetime,
        timezone: ZoneInfo,
        daily_time: time,
    ) -> int:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("scheduler now must be timezone-aware")
        if daily_time.tzinfo is not None:
            raise ValueError("scheduled daily time must be a wall-clock time")
        local_now = now.astimezone(timezone)
        today_due = local_now.time().replace(tzinfo=None) >= daily_time
        try:
            with self._sessions() as db, db.begin():
                policy = db.execute(
                    select(RetentionPolicy).where(RetentionPolicy.id == 1).with_for_update()
                ).scalar_one_or_none()
                if policy is None:
                    raise RetentionPolicyUnavailable
                latest_covered = self._latest_scheduled_local_date(db, timezone)
                inserted = 0
                today = local_now.date()
                if latest_covered is None and not today_due:
                    # There is no trustworthy historical baseline on first
                    # deployment. Record an immediate full eligible-item scan
                    # with an unknown date range rather than inventing one.
                    trigger = f"catchup:startup:{today.isoformat()}:{timezone.key}"
                    if self._insert_scheduled_run(
                        db,
                        trigger_kind="catchup",
                        trigger_key=trigger,
                        policy_revision=policy.revision,
                    ):
                        inserted += 1
                if latest_covered is not None and latest_covered < today:
                    first_missing = latest_covered + timedelta(days=1)
                    last_missing = today - timedelta(days=1)
                    if first_missing <= last_missing:
                        start = datetime.combine(first_missing, time.min, timezone).astimezone(UTC)
                        end = datetime.combine(today, time.min, timezone).astimezone(UTC)
                        trigger = (
                            f"catchup:{first_missing.isoformat()}:{last_missing.isoformat()}"
                            f":{timezone.key}"
                        )
                        if self._insert_scheduled_run(
                            db,
                            trigger_kind="catchup",
                            trigger_key=trigger,
                            policy_revision=policy.revision,
                            catchup_from=start,
                            catchup_to=end,
                        ):
                            inserted += 1
                if today_due and (latest_covered is None or latest_covered < today):
                    scheduled_for = datetime.combine(today, daily_time, timezone).astimezone(UTC)
                    trigger = (
                        f"scheduled:{today.isoformat()}:{daily_time.strftime('%H%M')}"
                        f":{timezone.key}"
                    )
                    if self._insert_scheduled_run(
                        db,
                        trigger_kind="scheduled",
                        trigger_key=trigger,
                        policy_revision=policy.revision,
                        scheduled_for=scheduled_for,
                    ):
                        inserted += 1
                return inserted
        except RetentionPolicyUnavailable:
            raise DomainError(503, "RETENTION_POLICY_UNAVAILABLE", "保留策略暂不可用") from None
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "定时清理记录暂时无法创建") from None

    def process_due_jobs(self, *, batch_size: int) -> CleanupWorkReport:
        if not 1 <= batch_size <= 500:
            raise ValueError("batch_size must be between 1 and 500")
        scanned = claimed = completed = retried = blocked = objects_deleted = 0
        try:
            run_ids = self._active_run_ids()[:batch_size]
            discovery_per_run = max(1, batch_size // max(1, len(run_ids)))
            for run_id in run_ids:
                self._discover_run(run_id, batch_size=min(discovery_per_run, _DISCOVERY_BATCH_MAX))
            for _ in range(batch_size):
                job_id, token = self._claim_one_job()
                if job_id is None or token is None:
                    break
                scanned += 1
                claimed += 1
                outcome, deleted_count = self._process_claimed_job(job_id, token)
                objects_deleted += deleted_count
                if outcome == "completed":
                    completed += 1
                elif outcome == "blocked":
                    blocked += 1
                elif outcome == "retry":
                    retried += 1
            self._refresh_run_statuses()
            return CleanupWorkReport(
                scanned=scanned,
                claimed=claimed,
                completed=completed,
                retried=retried,
                blocked=blocked,
                objects_deleted=objects_deleted,
            )
        except DomainError:
            raise
        except SQLAlchemyError:
            LOGGER.warning("retention cleanup database iteration failed")
            return CleanupWorkReport(
                scanned=scanned,
                claimed=claimed,
                completed=completed,
                retried=retried,
                blocked=blocked,
                objects_deleted=objects_deleted,
                safe_error_code="DATABASE_UNAVAILABLE",
            )

    def _manual_replay(self, run: CleanupRun, key_hash: bytes) -> CleanupRunAccepted:
        if run.idempotency_key_hash != key_hash or run.request_digest != _MANUAL_REQUEST_DIGEST:
            raise DomainError(409, "IDEMPOTENCY_CONFLICT", "幂等键已用于其他清理请求")
        return CleanupRunAccepted(run_id=run.id, status=run.status, replayed=True)

    def _recover_manual_replay(self, key_hash: bytes) -> CleanupRunAccepted:
        try:
            with self._sessions() as db:
                run = db.execute(
                    select(CleanupRun).where(CleanupRun.idempotency_key_hash == key_hash)
                ).scalar_one_or_none()
                if run is None:
                    raise DomainError(409, "CLEANUP_RUN_CONFLICT", "清理任务创建冲突")
                return self._manual_replay(run, key_hash)
        except DomainError:
            raise
        except SQLAlchemyError:
            raise DomainError(503, "DATABASE_UNAVAILABLE", "清理任务暂时无法恢复") from None

    @staticmethod
    def _insert_scheduled_run(
        db: Session,
        *,
        trigger_kind: str,
        trigger_key: str,
        policy_revision: int,
        scheduled_for: datetime | None = None,
        catchup_from: datetime | None = None,
        catchup_to: datetime | None = None,
    ) -> bool:
        if (
            db.execute(
                select(CleanupRun.id).where(CleanupRun.trigger_key == trigger_key)
            ).scalar_one_or_none()
            is not None
        ):
            return False
        db.add(
            CleanupRun(
                trigger_kind=trigger_kind,
                trigger_key=trigger_key,
                policy_revision=policy_revision,
                status="queued",
                counts={},
                scheduled_for=scheduled_for,
                catchup_from=catchup_from,
                catchup_to=catchup_to,
            )
        )
        db.flush()
        return True

    @staticmethod
    def _latest_scheduled_local_date(db: Session, timezone: ZoneInfo) -> date | None:
        # Runs are retained, so aggregate in SQL instead of loading the history
        # on every scheduler poll. scheduled_for is the scheduled instant;
        # catchup_to is an exclusive local-midnight boundary.
        scheduled_boundary, catchup_boundary = db.execute(
            select(func.max(CleanupRun.scheduled_for), func.max(CleanupRun.catchup_to))
        ).one()
        if scheduled_boundary is None and catchup_boundary is None:
            return None
        # History is interpreted using the currently effective timezone on each
        # poll. A timezone change can therefore move local coverage dates; the
        # scheduler catches any resulting gap with a deterministic catch-up.
        if scheduled_boundary is not None and (
            catchup_boundary is None or scheduled_boundary >= catchup_boundary
        ):
            return scheduled_boundary.astimezone(timezone).date()
        assert catchup_boundary is not None
        local_boundary = catchup_boundary.astimezone(timezone)
        covered_through = local_boundary.date()
        if local_boundary.timetz().replace(tzinfo=None) == time.min:
            # At midnight, a catch-up's exclusive end covers through yesterday.
            covered_through -= timedelta(days=1)
        return covered_through

    def _active_run_ids(self) -> list[UUID]:
        with self._sessions() as db:
            return list(
                db.execute(
                    select(CleanupRun.id)
                    .where(CleanupRun.status.in_(("queued", "running")))
                    .order_by(CleanupRun.created_at, CleanupRun.id)
                    .limit(100)
                ).scalars()
            )

    def _discover_run(self, run_id: UUID, *, batch_size: int = _DISCOVERY_BATCH_MAX) -> None:
        with self._sessions() as db, db.begin():
            run = db.execute(
                select(CleanupRun).where(CleanupRun.id == run_id).with_for_update()
            ).scalar_one_or_none()
            if run is None or run.status not in {"queued", "running"}:
                return
            now = _db_now(db)
            cutoff = now - self._grace
            run.status = "running"
            if run.started_at is None:
                run.started_at = now
            counts = dict(run.counts or {})
            business_complete = bool(counts.get("business_discovery_complete", 0))
            if not business_complete:
                business_complete = self._insert_eligible_jobs(
                    db,
                    run_id=run.id,
                    now=now,
                    cutoff=cutoff,
                    limit=batch_size,
                )
                counts["business_discovery_complete"] = int(business_complete)
            inventory_scanned = run.orphan_scan_completed_at is not None
            run.counts = counts
            db.flush()
        if not inventory_scanned:
            self._discover_orphan_inventory(run_id, batch_size=batch_size)

    @staticmethod
    def _insert_eligible_jobs(
        db: Session,
        *,
        run_id: UUID,
        now: datetime,
        cutoff: datetime,
        limit: int,
    ) -> bool:
        remaining = max(1, limit)
        complete = True
        sources = (
            ("task", Task, Task.id, _eligible_task_base(now), (Task.expires_at, Task.id)),
            (
                "recommendation",
                Recommendation,
                Recommendation.id,
                _eligible_recommendation_base(now),
                (Recommendation.data_expires_at, Recommendation.id),
            ),
            (
                "reference_set",
                ReferenceSet,
                ReferenceSet.id,
                _eligible_reference_base(now),
                (ReferenceSet.expires_at, ReferenceSet.id),
            ),
            (
                "upload_group",
                ReferenceSet,
                ReferenceSet.id,
                _eligible_upload_base(now, cutoff),
                (ReferenceSet.last_activity_at, ReferenceSet.id),
            ),
        )
        for kind, _model, identity_column, eligibility, ordering in sources:
            already_in_run = exists(
                select(CleanupJob.id)
                .where(
                    CleanupJob.run_id == run_id,
                    CleanupJob.aggregate_kind == kind,
                    CleanupJob.aggregate_id == identity_column,
                )
                .correlate(_model)
            )
            page = list(
                db.scalars(
                    select(identity_column)
                    .where(eligibility, ~already_in_run)
                    .order_by(*ordering)
                    .limit(remaining + 1)
                )
            )
            selected = page[:remaining]
            db.add_all(
                CleanupJob(
                    run_id=run_id,
                    aggregate_kind=kind,
                    aggregate_id=aggregate_id,
                    status="pending",
                )
                for aggregate_id in selected
            )
            remaining -= len(selected)
            if len(page) > len(selected) or remaining == 0:
                complete = False
                break
        db.flush()
        return complete

    def _discover_orphan_inventory(self, run_id: UUID, *, batch_size: int) -> None:
        if not isinstance(self._storage, StorageInventory):
            self._finish_inventory_without_capability(run_id)
            return
        lease = self._claim_orphan_scan_lease(run_id)
        if lease is None:
            return
        token, cursor = lease
        try:
            with self._sessions() as db:
                cutoff = _db_now(db) - self._grace
            page = self._storage.iter_inventory_page(
                prefixes=("users/", "generated/"),
                modified_before=cutoff,
                after_locator=cursor,
                max_scanned_items=min(max(1, batch_size), 1000),
            )
            _validate_inventory_page(
                page,
                expected_cursor=cursor,
                max_scanned_items=min(max(1, batch_size), 1000),
            )
            self._persist_inventory_page(
                run_id,
                token=token,
                expected_cursor=cursor,
                page=page,
            )
        except Exception:
            LOGGER.warning("retention orphan inventory scan failed")
            self._release_orphan_scan_lease_after_error(run_id, token=token, expected_cursor=cursor)

    def _claim_orphan_scan_lease(self, run_id: UUID) -> tuple[UUID, str | None] | None:
        with self._sessions() as db, db.begin():
            run = db.execute(
                select(CleanupRun).where(CleanupRun.id == run_id).with_for_update()
            ).scalar_one_or_none()
            if run is None or run.status not in {"queued", "running"}:
                return None
            now = _db_now(db)
            if run.orphan_scan_completed_at is not None:
                return None
            if run.orphan_scan_lease_until is not None and run.orphan_scan_lease_until > now:
                return None
            token = uuid4()
            cursor = run.orphan_scan_cursor
            run.orphan_scan_lease_token = token
            run.orphan_scan_lease_until = now + _JOB_LEASE
            counts = dict(run.counts or {})
            counts["inventory_available"] = 1
            run.counts = counts
            return token, cursor

    def _finish_inventory_without_capability(self, run_id: UUID) -> None:
        with self._sessions() as db, db.begin():
            run = db.execute(
                select(CleanupRun).where(CleanupRun.id == run_id).with_for_update()
            ).scalar_one_or_none()
            if run is None or run.status not in {"queued", "running"}:
                return
            now = _db_now(db)
            run.orphan_scan_completed_at = now
            run.orphan_scan_lease_token = None
            run.orphan_scan_lease_until = None
            counts = dict(run.counts or {})
            counts["inventory_available"] = 0
            counts["inventory_error"] = 1
            counts["reason:INVENTORY_UNAVAILABLE"] = (
                int(counts.get("reason:INVENTORY_UNAVAILABLE", 0)) + 1
            )
            run.counts = counts

    def _release_orphan_scan_lease_after_error(
        self, run_id: UUID, *, token: UUID, expected_cursor: str | None
    ) -> None:
        with self._sessions() as db, db.begin():
            run = db.execute(
                select(CleanupRun).where(CleanupRun.id == run_id).with_for_update()
            ).scalar_one_or_none()
            if (
                run is None
                or run.orphan_scan_lease_token != token
                or run.orphan_scan_cursor != expected_cursor
            ):
                return
            run.orphan_scan_lease_token = None
            run.orphan_scan_lease_until = None
            counts = dict(run.counts or {})
            counts["inventory_scan_errors"] = int(counts.get("inventory_scan_errors", 0)) + 1
            counts["reason:INVENTORY_SCAN_FAILED"] = (
                int(counts.get("reason:INVENTORY_SCAN_FAILED", 0)) + 1
            )
            run.counts = counts

    def _persist_inventory_page(
        self,
        run_id: UUID,
        *,
        token: UUID,
        expected_cursor: str | None,
        page: StorageInventoryPage,
    ) -> bool:
        try:
            with self._sessions() as db, db.begin():
                # Read scanner ownership without taking the run row lock. Target
                # inserts can need CleanupJob FK/unique locks, and orphan workers
                # take job/target locks before updating run counters.
                state = db.execute(
                    select(
                        CleanupRun.status,
                        CleanupRun.orphan_scan_lease_token,
                        CleanupRun.orphan_scan_cursor,
                        CleanupRun.orphan_scan_completed_at,
                    ).where(CleanupRun.id == run_id)
                ).one_or_none()
                if (
                    state is None
                    or state.status not in {"queued", "running"}
                    or state.orphan_scan_lease_token != token
                    or state.orphan_scan_cursor != expected_cursor
                    or state.orphan_scan_completed_at is not None
                ):
                    return False
                now = _db_now(db)
                for entry in page.entries:
                    if (
                        not _safe_inventory_entry(entry)
                        or entry.modified_at.tzinfo is None
                        or entry.modified_at > now - self._grace
                        or _inventory_entry_is_protected(db, entry, now=now)
                    ):
                        continue
                    locator = entry.relative_locator
                    aggregate_id = uuid5(_CLEANUP_NAMESPACE, f"{entry.kind}:{locator}")
                    job = db.execute(
                        select(CleanupJob).where(
                            CleanupJob.run_id == run_id,
                            CleanupJob.aggregate_kind == "orphan",
                            CleanupJob.aggregate_id == aggregate_id,
                        )
                    ).scalar_one_or_none()
                    if job is None:
                        job = CleanupJob(
                            run_id=run_id,
                            aggregate_kind="orphan",
                            aggregate_id=aggregate_id,
                            status="pending",
                        )
                        db.add(job)
                        db.flush()
                    target_exists = db.scalar(
                        select(CleanupTarget.id).where(
                            CleanupTarget.job_id == job.id,
                            CleanupTarget.entry_kind == entry.kind,
                            CleanupTarget.relative_locator == locator,
                        )
                    )
                    if target_exists is not None:
                        continue
                    next_sequence = (
                        int(
                            db.scalar(
                                select(func.coalesce(func.max(CleanupTarget.sequence), 0)).where(
                                    CleanupTarget.job_id == job.id
                                )
                            )
                            or 0
                        )
                        + 1
                    )
                    db.add(
                        CleanupTarget(
                            job_id=job.id,
                            sequence=next_sequence,
                            entry_kind=entry.kind,
                            relative_locator=locator,
                            object_key=entry.object_key,
                            source_kind=None,
                            source_id=None,
                            status="pending",
                        )
                    )

                # Force job/target and their FK locks before taking the run lock.
                db.flush()
                run = db.execute(
                    select(CleanupRun)
                    .where(CleanupRun.id == run_id)
                    .execution_options(populate_existing=True)
                    .with_for_update()
                ).scalar_one_or_none()
                if (
                    run is None
                    or run.status not in {"queued", "running"}
                    or run.orphan_scan_lease_token != token
                    or run.orphan_scan_cursor != expected_cursor
                    or run.orphan_scan_completed_at is not None
                ):
                    raise _StaleInventoryPage

                # Page targets and cursor/lease CAS commit together. A stale
                # holder rolls its target writes back as one unit.
                run.orphan_scan_cursor = page.next_cursor
                run.orphan_scan_lease_token = None
                run.orphan_scan_lease_until = None
                if page.exhausted:
                    run.orphan_scan_completed_at = now
                counts = dict(run.counts or {})
                counts["inventory_available"] = 1
                counts["inventory_scan_items"] = (
                    int(counts.get("inventory_scan_items", 0)) + page.scanned_items
                )
                counts.pop("inventory_scan_errors", None)
                counts.pop("inventory_error", None)
                run.counts = counts
                return True
        except _StaleInventoryPage:
            return False

    def _summarize_inventory(self, cutoff: datetime) -> tuple[int, int, int]:
        inventory = self._storage
        if not isinstance(inventory, StorageInventory):
            return 0, 0, 0
        count = 0
        total_bytes = 0
        protected = 0
        for entry in inventory.iter_inventory(
            prefixes=("users/", "generated/"), modified_before=cutoff
        ):
            if not _safe_inventory_entry(entry):
                continue
            with self._sessions() as db:
                now = _db_now(db)
                if entry.modified_at.tzinfo is not None and entry.modified_at <= now - self._grace:
                    if not _inventory_entry_is_protected(db, entry, now=now):
                        count += 1
                        total_bytes += entry.size_bytes
                    else:
                        protected += 1
        return count, total_bytes, protected

    def _claim_one_job(self) -> tuple[UUID | None, UUID | None]:
        with self._sessions() as db, db.begin():
            now = _db_now(db)
            expired = (
                db.execute(
                    select(CleanupJob)
                    .where(
                        CleanupJob.status == "claimed",
                        CleanupJob.lease_until <= now,
                    )
                    .order_by(CleanupJob.lease_until, CleanupJob.id)
                    .limit(100)
                    .with_for_update(skip_locked=True)
                )
                .scalars()
                .all()
            )
            for job in expired:
                job.status = "retry"
                job.lease_token = None
                job.lease_until = None
                job.next_retry_at = now
                job.last_error_code = "CLEANUP_LEASE_EXPIRED"
                job.updated_at = now
            job = db.execute(
                select(CleanupJob)
                .where(
                    or_(
                        CleanupJob.status == "pending",
                        and_(
                            CleanupJob.status == "retry",
                            or_(
                                CleanupJob.next_retry_at.is_(None), CleanupJob.next_retry_at <= now
                            ),
                        ),
                    )
                )
                .order_by(CleanupJob.created_at, CleanupJob.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if job is None:
                return None, None
            token = uuid4()
            job.status = "claimed"
            job.lease_token = token
            job.lease_until = now + _JOB_LEASE
            job.attempt_count += 1
            job.updated_at = now
            return job.id, token

    def _process_claimed_job(self, job_id: UUID, token: UUID) -> tuple[str, int]:
        prepared, state = self._prepare_claimed_job(job_id, token)
        if state != "claimed":
            return state, 0
        deleted = 0
        for target_id in prepared:
            entry_data = self._read_target_for_delete(job_id, target_id, token)
            if entry_data is None:
                continue
            try:
                actually_deleted = self._delete_target(entry_data)
                self._mark_target_deleted(
                    job_id, target_id, token, actually_deleted=actually_deleted
                )
                deleted += int(actually_deleted)
            except Exception:
                code = (
                    "INVENTORY_DELETE_FAILED"
                    if entry_data[0] == "orphan"
                    else "STORAGE_DELETE_FAILED"
                )
                self._mark_target_failed(job_id, target_id, token, code)
                self._release_job_for_retry(job_id, token, code)
                self._refresh_run_statuses()
                return "retry", deleted
        outcome = self._finalize_claimed_job(job_id, token)
        self._refresh_run_statuses()
        return outcome, deleted

    def _prepare_claimed_job(self, job_id: UUID, token: UUID) -> tuple[list[UUID], str]:
        with self._sessions() as db, db.begin():
            job = db.execute(
                select(CleanupJob)
                .where(
                    CleanupJob.id == job_id,
                    CleanupJob.status == "claimed",
                    CleanupJob.lease_token == token,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if job is None:
                return [], "retry"
            if job.aggregate_kind == "orphan":
                return self._pending_target_ids(db, job.id), "claimed"
            result = self._prepare_business_aggregate(db, job)
            if result == "blocked":
                self._block_job(job, "AGGREGATE_NOT_ELIGIBLE")
                self._increment_run_counter(db, job.run_id, "jobs_blocked")
                self._record_run_reason(db, job.run_id, "AGGREGATE_NOT_ELIGIBLE")
                return [], "blocked"
            if result == "stale":
                self._complete_without_delete(db, job)
                return [], "completed"
            if result == "deleted":
                counter = {
                    "task": "tasks_deleted",
                    "recommendation": "recommendations_deleted",
                    "reference_set": "reference_sets_deleted",
                    "upload_group": "reference_sets_deleted",
                }.get(job.aggregate_kind)
                if counter is not None:
                    self._increment_run_counter(db, job.run_id, counter)
                self._increment_run_counter(db, job.run_id, "jobs_succeeded")
                if job.aggregate_kind == "upload_group":
                    self._increment_run_counter(db, job.run_id, "upload_groups_deleted")
                self._complete_and_remove_job(db, job)
                return [], "completed"
            return self._pending_target_ids(db, job.id), "claimed"

    def _prepare_business_aggregate(self, db: Session, job: CleanupJob) -> str:
        if job.aggregate_kind == "task":
            return self._prepare_task(db, job)
        if job.aggregate_kind == "recommendation":
            return self._prepare_recommendation(db, job)
        if job.aggregate_kind in {"reference_set", "upload_group"}:
            return self._prepare_reference_set(db, job)
        return "blocked"

    def _prepare_task(self, db: Session, job: CleanupJob) -> str:
        hint = db.execute(
            select(
                Task.owner_user_id,
                Task.reference_set_id,
                Task.recommendation_id,
                Task.business_session_id,
            ).where(Task.id == job.aggregate_id)
        ).one_or_none()
        if hint is None:
            return "stale"
        owner_id, reference_id, recommendation_id, business_session_id = hint
        # Worker code locks OutboxEvent -> OutboxReceipt -> User -> ReferenceSet ->
        # BusinessSession -> Recommendation -> Task. Generation-start events use TaskItem IDs, so
        # snapshot those IDs before taking any parent lock and prelock all event
        # rows (including their receipts) in the same event-first order.
        initial_item_ids = _task_item_ids(db, job.aggregate_id)
        event_ids = _task_event_ids(db, job.aggregate_id, initial_item_ids)
        events = _lock_events_and_receipts(db, event_ids)
        owner = db.execute(
            select(User).where(User.id == owner_id).with_for_update()
        ).scalar_one_or_none()
        reference_set = db.execute(
            select(ReferenceSet)
            .where(ReferenceSet.id == reference_id, ReferenceSet.owner_user_id == owner_id)
            .with_for_update()
        ).scalar_one_or_none()
        business_session = None
        if business_session_id is not None:
            business_session = db.execute(
                select(BusinessSession)
                .where(
                    BusinessSession.id == business_session_id,
                    BusinessSession.owner_user_id == owner_id,
                    BusinessSession.reference_set_id == reference_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
        recommendation = db.execute(
            select(Recommendation)
            .where(
                Recommendation.id == recommendation_id,
                Recommendation.owner_user_id == owner_id,
                Recommendation.reference_set_id == reference_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        task = db.execute(
            select(Task)
            .where(
                Task.id == job.aggregate_id,
                Task.owner_user_id == owner_id,
                Task.reference_set_id == reference_id,
                Task.recommendation_id == recommendation_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if (
            owner is None
            or reference_set is None
            or (business_session_id is not None and business_session is None)
            or recommendation is None
            or task is None
        ):
            return "stale"
        # A callback can create a generation-start event while cleanup is
        # discovering the task. The parent Task lock serializes that writer; if
        # its event/item set changed after our event-first lock snapshot, retire
        # this attempt without marking the task deleting. A later run can retry.
        current_item_ids = _task_item_ids(db, task.id)
        if (
            current_item_ids != initial_item_ids
            or _task_event_ids(db, task.id, current_item_ids) != event_ids
        ):
            return "blocked"
        now = _db_now(db)
        if task.status == "deleting":
            own_manifest = db.scalar(select(exists().where(CleanupTarget.job_id == job.id)))
            if own_manifest:
                return "ready"
            another_manifest = db.scalar(
                select(
                    exists()
                    .where(
                        CleanupJob.aggregate_kind == "task",
                        CleanupJob.aggregate_id == task.id,
                        CleanupJob.id != job.id,
                    )
                    .where(
                        exists(
                            select(CleanupTarget.id).where(CleanupTarget.job_id == CleanupJob.id)
                        )
                    )
                )
            )
            if another_manifest:
                # A different run owns the durable target manifest. Retire this
                # duplicate; the owner run will finish the delete.
                return "stale"
            # A no-object task can be finalized by the current run. This also
            # recovers a crash after the deleting marker committed.
            return "deleted" if self._finalize_task(db, job) == "deleted" else "blocked"
        if task.status not in _TASK_TERMINAL or task.expires_at is None or task.expires_at > now:
            return "stale"
        if any(
            event.status in {"pending", "claimed"}
            or (receipt is not None and receipt.status in {"pending", "processing"})
            for event, receipt in events
        ):
            return "blocked"
        items = (
            db.execute(
                select(TaskItem)
                .where(TaskItem.task_id == task.id)
                .order_by(TaskItem.position, TaskItem.id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        questions = (
            db.execute(
                select(BusinessQuestion)
                .where(
                    BusinessQuestion.business_session_id == task.business_session_id,
                    BusinessQuestion.task_id == task.id,
                )
                .order_by(BusinessQuestion.id)
                .with_for_update()
            )
            .scalars()
            .all()
            if task.business_session_id is not None
            else []
        )
        answer_submissions = (
            db.execute(
                select(BusinessAnswerSubmission)
                .where(
                    BusinessAnswerSubmission.business_session_id == task.business_session_id,
                    BusinessAnswerSubmission.task_id == task.id,
                )
                .order_by(BusinessAnswerSubmission.id)
                .with_for_update()
            )
            .scalars()
            .all()
            if task.business_session_id is not None
            else []
        )
        if task.business_session_id is not None:
            db.execute(
                select(BusinessQuestionEvent)
                .where(BusinessQuestionEvent.business_session_id == task.business_session_id)
                .order_by(BusinessQuestionEvent.id)
                .with_for_update()
            ).scalars().all()
        if _has_blocking_business_questions(questions, answer_submissions, now=now):
            return "blocked"
        if len(items) != task.planned_count or any(
            item.status not in _TASK_ITEM_TERMINAL for item in items
        ):
            return "blocked"
        item_ids = [item.id for item in items]
        attempts = (
            db.execute(
                select(TaskItemAttempt)
                .where(TaskItemAttempt.task_item_id.in_(item_ids))
                .order_by(
                    TaskItemAttempt.task_item_id, TaskItemAttempt.attempt_no, TaskItemAttempt.id
                )
                .with_for_update()
            )
            .scalars()
            .all()
            if item_ids
            else []
        )
        if any(attempt.status not in _ATTEMPT_TERMINAL for attempt in attempts):
            return "blocked"
        attempt_ids = [attempt.id for attempt in attempts]
        contexts = (
            db.execute(
                select(GenerationAttemptContext)
                .where(GenerationAttemptContext.attempt_id.in_(attempt_ids))
                .order_by(GenerationAttemptContext.attempt_id)
                .with_for_update()
            )
            .scalars()
            .all()
            if attempt_ids
            else []
        )
        # Prompt actions lock Workflow -> Submission -> Action. Keep that order
        # after the generation context chain so cleanup cannot invert a worker.
        workflows = (
            db.execute(
                select(TaskPromptWorkflow)
                .where(TaskPromptWorkflow.task_id == task.id)
                .order_by(TaskPromptWorkflow.id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        if any(
            workflow.status in {"pending_submission", "submitted", "reconciling", "needs_input"}
            for workflow in workflows
        ):
            return "blocked"
        submissions = (
            db.execute(
                select(PromptSubmission)
                .where(PromptSubmission.task_id == task.id)
                .order_by(PromptSubmission.submission_id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        if _has_active_prompt_submissions(submissions, workflows):
            return "blocked"
        actions = (
            db.execute(
                select(Action)
                .where(Action.task_id == task.id)
                .order_by(
                    Action.scope_kind,
                    Action.scope_id,
                    Action.sequence_namespace,
                    Action.sequence,
                    Action.id,
                )
                .with_for_update()
            )
            .scalars()
            .all()
        )
        resolved_generation_requests = _resolved_generation_requests_for_cleanup(db, task.id)
        if _has_blocking_actions(
            actions,
            attempts=attempts,
            submissions=submissions,
            workflows=workflows,
            resolved_generation_requests=resolved_generation_requests,
        ):
            return "blocked"
        capacity_control = db.execute(
            select(GenerationCapacityControl)
            .where(GenerationCapacityControl.id == 1)
            .with_for_update()
        ).scalar_one_or_none()
        if capacity_control is None:
            return "blocked"
        reservations = (
            db.execute(
                select(GenerationCapacityReservation)
                .where(GenerationCapacityReservation.task_id == task.id)
                .order_by(
                    GenerationCapacityReservation.attempt_id, GenerationCapacityReservation.id
                )
                .with_for_update()
            )
            .scalars()
            .all()
        )
        if any(reservation.status == "active" for reservation in reservations):
            return "blocked"
        generation_results = (
            db.execute(
                select(GenerationResult)
                .where(GenerationResult.task_id == task.id)
                .order_by(GenerationResult.attempt_id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        generation_asset_ids = sorted(
            {
                asset_id
                for result in generation_results
                for asset_id in (result.original_asset_id, result.preview_asset_id)
            },
            key=str,
        )
        asset_rows = (
            db.execute(
                select(ImageAsset)
                .where(ImageAsset.id.in_(generation_asset_ids))
                .order_by(ImageAsset.id)
                .with_for_update()
            )
            .scalars()
            .all()
            if generation_asset_ids
            else []
        )
        task.status = "deleting"
        task.updated_at = now
        target_pairs: dict[str, tuple[str, UUID]] = {}
        for asset in asset_rows:
            target_pairs.setdefault(asset.object_key, ("image_asset", asset.id))
        for context in contexts:
            target_pairs.setdefault(
                context.original_object_key, ("generation_attempt", context.attempt_id)
            )
            target_pairs.setdefault(
                context.preview_object_key, ("generation_attempt", context.attempt_id)
            )
        self._add_object_targets(db, job, target_pairs, source_id=task.id, source_kind="task")
        if not db.scalar(select(exists().where(CleanupTarget.job_id == job.id))):
            return "deleted" if self._finalize_task(db, job) == "deleted" else "blocked"
        return "ready"

    def _prepare_recommendation(self, db: Session, job: CleanupJob) -> str:
        hint = db.execute(
            select(Recommendation.owner_user_id, Recommendation.reference_set_id).where(
                Recommendation.id == job.aggregate_id
            )
        ).one_or_none()
        if hint is None:
            return "stale"
        owner_id, reference_id = hint
        event_ids = list(
            db.execute(
                select(OutboxEvent.id)
                .where(
                    OutboxEvent.event_type == _RECOMMENDATION_EVENT,
                    OutboxEvent.aggregate_id == job.aggregate_id,
                )
                .order_by(OutboxEvent.id)
            ).scalars()
        )
        events = _lock_events_and_receipts(db, event_ids)
        owner = db.execute(
            select(User).where(User.id == owner_id).with_for_update()
        ).scalar_one_or_none()
        reference_set = db.execute(
            select(ReferenceSet)
            .where(ReferenceSet.id == reference_id, ReferenceSet.owner_user_id == owner_id)
            .with_for_update()
        ).scalar_one_or_none()
        recommendation = db.execute(
            select(Recommendation)
            .where(
                Recommendation.id == job.aggregate_id,
                Recommendation.owner_user_id == owner_id,
                Recommendation.reference_set_id == reference_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if owner is None or reference_set is None or recommendation is None:
            return "stale"
        now = _db_now(db)
        if (
            recommendation.status not in {"ready", "failed", "expired"}
            or recommendation.data_expires_at > now
        ):
            return "stale"
        if db.scalar(select(exists().where(Task.recommendation_id == recommendation.id))):
            return "blocked"
        if any(
            event.status in {"pending", "claimed"}
            or (receipt is not None and receipt.status in {"pending", "processing"})
            for event, receipt in events
        ):
            return "blocked"
        return "ready"

    def _prepare_reference_set(self, db: Session, job: CleanupJob) -> str:
        hint = db.execute(
            select(ReferenceSet.owner_user_id).where(ReferenceSet.id == job.aggregate_id)
        ).scalar_one_or_none()
        if hint is None:
            return "stale"
        owner = db.execute(
            select(User).where(User.id == hint).with_for_update()
        ).scalar_one_or_none()
        reference_set = db.execute(
            select(ReferenceSet)
            .where(ReferenceSet.id == job.aggregate_id, ReferenceSet.owner_user_id == hint)
            .with_for_update()
        ).scalar_one_or_none()
        if owner is None or reference_set is None:
            return "stale"
        now = _db_now(db)
        cutoff = now - self._grace
        if reference_set.status == "ready":
            if reference_set.expires_at is None or reference_set.expires_at > now:
                return "stale"
            if job.aggregate_kind != "reference_set":
                return "stale"
        elif reference_set.status == "failed":
            if reference_set.failed_at is None or reference_set.failed_at > cutoff:
                return "stale"
            if job.aggregate_kind != "upload_group":
                return "stale"
        elif reference_set.status == "uploading":
            if reference_set.last_activity_at > cutoff or (
                reference_set.upload_lease_until is not None
                and reference_set.upload_lease_until > cutoff
            ):
                return "stale"
            if job.aggregate_kind != "upload_group":
                return "stale"
            reference_set.status = "failed"
            reference_set.lease_token = None
            reference_set.upload_lease_until = None
            # Preserve the original stale age so a reclaimed job can safely
            # revalidate without waiting another full grace interval.
            reference_set.failed_at = reference_set.last_activity_at
            reference_set.failure_code = reference_set.failure_code or "UPLOAD_ABANDONED"
        else:
            return "stale"
        if db.scalar(select(exists().where(Task.reference_set_id == reference_set.id))):
            return "blocked"
        if db.scalar(select(exists().where(Recommendation.reference_set_id == reference_set.id))):
            return "blocked"
        assets = (
            db.execute(
                select(ImageAsset)
                .join(
                    ReferenceSetImage,
                    or_(
                        ReferenceSetImage.source_asset_id == ImageAsset.id,
                        ReferenceSetImage.model_asset_id == ImageAsset.id,
                    ),
                )
                .where(ReferenceSetImage.reference_set_id == reference_set.id)
                .order_by(ImageAsset.id)
                .with_for_update()
            )
            .scalars()
            .unique()
            .all()
        )
        upload_rows = (
            db.execute(
                select(ReferenceUploadObject)
                .where(ReferenceUploadObject.reference_set_id == reference_set.id)
                .order_by(ReferenceUploadObject.id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        asset_ids = [asset.id for asset in assets]
        if asset_ids and (
            db.scalar(
                select(
                    exists().where(
                        ReferenceSetImage.reference_set_id != reference_set.id,
                        or_(
                            ReferenceSetImage.source_asset_id.in_(asset_ids),
                            ReferenceSetImage.model_asset_id.in_(asset_ids),
                        ),
                    )
                )
            )
            or db.scalar(
                select(exists().where(TaskPromptItem.identity_anchor_asset_id.in_(asset_ids)))
            )
            or db.scalar(
                select(
                    exists().where(
                        or_(
                            GenerationResult.original_asset_id.in_(asset_ids),
                            GenerationResult.preview_asset_id.in_(asset_ids),
                        )
                    )
                )
            )
        ):
            return "blocked"
        target_pairs: dict[str, tuple[str, UUID]] = {}
        for asset in assets:
            target_pairs.setdefault(asset.object_key, ("image_asset", asset.id))
        for upload in upload_rows:
            if upload.status != "deleted":
                target_pairs.setdefault(upload.object_key, ("reference_upload_object", upload.id))
        self._add_object_targets(
            db, job, target_pairs, source_id=reference_set.id, source_kind="reference_set"
        )
        return "ready"

    @staticmethod
    def _add_object_targets(
        db: Session,
        job: CleanupJob,
        target_pairs: dict[str, tuple[str, UUID]],
        *,
        source_id: UUID,
        source_kind: str,
    ) -> None:
        sequence = int(
            db.scalar(
                select(func.coalesce(func.max(CleanupTarget.sequence), 0)).where(
                    CleanupTarget.job_id == job.id
                )
            )
            or 0
        )
        for object_key, (kind, row_id) in sorted(target_pairs.items()):
            if not object_key or "\\" in object_key or object_key.startswith("/"):
                continue
            exists_target = db.execute(
                select(CleanupTarget.id).where(
                    CleanupTarget.job_id == job.id,
                    CleanupTarget.entry_kind == "object",
                    CleanupTarget.relative_locator == object_key,
                )
            ).scalar_one_or_none()
            if exists_target is not None:
                continue
            sequence += 1
            db.add(
                CleanupTarget(
                    job_id=job.id,
                    sequence=sequence,
                    entry_kind="object",
                    relative_locator=object_key,
                    object_key=object_key,
                    source_kind=kind or source_kind,
                    source_id=row_id or source_id,
                    status="pending",
                )
            )
        db.flush()

    @staticmethod
    def _pending_target_ids(db: Session, job_id: UUID) -> list[UUID]:
        return list(
            db.execute(
                select(CleanupTarget.id)
                .where(
                    CleanupTarget.job_id == job_id, CleanupTarget.status.in_(("pending", "failed"))
                )
                .order_by(CleanupTarget.sequence, CleanupTarget.id)
            ).scalars()
        )

    def _read_target_for_delete(
        self, job_id: UUID, target_id: UUID, token: UUID
    ) -> tuple[str, str, str | None, str, datetime] | None:
        with self._sessions() as db:
            row = db.execute(
                select(
                    CleanupJob.aggregate_kind,
                    CleanupTarget.entry_kind,
                    CleanupTarget.relative_locator,
                    CleanupTarget.object_key,
                    CleanupTarget.created_at,
                )
                .join(CleanupTarget, CleanupTarget.job_id == CleanupJob.id)
                .where(
                    CleanupJob.id == job_id,
                    CleanupJob.status == "claimed",
                    CleanupJob.lease_token == token,
                    CleanupTarget.id == target_id,
                    CleanupTarget.status.in_(("pending", "failed")),
                )
            ).one_or_none()
            if row is None or row.relative_locator is None:
                return None
            locator = str(row.relative_locator)
            if row.aggregate_kind == "orphan":
                return ("orphan", str(row.entry_kind), row.object_key, locator, row.created_at)
            if row.entry_kind != "object" or row.object_key is None:
                raise StorageError("cleanup target is invalid")
            return ("business", "object", row.object_key, locator, row.created_at)

    def _delete_target(self, data: tuple[str, str, str | None, str, datetime]) -> bool:
        aggregate_kind, entry_kind, object_key, locator, created_at = data
        if aggregate_kind == "business":
            if object_key is None:
                raise StorageError("business cleanup target lacks an object key")
            existed = self._storage.stat(object_key) is not None
            self._storage.delete(object_key)
            return existed
        if not isinstance(self._storage, StorageInventory):
            raise StorageError("inventory cleanup capability is unavailable")
        prefix = "users/" if locator.startswith("users/") else "generated/"
        for entry in self._storage.iter_inventory(prefixes=(prefix,), modified_before=created_at):
            if (
                entry.relative_locator == locator
                and entry.kind == entry_kind
                and entry.object_key == object_key
            ):
                self._storage.delete_inventory_entry(entry)
                return True
        return False

    def _mark_target_deleted(
        self,
        job_id: UUID,
        target_id: UUID,
        token: UUID,
        *,
        actually_deleted: bool,
    ) -> None:
        with self._sessions() as db, db.begin():
            job = db.execute(
                select(CleanupJob).where(CleanupJob.id == job_id).with_for_update()
            ).scalar_one_or_none()
            if job is None or job.status != "claimed" or job.lease_token != token:
                return
            target = db.execute(
                select(CleanupTarget)
                .where(CleanupTarget.id == target_id, CleanupTarget.job_id == job_id)
                .with_for_update()
            ).scalar_one_or_none()
            if target is None or target.status == "deleted":
                return
            target.status = "deleted"
            target.relative_locator = None
            target.object_key = None
            target.last_error_code = None
            target.completed_at = _db_now(db)
            target.updated_at = target.completed_at
            target.attempt_count += 1
            if job.aggregate_kind == "orphan" and actually_deleted:
                self._increment_run_counter(db, job.run_id, "inventory_entries_deleted")

    def _mark_target_failed(self, job_id: UUID, target_id: UUID, token: UUID, code: str) -> None:
        with self._sessions() as db, db.begin():
            job = db.execute(
                select(CleanupJob).where(CleanupJob.id == job_id).with_for_update()
            ).scalar_one_or_none()
            if job is None or job.status != "claimed" or job.lease_token != token:
                return
            target = db.execute(
                select(CleanupTarget)
                .where(CleanupTarget.id == target_id, CleanupTarget.job_id == job_id)
                .with_for_update()
            ).scalar_one_or_none()
            if target is None:
                return
            target.status = "failed"
            target.last_error_code = code
            target.attempt_count += 1
            target.updated_at = _db_now(db)
            if job.aggregate_kind == "orphan":
                self._increment_run_counter(db, job.run_id, "inventory_entries_failed")
                self._record_run_reason(db, job.run_id, "INVENTORY_DELETE_FAILED")
            else:
                self._record_run_reason(db, job.run_id, "STORAGE_DELETE_FAILED")

    def _release_job_for_retry(self, job_id: UUID, token: UUID, code: str) -> None:
        with self._sessions() as db, db.begin():
            job = db.execute(
                select(CleanupJob).where(CleanupJob.id == job_id).with_for_update()
            ).scalar_one_or_none()
            if job is None or job.status != "claimed" or job.lease_token != token:
                return
            now = _db_now(db)
            delay = min(_RETRY_MAX_SECONDS, _RETRY_BASE_SECONDS * (2 ** min(job.attempt_count, 7)))
            job.status = "retry"
            job.lease_token = None
            job.lease_until = None
            job.next_retry_at = now + timedelta(seconds=delay)
            job.last_error_code = code
            job.updated_at = now

    def _finalize_claimed_job(self, job_id: UUID, token: UUID) -> str:
        with self._sessions() as db, db.begin():
            job = db.execute(
                select(CleanupJob)
                .where(CleanupJob.id == job_id, CleanupJob.lease_token == token)
                .with_for_update()
            ).scalar_one_or_none()
            if job is None or job.status != "claimed":
                return "retry"
            remaining = db.scalar(
                select(func.count(CleanupTarget.id)).where(
                    CleanupTarget.job_id == job.id,
                    CleanupTarget.status != "deleted",
                )
            )
            if remaining:
                self._block_job(job, "CLEANUP_TARGETS_INCOMPLETE")
                self._increment_run_counter(db, job.run_id, "jobs_blocked")
                self._record_run_reason(db, job.run_id, "CLEANUP_TARGETS_INCOMPLETE")
                return "blocked"
            if job.aggregate_kind == "orphan":
                self._increment_run_counter(db, job.run_id, "jobs_succeeded")
                self._complete_and_remove_job(db, job)
                return "completed"
            if job.aggregate_kind == "task":
                result = self._finalize_task(db, job)
                counter = "tasks_deleted"
            elif job.aggregate_kind == "recommendation":
                result = self._finalize_recommendation(db, job)
                counter = "recommendations_deleted"
            elif job.aggregate_kind in {"reference_set", "upload_group"}:
                result = self._finalize_reference_set(db, job)
                counter = "reference_sets_deleted"
            else:
                result = "blocked"
                counter = "tasks_deleted"
            if result == "stale":
                self._complete_without_delete(db, job)
                return "completed"
            if result != "deleted":
                self._block_job(job, "AGGREGATE_DELETE_BLOCKED")
                self._increment_run_counter(db, job.run_id, "jobs_blocked")
                self._record_run_reason(db, job.run_id, "AGGREGATE_DELETE_BLOCKED")
                return "blocked"
            self._increment_run_counter(db, job.run_id, "jobs_succeeded")
            self._increment_run_counter(db, job.run_id, counter)
            if job.aggregate_kind == "upload_group":
                self._increment_run_counter(db, job.run_id, "upload_groups_deleted")
            self._complete_and_remove_job(db, job)
            return "completed"

    def _finalize_task(self, db: Session, job: CleanupJob) -> str:
        hint = db.execute(
            select(
                Task.owner_user_id,
                Task.reference_set_id,
                Task.recommendation_id,
                Task.business_session_id,
            ).where(Task.id == job.aggregate_id)
        ).one_or_none()
        if hint is None:
            return "stale"
        owner_id, reference_id, recommendation_id, business_session_id = hint
        initial_item_ids = _task_item_ids(db, job.aggregate_id)
        event_ids = _task_event_ids(db, job.aggregate_id, initial_item_ids)
        events = _lock_events_and_receipts(db, event_ids)
        owner = db.execute(
            select(User).where(User.id == owner_id).with_for_update()
        ).scalar_one_or_none()
        reference_set = db.execute(
            select(ReferenceSet)
            .where(ReferenceSet.id == reference_id, ReferenceSet.owner_user_id == owner_id)
            .with_for_update()
        ).scalar_one_or_none()
        business_session = None
        if business_session_id is not None:
            business_session = db.execute(
                select(BusinessSession)
                .where(
                    BusinessSession.id == business_session_id,
                    BusinessSession.owner_user_id == owner_id,
                    BusinessSession.reference_set_id == reference_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
        recommendation = db.execute(
            select(Recommendation)
            .where(Recommendation.id == recommendation_id, Recommendation.owner_user_id == owner_id)
            .with_for_update()
        ).scalar_one_or_none()
        task = db.execute(
            select(Task)
            .where(Task.id == job.aggregate_id, Task.owner_user_id == owner_id)
            .with_for_update()
        ).scalar_one_or_none()
        if (
            owner is None
            or reference_set is None
            or (business_session_id is not None and business_session is None)
            or recommendation is None
            or task is None
        ):
            return "stale"
        if task.status != "deleting":
            return "stale"
        current_item_ids = _task_item_ids(db, task.id)
        if (
            current_item_ids != initial_item_ids
            or _task_event_ids(db, task.id, current_item_ids) != event_ids
        ):
            return "blocked"
        if any(
            event.status in {"pending", "claimed"}
            or (receipt is not None and receipt.status in {"pending", "processing"})
            for event, receipt in events
        ):
            return "blocked"
        task_id = task.id
        now = _db_now(db)
        items = (
            db.execute(
                select(TaskItem)
                .where(TaskItem.task_id == task_id)
                .order_by(TaskItem.position, TaskItem.id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        questions = (
            db.execute(
                select(BusinessQuestion)
                .where(
                    BusinessQuestion.business_session_id == task.business_session_id,
                    BusinessQuestion.task_id == task_id,
                )
                .order_by(BusinessQuestion.id)
                .with_for_update()
            )
            .scalars()
            .all()
            if task.business_session_id is not None
            else []
        )
        answer_submissions = (
            db.execute(
                select(BusinessAnswerSubmission)
                .where(
                    BusinessAnswerSubmission.business_session_id == task.business_session_id,
                    BusinessAnswerSubmission.task_id == task_id,
                )
                .order_by(BusinessAnswerSubmission.id)
                .with_for_update()
            )
            .scalars()
            .all()
            if task.business_session_id is not None
            else []
        )
        if task.business_session_id is not None:
            db.execute(
                select(BusinessQuestionEvent)
                .where(BusinessQuestionEvent.business_session_id == task.business_session_id)
                .order_by(BusinessQuestionEvent.id)
                .with_for_update()
            ).scalars().all()
        if _has_blocking_business_questions(questions, answer_submissions, now=now):
            return "blocked"
        attempts = (
            db.execute(
                select(TaskItemAttempt)
                .join(TaskItem, TaskItem.id == TaskItemAttempt.task_item_id)
                .where(TaskItem.task_id == task_id)
                .order_by(
                    TaskItemAttempt.task_item_id, TaskItemAttempt.attempt_no, TaskItemAttempt.id
                )
                .with_for_update()
            )
            .scalars()
            .all()
        )
        if (
            len(items) != task.planned_count
            or any(item.status not in _TASK_ITEM_TERMINAL for item in items)
            or any(attempt.status not in _ATTEMPT_TERMINAL for attempt in attempts)
        ):
            return "blocked"
        item_ids = [item.id for item in items]
        attempt_ids = [attempt.id for attempt in attempts]
        if attempt_ids:
            db.scalars(
                select(GenerationAttemptContext)
                .where(GenerationAttemptContext.attempt_id.in_(attempt_ids))
                .order_by(GenerationAttemptContext.attempt_id)
                .with_for_update()
            ).all()
        workflows = (
            db.execute(
                select(TaskPromptWorkflow)
                .where(TaskPromptWorkflow.task_id == task_id)
                .order_by(TaskPromptWorkflow.id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        submissions = (
            db.execute(
                select(PromptSubmission)
                .where(PromptSubmission.task_id == task_id)
                .order_by(PromptSubmission.submission_id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        actions = (
            db.execute(
                select(Action)
                .where(Action.task_id == task_id)
                .order_by(
                    Action.scope_kind,
                    Action.scope_id,
                    Action.sequence_namespace,
                    Action.sequence,
                    Action.id,
                )
                .with_for_update()
            )
            .scalars()
            .all()
        )
        resolved_generation_requests = _resolved_generation_requests_for_cleanup(db, task_id)
        if (
            _has_blocking_actions(
                actions,
                attempts=attempts,
                submissions=submissions,
                workflows=workflows,
                resolved_generation_requests=resolved_generation_requests,
            )
            or any(
                workflow.status in {"pending_submission", "submitted", "reconciling", "needs_input"}
                for workflow in workflows
            )
            or _has_active_prompt_submissions(submissions, workflows)
        ):
            return "blocked"
        capacity_control = db.execute(
            select(GenerationCapacityControl)
            .where(GenerationCapacityControl.id == 1)
            .with_for_update()
        ).scalar_one_or_none()
        if capacity_control is None:
            return "blocked"
        reservations = (
            db.execute(
                select(GenerationCapacityReservation)
                .where(GenerationCapacityReservation.task_id == task_id)
                .order_by(
                    GenerationCapacityReservation.attempt_id, GenerationCapacityReservation.id
                )
                .with_for_update()
            )
            .scalars()
            .all()
        )
        if any(reservation.status == "active" for reservation in reservations):
            return "blocked"
        generation_results = (
            db.execute(
                select(GenerationResult)
                .where(GenerationResult.task_id == task_id)
                .order_by(GenerationResult.attempt_id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        generation_asset_ids = sorted(
            {
                asset_id
                for result in generation_results
                for asset_id in (result.original_asset_id, result.preview_asset_id)
            },
            key=str,
        )
        asset_rows = (
            db.execute(
                select(ImageAsset)
                .where(ImageAsset.id.in_(generation_asset_ids))
                .order_by(ImageAsset.id)
                .with_for_update()
            )
            .scalars()
            .all()
            if generation_asset_ids
            else []
        )
        if len(asset_rows) != len(generation_asset_ids):
            return "blocked"
        if generation_asset_ids and (
            db.scalar(
                select(
                    exists().where(
                        GenerationResult.task_id != task_id,
                        or_(
                            GenerationResult.original_asset_id.in_(generation_asset_ids),
                            GenerationResult.preview_asset_id.in_(generation_asset_ids),
                        ),
                    )
                )
            )
            or db.scalar(
                select(
                    exists().where(
                        TaskPromptItem.identity_anchor_asset_id.in_(generation_asset_ids)
                    )
                )
            )
        ):
            return "blocked"
        db.execute(
            update(TaskIdempotencyRecord)
            .where(TaskIdempotencyRecord.task_id == task_id)
            .values(state="expired", updated_at=now)
        )
        db.execute(
            update(TaskRecommendationConsumption)
            .where(TaskRecommendationConsumption.task_id == task_id)
            .values(state="expired", updated_at=now)
        )
        db.execute(delete(GenerationResult).where(GenerationResult.task_id == task_id))
        if attempt_ids:
            db.execute(
                delete(GenerationAttemptContext).where(
                    GenerationAttemptContext.attempt_id.in_(attempt_ids)
                )
            )
            db.execute(
                delete(MockGenerationInvocation).where(
                    MockGenerationInvocation.request_key.in_(attempt_ids)
                )
            )
            db.execute(
                delete(MockGenerationJob).where(MockGenerationJob.request_key.in_(attempt_ids))
            )
        db.execute(
            delete(GenerationCapacityReservation).where(
                GenerationCapacityReservation.task_id == task_id
            )
        )
        db.execute(delete(Action).where(Action.task_id == task_id))
        if task.business_session_id is not None:
            db.execute(
                delete(BusinessQuestionEvent).where(
                    BusinessQuestionEvent.business_session_id == task.business_session_id
                )
            )
            db.execute(
                delete(BusinessAnswerSubmission).where(
                    BusinessAnswerSubmission.business_session_id == task.business_session_id,
                    BusinessAnswerSubmission.task_id == task_id,
                )
            )
            db.execute(
                delete(BusinessQuestion).where(
                    BusinessQuestion.business_session_id == task.business_session_id,
                    BusinessQuestion.task_id == task_id,
                )
            )
        if event_ids:
            db.execute(
                delete(OutboxRejectedMessage).where(OutboxRejectedMessage.event_id.in_(event_ids))
            )
            db.execute(delete(OutboxDemoEffect).where(OutboxDemoEffect.event_id.in_(event_ids)))
            db.execute(delete(OutboxReceipt).where(OutboxReceipt.event_id.in_(event_ids)))
            db.execute(delete(OutboxEvent).where(OutboxEvent.id.in_(event_ids)))
        db.execute(delete(MockPromptSubmission).where(MockPromptSubmission.task_id == task_id))
        db.execute(delete(TaskPromptItem).where(TaskPromptItem.task_id == task_id))
        db.execute(delete(TaskPromptPackage).where(TaskPromptPackage.task_id == task_id))
        db.execute(delete(PromptSubmission).where(PromptSubmission.task_id == task_id))
        db.execute(delete(TaskPromptWorkflow).where(TaskPromptWorkflow.task_id == task_id))
        if item_ids:
            db.execute(delete(TaskItemAttempt).where(TaskItemAttempt.task_item_id.in_(item_ids)))
            db.execute(delete(TaskItem).where(TaskItem.id.in_(item_ids)))
        if generation_asset_ids:
            db.execute(delete(ImageAsset).where(ImageAsset.id.in_(generation_asset_ids)))
        db.delete(task)
        return "deleted"

    def _finalize_recommendation(self, db: Session, job: CleanupJob) -> str:
        hint = db.execute(
            select(Recommendation.owner_user_id, Recommendation.reference_set_id).where(
                Recommendation.id == job.aggregate_id
            )
        ).one_or_none()
        if hint is None:
            return "stale"
        owner_id, reference_id = hint
        event_ids = list(
            db.execute(
                select(OutboxEvent.id)
                .where(
                    OutboxEvent.event_type == _RECOMMENDATION_EVENT,
                    OutboxEvent.aggregate_id == job.aggregate_id,
                )
                .order_by(OutboxEvent.id)
            ).scalars()
        )
        events = _lock_events_and_receipts(db, event_ids)
        owner = db.execute(
            select(User).where(User.id == owner_id).with_for_update()
        ).scalar_one_or_none()
        reference_set = db.execute(
            select(ReferenceSet).where(ReferenceSet.id == reference_id).with_for_update()
        ).scalar_one_or_none()
        recommendation = db.execute(
            select(Recommendation).where(Recommendation.id == job.aggregate_id).with_for_update()
        ).scalar_one_or_none()
        if owner is None or reference_set is None or recommendation is None:
            return "stale"
        if db.scalar(select(exists().where(Task.recommendation_id == recommendation.id))):
            return "blocked"
        if any(
            event.status in {"pending", "claimed"}
            or (receipt is not None and receipt.status in {"pending", "processing"})
            for event, receipt in events
        ):
            return "blocked"
        now = _db_now(db)
        db.execute(
            update(TaskRecommendationConsumption)
            .where(TaskRecommendationConsumption.recommendation_id == recommendation.id)
            .values(state="expired", updated_at=now)
        )
        db.execute(
            delete(MockRecommendationRun).where(
                MockRecommendationRun.business_id == recommendation.id
            )
        )
        if event_ids:
            db.execute(
                delete(OutboxRejectedMessage).where(OutboxRejectedMessage.event_id.in_(event_ids))
            )
            db.execute(delete(OutboxDemoEffect).where(OutboxDemoEffect.event_id.in_(event_ids)))
            db.execute(delete(OutboxReceipt).where(OutboxReceipt.event_id.in_(event_ids)))
            db.execute(delete(OutboxEvent).where(OutboxEvent.id.in_(event_ids)))
        db.delete(recommendation)
        return "deleted"

    def _finalize_reference_set(self, db: Session, job: CleanupJob) -> str:
        hint = db.execute(
            select(ReferenceSet.owner_user_id).where(ReferenceSet.id == job.aggregate_id)
        ).scalar_one_or_none()
        if hint is None:
            return "stale"
        owner = db.execute(
            select(User).where(User.id == hint).with_for_update()
        ).scalar_one_or_none()
        reference_set = db.execute(
            select(ReferenceSet).where(ReferenceSet.id == job.aggregate_id).with_for_update()
        ).scalar_one_or_none()
        if owner is None or reference_set is None:
            return "stale"
        if db.scalar(
            select(exists().where(Task.reference_set_id == reference_set.id))
        ) or db.scalar(select(exists().where(Recommendation.reference_set_id == reference_set.id))):
            return "blocked"
        image_rows = (
            db.execute(
                select(ReferenceSetImage)
                .where(ReferenceSetImage.reference_set_id == reference_set.id)
                .order_by(ReferenceSetImage.id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        asset_ids = sorted(
            {
                asset_id
                for row in image_rows
                for asset_id in (row.source_asset_id, row.model_asset_id)
            },
            key=str,
        )
        if asset_ids and (
            db.scalar(
                select(
                    exists().where(
                        ReferenceSetImage.reference_set_id != reference_set.id,
                        or_(
                            ReferenceSetImage.source_asset_id.in_(asset_ids),
                            ReferenceSetImage.model_asset_id.in_(asset_ids),
                        ),
                    )
                )
            )
            or db.scalar(
                select(exists().where(TaskPromptItem.identity_anchor_asset_id.in_(asset_ids)))
            )
            or db.scalar(
                select(
                    exists().where(
                        or_(
                            GenerationResult.original_asset_id.in_(asset_ids),
                            GenerationResult.preview_asset_id.in_(asset_ids),
                        )
                    )
                )
            )
        ):
            return "blocked"
        db.execute(
            delete(ReferenceSetImage).where(ReferenceSetImage.reference_set_id == reference_set.id)
        )
        db.execute(
            delete(ReferenceUploadObject).where(
                ReferenceUploadObject.reference_set_id == reference_set.id
            )
        )
        if asset_ids:
            db.execute(delete(ImageAsset).where(ImageAsset.id.in_(asset_ids)))
        db.delete(reference_set)
        return "deleted"

    @staticmethod
    def _block_job(job: CleanupJob, code: str) -> None:
        job.status = "blocked"
        job.lease_token = None
        job.lease_until = None
        job.last_error_code = code
        job.updated_at = func.clock_timestamp()

    @staticmethod
    def _complete_without_delete(db: Session, job: CleanupJob) -> None:
        run_id = job.run_id
        job.status = "completed"
        job.lease_token = None
        job.lease_until = None
        job.completed_at = _db_now(db)
        job.updated_at = job.completed_at
        RetentionCleanupService._increment_run_counter(db, run_id, "jobs_completed")
        RetentionCleanupService._increment_run_counter(db, run_id, "jobs_skipped")
        RetentionCleanupService._record_run_reason(db, run_id, "CANDIDATE_NO_LONGER_ELIGIBLE")
        db.execute(delete(CleanupTarget).where(CleanupTarget.job_id == job.id))
        db.delete(job)

    @staticmethod
    def _complete_and_remove_job(
        db: Session, job: CleanupJob, *, counter: str | None = None
    ) -> None:
        run_id = job.run_id
        job.status = "completed"
        job.lease_token = None
        job.lease_until = None
        job.completed_at = _db_now(db)
        job.updated_at = job.completed_at
        if counter is not None:
            RetentionCleanupService._increment_run_counter(db, run_id, counter)
        RetentionCleanupService._increment_run_counter(db, run_id, "jobs_completed")
        db.execute(delete(CleanupTarget).where(CleanupTarget.job_id == job.id))
        db.delete(job)

    @staticmethod
    def _increment_run_counter(db: Session, run_id: UUID, counter: str) -> None:
        run = db.execute(
            select(CleanupRun).where(CleanupRun.id == run_id).with_for_update()
        ).scalar_one_or_none()
        if run is None:
            return
        counts = dict(run.counts or {})
        counts[counter] = int(counts.get(counter, 0)) + 1
        run.counts = counts

    @staticmethod
    def _record_run_reason(db: Session, run_id: UUID, code: str) -> None:
        allowed = {
            "AGGREGATE_NOT_ELIGIBLE",
            "CANDIDATE_NO_LONGER_ELIGIBLE",
            "CLEANUP_TARGETS_INCOMPLETE",
            "AGGREGATE_DELETE_BLOCKED",
            "INVENTORY_DELETE_FAILED",
            "STORAGE_DELETE_FAILED",
            "INVENTORY_SCAN_FAILED",
            "INVENTORY_UNAVAILABLE",
        }
        if code in allowed:
            RetentionCleanupService._increment_run_counter(db, run_id, f"reason:{code}")

    def _refresh_run_statuses(self) -> None:
        with self._sessions() as db, db.begin():
            runs = (
                db.execute(
                    select(CleanupRun)
                    .where(CleanupRun.status.in_(("queued", "running")))
                    .order_by(CleanupRun.created_at, CleanupRun.id)
                    .limit(200)
                    .with_for_update(skip_locked=True)
                )
                .scalars()
                .all()
            )
            for run in runs:
                active = int(
                    db.scalar(
                        select(func.count(CleanupJob.id)).where(
                            CleanupJob.run_id == run.id,
                            CleanupJob.status.in_(("pending", "claimed", "retry")),
                        )
                    )
                    or 0
                )
                if active:
                    run.status = "running"
                    continue
                counts = dict(run.counts or {})
                if (
                    not counts.get("business_discovery_complete", 0)
                    or run.orphan_scan_completed_at is None
                ):
                    # A discovery batch found more source data to inspect. Keep
                    # the run claimable so the next scheduler tick resumes the
                    # next bounded page/stream segment.
                    run.status = "running"
                    run.counts = counts
                    continue
                blocked = int(
                    db.scalar(
                        select(func.count(CleanupJob.id)).where(
                            CleanupJob.run_id == run.id,
                            CleanupJob.status == "blocked",
                        )
                    )
                    or 0
                )
                failed = blocked > 0 or counts.get("inventory_available", 1) == 0
                counts["jobs_blocked"] = blocked
                counts["jobs_failed"] = blocked + int(counts.get("inventory_available", 1) == 0)
                run.status = "failed" if failed else "completed"
                run.completed_at = _db_now(db)
                run.counts = counts


def _policy_response(policy: RetentionPolicy) -> RetentionPolicyResponse:
    return RetentionPolicyResponse(
        revision=policy.revision,
        task_retention_days=policy.task_retention_days,
        reference_retention_days=policy.reference_retention_days,
        recommendation_retention_days=policy.recommendation_retention_days,
    )


def _run_response(run: CleanupRun, *, pending_jobs: int) -> CleanupRunResponse:
    counts = dict(run.counts or {})
    counters = CleanupCounters(
        tasks_deleted=int(counts.get("tasks_deleted", 0)),
        recommendations_deleted=int(counts.get("recommendations_deleted", 0)),
        reference_sets_deleted=int(counts.get("reference_sets_deleted", 0)),
        upload_groups_deleted=int(counts.get("upload_groups_deleted", 0)),
        inventory_entries_deleted=int(counts.get("inventory_entries_deleted", 0)),
        inventory_entries_failed=int(counts.get("inventory_entries_failed", 0)),
        jobs_pending=pending_jobs,
        jobs_completed=int(counts.get("jobs_completed", 0)),
        jobs_succeeded=int(counts.get("jobs_succeeded", 0)),
        jobs_blocked=int(counts.get("jobs_blocked", 0)),
        jobs_skipped=int(counts.get("jobs_skipped", 0)),
        jobs_failed=int(counts.get("jobs_failed", 0)),
        reason_counts=[
            CleanupReasonCount(code=code, count=int(counts.get(f"reason:{code}", 0)))
            for code in (
                "AGGREGATE_NOT_ELIGIBLE",
                "CANDIDATE_NO_LONGER_ELIGIBLE",
                "CLEANUP_TARGETS_INCOMPLETE",
                "AGGREGATE_DELETE_BLOCKED",
                "INVENTORY_DELETE_FAILED",
                "STORAGE_DELETE_FAILED",
                "INVENTORY_SCAN_FAILED",
                "INVENTORY_UNAVAILABLE",
            )
            if int(counts.get(f"reason:{code}", 0)) > 0
        ],
    )
    safe_error = None
    if run.status == "failed":
        safe_error = (
            "INVENTORY_UNAVAILABLE"
            if counts.get("inventory_available", 1) == 0
            else "CLEANUP_INCOMPLETE"
        )
    return CleanupRunResponse(
        run_id=run.id,
        trigger_kind=run.trigger_kind,
        status=run.status,
        policy_revision=run.policy_revision,
        counters=counters,
        scheduled_for=run.scheduled_for,
        catchup_from=run.catchup_from,
        catchup_to=run.catchup_to,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        safe_error_code=safe_error,
    )


def _db_now(db: Session) -> datetime:
    value = db.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RuntimeError("database clock did not return an aware timestamp")
    return value.astimezone(UTC)


def _exists(db: Session, statement: object) -> bool:
    return bool(db.scalar(select(exists(statement))))


def _eligible_task_base(now: datetime) -> object:
    nonterminal_items = exists(
        select(TaskItem.id).where(
            TaskItem.task_id == Task.id,
            TaskItem.status.not_in(_TASK_ITEM_TERMINAL),
        )
    )
    nonterminal_attempts = exists(
        select(TaskItemAttempt.id)
        .join(TaskItem, TaskItem.id == TaskItemAttempt.task_item_id)
        .where(TaskItem.task_id == Task.id, TaskItemAttempt.status.not_in(_ATTEMPT_TERMINAL))
    )
    active_action = exists(
        select(Action.id).where(Action.task_id == Task.id, Action.status.in_(_ACTIVE_ACTIONS))
    )
    recovery_action = aliased(Action)
    resolved_attempt_unknown = and_(
        TaskItemAttempt.status.in_(_ATTEMPT_TERMINAL),
        exists(
            select(recovery_action.id).where(
                recovery_action.scope_kind == "attempt",
                recovery_action.attempt_id == Action.attempt_id,
                recovery_action.sequence > Action.sequence,
                recovery_action.action_kind == "generation.recover.v1",
                recovery_action.status == "completed",
                recovery_action.last_outcome == "accepted",
            )
        ),
    )
    unresolved_attempt_action = exists(
        select(Action.id)
        .join(TaskItemAttempt, TaskItemAttempt.id == Action.attempt_id)
        .join(TaskItem, TaskItem.id == TaskItemAttempt.task_item_id)
        .where(
            TaskItem.task_id == Task.id,
            Action.status == "unknown",
            Action.scope_kind == "attempt",
            ~resolved_attempt_unknown,
        )
    )
    submission_workflow = aliased(TaskPromptWorkflow)
    submission = aliased(PromptSubmission)
    resolved_submission_unknown = and_(
        submission.callback_result_digest_version.is_not(None),
        submission.callback_result_digest_version == PROMPT_RESULT_DIGEST_VERSION,
        submission.callback_result_digest.is_not(None),
        submission.status.in_(("callback_received", "package_ready", "failed")),
        submission_workflow.status.in_(_PROMPT_CALLBACK_TERMINAL),
    )
    unresolved_submission_action = exists(
        select(Action.id)
        .join(submission, submission.submission_id == Action.submission_id)
        .join(submission_workflow, submission_workflow.id == submission.workflow_id)
        .where(
            Action.task_id == Task.id,
            Action.status == "unknown",
            Action.scope_kind == "submission",
            ~resolved_submission_unknown,
        )
    )
    unknown_unhandled_scope = exists(
        select(Action.id).where(
            Action.task_id == Task.id,
            Action.status == "unknown",
            Action.scope_kind.not_in(("attempt", "submission")),
        )
    )
    active_prompt = exists(
        select(TaskPromptWorkflow.id).where(
            TaskPromptWorkflow.task_id == Task.id,
            TaskPromptWorkflow.status.in_(
                ("pending_submission", "submitted", "reconciling", "needs_input")
            ),
        )
    )
    active_submission_workflow = aliased(TaskPromptWorkflow)
    active_submission_row = aliased(PromptSubmission)
    resolved_active_submission_callback = and_(
        active_submission_row.callback_result_digest_version.is_not(None),
        active_submission_row.callback_result_digest_version == PROMPT_RESULT_DIGEST_VERSION,
        active_submission_row.callback_result_digest.is_not(None),
        active_submission_workflow.status.in_(_PROMPT_CALLBACK_TERMINAL),
    )
    active_submission = exists(
        select(active_submission_row.submission_id)
        .join(
            active_submission_workflow,
            active_submission_workflow.id == active_submission_row.workflow_id,
        )
        .where(
            active_submission_row.task_id == Task.id,
            or_(
                active_submission_row.status.in_(_ACTIVE_PROMPT_SUBMISSIONS),
                and_(
                    active_submission_row.status == "callback_received",
                    ~resolved_active_submission_callback,
                ),
            ),
        )
    )
    active_event = exists(
        select(OutboxEvent.id)
        .join(Action, Action.outbox_event_id == OutboxEvent.id)
        .outerjoin(OutboxReceipt, OutboxReceipt.event_id == OutboxEvent.id)
        .where(
            Action.task_id == Task.id,
            or_(
                OutboxEvent.status.in_(("pending", "claimed")),
                OutboxReceipt.status.in_(("pending", "processing")),
            ),
        )
    )
    active_prompt_event = exists(
        select(OutboxEvent.id)
        .outerjoin(OutboxReceipt, OutboxReceipt.event_id == OutboxEvent.id)
        .where(
            OutboxEvent.event_type == _TASK_PROMPT_EVENT,
            OutboxEvent.aggregate_id == Task.id,
            or_(
                OutboxEvent.status.in_(("pending", "claimed")),
                OutboxReceipt.status.in_(("pending", "processing")),
            ),
        )
    )
    active_generation_start_event = exists(
        select(OutboxEvent.id)
        .join(TaskItem, TaskItem.id == OutboxEvent.aggregate_id)
        .outerjoin(OutboxReceipt, OutboxReceipt.event_id == OutboxEvent.id)
        .where(
            TaskItem.task_id == Task.id,
            OutboxEvent.event_type == _GENERATION_START_EVENT,
            or_(
                OutboxEvent.status.in_(("pending", "claimed")),
                OutboxReceipt.status.in_(("pending", "processing")),
            ),
        )
    )
    return and_(
        Task.status.in_(_TASK_TERMINAL),
        Task.terminal_at.is_not(None),
        Task.expires_at.is_not(None),
        Task.expires_at <= now,
        ~nonterminal_items,
        ~nonterminal_attempts,
        ~active_action,
        ~unresolved_attempt_action,
        ~unresolved_submission_action,
        ~unknown_unhandled_scope,
        ~active_prompt,
        ~active_submission,
        ~active_event,
        ~active_prompt_event,
        ~active_generation_start_event,
    )


def _eligible_task_ids(db: Session, now: datetime) -> list[UUID]:
    return list(
        db.execute(
            select(Task.id).where(_eligible_task_base(now)).order_by(Task.expires_at, Task.id)
        ).scalars()
    )


def _eligible_task_count(db: Session, now: datetime) -> int:
    return int(db.scalar(select(func.count(Task.id)).where(_eligible_task_base(now))) or 0)


def _has_active_prompt_submissions(
    submissions: list[PromptSubmission], workflows: list[TaskPromptWorkflow]
) -> bool:
    workflows_by_id = {workflow.id: workflow for workflow in workflows}
    for submission in submissions:
        if submission.status in _ACTIVE_PROMPT_SUBMISSIONS:
            return True
        if submission.status != "callback_received":
            continue
        workflow = workflows_by_id.get(submission.workflow_id)
        if not _has_terminal_prompt_callback(submission, workflow):
            return True
    return False


def _has_terminal_prompt_callback(
    submission: PromptSubmission, workflow: TaskPromptWorkflow | None
) -> bool:
    return (
        submission.callback_result_digest_version == PROMPT_RESULT_DIGEST_VERSION
        and submission.callback_result_digest is not None
        and submission.status in {"callback_received", "package_ready", "failed"}
        and workflow is not None
        and workflow.status in _PROMPT_CALLBACK_TERMINAL
    )


def _has_blocking_actions(
    actions: list[Action],
    *,
    attempts: list[TaskItemAttempt],
    submissions: list[PromptSubmission],
    workflows: list[TaskPromptWorkflow],
    resolved_generation_requests: frozenset[UUID] = frozenset(),
) -> bool:
    attempts_by_id = {attempt.id: attempt for attempt in attempts}
    submissions_by_id = {submission.submission_id: submission for submission in submissions}
    workflows_by_id = {workflow.id: workflow for workflow in workflows}
    actions_by_attempt: dict[UUID, list[Action]] = {}
    for action in actions:
        if action.scope_kind == "attempt" and action.attempt_id is not None:
            actions_by_attempt.setdefault(action.attempt_id, []).append(action)

    for action in actions:
        if action.status in _ACTIVE_ACTIONS:
            return True
        if action.status != "unknown":
            continue
        if (
            action.scope_kind == "generation_request"
            and action.action_kind == "chatflow.generation.recover.v1"
            and action.generation_request_id in resolved_generation_requests
        ):
            continue
        if action.scope_kind == "attempt":
            attempt = attempts_by_id.get(action.attempt_id)
            if attempt is None or attempt.status not in _ATTEMPT_TERMINAL:
                return True
            recovered = any(
                later.sequence > action.sequence
                and later.action_kind == "generation.recover.v1"
                and later.status == "completed"
                and later.last_outcome == "accepted"
                for later in actions_by_attempt.get(action.attempt_id, ())
            )
            if not recovered:
                return True
            continue
        if action.scope_kind == "submission":
            submission = submissions_by_id.get(action.submission_id)
            workflow = (
                workflows_by_id.get(submission.workflow_id) if submission is not None else None
            )
            if submission is None or not _has_terminal_prompt_callback(submission, workflow):
                return True
            continue
        # Unknown future scope kinds must remain protected until their resolver
        # has an explicit terminal-state proof here.
        return True
    return False


def _resolved_generation_requests_for_cleanup(db: Session, task_id: UUID) -> frozenset[UUID]:
    """Return only H6 requests resolved by negative closure or completed H7 ACK."""
    requests = (
        db.execute(
            select(ChatflowGenerationRequest).where(ChatflowGenerationRequest.task_id == task_id)
        )
        .scalars()
        .all()
    )
    resolved: set[UUID] = set()
    for request in requests:
        evidence = db.get(ChatflowGenerationAcceptanceEvidence, request.id)
        tracking = db.get(ChatflowGenerationTracking, request.id)
        if evidence is None or tracking is None:
            continue
        if (
            evidence.outcome == "rejected_expired"
            and evidence.closed_at is not None
            and evidence.batch_id is None
            and evidence.receipt_id is not None
            and evidence.receipt_digest is not None
            and evidence.service_authority_id == request.service_authority_id
            and tracking.status == "rejected_before_acceptance"
            and tracking.batch_id is None
            and tracking.source_sequence is None
        ):
            resolved.add(request.id)
            continue
        followup = db.get(ChatflowGenerationAcceptedBatchFollowup, request.id)
        if (
            evidence.outcome == "accepted"
            and evidence.batch_id is not None
            and evidence.service_authority_id == request.service_authority_id
            and followup is not None
            and followup.state == "complete"
            and followup.resolved_at is not None
            and followup.batch_id == evidence.batch_id
            and followup.acceptance_digest == evidence.evidence_digest
            and followup.service_authority_id == evidence.service_authority_id
            and tracking.batch_id == evidence.batch_id
            and tracking.reconcile_state == "complete"
        ):
            resolved.add(request.id)
    return frozenset(resolved)


def _has_blocking_business_questions(
    questions: Iterable[BusinessQuestion],
    answers: Iterable[BusinessAnswerSubmission],
    *,
    now: datetime,
) -> bool:
    if any(
        question.status in {"confirming", "reconciling"}
        or (
            question.status in {"awaiting_form_binding", "awaiting_answer"}
            and question.expires_at > now
        )
        for question in questions
    ):
        return True
    return any(answer.status in {"confirming", "unknown"} for answer in answers)


def _task_item_ids(db: Session, task_id: UUID) -> list[UUID]:
    return list(
        db.execute(
            select(TaskItem.id).where(TaskItem.task_id == task_id).order_by(TaskItem.id)
        ).scalars()
    )


def _task_event_ids(db: Session, task_id: UUID, item_ids: list[UUID]) -> list[UUID]:
    event_predicates = [
        OutboxEvent.id.in_(select(Action.outbox_event_id).where(Action.task_id == task_id)),
        and_(OutboxEvent.event_type == _TASK_PROMPT_EVENT, OutboxEvent.aggregate_id == task_id),
    ]
    if item_ids:
        event_predicates.append(
            and_(
                OutboxEvent.event_type == _GENERATION_START_EVENT,
                OutboxEvent.aggregate_id.in_(item_ids),
            )
        )
    return list(
        db.execute(
            select(OutboxEvent.id).where(or_(*event_predicates)).order_by(OutboxEvent.id)
        ).scalars()
    )


def _estimate_business_objects(
    db: Session,
    *,
    task_ids: list[UUID],
    reference_ids: list[UUID],
    upload_group_ids: list[UUID],
) -> tuple[int, int]:
    sizes: dict[str, int] = {}
    if task_ids:
        for key, size in db.execute(
            select(ImageAsset.object_key, ImageAsset.size_bytes)
            .join(
                GenerationResult,
                or_(
                    GenerationResult.original_asset_id == ImageAsset.id,
                    GenerationResult.preview_asset_id == ImageAsset.id,
                ),
            )
            .where(GenerationResult.task_id.in_(task_ids))
        ):
            sizes[str(key)] = max(0, int(size))
        for context in db.execute(
            select(
                GenerationAttemptContext.original_object_key,
                GenerationAttemptContext.original_size_bytes,
                GenerationAttemptContext.preview_object_key,
                GenerationAttemptContext.preview_size_bytes,
            )
            .join(TaskItemAttempt, TaskItemAttempt.id == GenerationAttemptContext.attempt_id)
            .join(TaskItem, TaskItem.id == TaskItemAttempt.task_item_id)
            .where(TaskItem.task_id.in_(task_ids))
        ):
            original_key, original_size, preview_key, preview_size = context
            sizes.setdefault(str(original_key), max(0, int(original_size or 0)))
            sizes.setdefault(str(preview_key), max(0, int(preview_size or 0)))
    reference_group_ids = sorted(set(reference_ids + upload_group_ids), key=str)
    if reference_ids:
        for key, size in db.execute(
            select(ImageAsset.object_key, ImageAsset.size_bytes)
            .join(
                ReferenceSetImage,
                or_(
                    ReferenceSetImage.source_asset_id == ImageAsset.id,
                    ReferenceSetImage.model_asset_id == ImageAsset.id,
                ),
            )
            .where(ReferenceSetImage.reference_set_id.in_(reference_ids))
        ):
            sizes.setdefault(str(key), max(0, int(size)))
    if reference_group_ids:
        for key, size in db.execute(
            select(
                ReferenceUploadObject.object_key, ReferenceUploadObject.expected_size_bytes
            ).where(
                ReferenceUploadObject.reference_set_id.in_(reference_group_ids),
                ReferenceUploadObject.status != "deleted",
            )
        ):
            sizes.setdefault(str(key), max(0, int(size)))
    return len(sizes), sum(sizes.values())


def _eligible_recommendation_base(now: datetime) -> object:
    has_task = exists(select(Task.id).where(Task.recommendation_id == Recommendation.id))
    active_event = exists(
        select(OutboxEvent.id)
        .outerjoin(OutboxReceipt, OutboxReceipt.event_id == OutboxEvent.id)
        .where(
            OutboxEvent.event_type == _RECOMMENDATION_EVENT,
            OutboxEvent.aggregate_id == Recommendation.id,
            or_(
                OutboxEvent.status.in_(("pending", "claimed")),
                OutboxReceipt.status.in_(("pending", "processing")),
            ),
        )
    )
    return and_(
        Recommendation.status.in_(("ready", "failed", "expired")),
        Recommendation.data_expires_at <= now,
        ~has_task,
        ~active_event,
    )


def _eligible_recommendation_ids(db: Session, now: datetime) -> list[UUID]:
    return list(
        db.execute(
            select(Recommendation.id)
            .where(_eligible_recommendation_base(now))
            .order_by(Recommendation.data_expires_at, Recommendation.id)
        ).scalars()
    )


def _eligible_recommendation_count(db: Session, now: datetime) -> int:
    return int(
        db.scalar(select(func.count(Recommendation.id)).where(_eligible_recommendation_base(now)))
        or 0
    )


def _no_task_or_recommendation(reference_id: object) -> object:
    has_task = exists(select(Task.id).where(Task.reference_set_id == reference_id))
    has_recommendation = exists(
        select(Recommendation.id).where(Recommendation.reference_set_id == reference_id)
    )
    return and_(~has_task, ~has_recommendation)


def _eligible_reference_base(now: datetime) -> object:
    ready_expired = and_(
        ReferenceSet.status == "ready",
        ReferenceSet.expires_at.is_not(None),
        ReferenceSet.expires_at <= now,
    )
    return and_(ready_expired, _no_task_or_recommendation(ReferenceSet.id))


def _eligible_reference_ids(db: Session, now: datetime) -> list[UUID]:
    return list(
        db.execute(
            select(ReferenceSet.id)
            .where(_eligible_reference_base(now))
            .order_by(ReferenceSet.expires_at, ReferenceSet.id)
        ).scalars()
    )


def _eligible_reference_count(db: Session, now: datetime) -> int:
    return int(
        db.scalar(select(func.count(ReferenceSet.id)).where(_eligible_reference_base(now))) or 0
    )


def _eligible_upload_base(now: datetime, cutoff: datetime) -> object:
    failed_stale = and_(
        ReferenceSet.status == "failed",
        ReferenceSet.failed_at.is_not(None),
        ReferenceSet.failed_at <= cutoff,
    )
    uploading_stale = and_(
        ReferenceSet.status == "uploading",
        ReferenceSet.last_activity_at <= cutoff,
        or_(ReferenceSet.upload_lease_until.is_(None), ReferenceSet.upload_lease_until <= cutoff),
    )
    return and_(or_(failed_stale, uploading_stale), _no_task_or_recommendation(ReferenceSet.id))


def _eligible_upload_group_ids(db: Session, now: datetime, cutoff: datetime) -> list[UUID]:
    return list(
        db.execute(
            select(ReferenceSet.id)
            .where(_eligible_upload_base(now, cutoff))
            .order_by(ReferenceSet.last_activity_at, ReferenceSet.id)
        ).scalars()
    )


def _eligible_upload_group_count(db: Session, now: datetime, cutoff: datetime) -> int:
    return int(
        db.scalar(select(func.count(ReferenceSet.id)).where(_eligible_upload_base(now, cutoff)))
        or 0
    )


def _lock_events_and_receipts(
    db: Session, event_ids: Iterable[UUID]
) -> list[tuple[OutboxEvent, OutboxReceipt | None]]:
    ordered = sorted(set(event_ids), key=str)
    if not ordered:
        return []
    events = (
        db.execute(
            select(OutboxEvent)
            .where(OutboxEvent.id.in_(ordered))
            .order_by(OutboxEvent.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    receipts = (
        db.execute(
            select(OutboxReceipt)
            .where(OutboxReceipt.event_id.in_(ordered))
            .order_by(OutboxReceipt.event_id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    receipt_by_id = {receipt.event_id: receipt for receipt in receipts}
    return [(event, receipt_by_id.get(event.id)) for event in events]


def _safe_inventory_entry(entry: StorageInventoryEntry) -> bool:
    if not isinstance(entry, StorageInventoryEntry):
        return False
    locator = entry.relative_locator
    if (
        not isinstance(locator, str)
        or len(locator) > 1024
        or "\\" in locator
        or locator.startswith("/")
        or any(part in {"", ".", ".."} for part in locator.split("/"))
        or not locator.startswith(("users/", "generated/"))
        or entry.kind not in {"object", "staging", "sidecar"}
        or entry.modified_at.tzinfo is None
        or entry.size_bytes < 0
    ):
        return False
    if entry.kind == "staging":
        return entry.object_key is None
    if (
        not entry.object_key
        or len(entry.object_key) > 512
        or "\\" in entry.object_key
        or entry.object_key.startswith("/")
        or any(part in {"", ".", ".."} for part in entry.object_key.split("/"))
    ):
        return False
    if entry.kind == "object":
        return entry.object_key == locator
    return entry.kind == "sidecar"


def _safe_inventory_cursor(cursor: str) -> bool:
    return (
        isinstance(cursor, str)
        and 0 < len(cursor) <= 1024
        and "\\" not in cursor
        and not cursor.startswith("/")
        and cursor.startswith(("users/", "generated/"))
        and all(part not in {"", ".", ".."} for part in cursor.split("/"))
    )


def _inventory_sort_key(locator: str) -> tuple[str, ...]:
    return tuple(locator.split("/"))


def _validate_inventory_page(
    page: StorageInventoryPage,
    *,
    expected_cursor: str | None,
    max_scanned_items: int,
) -> None:
    if (
        isinstance(page.scanned_items, bool)
        or not isinstance(page.scanned_items, int)
        or page.scanned_items < 0
        or page.scanned_items > max_scanned_items
        or len(page.entries) > page.scanned_items
        or not isinstance(page.exhausted, bool)
        or (page.next_cursor is not None and not _safe_inventory_cursor(page.next_cursor))
    ):
        raise StorageError("inventory page is invalid")

    expected_key = _inventory_sort_key(expected_cursor) if expected_cursor is not None else None
    next_key = _inventory_sort_key(page.next_cursor) if page.next_cursor is not None else None
    if page.scanned_items > 0:
        if next_key is None or (expected_key is not None and next_key <= expected_key):
            raise StorageError("inventory cursor did not advance")
    elif not page.exhausted or page.next_cursor != expected_cursor:
        raise StorageError("empty inventory page cannot advance its cursor")

    previous_entry_key: tuple[str, ...] | None = None
    for entry in page.entries:
        locator = entry.relative_locator
        if not isinstance(locator, str) or not locator:
            raise StorageError("inventory page contains an invalid locator")
        entry_key = _inventory_sort_key(locator)
        if (
            (expected_key is not None and entry_key <= expected_key)
            or (next_key is not None and entry_key > next_key)
            or (previous_entry_key is not None and entry_key <= previous_entry_key)
        ):
            raise StorageError("inventory page locators are not in cursor order")
        previous_entry_key = entry_key


def _inventory_entry_is_protected(
    db: Session, entry: StorageInventoryEntry, *, now: datetime
) -> bool:
    if entry.object_key is not None:
        if db.scalar(select(exists().where(ImageAsset.object_key == entry.object_key))):
            return True
        if db.scalar(
            select(
                exists().where(
                    ReferenceUploadObject.object_key == entry.object_key,
                    ReferenceUploadObject.status != "deleted",
                )
            )
        ):
            return True
        if db.scalar(
            select(
                exists().where(
                    or_(
                        GenerationAttemptContext.original_object_key == entry.object_key,
                        GenerationAttemptContext.preview_object_key == entry.object_key,
                    )
                )
            )
        ) or db.scalar(
            select(
                exists().where(
                    or_(
                        ChatflowGenerationDelivery.original_object_key == entry.object_key,
                        ChatflowGenerationDelivery.preview_object_key == entry.object_key,
                    )
                )
            )
        ):
            return True
    target_protection = [CleanupTarget.relative_locator == entry.relative_locator]
    if entry.object_key is not None:
        # Business manifests own the base object and its sidecar as a unit.
        # Orphan inventory entries are separate exact locators: a pending
        # sidecar must not suppress the neighboring object (or vice versa).
        target_protection.append(
            and_(
                CleanupJob.aggregate_kind != "orphan",
                CleanupTarget.object_key == entry.object_key,
            )
        )
    if db.scalar(
        select(
            exists(
                select(CleanupTarget.id)
                .join(CleanupJob, CleanupJob.id == CleanupTarget.job_id)
                .where(
                    CleanupTarget.status.in_(("pending", "failed")),
                    or_(*target_protection),
                )
            )
        )
    ):
        return True
    parts = entry.relative_locator.split("/")
    if parts[0] == "users" and len(parts) >= 4 and parts[2] == "reference-sets":
        try:
            reference_id = UUID(parts[3])
        except ValueError:
            return False
        if db.scalar(select(exists().where(ReferenceSet.id == reference_id))):
            return True
    if parts[0] == "generated" and len(parts) >= 2:
        try:
            attempt_id = UUID(parts[1])
        except ValueError:
            return False
        if db.scalar(
            select(
                exists().where(
                    or_(
                        GenerationAttemptContext.attempt_id == attempt_id,
                        TaskItemAttempt.id == attempt_id,
                    )
                )
            )
        ):
            return True
    # For staging paths, a live reference upload lease or a persisted generation
    # context protects sibling temp files even before an object key is attached.
    if parts[0] == "users" and len(parts) >= 4 and parts[2] == "reference-sets":
        try:
            reference_id = UUID(parts[3])
        except ValueError:
            return False
        if db.scalar(
            select(
                exists().where(
                    ReferenceSet.id == reference_id,
                    ReferenceSet.status == "uploading",
                    ReferenceSet.upload_lease_until > now,
                )
            )
        ):
            return True
    return False
