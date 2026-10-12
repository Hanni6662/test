from __future__ import annotations

import hashlib
from io import BytesIO
from types import SimpleNamespace
from uuid import UUID, uuid4

from app.integrations.generation.protocol import (
    GenerationAccepted,
    GenerationCommand,
    GenerationQueryPending,
    GenerationQuerySucceeded,
    GenerationRecoveryDefinitelyNotFound,
    GenerationRecoveryUnsupported,
    GenerationUnknown,
)
from app.integrations.storage.protocol import ObjectStat
from app.services.generation_runtime import GenerationActionRuntime, _valid_prompt_item
from app.services.prompt_validation import PROMPT_PACKAGE_VALIDATOR_VERSION
from app.workers.action_processor import ActionInput, ActionRegistration
from PIL import Image


class _Port:
    def __init__(self) -> None:
        self.submit_result: object = GenerationAccepted(
            "accepted", "mock-job-1", poll_after_seconds=2
        )
        self.query_result: object = GenerationQueryPending(
            "pending", "mock-job-1", poll_after_seconds=2
        )
        self.recovery_result: object = GenerationRecoveryDefinitelyNotFound(
            "definitely_not_found", uuid4()
        )
        self.download_bytes = _png_bytes()
        self.submit_calls: list[GenerationCommand] = []
        self.query_calls: list[str] = []
        self.recover_calls: list[UUID] = []
        self.download_calls: list[str] = []

    def submit(self, command: GenerationCommand):
        self.submit_calls.append(command)
        return self.submit_result

    def query(self, provider_job_id: str):
        self.query_calls.append(provider_job_id)
        return self.query_result

    def recover(self, request_key: UUID):
        self.recover_calls.append(request_key)
        return self.recovery_result

    def download(self, result_ref: str) -> bytes:
        self.download_calls.append(result_ref)
        return self.download_bytes


class _Storage:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, dict[str, str | int]]] = {}

    def put_immutable(self, key, stream, metadata):
        content = stream.read()
        digest = hashlib.sha256(content).hexdigest()
        existing = self.objects.get(key)
        if existing is not None:
            assert existing == (content, dict(metadata))
        else:
            self.objects[key] = (content, dict(metadata))
        return ObjectStat.create(
            key=key,
            size_bytes=len(content),
            sha256=digest,
            metadata=metadata,
        )

    def open_reader(self, key):
        return BytesIO(self.objects[key][0])

    def stat(self, key):
        value = self.objects.get(key)
        if value is None:
            return None
        content, metadata = value
        return ObjectStat.create(
            key=key,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            metadata=metadata,
        )

    def delete(self, key):
        self.objects.pop(key, None)


class _ShortReadStorage(_Storage):
    def open_reader(self, key):
        source = super().open_reader(key)

        class _Reader:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                source.close()

            def read(self, size=-1):
                return source.read(7 if size < 0 else min(size, 7))

        return _Reader()


def _png_bytes(width: int = 24, height: int = 16) -> bytes:
    image = Image.new("RGB", (width, height), (20, 80, 160))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _input(kind: str, payload: dict[str, object]) -> ActionInput:
    action_id = uuid4()
    attempt_id = uuid4()
    return ActionInput(
        action_id=action_id,
        task_id=uuid4(),
        scope_kind="attempt",
        scope_id=attempt_id,
        action_kind=kind,
        sequence=1,
        input_digest_version=1,
        input_digest=hashlib.sha256(kind.encode("ascii")).digest(),
        payload=payload,
        task_item_id=uuid4(),
        attempt_id=attempt_id,
    )


def test_registration_has_only_generation_attempt_scoped_steps() -> None:
    runtime = GenerationActionRuntime(_Port(), _Storage())

    registrations = runtime.registrations()

    assert set(registrations) == {
        "generation.submit.v1",
        "generation.recover.v1",
        "generation.query.v1",
        "generation.download.v1",
        "generation.save.v1",
    }
    assert all(isinstance(value, ActionRegistration) for value in registrations.values())
    assert all(value.scope_kinds == frozenset({"attempt"}) for value in registrations.values())


def test_full_capacity_schedules_retry_without_calling_generation_port() -> None:
    port = _Port()
    runtime = GenerationActionRuntime(port, _Storage())

    result = runtime.execute(
        _input("generation.submit.v1", {"capacity_granted": False})
    )

    assert result.outcome == "safe_retry"
    assert result.safe_reason_code == "GENERATION_CAPACITY_FULL"
    assert result.retry_after_seconds is not None
    assert port.submit_calls == []


