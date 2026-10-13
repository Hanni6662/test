from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class DisabledFeatureResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
