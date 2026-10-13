import json
from uuid import UUID, uuid4

from app.db.business_session_models import BusinessSession
from app.db.recommendation_models import Recommendation
from app.db.task_models import Task
from app.schemas.chatflow_v34 import chatflow_v34_stage_event_digest
from app.services.chatflow_v34_events import ChatflowV34StageEventService
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.integration.business_sessions.test_chatflow_v34_sessions_postgres import (
    _create_session,
    _v34_recommendation_ready_event,
)
from tests.integration.business_sessions.test_chatflow_v34_sessions_postgres import (
    v34_pg_engine as v34_pg_engine,
)


def test_unseen_primary_image_is_not_exposed_as_ready_recommendation(v34_pg_engine):
    client, owner, ref, created = _create_session(
        v34_pg_engine, flow_version="visual-v3.4-single-selection", idempotency_key=str(uuid4())
    )
    sid, rid = UUID(created["business_session_id"]), UUID(created["recommendation_id"])
    run = "missing-primary-" + str(uuid4())
    with Session(v34_pg_engine) as db, db.begin():
        session = db.get(BusinessSession, sid)
        session.status = "awaiting_recommendation"
        session.start_outcome = "accepted"
        session.dify_run_id = run
        db.get(Recommendation, rid).dify_run_id = run
    event = _v34_recommendation_ready_event(
        business_session_id=sid, recommendation_id=rid, run_id=run, event_key=run + ":prepare:ready"
    )
    audit = json.loads(event.payload["audit"])
    audit.update(
        summary="本次消息中 PRIMARY 主参考图为空，未附带任何实际图片。",
        primary_view="未提供 PRIMARY 图片，无可分析的视角与信息覆盖。",
    )
    event = event.model_copy(update={"payload": {"audit": json.dumps(audit, ensure_ascii=False)}})
    event = event.model_copy(update={"digest": chatflow_v34_stage_event_digest(event).hex()})
    accepted = ChatflowV34StageEventService(v34_pg_engine).accept(event)
    assert accepted.status == "rejected"
    public = client.get(f"/api/v1/business-sessions/{sid}").json()
    assert public["status"] == "failed"
    assert public["safe_reason_code"] == "V34_REFERENCE_NOT_OBSERVED"
    assert public["selection_ready"] is False
    assert public["recommendation"] is None
    with Session(v34_pg_engine) as db:
        assert db.get(Recommendation, rid).failure_code == "V34_REFERENCE_NOT_OBSERVED"
        assert db.scalar(select(Task.id).where(Task.business_session_id == sid)) is None
