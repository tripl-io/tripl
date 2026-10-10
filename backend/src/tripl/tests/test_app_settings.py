from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import select

from tripl import crypto
from tripl.config import settings
from tripl.models.app_setting import SERVICE_SETTINGS_KEY, AppSetting
from tripl.services import (
    _app_settings_fields,
    _app_settings_sources,
    app_settings_service,
    embedding_service,
    migration_status_service,
)
from tripl.tests.conftest import TestSessionLocal


@pytest.mark.asyncio
async def test_service_settings_update_uses_env_fallback_and_encrypts_secrets(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_key = settings.encryption_key
    settings.encryption_key = Fernet.generate_key().decode()
    crypto._fernet.cache_clear()
    # Pin the field the source assertion below reads, so the suite does not
    # depend on whatever AI_ENABLED the developer's shell happens to carry.
    monkeypatch.setattr(settings, "ai_enabled", False)
    try:
        await client.post("/api/v1/projects", json={"name": "AI", "slug": "settings-ai"})

        initial = await client.get("/api/v1/settings")
        assert initial.status_code == 200
        # "default", not "env": nothing delivers AI_ENABLED here and False is the
        # built-in default (config.py's ``ai_enabled: bool = False``). This
        # assertion used to read "env" because every field with no stored
        # override was asserted to come from the environment, which is exactly
        # the claim at issue — the badge was not evidence of
        # anything. The flip is the fix, not a regression.
        assert initial.json()["sources"]["ai.ai_enabled"] == "default"
        assert "runtime" in initial.json()
        assert "security" in initial.json()
        assert "storage" in initial.json()
        assert "observability" in initial.json()
        assert "email" in initial.json()
        assert "system" in initial.json()

        resp = await client.patch(
            "/api/v1/settings",
            json={
                "runtime": {
                    "app_base_url": "https://tripl.example",
                    "scan_row_limit_default": 123,
                },
                "ai": {
                    "ai_enabled": True,
                    "ai_api_key": "sk-runtime",
                    "describe_system_prompt": "Return JSON only.",
                },
                "email": {"smtp_password": "smtp-secret"},
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["runtime"]["app_base_url"] == "https://tripl.example"
        assert body["runtime"]["scan_row_limit_default"] == 123
        assert body["ai"]["ai_api_key_configured"] is True
        assert body["email"]["smtp_password_configured"] is True
        assert body["sources"]["runtime.app_base_url"] == "override"
        assert body["sources"]["ai.ai_api_key"] == "override"
        assert "sk-runtime" not in str(body)
        assert "smtp-secret" not in str(body)

        status = await client.get("/api/v1/projects/settings-ai/ai/status")
        assert status.json() == {"enabled": True}

        async with TestSessionLocal() as session:
            row = await session.scalar(
                select(AppSetting).where(AppSetting.key == SERVICE_SETTINGS_KEY)
            )
            assert row is not None
            assert row.value["ai_api_key"] != "sk-runtime"
            assert row.value["smtp_password"] != "smtp-secret"
            ai_config = await app_settings_service.get_ai_config(session, org_id=None)
            assert ai_config.ai_api_key == "sk-runtime"
            assert ai_config.describe_system_prompt == "Return JSON only."
    finally:
        settings.encryption_key = original_key
        crypto._fernet.cache_clear()


@pytest.mark.asyncio
async def test_service_settings_are_owner_only(anon_client: AsyncClient) -> None:
    owner = await anon_client.post(
        "/api/v1/auth/register",
        json={
            "email": "owner@example.com",
            "password": "Password123!",
            "name": "Owner",
        },
    )
    assert owner.status_code == 201
    await anon_client.post("/api/v1/auth/logout")

    # The second sign-up joins the default organization as a plain member
    # (F20 PR4: organization roles are owner | admin | member).
    member = await anon_client.post(
        "/api/v1/auth/register",
        json={
            "email": "member@example.com",
            "password": "Password123!",
            "name": "Member",
        },
    )
    assert member.status_code == 201
    assert member.json()["role"] == "member"

    resp = await anon_client.get("/api/v1/settings")
    assert resp.status_code == 403


def test_apply_startup_service_overrides_mutates_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One field from each of the three sections that are dead until applied at
    # startup, plus an AI field that must be ignored (AI is consumed via its own
    # resolved config, not off `settings`).
    overrides = {
        "hsts_enabled": True,  # security
        "photo_max_size_mb": 99,  # storage
        "log_level": "DEBUG",  # observability
        "ai_model": "should-not-be-applied",  # not a startup-applied field
    }
    monkeypatch.setattr(
        app_settings_service, "get_service_overrides_sync", lambda _session: overrides
    )

    touched = ("hsts_enabled", "photo_max_size_mb", "log_level", "ai_model")
    originals = {f: getattr(settings, f) for f in touched}
    try:
        applied = app_settings_service.apply_startup_service_overrides(session=object())  # type: ignore[arg-type]

        assert set(applied) == {"hsts_enabled", "photo_max_size_mb", "log_level"}
        assert settings.hsts_enabled is True
        assert settings.photo_max_size_mb == 99
        assert settings.log_level == "DEBUG"
        # AI fields are not applied onto settings by the startup hook.
        assert settings.ai_model == originals["ai_model"]
    finally:
        for field, value in originals.items():
            setattr(settings, field, value)
        # Process-lifetime state: a successful apply records what
        # the environment held before it, and pytest shares one process.
        app_settings_service._ENV_BEFORE_STARTUP_APPLY.clear()


def test_apply_startup_service_overrides_is_noop_on_db_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(_session: object) -> dict[str, object]:
        raise RuntimeError("db down")

    monkeypatch.setattr(app_settings_service, "get_service_overrides_sync", _boom)

    # Must swallow the error and apply nothing, so importing the app never fails
    # just because overrides can't be read.
    assert app_settings_service.apply_startup_service_overrides(session=object()) == []  # type: ignore[arg-type]


def test_startup_ignores_an_override_that_would_prevent_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stored setting must never be the reason the process cannot start.

    session_cookie_secure is applied at startup and assert_production_ready
    refuses a non-debug boot without it, so unticking "Secure cookie" in the
    admin UI used to brick the instance on the next restart — with the only
    recovery being hand-edited SQL, because the UI lives in the process that no
    longer starts.
    """
    monkeypatch.setattr(
        app_settings_service,
        "get_service_overrides_sync",
        lambda _session: {"session_cookie_secure": False, "hsts_enabled": True},
    )
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "session_cookie_secure", True)
    monkeypatch.setattr(settings, "hsts_enabled", False)

    applied = app_settings_service.apply_startup_service_overrides(session=object())  # type: ignore[arg-type]

    # Nothing is applied, and — the point of the fix — the env value survives so
    # the app boots and the operator can undo the change where they made it.
    assert applied == []
    assert settings.session_cookie_secure is True
    assert settings.hsts_enabled is False


def test_saving_an_override_that_would_prevent_boot_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping it silently at boot is safe but dishonest — say so at save time."""
    monkeypatch.setattr(settings, "debug", False)
    # The env must be healthy on this axis, or the override introduces nothing new.
    monkeypatch.setattr(settings, "session_cookie_secure", True)

    with pytest.raises(HTTPException) as excinfo:
        app_settings_service._reject_startup_breaking_overrides({"session_cookie_secure": False})

    assert excinfo.value.status_code == 422
    assert "stop the app from starting" in str(excinfo.value.detail)

    # A harmless override still passes.
    app_settings_service._reject_startup_breaking_overrides({"hsts_enabled": True})


def test_saving_is_not_blocked_by_problems_the_environment_already_has(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only what THIS change breaks.

    A deployment already missing SECRET_KEY must not have every unrelated
    settings save rejected on top of it — the check diffs against the current
    settings rather than judging the candidate in isolation.
    """
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "secret_key", "")

    app_settings_service._reject_startup_breaking_overrides({"hsts_enabled": True})


def test_registration_mode_is_resolved_live_not_at_startup() -> None:
    """Closing registration must not wait for a redeploy.

    Every other security override is pinned onto ``settings`` at process start;
    ``registration_mode`` is deliberately excluded and read per request instead.
    """
    assert "registration_mode" in _app_settings_fields.SECURITY_FIELDS
    assert "registration_mode" not in app_settings_service.STARTUP_APPLIED_FIELDS
    # The rest of the security section is still startup-applied.
    assert "hsts_enabled" in app_settings_service.STARTUP_APPLIED_FIELDS


@pytest.mark.asyncio
async def test_registration_mode_override_wins_over_env(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "registration_mode", "open")

    async with TestSessionLocal() as session:
        assert await app_settings_service.get_registration_mode(session) == "open"

    resp = await client.patch(
        "/api/v1/settings", json={"security": {"registration_mode": "disabled"}}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["sources"]["security.registration_mode"] == "override"

    async with TestSessionLocal() as session:
        assert await app_settings_service.get_registration_mode(session) == "disabled"

    # Clearing the override falls back to the env value.
    cleared = await client.patch("/api/v1/settings", json={"security": {"registration_mode": None}})
    assert cleared.status_code == 200, cleared.text
    # "default", not "env": the monkeypatch above pins "open", which IS the
    # built-in default (config.py's ``registration_mode: str = REGISTRATION_OPEN``),
    # and a resolved value equal to the default cannot be told apart from a
    # delivered one. The fallback itself is unchanged — only the
    # badge stopped claiming an environment variable it has no evidence for.
    assert cleared.json()["sources"]["security.registration_mode"] == "default"
    async with TestSessionLocal() as session:
        assert await app_settings_service.get_registration_mode(session) == "open"


@pytest.mark.asyncio
async def test_clearing_a_startup_applied_override_stops_crediting_the_environment(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case the registration_mode test above cannot reach.

    ``registration_mode`` is the one security field excluded from
    STARTUP_APPLIED_FIELDS, so its singleton is never mutated. Every other one is:
    ``apply_startup_service_overrides`` pins the stored value onto ``settings``
    and nothing puts it back. Clearing the override then popped the row while the
    singleton still held the deleted value, so the badge asserted an environment
    variable — HSTS_ENABLED here — that was never set anywhere, on the exact
    surface built to make deployment configuration verifiable.
    """
    monkeypatch.setattr(settings, "hsts_enabled", False)

    stored = await client.patch("/api/v1/settings", json={"security": {"hsts_enabled": True}})
    assert stored.status_code == 200, stored.text
    assert stored.json()["sources"]["security.hsts_enabled"] == "override"

    # The restart. main.py runs this at module scope, in the process that then
    # serves /api/v1/settings.
    async with TestSessionLocal() as session:
        overrides = await app_settings_service.get_service_overrides(session)
    monkeypatch.setattr(
        app_settings_service, "get_service_overrides_sync", lambda _session: overrides
    )
    try:
        applied = app_settings_service.apply_startup_service_overrides(session=object())  # type: ignore[arg-type]
        assert "hsts_enabled" in applied
        assert settings.hsts_enabled is True
        # Still an override, and still applied: the startup mutation changes
        # nothing about the field while the row exists.
        held = await client.get("/api/v1/settings")
        assert held.json()["security"]["hsts_enabled"] is True
        assert held.json()["sources"]["security.hsts_enabled"] == "override"

        cleared = await client.patch("/api/v1/settings", json={"security": {"hsts_enabled": None}})

        assert cleared.status_code == 200, cleared.text
        # False is config.py's ``hsts_enabled: bool = False`` — what this instance
        # will boot with next time, and what nothing delivered.
        assert cleared.json()["security"]["hsts_enabled"] is False
        assert cleared.json()["sources"]["security.hsts_enabled"] == "default"
    finally:
        app_settings_service._ENV_BEFORE_STARTUP_APPLY.clear()


@pytest.mark.asyncio
async def test_setting_source_distinguishes_a_delivered_value_from_the_code_default(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The assertion the system could not make before the source badges.

    "Is this instance using the shipped default, or was it told otherwise?" was
    unanswerable from any runtime surface: every field with no stored override
    was badged "env", so the badge said the same thing either way and the only
    sound check was diffing values against a copy of the source by hand.
    """
    default_model = _app_settings_sources._code_default("search_embedding_model")
    monkeypatch.setattr(settings, "search_embedding_model", default_model)

    resting = await client.get("/api/v1/settings")
    assert resting.status_code == 200
    assert resting.json()["sources"]["ai.search_embedding_model"] == "default"

    monkeypatch.setattr(settings, "search_embedding_model", "text-embedding-3-large")

    delivered = await client.get("/api/v1/settings")
    assert delivered.json()["sources"]["ai.search_embedding_model"] == "env"

    # An override wins over both — including one whose value equals the built-in
    # default, because a row exists and Reset will clear it.
    stored = await client.patch(
        "/api/v1/settings", json={"ai": {"search_embedding_model": default_model}}
    )
    assert stored.status_code == 200, stored.text
    assert stored.json()["sources"]["ai.search_embedding_model"] == "override"


@pytest.mark.asyncio
async def test_ai_settings_report_the_embeddings_endpoint_and_agree_with_the_caller(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where indexed plan text is POSTed has to be readable from the running stack.

    SEARCH_EMBEDDING_BASE_URL has been dropped from the compose env allowlist
    three times. Each time the only way to notice was to read the source and
    diff by hand, because no API surface carried the value at all.
    """
    self_hosted = "https://llm.internal.example/v1"
    monkeypatch.setattr(settings, "search_embedding_base_url", self_hosted)

    resp = await client.get("/api/v1/settings/ai")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ai"]["search_embedding_base_url"] == self_hosted
    assert body["sources"]["ai.search_embedding_base_url"] == "env"
    # What the API reports must be the endpoint the text is actually POSTed to,
    # not a parallel copy that can drift away from the caller.
    assert embedding_service.embeddings_url().startswith(self_hosted)

    # And the prod state the issue documents: nothing delivered, so the vectors
    # are going to OpenAI. The badge has to say "default" rather than assert an
    # environment variable that was never seen.
    monkeypatch.setattr(
        settings,
        "search_embedding_base_url",
        _app_settings_sources._code_default("search_embedding_base_url"),
    )
    at_default = await client.get("/api/v1/settings/ai")
    assert at_default.json()["sources"]["ai.search_embedding_base_url"] == "default"


@pytest.mark.asyncio
async def test_the_embeddings_endpoint_cannot_be_repointed_at_runtime(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reported, never editable — pinned at BOTH gates.

    The vectors already in pgvector came from whatever endpoint produced them,
    and similarity between two embedding spaces is meaningless, so repointing
    this at runtime would poison the index with no error anywhere. It is kept out
    of ``AiSettingsUpdate`` AND out of ``EDITABLE_FIELDS``; a later refactor that
    adds it to AI_FIELDS for badge convenience must fail here rather than
    silently make it persistable.
    """
    monkeypatch.setattr(settings, "search_embedding_base_url", "https://api.openai.com/v1")

    # PATCH /settings is the one AI write route (PUT /settings/ai is gone).
    resp = await client.patch(
        "/api/v1/settings", json={"ai": {"search_embedding_base_url": "https://evil.example/v1"}}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["ai"]["search_embedding_base_url"] == "https://api.openai.com/v1"
    assert embedding_service.embeddings_url() == "https://api.openai.com/v1/embeddings"
    assert "search_embedding_base_url" not in app_settings_service.EDITABLE_FIELDS

    async with TestSessionLocal() as session:
        row = await session.scalar(select(AppSetting).where(AppSetting.key == SERVICE_SETTINGS_KEY))
        stored = dict(row.value) if row is not None and isinstance(row.value, dict) else {}
        assert "search_embedding_base_url" not in stored


@pytest.mark.asyncio
async def test_system_section_reports_the_applied_revision_and_head(
    client: AsyncClient,
) -> None:
    """A serving app used to only IMPLY that migrations ran.

    Note what this suite can and cannot show: its schema is built by
    ``Base.metadata.create_all``, so there is no ``alembic_version`` table at
    all. That makes it the exact honest-unknown case — and the case that must
    still answer 200, because the settings page may never 500 over a revision it
    could not read.
    """
    migration_status_service.head_revision.cache_clear()

    resp = await client.get("/api/v1/settings")

    assert resp.status_code == 200
    system = resp.json()["system"]
    assert system["alembic_head_revision"] == migration_status_service.head_revision()
    assert system["alembic_head_revision"] is not None
    assert system["alembic_revision"] is None
    assert system["alembic_up_to_date"] is None


@pytest.mark.asyncio
async def test_patch_response_carries_the_same_migration_status_as_get(
    client: AsyncClient,
) -> None:
    """The frontend writes the PATCH response straight into its query cache.

    So a ``system`` field that GET carries and PATCH does not would blank the
    schema-revision tile after every save. This is why the migration status is
    threaded through one payload helper both routes call, rather than defaulted
    on the formatter.
    """
    fields = ("alembic_revision", "alembic_head_revision", "alembic_up_to_date")

    fetched = await client.get("/api/v1/settings")
    patched = await client.patch(
        "/api/v1/settings", json={"runtime": {"scan_row_limit_default": 7}}
    )

    assert patched.status_code == 200, patched.text
    for field in fields:
        assert patched.json()["system"][field] == fetched.json()["system"][field]
