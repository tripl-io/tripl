from __future__ import annotations

import pytest
from pydantic import ValidationError

from tripl.config import REGISTRATION_DISABLED, REGISTRATION_OPEN, Settings


def test_cors_origins_explicit_list_wins() -> None:
    s = Settings(cors_allow_origins="https://a.example, https://b.example", debug=False)
    assert s.cors_origins() == ["https://a.example", "https://b.example"]


def test_cors_origins_debug_default_is_wildcard() -> None:
    s = Settings(cors_allow_origins="", debug=True, app_base_url="")
    assert s.cors_origins() == ["*"]


def test_cors_origins_production_falls_back_to_app_base_url() -> None:
    s = Settings(
        cors_allow_origins="",
        debug=False,
        app_base_url="https://app.example.com/",
    )
    # Trailing slash is trimmed so the value matches the browser's Origin header.
    assert s.cors_origins() == ["https://app.example.com"]


def test_cors_origins_production_with_no_origin_is_empty() -> None:
    s = Settings(cors_allow_origins="", debug=False, app_base_url="")
    assert s.cors_origins() == []


def test_assert_production_ready_skipped_in_debug() -> None:
    Settings(debug=True, encryption_key="").assert_production_ready()


def test_debug_accepts_release_env_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEBUG", "release")
    assert Settings().debug is False


def test_debug_accepts_development_alias() -> None:
    assert Settings(debug="development").debug is True


def test_assert_production_ready_requires_encryption_key() -> None:
    s = Settings(
        debug=False,
        encryption_key="",
        session_cookie_secure=True,
        cors_allow_origins="https://a.example",
    )
    with pytest.raises(RuntimeError) as exc:
        s.assert_production_ready()
    assert "ENCRYPTION_KEY" in str(exc.value)


def test_assert_production_ready_rejects_invalid_encryption_key() -> None:
    s = Settings(
        debug=False,
        encryption_key="not-a-real-fernet-key",
        session_cookie_secure=True,
        cors_allow_origins="https://a.example",
    )
    with pytest.raises(RuntimeError) as exc:
        s.assert_production_ready()
    assert "Fernet" in str(exc.value)


