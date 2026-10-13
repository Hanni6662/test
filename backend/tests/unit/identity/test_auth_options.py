from __future__ import annotations

import pytest
from app.services.auth import DEVELOPMENT_COOKIE_NAME, PRODUCTION_COOKIE_NAME, AuthOptions


def test_production_defaults_to_host_cookie_and_canonical_origins() -> None:
    options = AuthOptions(frozenset({"https://app.example.com"}))

    assert options.cookie_name == PRODUCTION_COOKIE_NAME
    assert options.cookie_secure is True
    assert options.trusted_origins == frozenset({"https://app.example.com"})


def test_development_cookie_is_explicit_and_rejected_for_production() -> None:
    with pytest.raises(ValueError):
        AuthOptions(frozenset({"http://localhost:5173"}), use_dev_cookie=True)

    options = AuthOptions(
        frozenset({"http://localhost:5173"}),
        production=False,
        use_dev_cookie=True,
    )
    assert options.cookie_name == DEVELOPMENT_COOKIE_NAME
    assert options.cookie_secure is False


def test_production_origin_allowlist_requires_https() -> None:
    with pytest.raises(ValueError):
        AuthOptions(frozenset({"http://localhost:5173"}))

    options = AuthOptions(
        frozenset({"http://localhost:5173"}), production=False, use_dev_cookie=True
    )
    assert options.trusted_origins == frozenset({"http://localhost:5173"})


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "null",
        "https://app.example.com/",
        "https://user@app.example.com",
        "https://app.example.com/path",
    ],
)
def test_origin_allowlist_rejects_non_origin_values(origin: str) -> None:
    with pytest.raises(ValueError):
        AuthOptions(frozenset({origin}))
