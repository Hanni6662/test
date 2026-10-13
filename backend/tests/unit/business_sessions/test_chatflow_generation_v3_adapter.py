from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from app.services.chatflow_generation_actions import (
    GENERATION_RECOVERY_ACTION,
    HttpGenerationStatusPort,
    ImageGenerationReadError,
    generation_action_digest,
    prepare_generation_recovery,
)
from app.services.chatflow_generation_contracts import (
    IMAGE_GENERATION_CONTRACT_V2,
    IMAGE_GENERATION_CONTRACT_V3,
    image_generation_contract_for_flow_version,
)
from app.services.chatflow_generation_delivery_actions import (
    DeliveryPortError,
    HttpImageGenerationDeliveryPort,
    prepare_generation_delivery,
)
from app.services.chatflow_generation_results import (
    DELIVERY_DOWNLOAD_ACTION,
)

BASE_URL = "https://image-generation.test"
BEARER_TOKEN = "test-bearer-token"
GENERATION_REQUEST_ID = UUID("00000000-0000-4000-8000-000000000123")


def _mock_status_port(
    handler: Callable[[httpx.Request], httpx.Response],
) -> HttpGenerationStatusPort:
    port = HttpGenerationStatusPort(BASE_URL, BEARER_TOKEN)
    assert port._client.follow_redirects is False
    port._client.close()
    port._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
        headers={"Authorization": f"Bearer {BEARER_TOKEN}"},
    )
    return port


def _mock_delivery_port(
    handler: Callable[[httpx.Request], httpx.Response],
) -> HttpImageGenerationDeliveryPort:
    port = HttpImageGenerationDeliveryPort(BASE_URL, BEARER_TOKEN)
    assert port._client.follow_redirects is False
    port._client.close()
    port._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
        headers={
            "Authorization": f"Bearer {BEARER_TOKEN}",
            "Accept-Encoding": "identity",
        },
    )
    return port


def test_flow_version_selects_only_the_fixed_v2_or_v3_contract() -> None:
    assert IMAGE_GENERATION_CONTRACT_V2 == "chatflow-image-generation-v2"
    assert IMAGE_GENERATION_CONTRACT_V3 == "chatflow-image-generation-v3"
    assert image_generation_contract_for_flow_version("legacy") == IMAGE_GENERATION_CONTRACT_V2
    assert (
        image_generation_contract_for_flow_version("visual-v3.4-single-selection")
        == IMAGE_GENERATION_CONTRACT_V3
    )

    for unsupported_flow_version in ("", "visual-v3.3", "unknown"):
        with pytest.raises(ValueError):
            image_generation_contract_for_flow_version(unsupported_flow_version)


class _SingleResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value

    def scalars(self):
        return self

    def all(self) -> list[object]:
        return self.value


class _PreparationDb:
    def __init__(self, *results: object, business_session: object) -> None:
        self.results = list(results)
        self.business_session = business_session

    def execute(self, _statement: object) -> _SingleResult:
        return _SingleResult(self.results.pop(0))

    def get(self, _model: object, _identity: object) -> object:
        return self.business_session


