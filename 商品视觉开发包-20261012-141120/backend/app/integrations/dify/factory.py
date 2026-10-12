from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Literal

from app.integrations.dify.contracts import (
    DifyNotConfigured,
    DifyPromptWorkflowPort,
    DifyRecommendationPort,
)
from app.integrations.dify.mock import MockDifyAdapter, MockPromptWorkflowAdapter
from sqlalchemy.orm import Session


def create_chatflow_adapter(
    mode: Literal["stub", "http"],
    *,
    api_base_url: str | None = None,
    app_id: str | None = None,
    app_api_token: str | None = None,
    workflow_id: str | None = None,
):
    from app.integrations.dify.chatflow import StubDifyChatflowAdapter
    from app.integrations.dify.chatflow_v34_port import DifyV34ChatflowPort

    if mode == "stub":
        return StubDifyChatflowAdapter()
    if mode == "http":
        if not all((api_base_url, app_id, app_api_token, workflow_id)):
            raise DifyNotConfigured("HTTP Chatflow adapter requires fixed Dify API settings")
        return DifyV34ChatflowPort(
            api_base_url=api_base_url,
            app_id=app_id,
            app_api_token=app_api_token,
            workflow_id=workflow_id,
        )
    raise ValueError("unsupported Chatflow adapter mode")


def create_dify_adapter(
    mode: Literal["mock", "real"], *, form_ttl_days: int = 3
) -> DifyRecommendationPort:
    if mode == "mock":
        return MockDifyAdapter(form_ttl_days=form_ttl_days)
    if mode == "real":
        raise DifyNotConfigured("real Dify integration is not configured")
    raise ValueError("unsupported Dify mode")


def create_test_dify_adapter(
    *,
    auxiliary_relation_overrides: Mapping[str, str],
    form_ttl_days: int = 3,
    outcome: Literal["analyzing", "ready", "failed"] = "ready",
) -> MockDifyAdapter:
    """Isolated test seam; ordinary application settings and HTTP JSON cannot reach it."""
    return MockDifyAdapter.for_testing(
        auxiliary_relation_overrides=auxiliary_relation_overrides,
        form_ttl_days=form_ttl_days,
        outcome=outcome,
    )


def create_prompt_workflow_adapter(
    mode: Literal["mock", "real"],
    *,
    session_factory: Callable[[], Session],
) -> DifyPromptWorkflowPort:
    """Build the private prompt adapter; real Dify remains deliberately unavailable."""
    if mode == "mock":
        return MockPromptWorkflowAdapter(session_factory)
    if mode == "real":
        raise DifyNotConfigured("real Dify prompt submission is not configured")
    raise ValueError("unsupported Dify prompt mode")


def create_test_prompt_workflow_adapter(
    *,
    session_factory: Callable[[], Session],
    reject_before_accept_once: bool = False,
    accepted_response_lost_once: bool = False,
    recovery_unknown_once: bool = False,
) -> MockPromptWorkflowAdapter:
    """Isolated test seam for prompt submit outcomes; no request/config path exposes it."""
    return MockPromptWorkflowAdapter.for_testing(
        session_factory,
        reject_before_accept_once=reject_before_accept_once,
        accepted_response_lost_once=accepted_response_lost_once,
        recovery_unknown_once=recovery_unknown_once,
    )
