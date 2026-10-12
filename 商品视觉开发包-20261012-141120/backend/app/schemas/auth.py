from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class AuthFeatures(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    account_login: Literal[False] = False
    billing: Literal[False] = False
    payments: Literal[False] = False


class IdentityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: UUID
    identity_kind: Literal["guest", "member"]
    authenticated: Literal[True] = True
    features: AuthFeatures = AuthFeatures()