@pytest.mark.parametrize(
    ("flow_version", "expected_contract"),
    [
        ("legacy", IMAGE_GENERATION_CONTRACT_V2),
        ("visual-v3.4-single-selection", IMAGE_GENERATION_CONTRACT_V3),
    ],
)
def test_persisted_flow_version_selects_h6_recovery_contract(
    flow_version: str, expected_contract: str
) -> None:
    request_id, task_id, session_id = UUID(int=1), UUID(int=2), UUID(int=3)
    request = SimpleNamespace(
        id=request_id,
        task_id=task_id,
        business_session_id=session_id,
        dify_run_id="run-flow-contract",
        package_digest=b"a" * 32,
    )
    tracking = SimpleNamespace(
        reconcile_state="pending", reconcile_query_count=0, batch_id="batch-flow-contract"
    )
    permit = SimpleNamespace(slot_mapping_digest=b"b" * 32, reference_manifest_digest=b"c" * 32)
    slots = [
        SimpleNamespace(
            slot_id="slot_01",
            task_item_id=UUID(int=4),
            attempt_id=UUID(int=5),
            position=1,
            image_type="品牌导购主视觉",
        )
    ]
    action = SimpleNamespace(
        id=UUID(int=6),
        action_kind=GENERATION_RECOVERY_ACTION,
        scope_kind="generation_request",
        scope_id=request_id,
        generation_request_id=request_id,
        task_id=task_id,
        business_session_id=session_id,
        sequence_namespace="default",
        sequence=1,
        input_digest_version=1,
        input_digest=generation_action_digest(
            generation_request_id=request_id,
            task_id=task_id,
            session_id=session_id,
            sequence=1,
        ),
    )
    db = _PreparationDb(
        request,
        tracking,
        permit,
        slots,
        business_session=SimpleNamespace(flow_version=flow_version),
    )

    action_input = prepare_generation_recovery(db, action)

    assert action_input.payload["image_generation_contract_version"] == expected_contract
    assert action_input.payload["generation_request_id"] == str(request_id)
    assert action_input.payload["batch_id"] == "batch-flow-contract"


@pytest.mark.parametrize(
    ("flow_version", "expected_contract"),
    [
        ("legacy", IMAGE_GENERATION_CONTRACT_V2),
        ("visual-v3.4-single-selection", IMAGE_GENERATION_CONTRACT_V3),
    ],
)
def test_persisted_flow_version_selects_h7_download_and_ack_contract(
    flow_version: str, expected_contract: str
) -> None:
    request_id, task_id, session_id = UUID(int=11), UUID(int=12), UUID(int=13)
    item_id, attempt_id, delivery_id = UUID(int=14), UUID(int=15), UUID(int=16)
    request = SimpleNamespace(
        id=request_id,
        task_id=task_id,
        business_session_id=session_id,
    )
    delivery = SimpleNamespace(
        id=delivery_id,
        generation_request_id=request_id,
        task_id=task_id,
        task_item_id=item_id,
        attempt_id=attempt_id,
        download_action_sequence=1,
        confirmation_action_sequence=2,
        status="pending_download",
        slot_id="slot_01",
        batch_id="batch-flow-contract",
        source_file_id="slot_01-original-1.png",
        source_sha256=b"d" * 32,
        source_mime_type="image/png",
        source_size_bytes=128,
        download_attempt_count=0,
        confirmation_attempt_count=0,
        original_object_key=None,
        preview_object_key=None,
    )
    action = SimpleNamespace(
        id=UUID(int=17),
        action_kind=DELIVERY_DOWNLOAD_ACTION,
        scope_kind="generation_delivery",
        scope_id=delivery_id,
        generation_delivery_id=delivery_id,
        generation_request_id=request_id,
        task_id=task_id,
        business_session_id=session_id,
        task_item_id=item_id,
        attempt_id=attempt_id,
        sequence=1,
        input_digest_version=1,
        input_digest=b"e" * 32,
    )
    db = _PreparationDb(
        delivery,
        request,
        business_session=SimpleNamespace(flow_version=flow_version),
    )

    action_input = prepare_generation_delivery(db, action)

    assert action_input.payload["image_generation_contract_version"] == expected_contract
    assert action_input.payload["generation_request_id"] == str(request_id)
    assert action_input.payload["file_id"] == "slot_01-original-1.png"


def test_status_adapter_keeps_legacy_direct_callers_on_v2_paths() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"result": "ok"})

    port = _mock_status_port(handle)
    try:
        assert port.get_by_request(GENERATION_REQUEST_ID) == {"result": "ok"}
        assert port.get_by_batch("batch-legacy") == {"result": "ok"}
    finally:
        port._client.close()

    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", f"/v2/generation-requests/{GENERATION_REQUEST_ID}"),
        ("GET", "/v2/generation-batches/batch-legacy"),
    ]
    assert all(request.headers["Authorization"] == f"Bearer {BEARER_TOKEN}" for request in requests)


