from __future__ import annotations

from collections.abc import Mapping
from functools import partial

from app.integrations.dify.contracts import DifyRecommendationPort
from app.services.recommendations import (
    RECOMMENDATION_EVENT_TYPE,
    RecommendationOptions,
    process_recommendation_event,
)
from app.workers.processor import HandlerRegistration


def recommendation_handler_registration(
    options: RecommendationOptions, dify_port: DifyRecommendationPort
) -> HandlerRegistration:
    """Build the explicit S5 registration; public worker registries stay untouched."""
    handler = partial(
        process_recommendation_event,
        options=options,
        dify_port=dify_port,
    )
    return HandlerRegistration(handler, operation_versions=frozenset({1}))


def recommendation_handlers(
    options: RecommendationOptions, dify_port: DifyRecommendationPort
) -> Mapping[str, HandlerRegistration]:
    return {
        RECOMMENDATION_EVENT_TYPE: recommendation_handler_registration(options, dify_port)
    }
