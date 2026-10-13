from __future__ import annotations

import os

from celery import Celery
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.session import create_database_engine
from app.integrations.dify import create_dify_adapter
from app.integrations.dify.factory import (
    create_chatflow_adapter,
    create_prompt_workflow_adapter,
)
from app.integrations.generation.mock import MockGenerationPort, _TestOnlyFaults
from app.integrations.generation_acceptance import HttpGenerationAcceptancePort
from app.integrations.storage.local import LocalPrivateStorage
from app.services.chatflow_actions import build_chatflow_action_registrations
from app.services.chatflow_generation_actions import (
    HttpGenerationStatusPort,
    UnavailableGenerationStatusPort,
    build_generation_recovery_registration,
)
from app.services.chatflow_generation_closure import (
    configured_close_registrations,
)
from app.services.chatflow_generation_delivery_actions import (
    HttpImageGenerationDeliveryPort,
    UnavailableImageGenerationDeliveryPort,
    build_generation_delivery_registrations,
)
from app.services.chatflow_generation_expiry import (
    UnavailableGenerationAcceptancePort,
    build_generation_expiry_registrations,
)
from app.services.generation_runtime import (
    GENERATION_START_EVENT_TYPE,
    GenerationActionRuntime,
    handle_generation_start_event,
)
from app.services.prompt_callbacks import PromptCallbackService
from app.services.prompt_workflows import build_prompt_action_registrations
from app.services.recommendations import RecommendationOptions, recommendation_handlers
from app.services.tasks import task_prompt_handlers
from app.workers.celery_app import create_celery_app
from app.workers.options import queue_options_from_settings
from app.workers.processor import HandlerRegistration

_settings = get_settings()
_options = queue_options_from_settings(
    _settings,
    queue_name=os.environ.get("IMAGE_BACKEND_QUEUE_NAME", "image-backend-events"),
)
_recommendation_options = RecommendationOptions(
    max_active_per_owner=_settings.recommendation_max_active_per_owner,
    simulated_form_ttl_days=_settings.recommendation_form_ttl_days,
)
_recommendation_adapter = create_dify_adapter(
    _settings.recommendation_dify_mode,
    form_ttl_days=_recommendation_options.simulated_form_ttl_days,
)


def _engine_factory():
    # Celery's prefork child calls this lazily on first task execution. No
    # SQLAlchemy pool is inherited from the CLI parent process.
    return create_database_engine(_settings)


def _runtime_registrations(
    engine,
    *,
    generation_test_faults: _TestOnlyFaults | None = None,
):
    runtime_sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    prompt_port = create_prompt_workflow_adapter(
        _settings.dify_mode, session_factory=runtime_sessions
    )
    generation_port = MockGenerationPort(
        runtime_sessions,
        _test_faults=generation_test_faults,
    )
    private_storage = LocalPrivateStorage(_settings.private_storage_root)
    generation_runtime = GenerationActionRuntime(generation_port, private_storage)
    event_handlers = task_prompt_handlers()
    event_handlers[GENERATION_START_EVENT_TYPE] = HandlerRegistration(
        handle_generation_start_event, frozenset({1})
    )
    mock_result_callback_service = (
        PromptCallbackService(engine) if _settings.dify_mode == "mock" else None
    )
    action_registrations = build_prompt_action_registrations(
        prompt_port,
        mock_result_callback_service=mock_result_callback_service,
    )
    if _settings.chatflow_handoff_enabled:
        from app.integrations.dify.reference_files import DifyReferenceFileUploadClient

        action_registrations.update(configured_close_registrations(_settings))

        reference_upload_port = (
            DifyReferenceFileUploadClient(
                api_base_url=str(_settings.dify_reference_upload_api_base_url),
                app_id=str(_settings.dify_app_id),
                app_api_token=_settings.dify_reference_upload_api_token.get_secret_value(),
            )
            if _settings.dify_reference_upload_enabled
            and _settings.dify_reference_upload_api_base_url is not None
            and _settings.dify_reference_upload_api_token is not None
            and _settings.dify_app_id is not None
            else None
        )
        action_registrations.update(
            build_chatflow_action_registrations(
                create_chatflow_adapter(
                    _settings.chatflow_dify_mode,
                    api_base_url=(
                        str(_settings.dify_reference_upload_api_base_url)
                        if _settings.dify_reference_upload_api_base_url is not None
                        else None
                    ),
                    app_id=_settings.dify_app_id,
                    app_api_token=(
                        _settings.dify_reference_upload_api_token.get_secret_value()
                        if _settings.dify_reference_upload_api_token is not None
                        else None
                    ),
                    workflow_id=_settings.dify_workflow_id,
                ),
                reference_storage=private_storage,
                reference_upload_port=reference_upload_port,
            )
        )
        if (
            _settings.image_generation_service_url is not None
            and _settings.image_generation_service_token is not None
        ):
            generation_status_port = HttpGenerationStatusPort(
                str(_settings.image_generation_service_url),
                _settings.image_generation_service_token.get_secret_value(),
            )
            generation_acceptance_port = HttpGenerationAcceptancePort(
                str(_settings.image_generation_service_url),
                _settings.image_generation_service_token.get_secret_value(),
            )
        else:
            generation_status_port = UnavailableGenerationStatusPort()
            generation_acceptance_port = UnavailableGenerationAcceptancePort()
        action_registrations.update(
            {
                "chatflow.generation.recover.v1": build_generation_recovery_registration(
                    generation_status_port
                )
            }
        )
        action_registrations.update(
            build_generation_expiry_registrations(
                generation_acceptance_port,
                generation_status_port,
            )
        )
        if (
            _settings.image_generation_service_url is not None
            and _settings.image_generation_service_token is not None
        ):
            delivery_port = HttpImageGenerationDeliveryPort(
                str(_settings.image_generation_service_url),
                _settings.image_generation_service_token.get_secret_value(),
            )
        else:
            delivery_port = UnavailableImageGenerationDeliveryPort()
        action_registrations.update(
            build_generation_delivery_registrations(
                delivery_port,
                private_storage,
            )
        )
    action_registrations.update(generation_runtime.registrations())
    return event_handlers, action_registrations


def _build_worker_app(
    *,
    generation_test_faults: _TestOnlyFaults | None = None,
) -> Celery:
    def runtime_registrations(engine):
        return _runtime_registrations(engine, generation_test_faults=generation_test_faults)

    return create_celery_app(
        _options,
        engine_factory=_engine_factory,
        handlers=recommendation_handlers(_recommendation_options, _recommendation_adapter),
        runtime_registrations_factory=runtime_registrations,
    )


app: Celery = _build_worker_app()
