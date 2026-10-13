from __future__ import annotations

import pytest
from app.core.config import ConfigurationError, Settings, get_settings
from pydantic import ValidationError

TEST_DATABASE_URL = "postgresql+psycopg://test-user:test-password@127.0.0.1:1/image_backend"


def test_defaults_keep_external_and_paid_features_disabled() -> None:
    settings = Settings(database_url=TEST_DATABASE_URL)

    assert settings.guest_session_ttl_days == 120
    assert settings.dify_mode == "mock"
    assert settings.recommendation_dify_mode == "mock"
    assert settings.recommendation_max_active_per_owner == 1
    assert settings.recommendation_form_ttl_days == 3
    assert settings.cleanup_schedule_timezone == "Asia/Shanghai"
    assert settings.cleanup_schedule_daily_time.isoformat(timespec="minutes") == "03:00"
    assert settings.cleanup_batch_size == 25
    assert settings.dify_callback_secret is None
    assert settings.generation_mode == "mock"
    assert settings.generation_runtime_enabled is False
    assert settings.tasks_api_enabled is False
    assert settings.original_download_max_concurrent == 4
    assert settings.original_download_timeout_seconds == 120
    assert settings.billing_enabled is False
    assert settings.account_login_enabled is False
    assert settings.allow_paid_calls is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"dify_mode": "real"},
        {"generation_mode": "real"},
        {"billing_enabled": True},
        {"account_login_enabled": True},
        {"allow_paid_calls": True},
    ],
)
def test_real_integrations_and_paid_features_cannot_be_enabled_in_s0_s1(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        Settings(database_url=TEST_DATABASE_URL, **overrides)


def test_task_api_requires_explicit_mock_generation_runtime_gate() -> None:
    with pytest.raises(ValidationError):
        Settings(database_url=TEST_DATABASE_URL, tasks_api_enabled=True)

    enabled = Settings(
        database_url=TEST_DATABASE_URL,
        tasks_api_enabled=True,
        generation_runtime_enabled=True,
    )
    assert enabled.tasks_api_enabled is True
    assert enabled.generation_runtime_enabled is True
    assert enabled.generation_mode == "mock"


def test_recommendation_settings_are_explicit_and_bounded() -> None:
    settings = Settings(
        database_url=TEST_DATABASE_URL,
        recommendation_max_active_per_owner=4,
        recommendation_form_ttl_days=5,
    )
    assert settings.recommendation_max_active_per_owner == 4
    assert settings.recommendation_form_ttl_days == 5

    with pytest.raises(ValidationError):
        Settings(database_url=TEST_DATABASE_URL, recommendation_max_active_per_owner=0)


def test_original_download_limits_are_positive_and_bounded() -> None:
    settings = Settings(
        database_url=TEST_DATABASE_URL,
        original_download_max_concurrent=8,
        original_download_timeout_seconds=300,
    )
    assert settings.original_download_max_concurrent == 8
    assert settings.original_download_timeout_seconds == 300

    for override in (
        {"original_download_max_concurrent": 0},
        {"original_download_max_concurrent": 101},
        {"original_download_timeout_seconds": 0},
        {"original_download_timeout_seconds": 3601},
    ):
        with pytest.raises(ValidationError):
            Settings(database_url=TEST_DATABASE_URL, **override)


def test_invalid_database_url_error_names_field_without_echoing_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_value = "not-a-url-with-private-value"
    monkeypatch.setenv("IMAGE_BACKEND_DATABASE_URL", secret_value)
    get_settings.cache_clear()
    try:
        with pytest.raises(ConfigurationError) as raised:
            get_settings()
    finally:
        get_settings.cache_clear()
        monkeypatch.delenv("IMAGE_BACKEND_DATABASE_URL", raising=False)

    assert "database_url" in str(raised.value)
    assert secret_value not in str(raised.value)


def test_callback_secret_is_optional_redacted_and_empty_means_disabled() -> None:
    disabled = Settings(database_url=TEST_DATABASE_URL, dify_callback_secret="")
    configured = Settings(
        database_url=TEST_DATABASE_URL,
        dify_callback_secret="independent-callback-secret-value-1234567890",
    )

    assert disabled.dify_callback_secret is None
    assert configured.dify_callback_secret is not None
    assert "independent-callback-secret-value-1234567890" not in repr(
        configured.dify_callback_secret
    )


def test_blank_callback_secret_environment_variable_disables_callback_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_BACKEND_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("IMAGE_BACKEND_DIFY_CALLBACK_SECRET", "")
    get_settings.cache_clear()
    try:
        settings = get_settings()
    finally:
        get_settings.cache_clear()
        monkeypatch.delenv("IMAGE_BACKEND_DIFY_CALLBACK_SECRET", raising=False)
        monkeypatch.delenv("IMAGE_BACKEND_DATABASE_URL", raising=False)

    assert settings.dify_callback_secret is None


def test_maintenance_secret_is_optional_and_validated() -> None:
    disabled = Settings(database_url=TEST_DATABASE_URL, maintenance_secret="")
    configured = Settings(
        database_url=TEST_DATABASE_URL,
        maintenance_secret="independent-maintenance-secret-value-123456789",
    )

    assert disabled.maintenance_secret is None
    assert configured.maintenance_secret is not None
    assert "independent-maintenance-secret-value-123456789" not in repr(
        configured.maintenance_secret
    )

    with pytest.raises(ValidationError):
        Settings(database_url=TEST_DATABASE_URL, maintenance_secret="too-short")

    shared = "one-secret-shared-between-services-123456"
    with pytest.raises(ValidationError):
        Settings(
            database_url=TEST_DATABASE_URL,
            dify_callback_secret=shared,
            maintenance_secret=shared,
        )


def test_cleanup_schedule_settings_are_validated() -> None:
    settings = Settings(
        database_url=TEST_DATABASE_URL,
        cleanup_schedule_timezone="UTC",
        cleanup_schedule_daily_time="04:15",
    )
    assert settings.cleanup_schedule_timezone == "UTC"
    assert settings.cleanup_schedule_daily_time.isoformat(timespec="minutes") == "04:15"

    with pytest.raises(ValidationError):
        Settings(database_url=TEST_DATABASE_URL, cleanup_schedule_timezone="Not/AZone")

    with pytest.raises(ValidationError):
        Settings(database_url=TEST_DATABASE_URL, cleanup_schedule_daily_time="03:00+00:00")


@pytest.mark.parametrize("daily_time", ["04:15:01", "04:15:00.000001"])
def test_cleanup_schedule_rejects_subminute_precision(daily_time: str) -> None:
    with pytest.raises(ValidationError):
        Settings(database_url=TEST_DATABASE_URL, cleanup_schedule_daily_time=daily_time)


@pytest.mark.parametrize(
    "secret_value",
    ["too-short", "non-ascii-callback-secret-長長長長長長"],
)
def test_invalid_callback_secret_is_rejected_without_echoing_value(
    secret_value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_BACKEND_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("IMAGE_BACKEND_DIFY_CALLBACK_SECRET", secret_value)
    get_settings.cache_clear()
    try:
        with pytest.raises(ConfigurationError) as raised:
            get_settings()
    finally:
        get_settings.cache_clear()
        monkeypatch.delenv("IMAGE_BACKEND_DIFY_CALLBACK_SECRET", raising=False)
        monkeypatch.delenv("IMAGE_BACKEND_DATABASE_URL", raising=False)

    assert "dify_callback_secret" in str(raised.value)
    assert secret_value not in str(raised.value)