def test_status_adapter_dispatches_v3_request_and_batch_reads_without_fallback() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"contract": "v3"})

    port = _mock_status_port(handle)
    try:
        assert port.get_by_request(
            GENERATION_REQUEST_ID, contract_version=IMAGE_GENERATION_CONTRACT_V3
        ) == {"contract": "v3"}
        assert port.get_by_batch("batch-v3", contract_version=IMAGE_GENERATION_CONTRACT_V3) == {
            "contract": "v3"
        }
    finally:
        port._client.close()

    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", f"/v3/generation-requests/{GENERATION_REQUEST_ID}"),
        ("GET", "/v3/generation-batches/batch-v3"),
    ]


def test_delivery_adapter_dispatches_v3_artifact_and_exact_ack_path() -> None:
    requests: list[httpx.Request] = []
    ack_payload = {"source_sequence": "17", "delivery_id": "delivery-17"}

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                content=b"fixture-image-bytes",
                headers={"content-type": "image/png"},
            )
        return httpx.Response(200, json={"status": "confirmed"})

    port = _mock_delivery_port(handle)
    try:
        image, mime_type = port.get_artifact(
            "batch /v3",
            "slot/blue",
            "file?red",
            contract_version=IMAGE_GENERATION_CONTRACT_V3,
        )
        port.confirm_delivery(
            "batch /v3",
            "slot/blue",
            ack_payload,
            contract_version=IMAGE_GENERATION_CONTRACT_V3,
        )
    finally:
        port.close()

    assert image == b"fixture-image-bytes"
    assert mime_type == "image/png"
    assert [(request.method, request.url.raw_path) for request in requests] == [
        (
            "GET",
            b"/v3/generation-batches/batch%20%2Fv3/slots/slot%2Fblue/artifacts/file%3Fred",
        ),
        (
            "POST",
            b"/v3/generation-batches/batch%20%2Fv3/slots/slot%2Fblue/delivery-confirmations",
        ),
    ]
    assert json.loads(requests[1].content) == ack_payload
    assert all(request.headers["Authorization"] == f"Bearer {BEARER_TOKEN}" for request in requests)


def test_v3_adapters_do_not_follow_redirects_or_probe_another_version() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"location": "/v2/fallback"})

    status_port = _mock_status_port(handle)
    delivery_port = _mock_delivery_port(handle)
    try:
        with pytest.raises(ImageGenerationReadError) as status_error:
            status_port.get_by_request(
                GENERATION_REQUEST_ID, contract_version=IMAGE_GENERATION_CONTRACT_V3
            )
        assert status_error.value.code == "IMAGE_SERVICE_READ_REJECTED"

        with pytest.raises(DeliveryPortError) as artifact_error:
            delivery_port.get_artifact(
                "batch-v3",
                "slot-v3",
                "file-v3",
                contract_version=IMAGE_GENERATION_CONTRACT_V3,
            )
        assert artifact_error.value.code == "IMAGE_SOURCE_REJECTED"

        with pytest.raises(DeliveryPortError) as ack_error:
            delivery_port.confirm_delivery(
                "batch-v3",
                "slot-v3",
                {"delivery_id": "delivery-v3"},
                contract_version=IMAGE_GENERATION_CONTRACT_V3,
            )
        assert ack_error.value.code == "IMAGE_DELIVERY_ACK_REJECTED"
    finally:
        status_port._client.close()
        delivery_port.close()

    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", f"/v3/generation-requests/{GENERATION_REQUEST_ID}"),
        ("GET", "/v3/generation-batches/batch-v3/slots/slot-v3/artifacts/file-v3"),
        ("POST", "/v3/generation-batches/batch-v3/slots/slot-v3/delivery-confirmations"),
    ]


