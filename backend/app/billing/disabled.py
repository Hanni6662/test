from __future__ import annotations

from app.core.domain_errors import DomainError


class DisabledAccountService:
    def login(self) -> None:
        raise DomainError(503, "FEATURE_DISABLED", "账号登录暂未开放")

    def logout(self) -> None:
        raise DomainError(503, "FEATURE_DISABLED", "账号登录暂未开放")


class DisabledBillingService:
    def quota_status(self) -> dict[str, bool]:
        return {"enabled": False}

    def balance_status(self) -> dict[str, bool]:
        return {"enabled": False}

    def create_recharge_order(self) -> None:
        raise DomainError(503, "FEATURE_DISABLED", "充值功能暂未开放")
