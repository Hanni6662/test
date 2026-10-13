from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import Engine

from app.api.auth import router as auth_router
from app.api.billing import router as billing_router
from app.api.business_sessions import router as business_sessions_router
from app.api.health import router as health_router
from app.api.internal_dify import create_internal_dify_router
from app.api.internal_image_generation import create_internal_image_generation_router
from app.api.recommendations import router as recommendations_router
from app.api.references import router as references_router
from app.api.task_preferences import router as task_preferences_router
from app.api.tasks import router as tasks_router
from app.billing.disabled import DisabledBillingService
from app.core.config import Settings, get_settings
from app.core.errors import install_exception_handlers
from app.core.request_id import RequestIdMiddleware
from app.db import (  # noqa: F401
    action_models,
    business_question_models,
    business_session_models,
    chatflow_generation_models,
    chatflow_reference_files,
    chatflow_v34_models,
    generation_models,
    operations_models,
    outbox_models,
    prompt_models,
    recommendation_models,
    retention_models,
    task_models,
)
from app.db.session import create_database_engine
from app.integrations.dify import create_dify_adapter
from app.integrations.dify.factory import create_chatflow_adapter
from app.integrations.storage.local import LocalPrivateStorage
from app.integrations.storage.protocol import Storage
from app.services.auth import AuthOptions, AuthService
from app.services.business_questions import BusinessQuestionService
from app.services.business_sessions import BusinessSessionOptions, BusinessSessionService
from app.services.chatflow_generation import ChatflowGenerationEventService
from app.services.chatflow_generation_results import ChatflowGenerationResultService
from app.services.chatflow_prompt_packages import ChatflowPromptPackageService
from app.services.chatflow_v34_events import ChatflowV34ContextService, ChatflowV34StageEventService
from app.services.original_downloads import DownloadGate
from app.services.outbox import OutboxService
from app.services.prompt_callbacks import PromptCallbackService
from app.services.prompt_preferences import PromptPreferenceService
from app.services.recommendation_callbacks import RecommendationCallbackService
from app.services.recommendations import RecommendationOptions, RecommendationService
from app.services.references import ReferenceOptions, ReferenceService
from app.services.tasks import TaskOptions, TaskService


