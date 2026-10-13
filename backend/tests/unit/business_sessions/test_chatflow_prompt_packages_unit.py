from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from app.core.config import Settings
from app.schemas.chatflow_prompt_packages import ChatflowPromptPackageSubmission
from app.schemas.prompt_packages import PromptPackage
from app.schemas.prompt_workflow import canonical_sha256
from app.services.chatflow_prompt_packages import _sign_permit
from app.services.prompt_validation import IMAGE_TYPES
from pydantic import SecretStr, ValidationError
from tests.unit.prompt_packages.test_prompt_validation import _package

TEST_DATABASE_URL = "postgresql+psycopg://test-user:test-password@127.0.0.1:1/image_backend"
CALLBACK_SECRET = "independent-callback-secret-value-1234567890"
PERMIT_SECRET = "separate-generation-permit-secret-123456789"
MAINTENANCE_SECRET = "separate-maintenance-secret-value-1234567890"


def _submission_payload() -> dict[str, object]:
    package, _references, _relations = _package(
        [IMAGE_TYPES[0]], platform="TEMU", auxiliary_count=1
    )
    package_value = PromptPackage.model_validate(package, strict=True)
    return {
        "contract_version": "chatflow-handoff-v1",
        "business_session_id": uuid4(),
        "recommendation_id": uuid4(),
        "recommendation_revision": 1,
        "dify_run_id": "stub-run-unit",
        "task_id": uuid4(),
        "submission_id": uuid4(),
        "package_version": "chatflow-prompt-package-v1",
        "package_digest_version": 1,
        "package_digest": canonical_sha256(package_value.model_dump(mode="json")).hex(),
        "image_types": [IMAGE_TYPES[0]],
        "references": [
            {
                "ref_id": "PRIMARY",
                "asset_id": uuid4(),
                "order": 1,
                "content_sha256": "a" * 64,
            },
            {
                "ref_id": "AUX01",
                "asset_id": uuid4(),
                "order": 2,
                "content_sha256": "b" * 64,
            },
        ],
        "prompt_package": package_value,
    }


def test_prompt_package_submission_requires_ordered_unique_references_and_types() -> None:
    payload = _submission_payload()
    accepted = ChatflowPromptPackageSubmission.model_validate(payload, strict=True)
    assert [reference.ref_id for reference in accepted.references] == ["PRIMARY", "AUX01"]
    assert "product" not in repr(accepted)
    assert accepted.prompt_package.basis.product not in repr(accepted)

    reversed_references = dict(payload)
    reversed_references["references"] = list(reversed(payload["references"]))
    with pytest.raises(ValidationError):
        ChatflowPromptPackageSubmission.model_validate(reversed_references, strict=True)

    duplicate_types = dict(payload)
    duplicate_types["image_types"] = [IMAGE_TYPES[0], IMAGE_TYPES[0]]
    with pytest.raises(ValidationError):
        ChatflowPromptPackageSubmission.model_validate(duplicate_types, strict=True)


def test_permit_signature_is_stable_and_binds_every_required_claim() -> None:
    secret = b"separate-generation-permit-secret-123456"
    permit_id = uuid4()
    request_id = uuid4()
    task_id = uuid4()
    package_digest = hashlib.sha256(b"package").digest()
    slot_mapping_digest = hashlib.sha256(b"slots").digest()
    reference_manifest_digest = hashlib.sha256(b"references").digest()
    expires_at = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)

    first = _sign_permit(
        secret,
        permit_id=permit_id,
        generation_request_id=request_id,
        task_id=task_id,
        package_digest=package_digest,
        slot_mapping_digest=slot_mapping_digest,
        reference_manifest_digest=reference_manifest_digest,
        expires_at=expires_at,
    )
    second = _sign_permit(
        secret,
        permit_id=permit_id,
        generation_request_id=request_id,
        task_id=task_id,
        package_digest=package_digest,
        slot_mapping_digest=slot_mapping_digest,
        reference_manifest_digest=reference_manifest_digest,
        expires_at=expires_at,
    )
    assert first == second

    prefix, encoded, signature = first.split(".")
    assert prefix == "h4p2"
    encoded_bytes = encoded.encode("ascii")
    payload_bytes = base64.urlsafe_b64decode(encoded_bytes + b"=" * (-len(encoded_bytes) % 4))
    signature_bytes = base64.urlsafe_b64decode(
        signature.encode("ascii") + b"=" * (-len(signature) % 4)
    )
    assert hmac.compare_digest(
        signature_bytes,
        hmac.digest(secret, b"chatflow-generation-permit-v2." + encoded_bytes, "sha256"),
    )
    claims = json.loads(payload_bytes)
    assert claims == {
        "contract_version": "chatflow-generation-permit-v2",
        "expires_at": expires_at.isoformat(),
        "generation_request_id": str(request_id),
        "package_digest": package_digest.hex(),
        "permit_id": str(permit_id),
        "reference_manifest_digest": reference_manifest_digest.hex(),
        "slot_mapping_digest": slot_mapping_digest.hex(),
        "task_id": str(task_id),
        "token_version": 2,
    }


def test_generation_permit_secret_is_optional_bounded_and_independent() -> None:
    assert Settings(database_url=TEST_DATABASE_URL).generation_permit_secret is None
    with pytest.raises(ValidationError):
        Settings(database_url=TEST_DATABASE_URL, generation_permit_secret="short")
    with pytest.raises(ValidationError):
        Settings(
            database_url=TEST_DATABASE_URL,
            chatflow_handoff_enabled=True,
            dify_callback_secret=CALLBACK_SECRET,
            generation_permit_secret=CALLBACK_SECRET,
        )
    with pytest.raises(ValidationError):
        Settings(
            database_url=TEST_DATABASE_URL,
            chatflow_handoff_enabled=True,
            dify_callback_secret=CALLBACK_SECRET,
            generation_permit_secret=PERMIT_SECRET,
            maintenance_secret=PERMIT_SECRET,
        )

    enabled = Settings(
        database_url=TEST_DATABASE_URL,
        chatflow_handoff_enabled=True,
        dify_callback_secret=CALLBACK_SECRET,
        generation_permit_secret=SecretStr(PERMIT_SECRET),
        maintenance_secret=MAINTENANCE_SECRET,
    )
    assert enabled.generation_permit_secret is not None
    assert PERMIT_SECRET not in repr(enabled.generation_permit_secret)
