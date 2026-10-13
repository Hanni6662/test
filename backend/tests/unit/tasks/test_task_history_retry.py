from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from uuid import uuid4

import pytest
from app.api.dependencies import Principal, require_principal, require_same_origin
from app.api.tasks import router
from app.core.domain_errors import DomainError
from app.db.task_models import Task, TaskItem
from app.schemas.recommendations import IMAGE_TYPES
from app.schemas.task_history import (
    TaskHistoryEntry,
    TaskHistoryPage,
    TaskRetryAccepted,
    TaskRetryAttempt,
    TaskRetryRequest,
)
from app.services.tasks import (
    PreviewStream,
    TaskService,
    _decode_history_cursor,
    _encode_history_cursor,
    _retry_attempt_id,
    retry_request_digest,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError


def _task(*, planned_count: int = 1, status: str = "reconciling") -> Task:
    return Task(
        id=uuid4(),
        owner_user_id=uuid4(),
        reference_set_id=uuid4(),
        recommendation_id=uuid4(),
        recommendation_revision=1,
        recommendation_snapshot_version="synthetic-v1",
        schema_version=2,
        workflow_version="business-task-v1",
        request_digest=b"r" * 32,
        request_digest_version=1,
        request_snapshot={},
        resolved_preferences={},
        recommendation_snapshot={},
        reference_assets_snapshot=[],
        category_snapshot="test",
        image_types=list(IMAGE_TYPES[:planned_count]),
        planned_count=planned_count,
        billing_mode="disabled",
        billing_policy_version="disabled-v1",
        billing_snapshot={"charged_units": 0},
        status=status,
        retention_days=60,
        retention_policy_revision=1,
    )


def _item(task: Task, *, status: str = "unknown") -> TaskItem:
    return TaskItem(
        id=uuid4(),
        task_id=task.id,
        position=1,
        image_type=IMAGE_TYPES[0],
        status=status,
        preview_ready=False,
    )


def test_history_cursor_round_trips_utc_timestamp_and_uuid() -> None:
    created_at = datetime(2026, 10, 7, 4, 5, 6, 123456, tzinfo=UTC)
    task_id = uuid4()

    cursor = _encode_history_cursor(created_at, task_id)
    decoded = _decode_history_cursor(cursor)

    assert decoded.created_at == created_at
    assert decoded.task_id == task_id


@pytest.mark.parametrize("cursor", ["", "!!!", "eyJ2IjoiMiJ9", "x" * 257])
def test_history_cursor_rejects_malformed_or_unbounded_values(cursor: str) -> None:
    with pytest.raises(ValueError):
        _decode_history_cursor(cursor)


def test_retry_digest_and_attempt_ids_are_canonical_and_scoped() -> None:
    task_id = uuid4()
    first_item_id = uuid4()
    second_item_id = uuid4()
    key_hash = b"k" * 32

    assert retry_request_digest(task_id, (first_item_id, second_item_id)) == retry_request_digest(
        task_id, (second_item_id, first_item_id)
    )
    assert retry_request_digest(task_id, (first_item_id,)) != retry_request_digest(
        task_id, (second_item_id,)
    )
    assert _retry_attempt_id(task_id, first_item_id, key_hash) == _retry_attempt_id(
        task_id, first_item_id, key_hash
    )
    assert _retry_attempt_id(task_id, first_item_id, b"z" * 32) != _retry_attempt_id(
        task_id, first_item_id, key_hash
    )


def test_retry_request_requires_a_nonempty_unique_explicit_item_list() -> None:
    first = uuid4()
    second = uuid4()
    assert TaskRetryRequest(failed_item_ids=[first, second]).failed_item_ids == [first, second]
    for value in ([], [first, first], [uuid4() for _ in range(9)]):
        with pytest.raises(ValidationError):
            TaskRetryRequest(failed_item_ids=value)
    with pytest.raises(ValidationError):
        TaskRetryRequest.model_validate({"failed_item_ids": [first], "retry_all": True})


def test_public_projection_counts_unknown_as_unresolved_and_checks_invariant() -> None:
    task = _task()
    item = _item(task, status="unknown")

    result = TaskService._to_public_response(task, [item])

    assert result.counts.model_dump() == {
        "planned": 1,
        "succeeded": 0,
        "failed": 0,
        "pending": 0,
        "unresolved": 1,
    }
    assert result.items[0].status == "reconciling"
    with pytest.raises(DomainError) as mismatch:
        TaskService._to_public_response(_task(planned_count=2), [item])
    assert mismatch.value.code == "TASK_STATE_UNAVAILABLE"


class _TrackingReader(BytesIO):
    def __init__(self, content: bytes) -> None:
        super().__init__(content)
        self.was_closed = False

    def close(self) -> None:
        self.was_closed = True
        super().close()


class _TaskServiceStub:
    def __init__(self, reader: _TrackingReader) -> None:
        self.reader = reader
        self.owner_id: object | None = None
        self.retry_args: dict[str, object] | None = None

    def open_preview(self, **kwargs: object) -> PreviewStream:
        self.owner_id = kwargs["owner_user_id"]
        return PreviewStream(body=self.reader, mime_type="image/webp", size_bytes=4)

    def list_tasks(self, **kwargs: object) -> TaskHistoryPage:
        now = datetime.now(UTC)
        return TaskHistoryPage(
            items=[
                TaskHistoryEntry(
                    task_id=uuid4(),
                    status="succeeded",
                    created_at=now,
                    counts={
                        "planned": 1,
                        "succeeded": 1,
                        "failed": 0,
                        "pending": 0,
                        "unresolved": 0,
                    },
                    items=[],
                )
            ],
            next_cursor=None,
        )

    def retry_failed_items(self, **kwargs: object) -> TaskRetryAccepted:
        self.retry_args = kwargs
        request = kwargs["request"]
        assert isinstance(request, TaskRetryRequest)
        task_id = kwargs["task_id"]
        assert isinstance(task_id, type(uuid4()))
        return TaskRetryAccepted(
            task_id=task_id,
            attempts=[
                TaskRetryAttempt(task_item_id=item_id, attempt_id=uuid4())
                for item_id in request.failed_item_ids
            ],
        )


def test_private_preview_stream_and_retry_routes_keep_headers_and_identity() -> None:
    owner_id = uuid4()
    reader = _TrackingReader(b"WEBP")
    task_service = _TaskServiceStub(reader)
    app = FastAPI()
    app.state.task_service = task_service
    app.state.storage = object()
    app.include_router(router)
    app.dependency_overrides[require_principal] = lambda: Principal(
        user_id=owner_id,
        identity_kind="guest",
        session_id=uuid4(),
    )
    app.dependency_overrides[require_same_origin] = lambda: None
    client = TestClient(app)

    preview = client.get(f"/api/v1/tasks/{uuid4()}/images/{uuid4()}/preview")
    assert preview.status_code == 200
    assert preview.content == b"WEBP"
    assert preview.headers["content-type"] == "image/webp"
    assert preview.headers["cache-control"] == "private, no-store"
    assert preview.headers["x-content-type-options"] == "nosniff"
    assert reader.was_closed
    assert task_service.owner_id == owner_id

    history = client.get("/api/v1/tasks?limit=7")
    assert history.status_code == 200
    assert history.headers["cache-control"] == "no-store"

    task_id = uuid4()
    item_id = uuid4()
    retried = client.post(
        f"/api/v1/tasks/{task_id}/retry",
        headers={"Idempotency-Key": "retry-1"},
        json={"failed_item_ids": [str(item_id)]},
    )
    assert retried.status_code == 202
    assert retried.headers["cache-control"] == "no-store"
    assert task_service.retry_args is not None
    assert task_service.retry_args["owner_user_id"] == owner_id
    assert task_service.retry_args["idempotency_key"] == "retry-1"
    assert isinstance(task_service.retry_args["request"], TaskRetryRequest)
