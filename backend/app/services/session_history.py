from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.core.domain_errors import DomainError
from app.db.action_models import Action
from app.db.business_session_models import BusinessSession
from app.db.chatflow_generation_models import ChatflowGenerationRequest
from app.db.chatflow_reference_files import ChatflowDifyReferenceFile
from app.db.chatflow_v34_models import ChatflowV34SessionState, ChatflowV34StageEventReceipt
from app.db.recommendation_models import Recommendation
from app.db.reference_models import ReferenceSet
from app.db.task_models import Task
from app.schemas.business_sessions import BusinessSessionHistoryEntry, BusinessSessionHistoryPage


def can_end_unstarted(
    db: Session, session: BusinessSession, recommendation: Recommendation
) -> bool:
    """Only an ambiguous file upload with no run-start intent can be abandoned."""
    if (
        session.flow_version != "visual-v3.4-single-selection"
        or session.status != "reconciling"
        or session.start_outcome != "unknown"
        or session.dify_run_id is not None
        or recommendation.dify_run_id is not None
        or (recommendation.state or {}).get("stage") != "reference_upload_unknown"
    ):
        return False
    state = db.scalar(
        select(ChatflowV34SessionState).where(
            ChatflowV34SessionState.business_session_id == session.id
        )
    )
    if (
        state is None
        or state.form_token is not None
        or state.form_status != "awaiting_event"
        or state.selection_status != "not_started"
    ):
        return False
    for model in (Task, ChatflowGenerationRequest, ChatflowV34StageEventReceipt):
        if db.scalar(select(model.id).where(model.business_session_id == session.id).limit(1)):
            return False
    actions = db.scalars(select(Action).where(Action.business_session_id == session.id)).all()
    if not actions or any(
        action.action_kind != "dify.chatflow.reference.upload.v1"
        or action.status not in {"completed", "rejected", "unknown"}
        or action.claim_token is not None
        or action.lease_until is not None
        for action in actions
    ):
        return False
    return (
        db.scalar(
            select(ChatflowDifyReferenceFile.id)
            .where(
                ChatflowDifyReferenceFile.business_session_id == session.id,
                ChatflowDifyReferenceFile.status == "unknown",
            )
            .limit(1)
        )
        is not None
    )


def list_session_history(
    db: Session, owner_user_id: UUID, now: datetime, limit: int, cursor: str | None
) -> BusinessSessionHistoryPage:
    filters = [BusinessSession.owner_user_id == owner_user_id, BusinessSession.expires_at > now]
    if cursor is not None:
        try:
            if not 1 <= len(cursor) <= 256:
                raise ValueError
            value = json.loads(
                base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            )
            if not isinstance(value, list) or len(value) != 3 or value[0] != "session-v1":
                raise ValueError
            timestamp, session_id = datetime.fromisoformat(value[1]), UUID(value[2])
            if timestamp.tzinfo is None or timestamp.utcoffset() != UTC.utcoffset(timestamp):
                raise ValueError
        except (ValueError, TypeError, binascii.Error, UnicodeError):
            raise DomainError(400, "HISTORY_CURSOR_INVALID", "历史游标无效") from None
        filters.append(
            or_(
                BusinessSession.created_at < timestamp,
                and_(
                    BusinessSession.created_at == timestamp,
                    BusinessSession.id < session_id,
                ),
            )
        )
    rows = db.execute(
        select(
            BusinessSession,
            ReferenceSet.product_category,
            func.coalesce(ChatflowV34SessionState.safe_reason_code, Recommendation.failure_code),
            Task.id,
            Task.status,
        )
        .join(ReferenceSet, ReferenceSet.id == BusinessSession.reference_set_id)
        .outerjoin(
            ChatflowV34SessionState,
            ChatflowV34SessionState.business_session_id == BusinessSession.id,
        )
        .outerjoin(Task, Task.business_session_id == BusinessSession.id)
        .outerjoin(Recommendation, Recommendation.business_session_id == BusinessSession.id)
        .where(*filters)
        .order_by(BusinessSession.created_at.desc(), BusinessSession.id.desc())
        .limit(limit + 1)
    ).all()
    from app.services.chatflow_generation_closure import project_generation_completion

    completions = {
        task_id: project_generation_completion(db, task_id)
        for _, _, _, task_id, _ in rows[:limit] if task_id is not None
    }
    entries = [
        BusinessSessionHistoryEntry(
            business_session_id=session.id,
            reference_set_id=session.reference_set_id,
            product_category=category,
            created_at=session.created_at,
            status=session.status,
            safe_reason_code=(
                completions[task_id]["reason_code"]
                if completions.get(task_id) is not None and completions[task_id][
                    "reconciliation_status"
                ] in {"completed", "completed_with_execution_warning"}
                else reason
            ),
            completion=completions.get(task_id),
            task_id=task_id,
            task_status=task_status,
        )
        for session, category, reason, task_id, task_status in rows[:limit]
    ]
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1][0]
        next_cursor = (
            base64.urlsafe_b64encode(
                json.dumps(
                    ["session-v1", last.created_at.isoformat(), str(last.id)],
                    separators=(",", ":"),
                ).encode()
            )
            .decode()
            .rstrip("=")
        )
    return BusinessSessionHistoryPage(items=entries, next_cursor=next_cursor)
