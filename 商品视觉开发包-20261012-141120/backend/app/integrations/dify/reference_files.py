from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit

import httpx
from app.integrations.dify.chatflow import (
    DifyReferenceUploadCommand,
    DifyReferenceUploadResult,
)

_FILE_ID = re.compile(r"^[A-Za-z0-9_-]{1,255}$")
_MIME_EXTENSION = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


class DifyReferenceFileUploadClient:
    """Upload one normalized reference to a fixed Dify App Service API origin."""

    def __init__(
        self,
        *,
        api_base_url: str,
        app_id: str,
        app_api_token: str,
        timeout_seconds: float = 15.0,
        client: httpx.Client | None = None,
    ) -> None:
        parsed = urlsplit(api_base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("Dify API base must be a fixed HTTP origin")
        if not app_id or len(app_id) > 160 or not app_api_token:
            raise ValueError("Dify App identity and API token are required")
        self._api_base_url = api_base_url.rstrip("/")
        self._app_id = app_id
        self._app_api_token = app_api_token
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 5.0)),
            follow_redirects=False,
            trust_env=False,
        )
        self._owns_client = client is None

    def upload_reference(
        self, command: DifyReferenceUploadCommand
    ) -> DifyReferenceUploadResult:
        if command.dify_app_id != self._app_id:
            return DifyReferenceUploadResult(
                "definitely_rejected", safe_reason_code="DIFY_APP_ID_MISMATCH"
            )
        expected_extension = _MIME_EXTENSION.get(command.mime_type)
        if (
            expected_extension is None
            or not command.content
            or len(command.content) != command.size_bytes
            or hashlib.sha256(command.content).hexdigest() != command.content_sha256
        ):
            return DifyReferenceUploadResult(
                "definitely_rejected", safe_reason_code="DIFY_REFERENCE_BYTES_INVALID"
            )
        try:
            response = self._client.post(
                f"{self._api_base_url}/v1/files/upload",
                headers={"Authorization": f"Bearer {self._app_api_token}"},
                data={"user": command.dify_user_id},
                files={
                    "file": (
                        f"{command.slot.lower()}.{expected_extension}",
                        command.content,
                        command.mime_type,
                    )
                },
            )
        except httpx.HTTPError:
            return DifyReferenceUploadResult(
                "unknown", safe_reason_code="DIFY_REFERENCE_UPLOAD_OUTCOME_UNKNOWN"
            )
        if response.status_code not in {200, 201}:
            if response.status_code in {400, 401, 403, 413, 415, 422}:
                return DifyReferenceUploadResult(
                    "definitely_rejected", safe_reason_code="DIFY_REFERENCE_UPLOAD_REJECTED"
                )
            return DifyReferenceUploadResult(
                "unknown", safe_reason_code="DIFY_REFERENCE_UPLOAD_OUTCOME_UNKNOWN"
            )
        try:
            document = response.json()
        except (ValueError, TypeError):
            document = None
        file_id = document.get("id") if isinstance(document, dict) else None
        if not isinstance(file_id, str) or _FILE_ID.fullmatch(file_id) is None:
            return DifyReferenceUploadResult(
                "unknown", safe_reason_code="DIFY_REFERENCE_UPLOAD_RESULT_INVALID"
            )
        return DifyReferenceUploadResult("uploaded", dify_file_id=file_id)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