def _valid_production_settings(**overrides: object) -> Settings:
    """A Settings instance that passes assert_production_ready, with explicit
    non-default DB/queue URLs so the dev-credential guard doesn't trip. Tests
    override one field at a time to isolate a single failure."""
    from cryptography.fernet import Fernet

    base: dict[str, object] = {
        "debug": False,
        "encryption_key": Fernet.generate_key().decode(),
        "secret_key": "x" * 32,
        "session_cookie_secure": True,
        "cors_allow_origins": "https://app.example",
        "database_url": "postgresql+asyncpg://produser:prodpw@db:5432/tripl",
        "sync_database_url": "postgresql+psycopg://produser:prodpw@db:5432/tripl",
        "rabbitmq_url": "amqp://produser:prodpw@mq:5672//",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_assert_production_ready_passes_with_valid_setup() -> None:
    _valid_production_settings().assert_production_ready()


def test_assert_production_ready_requires_secret_key() -> None:
    s = _valid_production_settings(secret_key="")
    with pytest.raises(RuntimeError) as exc:
        s.assert_production_ready()
    assert "SECRET_KEY" in str(exc.value)


def test_assert_production_ready_rejects_default_database_credentials() -> None:
    s = _valid_production_settings(
        database_url="postgresql+asyncpg://tripl:tripl@localhost:5432/tripl",
    )
    with pytest.raises(RuntimeError) as exc:
        s.assert_production_ready()
    assert "DATABASE_URL" in str(exc.value)


def test_assert_production_ready_rejects_default_sync_database_credentials() -> None:
    s = _valid_production_settings(
        sync_database_url="postgresql+psycopg://tripl:tripl@localhost:5432/tripl",
    )
    with pytest.raises(RuntimeError) as exc:
        s.assert_production_ready()
    assert "SYNC_DATABASE_URL" in str(exc.value)


def test_assert_production_ready_rejects_default_rabbitmq_credentials() -> None:
    s = _valid_production_settings(rabbitmq_url="amqp://guest:guest@localhost:5672//")
    with pytest.raises(RuntimeError) as exc:
        s.assert_production_ready()
    assert "RABBITMQ_URL" in str(exc.value)


def test_assert_production_ready_rejects_wildcard_cors() -> None:
    # An explicit "*" in the CORS list resolves to the wildcard, which breaks
    # cookie auth in browsers.
    s = _valid_production_settings(cors_allow_origins="*")
    with pytest.raises(RuntimeError) as exc:
        s.assert_production_ready()
    assert "wildcard" in str(exc.value).lower()


def test_assert_production_ready_requires_cors_origin() -> None:
    from cryptography.fernet import Fernet

    s = Settings(
        debug=False,
        encryption_key=Fernet.generate_key().decode(),
        session_cookie_secure=True,
        cors_allow_origins="",
        app_base_url="",
    )
    with pytest.raises(RuntimeError) as exc:
        s.assert_production_ready()
    assert "CORS" in str(exc.value)


def test_assert_production_ready_requires_secure_cookies() -> None:
    from cryptography.fernet import Fernet

    s = Settings(
        debug=False,
        encryption_key=Fernet.generate_key().decode(),
        session_cookie_secure=False,
        cors_allow_origins="https://app.example",
    )
    with pytest.raises(RuntimeError) as exc:
        s.assert_production_ready()
    assert "SESSION_COOKIE_SECURE" in str(exc.value)


def test_registration_is_open_by_default() -> None:
    # Deliberate trade-off: there is no owner-initiated
    # account-create or invite endpoint yet, so a "disabled" default leaves an
    # instance unable to onboard anybody. Operators close it explicitly once
    # their team has accounts.
    assert Settings().registration_mode == REGISTRATION_OPEN


def test_registration_mode_env_can_still_close_the_door() -> None:
    # The fail-closed path stays reachable and unchanged: setting the env var
    # (REGISTRATION_MODE=disabled) is what an operator does before exposing the
    # instance publicly.
    assert Settings(registration_mode="disabled").registration_mode == REGISTRATION_DISABLED


def test_registration_mode_is_case_and_whitespace_insensitive() -> None:
    assert Settings(registration_mode=" Open ").registration_mode == REGISTRATION_OPEN


def test_registration_mode_rejects_unknown_values() -> None:
    with pytest.raises(ValidationError, match="registration_mode must be one of"):
        Settings(registration_mode="invite-only")


def test_empty_env_values_fall_back_to_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty environment variable means "unset", not "parse this as a bool".

    This is not hypothetical tidiness. compose.yaml forwards ~25 optional
    settings as `VAR: ${VAR:-}` so that a value placed in .env actually reaches
    the container — the omission that made DEMO_ENABLED and then
    REGISTRATION_MODE inert. But Compose's map syntax
    materialises an undefined variable as the empty STRING, and before
    `env_ignore_empty` pydantic raised five validation errors on exactly the
    five typed members of that list. `Settings()` is constructed at module
    import, so the app, migrate, worker and beat containers all exited on boot
    of every fresh `docker compose up` — which is precisely the machine
    `tripl install` promises to take from nothing to running.

    The five names below are the five that failed, reproduced from a .env
    derived from .env.example; the rest of the passthrough list is `str`, where
    "" and the default coincide.
    """
    for name in (
        "REGISTRATION_MODE",
        "RATE_LIMIT_TRUST_FORWARDED_FOR",
        "SMTP_PORT",
        "SMTP_USE_TLS",
        "AI_ENABLED",
    ):
        monkeypatch.setenv(name, "")

    settings = Settings()

    assert settings.registration_mode == REGISTRATION_OPEN
    assert settings.rate_limit_trust_forwarded_for is False
    assert settings.smtp_port == 587
    assert settings.smtp_use_tls is True
    assert settings.ai_enabled is False
