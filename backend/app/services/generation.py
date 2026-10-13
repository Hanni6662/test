"""One-step orchestration facade for the private generation port."""

from __future__ import annotations

from uuid import UUID

from app.integrations.generation.protocol import (
    GenerationCommand,
    GenerationPort,
    GenerationQueryResult,
    GenerationRecoveryResult,
    GenerationSubmitResult,
)


class GenerationService:
    """Delegates exactly one adapter operation per call, without polling or sleeping."""

    def __init__(self, port: GenerationPort) -> None:
        self._port = port

    def submit(self, command: GenerationCommand) -> GenerationSubmitResult:
        return self._port.submit(command)

    def query(self, provider_job_id: str) -> GenerationQueryResult:
        return self._port.query(provider_job_id)

    def recover(self, request_key: UUID) -> GenerationRecoveryResult:
        return self._port.recover(request_key)

    def download(self, result_ref: str) -> bytes:
        return self._port.download(result_ref)