def create_app(
    *,
    settings: Settings | None = None,
    engine: Engine | None = None,
    storage: Storage | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    resolved_engine = engine or create_database_engine(resolved_settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        resolved_engine.dispose()

    application = FastAPI(
        title="Image Business Backend",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.database_engine = resolved_engine
    auth_options = AuthOptions(
        trusted_origins=resolved_settings.trusted_origins,
        production=resolved_settings.production,
        use_dev_cookie=resolved_settings.use_dev_cookie,
    )
    reference_storage = storage or LocalPrivateStorage(resolved_settings.private_storage_root)
    application.state.auth_service = AuthService(resolved_engine, auth_options)
    application.state.billing_service = DisabledBillingService()
    application.state.outbox_service = OutboxService(resolved_engine)
    application.state.storage = reference_storage
    application.state.reference_service = ReferenceService(
        resolved_engine,
        reference_storage,
        ReferenceOptions(
            upload_lease_seconds=resolved_settings.reference_upload_lease_seconds,
            orphan_grace_hours=resolved_settings.reference_orphan_grace_hours,
        ),
    )
    recommendation_options = RecommendationOptions(
        max_active_per_owner=resolved_settings.recommendation_max_active_per_owner,
        simulated_form_ttl_days=resolved_settings.recommendation_form_ttl_days,
    )
    recommendation_adapter = create_dify_adapter(
        resolved_settings.recommendation_dify_mode,
        form_ttl_days=recommendation_options.simulated_form_ttl_days,
    )
    application.state.recommendation_service = RecommendationService(
        resolved_engine,
        recommendation_options,
        recommendation_adapter,
    )
    application.state.prompt_callback_service = PromptCallbackService(resolved_engine)
    application.state.recommendation_callback_service = RecommendationCallbackService(
        resolved_engine
    )
    application.state.prompt_preference_service = PromptPreferenceService(resolved_engine)
    task_service = None
    if resolved_settings.tasks_api_enabled or resolved_settings.chatflow_handoff_enabled:
        task_service = TaskService(resolved_engine, TaskOptions())
    if task_service is not None:
        application.state.task_service = task_service
    if resolved_settings.tasks_api_enabled:
        application.state.original_download_gate = DownloadGate(
            max_concurrent=resolved_settings.original_download_max_concurrent,
            timeout_seconds=resolved_settings.original_download_timeout_seconds,
        )
    if resolved_settings.chatflow_handoff_enabled:
        application.state.chatflow_adapter = create_chatflow_adapter(
            resolved_settings.chatflow_dify_mode,
            api_base_url=(
                str(resolved_settings.dify_reference_upload_api_base_url)
                if resolved_settings.dify_reference_upload_api_base_url is not None
                else None
            ),
            app_id=resolved_settings.dify_app_id,
            app_api_token=(
                resolved_settings.dify_reference_upload_api_token.get_secret_value()
                if resolved_settings.dify_reference_upload_api_token is not None
                else None
            ),
            workflow_id=resolved_settings.dify_workflow_id,
        )
        application.state.business_question_service = BusinessQuestionService(resolved_engine)
        application.state.business_session_service = BusinessSessionService(
            resolved_engine,
            task_service=task_service,
            question_service=application.state.business_question_service,
            options=BusinessSessionOptions(
                max_active_per_owner=resolved_settings.recommendation_max_active_per_owner,
                dify_app_id=resolved_settings.dify_app_id or "mock-chatflow-v1",
                flow_version=resolved_settings.chatflow_flow_version,
                dify_workflow_id=resolved_settings.dify_workflow_id,
            ),
        )
        if resolved_settings.chatflow_flow_version == "visual-v3.4-single-selection":
            application.state.chatflow_v34_stage_event_service = ChatflowV34StageEventService(
                resolved_engine
            )
            application.state.chatflow_v34_context_service = ChatflowV34ContextService(
                resolved_engine,
                dify_app_id=resolved_settings.dify_app_id,
                dify_workflow_id=resolved_settings.dify_workflow_id,
            )
        if resolved_settings.generation_permit_secret is not None:
            application.state.chatflow_prompt_package_service = ChatflowPromptPackageService(
                resolved_engine,
                permit_secret=resolved_settings.generation_permit_secret,
                service_authority_id=resolved_settings.image_generation_service_authority_id,
            )
            application.state.chatflow_generation_event_service = ChatflowGenerationEventService(
                resolved_engine
            )
            application.state.chatflow_generation_result_service = ChatflowGenerationResultService(
                resolved_engine,
                generation_event_service=application.state.chatflow_generation_event_service,
            )

    # RequestId is outermost: its state value and response header are shared with
    # early upload-limit responses, before FastAPI/multipart parsing begins.
    from app.core.upload_limits import RequestBodySizeLimitMiddleware

    application.add_middleware(RequestBodySizeLimitMiddleware)
    application.add_middleware(RequestIdMiddleware)
    application.include_router(health_router)
    application.include_router(auth_router)
    application.include_router(billing_router)
    application.include_router(references_router)
    application.include_router(recommendations_router)
    application.include_router(task_preferences_router)
    if resolved_settings.tasks_api_enabled:
        application.include_router(tasks_router)
    if resolved_settings.chatflow_handoff_enabled:
        application.include_router(business_sessions_router)
    if resolved_settings.dify_callback_secret is not None:
        application.include_router(
            create_internal_dify_router(
                engine=resolved_engine,
                callback_credential=resolved_settings.dify_callback_secret,
                prompt_service=application.state.prompt_callback_service,
                recommendation_service=application.state.recommendation_callback_service,
                business_event_service=(
                    application.state.business_session_service
                    if resolved_settings.chatflow_handoff_enabled
                    else None
                ),
                v34_stage_event_service=getattr(
                    application.state, "chatflow_v34_stage_event_service", None
                ),
                v34_context_service=getattr(
                    application.state, "chatflow_v34_context_service", None
                ),
                prompt_package_service=getattr(
                    application.state, "chatflow_prompt_package_service", None
                ),
                generation_event_service=getattr(
                    application.state, "chatflow_generation_event_service", None
                ),
                generation_result_service=getattr(
                    application.state, "chatflow_generation_result_service", None
                ),
            )
        )
    if (
        resolved_settings.chatflow_handoff_enabled
        and resolved_settings.generation_permit_secret is not None
        and resolved_settings.image_generation_service_token is not None
    ):
        application.include_router(
            create_internal_image_generation_router(
                engine=resolved_engine,
                storage=reference_storage,
                service_credential=(
                    resolved_settings.image_generation_service_token.get_secret_value()
                ),
            )
        )
    if resolved_settings.maintenance_secret is not None:
        from app.api.internal_maintenance import create_internal_maintenance_router
        from app.services.operations import OperationsDiagnosticsService
        from app.services.retention_cleanup import RetentionCleanupService

        cleanup_service = RetentionCleanupService(resolved_engine, reference_storage)
        operations_service = OperationsDiagnosticsService(
            resolved_engine,
            broker_url=(
                resolved_settings.broker_url.get_secret_value()
                if resolved_settings.broker_url is not None
                else None
            ),
            queue_name=os.environ.get("IMAGE_BACKEND_QUEUE_NAME", "image-backend-events"),
            heartbeat_stale_seconds=resolved_settings.operations_heartbeat_stale_seconds,
            long_state_seconds=resolved_settings.operations_long_state_seconds,
            detail_limit=resolved_settings.operations_detail_limit,
        )
        application.state.retention_cleanup_service = cleanup_service
        application.state.operations_diagnostics_service = operations_service
        application.include_router(
            create_internal_maintenance_router(
                service=cleanup_service,
                operations_service=operations_service,
                bearer_secret=resolved_settings.maintenance_secret,
                schedule_timezone=resolved_settings.cleanup_schedule_timezone,
                schedule_daily_time=resolved_settings.cleanup_schedule_daily_time.isoformat(
                    timespec="minutes"
                ),
            )
        )
    install_exception_handlers(application)

    return application
