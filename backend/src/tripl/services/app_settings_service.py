"""Runtime settings stored in the ``app_settings`` table, per scope.

Two scopes (F20 PR9). The OPERATOR scope (``organization_id IS NULL``) stores
explicit overrides of the env-based defaults from :class:`tripl.config.Settings`
for every editable field. An ORGANIZATION scope stores that organization's own
values of the :data:`ORG_FIELDS` (mail, AI chat, search embeddings, row limits,
photo storage).
Resolution:

* operator view: operator override -> env;
* an organization: org override -> operator override -> env, with the
  credential-group, fallback-policy and ceiling rules of
  :mod:`tripl.services._org_settings_merge`. On a self-hosted instance the
  default organization IS the operator scope (:func:`settings_scope_for`).

Secret overrides are encrypted at rest and never returned to clients; clients
only see ``*_configured`` booleans.

Both async (API) and sync (Celery worker) accessors are provided. The operator
scope's sync accessors fall back to environment values on DB errors; an
organization's fail CLOSED (AI and mail off; row limits re-raise) — env holds
the operator's keys, not the organization's (critique #19). Every degradation is
counted in ``tripl_settings_read_failures_total``.

Layout: this module is the public facade. The field catalogue, the stored
overrides, each section's config builder, the resolution and the public payload
live in private ``_app_settings_*`` siblings. A name is re-exported here (every
``X as X`` below is deliberate) only when this module uses it or a caller
reaches it through this module; anything else is imported from its sibling.
The accessors that read a scope, and every caller of a function tests patch on
this module, stay here, so a ``monkeypatch.setattr(app_settings_service, ...)``
still reaches them.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from tripl import crypto
from tripl.config import Settings, settings
from tripl.models.app_setting import AI_SETTINGS_KEY, SERVICE_SETTINGS_KEY, AppSetting
from tripl.observability.metrics import settings_read_failures_total
from tripl.services._app_settings_ai import (
    AI_CONFIG_FIELDS as AI_CONFIG_FIELDS,
)
from tripl.services._app_settings_ai import (
    AiConfig as AiConfig,
)
from tripl.services._app_settings_ai import (
    ai_config_for as ai_config_for,
)
from tripl.services._app_settings_ai import (
    ai_prompt_defaults as ai_prompt_defaults,
)
from tripl.services._app_settings_ai import (
    disabled_ai_config as disabled_ai_config,
)
from tripl.services._app_settings_ai import (
    env_ai_config as env_ai_config,
)
from tripl.services._app_settings_core import (
    _ENV_BEFORE_STARTUP_APPLY as _ENV_BEFORE_STARTUP_APPLY,
)
from tripl.services._app_settings_core import (
    _operator_setting as _operator_setting,
)
from tripl.services._app_settings_core import (
    _org_setting as _org_setting,
)
from tripl.services._app_settings_core import (
    _reject_startup_breaking_overrides as _reject_startup_breaking_overrides,
)
from tripl.services._app_settings_core import (
    apply_org_override_changes as apply_org_override_changes,
)
from tripl.services._app_settings_core import (
    build_service_values as build_service_values,
)
from tripl.services._app_settings_core import (
    get_org_overrides as get_org_overrides,
)
from tripl.services._app_settings_core import (
    get_org_overrides_sync as get_org_overrides_sync,
)
from tripl.services._app_settings_core import (
    get_service_overrides as get_service_overrides,
)
from tripl.services._app_settings_core import (
    get_service_overrides_sync as get_service_overrides_sync,
)
from tripl.services._app_settings_core import (
    settings_scope_for as settings_scope_for,
)
from tripl.services._app_settings_email import (
    EmailConfig as EmailConfig,
)
from tripl.services._app_settings_email import (
    _email_config_of as _email_config_of,
)
from tripl.services._app_settings_email import (
    build_email_config as build_email_config,
)
from tripl.services._app_settings_email import (
    disabled_email_config as disabled_email_config,
)
from tripl.services._app_settings_email import (
    email_can_send as email_can_send,
)
from tripl.services._app_settings_email import (
    email_config_for as email_config_for,
)
from tripl.services._app_settings_email import (
    email_sender_for as email_sender_for,
)
from tripl.services._app_settings_email import (
    env_email_config as env_email_config,
)
from tripl.services._app_settings_email import (
    relay_is_scope_owned as relay_is_scope_owned,
)
from tripl.services._app_settings_fields import (
    AI_CHAT_FIELDS as AI_CHAT_FIELDS,
)
from tripl.services._app_settings_fields import (
    AI_FIELDS as AI_FIELDS,
)
from tripl.services._app_settings_fields import (
    EDITABLE_FIELDS as EDITABLE_FIELDS,
)
from tripl.services._app_settings_fields import (
    EMAIL_FIELDS as EMAIL_FIELDS,
)
from tripl.services._app_settings_fields import (
    EMBEDDING_FIELDS as EMBEDDING_FIELDS,
)
from tripl.services._app_settings_fields import (
    OPERATOR_FIELDS as OPERATOR_FIELDS,
)
from tripl.services._app_settings_fields import (
    ORG_FIELDS as ORG_FIELDS,
)
from tripl.services._app_settings_fields import (
    SECRET_FIELDS as SECRET_FIELDS,
)
from tripl.services._app_settings_fields import (
    STARTUP_APPLIED_FIELDS as STARTUP_APPLIED_FIELDS,
)
from tripl.services._app_settings_fields import (
    STORAGE_FIELDS as STORAGE_FIELDS,
)
from tripl.services._app_settings_fields import (
    STORAGE_ORG_FIELDS as STORAGE_ORG_FIELDS,
)
from tripl.services._app_settings_payload import (
    resolved_settings_payload as resolved_settings_payload,
)
from tripl.services._app_settings_payload import (
    service_settings_payload as service_settings_payload,
)
from tripl.services._app_settings_runtime import (
    RuntimeConfig as RuntimeConfig,
)
from tripl.services._app_settings_runtime import (
    _runtime_config_from as _runtime_config_from,
)
from tripl.services._app_settings_runtime import (
    build_runtime_config as build_runtime_config,
)
from tripl.services._app_settings_runtime import (
    env_runtime_config as env_runtime_config,
)
from tripl.services._app_settings_sources import (
    ResolvedSettings as ResolvedSettings,
)
from tripl.services._app_settings_sources import (
    resolve_settings as resolve_settings,
)

logger = logging.getLogger(__name__)


async def get_row_limit_defaults(
    session: AsyncSession, *, org_id: uuid.UUID | None = None
) -> RuntimeConfig:
    """The effective row caps: the organization's (clamped), else operator, else env.

    ``org_id`` may be omitted: row limits are not secrets, so an unscoped read
    only answers with the operator's caps.
    """
    return _runtime_config_from((await resolve_for_org(session, org_id)).values)


async def resolve_for_org(session: AsyncSession, org_id: uuid.UUID | None) -> ResolvedSettings:
    """The effective settings an organization runs with (``None``: the operator's).

    No env fallback on a database error: the exception propagates, so an
    organization's request fails rather than running on the operator's keys.
    """
    operator = await get_service_overrides(session)
    scope = settings_scope_for(org_id)
    if scope is None:
        return resolve_settings(operator, None)
    return resolve_settings(operator, await get_org_overrides(session, scope), org_scope=scope)


def resolve_for_org_sync(session: Session, org_id: uuid.UUID | None) -> ResolvedSettings:
    operator = get_service_overrides_sync(session)
    scope = settings_scope_for(org_id)
    if scope is None:
        return resolve_settings(operator, None)
    return resolve_settings(operator, get_org_overrides_sync(session, scope), org_scope=scope)


async def get_ai_config(session: AsyncSession, *, org_id: uuid.UUID | None) -> AiConfig:
    """The AI config an organization's call runs with.

    ``org_id`` is required: ``None`` is the operator's own config and must be
    asked for explicitly, so a call site cannot forget its organization and
    quietly borrow the operator's key.
    """
    return ai_config_for(await resolve_for_org(session, org_id))


async def get_email_config(session: AsyncSession, *, org_id: uuid.UUID | None) -> EmailConfig:
    """The async twin of ``get_email_config_sync``, for request-path callers."""
    resolved = await resolve_for_org(session, org_id)
    if not resolved.guarded_hosts:
        return _email_config_of(resolved)
    return await asyncio.to_thread(email_config_for, resolved)


async def get_service_settings(session: AsyncSession) -> dict[str, Any]:
    return await service_settings_payload(session, await get_service_overrides(session))


def _open_sync_session() -> Session:
    from tripl.worker.db import SyncSessionLocal

    return SyncSessionLocal()


def _resolve_sync(session: Session | None, org_id: uuid.UUID | None) -> ResolvedSettings:
    if session is not None:
        return resolve_for_org_sync(session, org_id)
    with _open_sync_session() as own_session:
        return resolve_for_org_sync(own_session, org_id)


def get_ai_config_sync(session: Session | None = None, *, org_id: uuid.UUID | None) -> AiConfig:
    """Sync twin of :func:`get_ai_config` for workers.

    On a read failure the operator scope falls back to env (the operator's own
    configuration either way); an organization scope fails CLOSED with AI off
    (critique #19) — env holds the operator's key, not the organization's.
    """
    try:
        return ai_config_for(_resolve_sync(session, org_id))
    except Exception:  # noqa: BLE001
        settings_read_failures_total.labels(section="ai").inc()
        if settings_scope_for(org_id) is not None:
            logger.warning(
                "AI disabled for org %s: app_settings read failed", org_id, exc_info=True
            )
            return disabled_ai_config()
        logger.warning("Falling back to env AI config: app_settings read failed", exc_info=True)
        return env_ai_config()


def get_email_config_sync(
    session: Session | None = None, *, org_id: uuid.UUID | None
) -> EmailConfig:
    """Sync twin of :func:`get_email_config`; fails closed for an organization."""
    try:
        return email_config_for(_resolve_sync(session, org_id))
    except Exception:  # noqa: BLE001
        settings_read_failures_total.labels(section="email").inc()
        if settings_scope_for(org_id) is not None:
            logger.warning(
                "Email disabled for org %s: app_settings read failed", org_id, exc_info=True
            )
            return disabled_email_config()
        logger.warning("Falling back to env email config: app_settings read failed", exc_info=True)
        return env_email_config()


def get_runtime_config_sync(
    session: Session | None = None, *, org_id: uuid.UUID | None = None
) -> RuntimeConfig:
    """``app_base_url`` (always the operator's) and the row-limit defaults.

    ``org_id`` gives the organization's row limits; omitted, the operator's.
    An organization scope re-raises a read failure instead of using env limits.
    """
    try:
        return _runtime_config_from(_resolve_sync(session, org_id).values)
    except Exception:
        settings_read_failures_total.labels(section="runtime").inc()
        if settings_scope_for(org_id) is not None:
            logger.warning("Row limits for org %s unreadable: app_settings read failed", org_id)
            raise
        logger.warning(
            "Falling back to env runtime config: app_settings read failed", exc_info=True
        )
        return env_runtime_config()


async def get_embedding_config(session: AsyncSession, *, org_id: uuid.UUID | None) -> AiConfig:
    """The config an organization's search embeddings run with (F20 PR10).

    Each organization has its own vector space: its endpoint, provider, model
    and key (one credential group), stamped on its documents through
    ``embedding_service.embedding_provenance``. ``org_id`` is required for the
    same reason as :func:`get_ai_config`. Only the ``search_embedding*``
    fields of the result may be used.
    """
    return await get_ai_config(session, org_id=org_id)


def get_embedding_config_sync(
    session: Session | None = None, *, org_id: uuid.UUID | None
) -> AiConfig:
    """Sync twin of :func:`get_embedding_config` for the search worker; fails closed."""
    return get_ai_config_sync(session, org_id=org_id)


async def project_org_id(session: AsyncSession, project_id: uuid.UUID) -> uuid.UUID | None:
    """The organization a project belongs to, read from the database."""
    from tripl.models.project import Project

    org_id: uuid.UUID | None = await session.scalar(
        select(Project.organization_id).where(Project.id == project_id)
    )
    return org_id


async def get_embedding_config_for_project(
    session: AsyncSession, project_id: uuid.UUID
) -> AiConfig:
    """The embedding config of the organization owning ``project_id``; off if unknown."""
    org_id = await project_org_id(session, project_id)
    if org_id is None:
        return disabled_ai_config()
    return await get_embedding_config(session, org_id=org_id)


async def get_search_embedding_config(session: AsyncSession, *, project_id: uuid.UUID) -> AiConfig:
    """The embedding config a search query in ``project_id`` runs with.

    Always the PROJECT's organization, read from the database: its key and
    endpoint embed the query, and its provenance is what the query-time
    ``embedding_model`` filter compares stored vectors with, so a query never
    ranks another organization's vector space (F20 PR10). A bound request
    organization that is not the project's fails closed (semantic search off)
    rather than spend either organization's key on the other's corpus.
    """
    from tripl.middleware.org_context import current_org_id

    org_id = await project_org_id(session, project_id)
    if org_id is None:
        return disabled_ai_config()
    bound = current_org_id()
    if bound is not None and bound != org_id:
        return disabled_ai_config()
    return await get_embedding_config(session, org_id=org_id)


def get_embedding_config_for_project_sync(
    session: Session | None, project_id: uuid.UUID
) -> AiConfig:
    """Sync twin of :func:`get_embedding_config_for_project` (worker paths)."""
    return get_ai_config_for_project_sync(session, project_id)


def project_org_id_sync(session: Session | None, project_id: uuid.UUID) -> uuid.UUID | None:
    """The organization a project belongs to, read from the database (worker paths)."""
    from tripl.models.project import Project

    stmt = select(Project.organization_id).where(Project.id == project_id)
    if session is not None:
        return session.scalar(stmt)
    with _open_sync_session() as own_session:
        return own_session.scalar(stmt)


def get_ai_config_for_project_sync(session: Session | None, project_id: uuid.UUID) -> AiConfig:
    """The AI config of the organization owning ``project_id``; AI off if unknown."""
    try:
        org_id = project_org_id_sync(session, project_id)
    except Exception:  # noqa: BLE001
        logger.warning("AI disabled: cannot read the organization of project %s", project_id)
        return disabled_ai_config()
    if org_id is None:
        return disabled_ai_config()
    return get_ai_config_sync(session, org_id=org_id)


def get_email_config_for_project_sync(
    session: Session | None, project_id: uuid.UUID
) -> EmailConfig:
    """The email config of the organization owning ``project_id``; no relay if unknown."""
    try:
        org_id = project_org_id_sync(session, project_id)
    except Exception:  # noqa: BLE001
        logger.warning("Email disabled: cannot read the organization of project %s", project_id)
        return disabled_email_config()
    if org_id is None:
        return disabled_email_config()
    return get_email_config_sync(session, org_id=org_id)


async def get_operator_email_config(session: AsyncSession) -> EmailConfig:
    """The operator's relay, for account mail sent from a request."""
    return await get_email_config(session, org_id=None)


async def get_registration_mode(session: AsyncSession) -> str:
    """Effective self-service registration mode (DB override -> env).

    Read per request rather than pinned onto ``settings`` at startup, so an
    owner toggling registration in Settings -> Security closes (or reopens) the
    door immediately. Returns one of :data:`tripl.config.REGISTRATION_MODES`.
    """
    values = build_service_values(await get_service_overrides(session))
    return str(values["registration_mode"])


def apply_startup_service_overrides(session: Session | None = None) -> list[str]:
    """Apply persisted Security/Storage/Observability overrides onto ``settings``.

    Must run at the very top of the API and worker entry modules, before the
    middleware, rate limiters, logging and metrics route are wired from
    ``settings`` — those read it once at import, so a later apply would be too
    late. This is what makes these overrides "take effect on the next deploy",
    as the admin settings UI states.

    Degrades to a no-op (env-only config) when the DB is unreachable, so simply
    importing the app never fails because overrides cannot be read. Returns the
    list of fields actually applied (for logging and tests).
    """
    try:
        if session is not None:
            overrides = get_service_overrides_sync(session)
        else:
            from tripl.worker.db import SyncSessionLocal

            with SyncSessionLocal() as own_session:
                overrides = get_service_overrides_sync(own_session)
    except Exception:  # noqa: BLE001
        logger.warning(
            "Startup service-override apply skipped: app_settings read failed",
            exc_info=True,
        )
        return []

    candidate_values = {
        field: overrides[field]
        for field in STARTUP_APPLIED_FIELDS
        if overrides.get(field) is not None and hasattr(settings, field)
    }
    try:
        validated = Settings.model_validate({**settings.model_dump(), **candidate_values})
    except ValidationError as exc:
        logger.error("Ignoring invalid stored service overrides: %s", exc)
        return []

    applied: list[str] = []
    previous: dict[str, Any] = {}
    for field in STARTUP_APPLIED_FIELDS:
        value = overrides.get(field)
        if value is None or not hasattr(settings, field):
            continue
        previous[field] = getattr(settings, field)
        setattr(settings, field, getattr(validated, field))
        applied.append(field)

    # A stored override must never be the reason the process cannot boot.
    #
    # These fields land on ``settings`` here and ``assert_production_ready`` runs
    # a few lines later in main.py. Unticking "Secure cookie" (or setting CORS to
    # "*") under Settings -> Instance -> Security therefore bricked the instance
    # on the NEXT restart — and because the UI that set it lives in the process
    # that now refuses to start, the only way out was hand-editing app_settings
    # in Postgres. Falling back to env-only config keeps the
    # instance reachable so the operator can undo the change where they made it.
    if applied and not settings.debug:
        with_overrides = settings.production_problems()
        if with_overrides:
            # Compare against env-only config: a deployment that was already
            # unready must still fail on its own merits, with the operator's
            # intended settings in place. Only roll back what the OVERRIDES break.
            for field, value in previous.items():
                setattr(settings, field, value)
            introduced = [p for p in with_overrides if p not in settings.production_problems()]
            if not introduced:
                for field in applied:
                    setattr(settings, field, getattr(validated, field))
            else:
                logger.error(
                    "Ignoring %d stored service override(s) — applying them would stop "
                    "this process from starting. Falling back to environment "
                    "configuration; correct or clear them under Settings -> Instance. "
                    "Fields: %s. Problems: %s",
                    len(applied),
                    ", ".join(sorted(applied)),
                    "; ".join(introduced),
                )
                return []

    if applied:
        # This apply is permanent for the life of the process — the only restore
        # is the rollback branch above, which returns [] before reaching here —
        # so ``settings`` has now lost what the environment delivered for these
        # fields. Keep it, or clearing the override later reports the deleted
        # value and badges it "Env".
        #
        # ``setdefault``, not ``update``: only the FIRST apply in a process saw
        # the pre-override value, so a repeat call must not record what it wrote.
        for field, env_value in previous.items():
            _ENV_BEFORE_STARTUP_APPLY.setdefault(field, env_value)
        logger.info("Applied %d service override(s) onto settings at startup", len(applied))
    return applied


async def update_service_overrides(
    session: AsyncSession,
    changes: dict[str, Any],
) -> dict[str, Any]:
    row = await session.scalar(_operator_setting(SERVICE_SETTINGS_KEY))
    overrides: dict[str, Any] = (
        dict(row.value) if row is not None and isinstance(row.value, dict) else {}
    )

    for key, value in changes.items():
        if key not in EDITABLE_FIELDS:
            continue
        if value is None or (key in SECRET_FIELDS and value == ""):
            overrides.pop(key, None)
            continue
        overrides[key] = crypto.encrypt_value(str(value)) if key in SECRET_FIELDS else value

    _reject_startup_breaking_overrides(overrides)

    if row is None:
        row = AppSetting(key=SERVICE_SETTINGS_KEY, value=overrides, organization_id=None)
        session.add(row)
    else:
        row.value = overrides

    # If a partial local DB has the first-cut key="ai" row, make resets behave
    # predictably by removing touched AI fields from that legacy document.
    legacy_row = await session.scalar(_operator_setting(AI_SETTINGS_KEY))
    if legacy_row is not None and isinstance(legacy_row.value, dict):
        legacy = dict(legacy_row.value)
        for key in changes:
            if key in AI_CONFIG_FIELDS:
                legacy.pop(key, None)
        legacy_row.value = legacy

    await session.commit()
    return await get_service_overrides(session)


async def update_org_overrides(
    session: AsyncSession,
    org_scope: uuid.UUID,
    changes: dict[str, Any],
) -> dict[str, Any]:
    """Write an organization's own overrides (ORG_FIELDS only; the caller validates).

    Same sparse semantics as the operator document: ``None`` clears a field, an
    empty secret clears it too, anything else is stored (secrets encrypted).
    Returns the organization's raw overrides after the write.
    """
    row = await session.scalar(_org_setting(SERVICE_SETTINGS_KEY, org_scope))
    overrides = apply_org_override_changes(
        dict(row.value) if row is not None and isinstance(row.value, dict) else {}, changes
    )
    if row is None:
        session.add(
            AppSetting(key=SERVICE_SETTINGS_KEY, value=overrides, organization_id=org_scope)
        )
    else:
        row.value = overrides
    await session.commit()
    return await get_org_overrides(session, org_scope)
