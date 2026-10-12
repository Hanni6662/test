from __future__ import annotations

import hashlib
from uuid import uuid4

import httpx
from app.integrations.dify.chatflow import DifyReferenceUploadCommand
from app.integrations.dify.reference_files import DifyReferenceFileUploadClient


def _command(*, app_id: str = "isolated-app") -> DifyReferenceUploadCommand:
    content = b"normalized-reference-content"
    return DifyReferenceUploadCommand(
        business_session_id=uuid4(),
        dify_app_id=app_id,
        dify_user_id="bs-isolated-session",
        reference_set_image_id=uuid4(),
        model_asset_id=uuid4(),
        slot="PRIMARY",
        position=0,
        content_sha256=hashlib.sha256(content).hexdigest(),
        mime_type="image/png",
        size_bytes=len(content),
        content=content,
    )


def test_upload_client_uses_fixed_app_origin_and_same_end_user() -> None:
    observed: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(201, json={"id": "dify-file-1"})

    client = httpx.Client(transport=httpx.MockTransport(respond))
    adapter = DifyReferenceFileUploadClient(
        api_base_url="http://dify-api:5001",
        app_id="isolated-app",
        app_api_token="isolated-app-secret-not-logged",
        client=client,
    )
    result = adapter.upload_reference(_command())

    assert result.outcome == "uploaded"
    assert result.dify_file_id == "dify-file-1"
    assert len(observed) == 1
    assert observed[0].method == "POST"
    assert str(observed[0].url) == "http://dify-api:5001/v1/files/upload"
    assert observed[0].headers["Authorization"] == "Bearer isolated-app-secret-not-logged"
    body = observed[0].read().decode("latin-1")
    assert 'name="user"' in body
    assert "bs-isolated-session" in body
    assert 'name="file"; filename="primary.png"' in body
    assert "normalized-reference-content" in body
    client.close()


def test_upload_client_rejects_other_app_without_http_call() -> None:
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(201, json={"id": "unexpected"})

    client = httpx.Client(transport=httpx.MockTransport(respond))
    adapter = DifyReferenceFileUploadClient(
        api_base_url="http://dify-api:5001",
        app_id="isolated-app",
        app_api_token="isolated-app-secret-not-logged",
        client=client,
    )
    result = adapter.upload_reference(_command(app_id="different-app"))

    assert result.outcome == "definitely_rejected"
    assert result.safe_reason_code == "DIFY_APP_ID_MISMATCH"
    assert calls == 0
    client.close()


def test_upload_client_keeps_ambiguous_http_failure_unknown() -> None:
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, json={"error": "internal"})

    client = httpx.Client(transport=httpx.MockTransport(respond))
    adapter = DifyReferenceFileUploadClient(
        api_base_url="http://dify-api:5001",
        app_id="isolated-app",
        app_api_token="isolated-app-secret-not-logged",
        client=client,
    )
    result = adapter.upload_reference(_command())

    assert result.outcome == "unknown"
    assert result.dify_file_id is None
    assert calls == 1
    client.close()