def test_submit_uses_only_persisted_allowed_reference_ids_once() -> None:
    port = _Port()
    runtime = GenerationActionRuntime(port, _Storage())
    request_key = uuid4()
    primary_id, allowed_aux_id = uuid4(), uuid4()
    result = runtime.execute(
        _input(
            "generation.submit.v1",
            {
                "capacity_granted": True,
                "request_key": str(request_key),
                "image_type": "front_view",
                "prompt_package_version": "v3.3",
                "prompt_text": "trusted design\n\ntrusted identity",
                "reference_asset_ids": (str(primary_id), str(allowed_aux_id)),
            },
        )
    )

    assert result.outcome == "accepted"
    assert result.value["kind"] == "job_accepted"
    assert len(port.submit_calls) == 1
    command = port.submit_calls[0]
    assert command.request_key == request_key
    assert command.reference_asset_ids == (primary_id, allowed_aux_id)
    assert "excluded" not in command.prompt_text.lower()


def _bound_prompt_fixture():
    task_id, item_id, package_id = uuid4(), uuid4(), uuid4()
    asset_ids = (uuid4(), uuid4(), uuid4())
    aliases = ("PRIMARY", "AUX01", "AUX02")
    task = SimpleNamespace(
        id=task_id,
        reference_assets_snapshot=[
            {"position": index, "slot": alias, "model_asset_id": str(asset_id)}
            for index, (alias, asset_id) in enumerate(zip(aliases, asset_ids, strict=True))
        ],
    )
    item = SimpleNamespace(id=item_id, task_id=task_id, image_type="品牌导购主视觉", position=1)
    package = SimpleNamespace(
        id=package_id,
        task_id=task_id,
        validator_version=PROMPT_PACKAGE_VALIDATOR_VERSION,
    )
    prompt_item = SimpleNamespace(
        package_id=package_id,
        task_id=task_id,
        task_item_id=item_id,
        image_type=item.image_type,
        position=item.position,
        design_prompt="trusted design",
        identity_prompt="trusted identity",
        identity_anchor_asset_id=asset_ids[0],
        allowed_reference_aliases=list(aliases),
        excluded_reference_aliases=[],
        allowed_reference_asset_ids=[str(asset_id) for asset_id in asset_ids],
    )
    return task, item, package, prompt_item, asset_ids


def test_generation_runtime_rejects_partial_foreign_or_cross_task_references() -> None:
    task, item, package, prompt_item, asset_ids = _bound_prompt_fixture()
    assert _valid_prompt_item(prompt_item, item, task, package)

    prompt_item.allowed_reference_aliases = ["PRIMARY"]
    prompt_item.allowed_reference_asset_ids = [str(asset_ids[0])]
    assert not _valid_prompt_item(prompt_item, item, task, package)

    prompt_item.allowed_reference_aliases = ["PRIMARY", "AUX01", "AUX02"]
    prompt_item.excluded_reference_aliases = []
    prompt_item.allowed_reference_asset_ids = [str(asset_ids[0]), str(asset_ids[1]), str(uuid4())]
    assert not _valid_prompt_item(prompt_item, item, task, package)

    prompt_item.allowed_reference_asset_ids = [str(asset_id) for asset_id in asset_ids]
    prompt_item.task_id = uuid4()
    assert not _valid_prompt_item(prompt_item, item, task, package)

    prompt_item.task_id = task.id
    package.validator_version = "unknown-validator-v9"
    assert not _valid_prompt_item(prompt_item, item, task, package)


def test_unknown_submit_stays_unknown_without_blind_retry() -> None:
    port = _Port()
    port.submit_result = GenerationUnknown(
        "unknown", "MOCK_ACCEPTED_RESPONSE_LOST", reconcile_after_seconds=17
    )
    runtime = GenerationActionRuntime(port, _Storage())
    request_key = uuid4()
    result = runtime.execute(
        _input(
            "generation.submit.v1",
            {
                "capacity_granted": True,
                "request_key": str(request_key),
                "image_type": "front_view",
                "prompt_package_version": "v3.3",
                "prompt_text": "prompt",
                "reference_asset_ids": (str(uuid4()),),
            },
        )
    )
    assert result.outcome == "unknown"
    assert result.safe_reason_code == "MOCK_ACCEPTED_RESPONSE_LOST"
    assert result.value["kind"] == "submit_unknown"
    assert len(port.submit_calls) == 1


def test_confirmed_missing_retries_same_key_but_unsupported_recovery_stays_unknown() -> None:
    port = _Port()
    key = uuid4()
    runtime = GenerationActionRuntime(port, _Storage())
    port.recovery_result = GenerationRecoveryDefinitelyNotFound("definitely_not_found", key)

    retry = runtime.execute(_input("generation.recover.v1", {"request_key": str(key)}))

    assert retry.outcome == "safe_retry"
    assert retry.safe_reason_code == "GENERATION_NOT_FOUND_CONFIRMED"
    assert port.recover_calls == [key]

    port.recovery_result = GenerationRecoveryUnsupported("unsupported", key)
    unsupported = runtime.execute(
        _input("generation.recover.v1", {"request_key": str(key)})
    )
    assert unsupported.outcome == "unknown"
    assert unsupported.safe_reason_code == "RECOVERY_UNSUPPORTED"
    assert port.recover_calls == [key, key]


