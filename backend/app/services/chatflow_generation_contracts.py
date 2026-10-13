"""Fixed image-generation API contract selected by immutable business flow version."""

from __future__ import annotations

from app.schemas.chatflow_v34 import V34_FLOW_VERSION

IMAGE_GENERATION_CONTRACT_V2 = "chatflow-image-generation-v2"
IMAGE_GENERATION_CONTRACT_V3 = "chatflow-image-generation-v3"
LEGACY_BUSINESS_FLOW_VERSION = "legacy"


def image_generation_contract_for_flow_version(flow_version: str) -> str:
    """Choose a read/write API contract without probing or fallback requests."""
    if flow_version == V34_FLOW_VERSION:
        return IMAGE_GENERATION_CONTRACT_V3
    if flow_version == LEGACY_BUSINESS_FLOW_VERSION:
        return IMAGE_GENERATION_CONTRACT_V2
    raise ValueError("business flow version has no image-generation contract")
