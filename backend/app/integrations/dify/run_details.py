from __future__ import annotations

import hashlib
import json
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

import httpx

TERMINAL_STATUSES = frozenset({"succeeded", "partial-succeeded", "failed", "stopped"})
RUN_STATUSES = TERMINAL_STATUSES | {"running", "paused"}


class DifyRunDetailsPort(Protocol):
    def get_run(self, run_id: str, app_id: str, workflow_id: str) -> dict: ...


def normalize_run_detail(body: object, run_id: str, workflow_id: str) -> dict:
    if not isinstance(body, dict):
        raise ValueError("invalid run detail")
    if body.get("id") != run_id or body.get("workflow_id") != workflow_id:
        raise ValueError("run detail identity mismatch")
    status = body.get("status")
    if status not in RUN_STATUSES:
        raise ValueError("unknown run status")
    error = body.get("error")
    if error is not None and (not isinstance(error, str) or len(error) > 65536):
        raise ValueError("invalid run error")
    code = None
    if status in {"stopped", "failed"}:
        code = (
            "DIFY_EXECUTION_STEP_LIMIT"
            if "Maximum execution steps exceeded:" in (error or "")
            else "DIFY_EXECUTION_TIME_LIMIT"
            if "Maximum execution time exceeded:" in (error or "")
            else "DIFY_RUN_STOPPED"
            if status == "stopped"
            else "DIFY_RUN_FAILED"
        )
    digest = hashlib.sha256(
        json.dumps(
            {"id": run_id, "workflow_id": workflow_id, "status": status, "error": error},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).digest()
    return {"status": status, "error_code": code, "digest": digest}


class HttpDifyRunDetailsPort:
    """Bounded, GET-only Service API reader; never resumes or creates a run."""

    def __init__(self, origin: str, app_id: str, token: str, *, transport=None):
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not app_id
            or not token
        ):
            raise ValueError("invalid Dify run reader configuration")
        self._origin = origin.rstrip("/")
        self._app_id = app_id
        self._token = token
        self._transport = transport

    def get_run(self, run_id: str, app_id: str, workflow_id: str) -> dict:
        if app_id != self._app_id or str(UUID(run_id)) != run_id:
            raise ValueError("run reader identity mismatch")
        with httpx.Client(
            base_url=self._origin,
            timeout=10,
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
            headers={"Authorization": f"Bearer {self._token}"},
        ) as client:
            response = client.get(f"/v1/workflows/run/{run_id}")
            response.raise_for_status()
            return normalize_run_detail(response.json(), run_id, workflow_id)