def test_query_pending_and_succeeded_are_distinct_single_calls() -> None:
    port = _Port()
    runtime = GenerationActionRuntime(port, _Storage())

    pending = runtime.execute(
        _input("generation.query.v1", {"provider_job_id": "mock-job-1"})
    )
    assert pending.outcome == "accepted"
    assert pending.value["kind"] == "job_pending"

    raw = _png_bytes()
    port.query_result = GenerationQuerySucceeded(
        "succeeded",
        "mock-job-1",
        "mock-result:opaque",
        hashlib.sha256(raw).digest(),
    )
    succeeded = runtime.execute(
        _input("generation.query.v1", {"provider_job_id": "mock-job-1"})
    )
    assert succeeded.outcome == "accepted"
    assert succeeded.value["kind"] == "job_succeeded"
    assert succeeded.value["result_digest"] == hashlib.sha256(raw).digest()
    assert port.query_calls == ["mock-job-1", "mock-job-1"]


def test_download_checks_digest_and_writes_only_the_deterministic_private_key() -> None:
    raw = _png_bytes()
    storage = _Storage()
    port = _Port()
    port.download_bytes = raw
    runtime = GenerationActionRuntime(port, storage)
    key = "generated/9f8a2ef1-392b-4bd5-9977-38e9acbcf5c5/original"
    result = runtime.execute(
        _input(
            "generation.download.v1",
            {
                "result_ref": "mock-result:opaque",
                "result_digest": hashlib.sha256(raw).digest(),
                "original_object_key": key,
            },
        )
    )
    assert result.outcome == "accepted"
    assert result.value["kind"] == "original_stored"
    assert storage.objects[key][0] == raw
    assert storage.objects[key][1]["asset_kind"] == "generation_original"
    assert len(port.download_calls) == 1

    bad = runtime.execute(
        _input(
            "generation.download.v1",
            {
                "result_ref": "mock-result:opaque",
                "result_digest": b"x" * 32,
                "original_object_key": "generated/another/original",
            },
        )
    )
    assert bad.outcome == "definitely_rejected"
    assert bad.safe_reason_code == "GENERATION_RESULT_DIGEST_MISMATCH"
    assert "generated/another/original" not in storage.objects


def test_save_renders_bounded_webp_and_reuses_immutable_key() -> None:
    raw = _png_bytes(1800, 900)
    storage = _Storage()
    original_key = "generated/4dc4e91f-f640-4a0b-970f-493d68ee242d/original"
    preview_key = "generated/4dc4e91f-f640-4a0b-970f-493d68ee242d/preview.webp"
    storage.put_immutable(
        original_key,
        BytesIO(raw),
        {"asset_kind": "generation_original", "sha256": hashlib.sha256(raw).hexdigest()},
    )
    runtime = GenerationActionRuntime(_Port(), storage)
    action_input = _input(
        "generation.save.v1",
        {
            "original_object_key": original_key,
            "preview_object_key": preview_key,
            "original_sha256": hashlib.sha256(raw).hexdigest(),
        },
    )

    result = runtime.execute(action_input)
    before_recovery = storage.objects[preview_key]
    replay = runtime.recover(action_input)

    assert result.outcome == "accepted"
    assert replay.outcome == "accepted"
    assert result.value["kind"] == "preview_stored"
    assert result.value["preview_width"] == 1024
    assert result.value["preview_height"] == 512
    stored_preview = storage.objects[preview_key][0]
    assert Image.open(BytesIO(stored_preview)).format == "WEBP"
    assert storage.objects[preview_key] == before_recovery


def test_save_reads_short_binary_streams_until_eof() -> None:
    raw = _png_bytes(32, 24)
    storage = _ShortReadStorage()
    original_key = "generated/short-read/original"
    preview_key = "generated/short-read/preview.webp"
    storage.put_immutable(
        original_key,
        BytesIO(raw),
        {"asset_kind": "generation_original", "sha256": hashlib.sha256(raw).hexdigest()},
    )
    runtime = GenerationActionRuntime(_Port(), storage)

    result = runtime.execute(
        _input(
            "generation.save.v1",
            {
                "original_object_key": original_key,
                "preview_object_key": preview_key,
                "original_sha256": hashlib.sha256(raw).hexdigest(),
            },
        )
    )

    assert result.outcome == "accepted"
    assert preview_key in storage.objects


def test_save_with_missing_original_remains_recoverable_unknown() -> None:
    runtime = GenerationActionRuntime(_Port(), _Storage())
    result = runtime.execute(
        _input(
            "generation.save.v1",
            {
                "original_object_key": "generated/missing/original",
                "preview_object_key": "generated/missing/preview.webp",
                "original_sha256": "0" * 64,
            },
        )
    )
    assert result.outcome == "unknown"
    assert result.safe_reason_code == "GENERATION_ORIGINAL_READ_FAILED"