def test_delivery_adapter_keeps_legacy_direct_callers_on_v2_paths() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=(b"fixture-v2-image" if request.method == "GET" else b'{"status":"confirmed"}'),
            headers={
                "content-type": ("image/jpeg" if request.method == "GET" else "application/json")
            },
        )

    port = _mock_delivery_port(handle)
    try:
        image, mime_type = port.get_artifact("batch-v2", "slot-2", "file-2")
        port.confirm_delivery("batch-v2", "slot-2", {"delivery_id": "legacy"})
    finally:
        port.close()

    assert (image, mime_type) == (b"fixture-v2-image", "image/jpeg")
    assert [(request.method, request.url.path) for request in requests] == [
        (
            "GET",
            "/v2/generation-batches/batch-v2/slots/slot-2/artifacts/file-2",
        ),
        (
            "POST",
            "/v2/generation-batches/batch-v2/slots/slot-2/delivery-confirmations",
        ),
    ]


@pytest.mark.parametrize("ack_status", ["confirmed", "replayed"])
def test_delivery_ack_accepts_only_confirmed_or_replayed_json_status(ack_status: str) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": ack_status})

    port = _mock_delivery_port(handle)
    try:
        port.confirm_delivery(
            "batch-v3",
            "slot-v3",
            {"delivery_id": "delivery-v3"},
            contract_version=IMAGE_GENERATION_CONTRACT_V3,
        )
    finally:
        port.close()

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == (
        "/v3/generation-batches/batch-v3/slots/slot-v3/delivery-confirmations"
    )
    assert json.loads(requests[0].content) == {"delivery_id": "delivery-v3"}


@pytest.mark.parametrize(
    ("status_code", "body", "content_type"),
    [
        (204, b"", "application/json"),
        (200, b"", "application/json"),
        (200, b'{"status":"accepted"}', "application/json"),
        (200, b'{"status":"confirmed","extra":"not-allowed"}', "application/json"),
        (200, b'["confirmed"]', "application/json"),
        (200, b"{not-json", "application/json"),
    ],
    ids=[
        "no-content",
        "empty-body",
        "unknown-status",
        "extra-field",
        "non-object-json",
        "invalid-json",
    ],
)
def test_delivery_ack_rejects_responses_without_an_exact_terminal_json_status(
    status_code: int,
    body: bytes,
    content_type: str,
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status_code,
            content=body,
            headers={"content-type": content_type},
        )

    port = _mock_delivery_port(handle)
    try:
        with pytest.raises(DeliveryPortError):
            port.confirm_delivery(
                "batch-v3",
                "slot-v3",
                {"delivery_id": "delivery-v3"},
                contract_version=IMAGE_GENERATION_CONTRACT_V3,
            )
    finally:
        port.close()

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == (
        "/v3/generation-batches/batch-v3/slots/slot-v3/delivery-confirmations"
    )


def test_unsupported_contract_versions_fail_closed_without_http_requests() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    status_port = _mock_status_port(handle)
    delivery_port = _mock_delivery_port(handle)
    try:
        with pytest.raises(ImageGenerationReadError) as request_error:
            status_port.get_by_request(GENERATION_REQUEST_ID, contract_version="v9")
        assert request_error.value.code == "IMAGE_SERVICE_CONTRACT_UNSUPPORTED"

        with pytest.raises(ImageGenerationReadError) as batch_error:
            status_port.get_by_batch("batch", contract_version="v9")
        assert batch_error.value.code == "IMAGE_SERVICE_CONTRACT_UNSUPPORTED"

        with pytest.raises(DeliveryPortError) as artifact_error:
            delivery_port.get_artifact("batch", "slot", "file", contract_version="v9")
        assert artifact_error.value.code == "IMAGE_SERVICE_CONTRACT_UNSUPPORTED"
        assert artifact_error.value.retryable is False

        with pytest.raises(DeliveryPortError) as ack_error:
            delivery_port.confirm_delivery("batch", "slot", {}, contract_version="v9")
        assert ack_error.value.code == "IMAGE_SERVICE_CONTRACT_UNSUPPORTED"
        assert ack_error.value.retryable is False
    finally:
        status_port._client.close()
        delivery_port.close()

    assert requests == []
