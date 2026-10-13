from __future__ import annotations

from datetime import time
from functools import lru_cache
from pathlib import Path
from tempfile import gettempdir
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AnyHttpUrl, Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="IMAGE_BACKEND_",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
    )

    database_url: SecretStr
    broker_url: SecretStr | None = None
    guest_session_ttl_days: int = Field(default=120, ge=1, le=3650)
    production: bool = False
    use_dev_cookie: bool = False
    trusted_origins: frozenset[str] = Field(
        default_factory=lambda: frozenset({"http://127.0.0.1:8001"})
    )
    storage_driver: Literal["local"] = "local"
    private_storage_root: Path = Path(gettempdir()) / "image-backend-private"
    reference_upload_lease_seconds: int = Field(default=1800, ge=60, le=86400)
    reference_orphan_grace_hours: int = Field(default=24, ge=24, le=8760)
    recommendation_dify_mode: Literal["mock", "real"] = "mock"
    recommendation_max_active_per_owner: int = Field(default=1, ge=1, le=100)
    recommendation_form_ttl_days: int = Field(default=3, ge=1, le=3650)
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=2, ge=0, le=50)
    dify_mode: Literal["mock"] = "mock"
    chatflow_dify_mode: Literal["stub", "http"] = "stub"
    dify_callback_secret: SecretStr | None = None
    dify_app_id: str | None = None
    dify_workflow_id: str | None = None
    chatflow_flow_version: Literal["legacy", "visual-v3.4-single-selection"] = "legacy"
    dify_reference_upload_enabled: bool = False
    dify_reference_upload_api_base_url: AnyHttpUrl | None = None
    dify_reference_upload_api_token: SecretStr | None = None
    generation_permit_secret: SecretStr | None = None
    image_generation_service_url: AnyHttpUrl | None = None
    image_generation_service_token: SecretStr | None = None
    image_generation_service_authority_id: UUID | None = None
    maintenance_secret: SecretStr | None = None
    cleanup_schedule_timezone: str = "Asia/Shanghai"
    cleanup_schedule_daily_time: time = time(hour=3)
    cleanup_batch_size: int = Field(default=25, ge=1, le=500)
    operations_heartbeat_stale_seconds: int = Field(default=90, ge=5, le=3600)
    operations_long_state_seconds: int = Field(default=1800, ge=60, le=604800)
    operations_detail_limit: int = Field(default=50, ge=1, le=200)
    generation_mode: Literal["mock"] = "mock"
    generation_runtime_enabled: bool = False
    tasks_api_enabled: bool = False
    original_download_max_concurrent: int = Field(default=4, ge=1, le=100)
    original_download_timeout_seconds: int = Field(default=120, ge=1, le=3600)
    chatflow_handoff_enabled: bool = False
    billing_enabled: bool = False
    account_login_enabled: bool = False
    allow_paid_calls: bool = False

    @field_validator("dify_callback_secret", mode="before")
    @classmethod
    def normalize_empty_callback_secret(cls, value: object) -> object:
        if value is None or value == "":
            return None
        return value

    @field_validator("dify_callback_secret")
    @classmethod
    def require_independent_callback_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value()
        if len(raw) < 32 or not raw.isascii() or any(character.isspace() for character in raw):
            raise ValueError("must contain at least 32 non-whitespace ASCII characters")
        return value

    @field_validator("dify_app_id", mode="before")
    @classmethod
    def normalize_empty_dify_app_id(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("dify_app_id")
    @classmethod
    def require_safe_dify_app_id(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or len(value) > 160 or value != value.strip()):
            raise ValueError("must be a non-empty Dify App identity of at most 160 characters")
        return value

    @field_validator("dify_workflow_id", mode="before")
    @classmethod
    def normalize_empty_dify_workflow_id(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("dify_workflow_id")
    @classmethod
    def require_safe_dify_workflow_id(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or len(value) > 160 or value != value.strip()):
            raise ValueError("must be a non-empty Dify workflow identity of at most 160 characters")
        return value

    @field_validator("dify_reference_upload_api_base_url", mode="before")
    @classmethod
    def normalize_empty_dify_reference_upload_url(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("dify_reference_upload_api_token", mode="before")
    @classmethod
    def normalize_empty_dify_reference_upload_token(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("dify_reference_upload_api_token")
    @classmethod
    def require_dify_reference_upload_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            raw = value.get_secret_value()
            if len(raw) < 16 or not raw.isascii() or any(character.isspace() for character in raw):
                raise ValueError("must contain at least 16 non-whitespace ASCII characters")
        return value

    @field_validator("generation_permit_secret", mode="before")
    @classmethod
    def normalize_empty_generation_permit_secret(cls, value: object) -> object:
        if value is None or value == "":
            return None
        return value

    @field_validator("generation_permit_secret")
    @classmethod
    def require_generation_permit_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value()
        if len(raw) < 32 or not raw.isascii() or any(character.isspace() for character in raw):
            raise ValueError("must contain at least 32 non-whitespace ASCII characters")
        return value

    @field_validator("image_generation_service_url", mode="before")
    @classmethod
    def normalize_empty_image_generation_url(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("image_generation_service_token", mode="before")
    @classmethod
    def normalize_empty_image_generation_token(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("image_generation_service_token")
    @classmethod
    def require_image_generation_service_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value()
        if len(raw) < 16 or not raw.isascii() or any(character.isspace() for character in raw):
            raise ValueError("must contain at least 16 non-whitespace ASCII characters")
        return value

    @field_validator("image_generation_service_authority_id", mode="before")
    @classmethod
    def normalize_empty_image_generation_authority(cls, value: object) -> object:
        if value is None or value == "":
            return None
        if isinstance(value, UUID):
            return value
        if isinstance(value, str):
            try:
                parsed = UUID(value)
            except ValueError:
                raise ValueError("must be a canonical lowercase UUID") from None
            if str(parsed) != value:
                raise ValueError("must be a canonical lowercase UUID")
            return parsed
        raise ValueError("must be a canonical lowercase UUID")

    @field_validator("maintenance_secret", mode="before")
    @classmethod
    def normalize_empty_maintenance_secret(cls, value: object) -> object:
        if value is None or value == "":
            return None
        return value

    @field_validator("maintenance_secret")
    @classmethod
    def require_independent_maintenance_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value()
        if len(raw) < 32 or not raw.isascii() or any(character.isspace() for character in raw):
            raise ValueError("must contain at least 32 non-whitespace ASCII characters")
        return value

    @field_validator("cleanup_schedule_timezone")
    @classmethod
    def require_valid_cleanup_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("must name a valid IANA timezone") from None
        return value

    @field_validator("cleanup_schedule_daily_time")
    @classmethod
    def require_cleanup_wall_clock_time(cls, value: time) -> time:
        if value.tzinfo is not None:
            raise ValueError("must be a local wall-clock time without a timezone")
        if value.second != 0 or value.microsecond != 0:
            raise ValueError("must have minute precision")
        return value

    @field_validator("database_url")
    @classmethod
    def require_postgres_psycopg(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
        except ArgumentError:
            raise ValueError("must be a valid PostgreSQL psycopg URL") from None
        if url.drivername != "postgresql+psycopg" or not url.host or not url.database:
            raise ValueError("must use postgresql+psycopg with a host and database")
        return value

    @model_validator(mode="after")
    def enforce_disabled_external_features(self) -> Settings:
        if self.billing_enabled or self.account_login_enabled or self.allow_paid_calls:
            raise ValueError("billing, account login, and paid calls are disabled in this phase")
        if self.tasks_api_enabled and not self.generation_runtime_enabled:
            raise ValueError("task API requires the generation runtime gate")
        if self.chatflow_handoff_enabled and self.dify_callback_secret is None:
            raise ValueError("Chatflow handoff requires the independent Dify callback secret")
        if self.chatflow_flow_version == "visual-v3.4-single-selection":
            if not self.chatflow_handoff_enabled:
                raise ValueError("v3.4 Chatflow requires Chatflow handoff to be enabled")
            if self.dify_app_id is None or self.dify_workflow_id is None:
                raise ValueError("v3.4 Chatflow requires fixed Dify App and workflow identities")
        if self.chatflow_dify_mode == "http":
            if (
                not self.chatflow_handoff_enabled
                or self.chatflow_flow_version != "visual-v3.4-single-selection"
                or self.dify_app_id is None
                or self.dify_workflow_id is None
                or not self.dify_reference_upload_enabled
                or self.dify_reference_upload_api_base_url is None
                or self.dify_reference_upload_api_token is None
            ):
                raise ValueError(
                    "HTTP Chatflow requires v3.4 handoff, fixed App/workflow, and the same "
                    "Dify Service API credentials used for reference upload"
                )
        if (self.dify_reference_upload_api_base_url is None) != (
            self.dify_reference_upload_api_token is None
        ):
            raise ValueError(
                "Dify reference upload API origin and App token must be configured together"
            )
        if self.dify_reference_upload_enabled and (
            not self.chatflow_handoff_enabled
            or self.dify_app_id is None
            or self.dify_reference_upload_api_base_url is None
            or self.dify_reference_upload_api_token is None
        ):
            raise ValueError(
                "Dify reference upload requires Chatflow handoff, an explicit App ID, "
                "origin, and App token"
            )
        if self.generation_permit_secret is not None and not self.chatflow_handoff_enabled:
            raise ValueError("generation permit route requires Chatflow handoff to be enabled")
        if (self.image_generation_service_url is None) != (
            self.image_generation_service_token is None
        ):
            raise ValueError(
                "image-generation status service URL and token must be configured together"
            )
        if (
            self.generation_permit_secret is not None
            and self.dify_callback_secret is not None
            and self.generation_permit_secret.get_secret_value()
            == self.dify_callback_secret.get_secret_value()
        ):
            raise ValueError(
                "generation permit secret must be independent from Dify callback secret"
            )
        if (
            self.generation_permit_secret is not None
            and self.maintenance_secret is not None
            and self.generation_permit_secret.get_secret_value()
            == self.maintenance_secret.get_secret_value()
        ):
            raise ValueError("generation permit secret must be independent from maintenance secret")
        if (
            self.dify_callback_secret is not None
            and self.maintenance_secret is not None
            and self.dify_callback_secret.get_secret_value()
            == self.maintenance_secret.get_secret_value()
        ):
            raise ValueError("maintenance secret must be independent from the Dify callback secret")
        if self.production and self.use_dev_cookie:
            raise ValueError("development cookies are forbidden in production")
        if self.production and self.storage_driver == "local":
            raise ValueError("local private storage is forbidden in production")
        return self


class ConfigurationError(RuntimeError):
    """Configuration failure whose message never contains supplied values."""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        fields = sorted(
            {str(error["loc"][0]) for error in exc.errors(include_input=False) if error.get("loc")}
        )
        safe_fields = ", ".join(fields) if fields else "one or more settings"
        raise ConfigurationError(f"Invalid configuration fields: {safe_fields}") from None
