"""An organization's own settings: the view, the write and its checks (F20 PR9, PR10).

Resolution itself lives in :mod:`tripl.services.app_settings_service`
(``resolve_for_org`` and the ``get_*_config`` getters); this module is the
organization-facing surface over it:

* :func:`org_settings_payload` — what ``GET /orgs/{org}/settings`` answers: the
  effective values, where each came from, what the organization would inherit
  with nothing of its own, and the operator's ceilings.
* :func:`write_org_changes` — the save, with the checks that only apply to an
  ORGANIZATION's values: an organization's hosts must be public
  (critique #14; every organization scope, hosted or self-hosted), its
  limits may not exceed the operator's (critique #15), and its own embedding
  model must answer with vectors the index can store (F20 PR10: a test
  embedding of ``search_embedding_dimensions`` values, else 422). A save that
  moves an organization's embedding identity enqueues a reindex of that
  organization's projects only.

On a self-hosted instance the default organization is an alias of the operator
scope (critique #17), so its writes land in the operator document and its
checks are the operator's (none of the above) — except that the credential
groups there (SMTP relay, AI endpoint and key) carry every user's account mail
and, under ``ORG_SETTINGS_OPERATOR_FALLBACK=all``, every other organization's AI
traffic, so writing them still needs a platform admin
(:func:`require_operator_credential_writer`).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.alerting_validation import reject_private_host
from tripl.config import settings
from tripl.services import app_settings_service, embedding_service
from tripl.services._celery_dispatch import dispatch
from tripl.services._org_settings_merge import (
    CEILING_FIELDS,
    CREDENTIAL_GROUPS,
    EMBEDDING_GROUP,
    GROUP_SWITCHES,
)
from tripl.services.app_settings_service import (
    EMBEDDING_FIELDS,
    ORG_FIELDS,
    ResolvedSettings,
)

logger = logging.getLogger(__name__)

#: Response sections of the organization view, and the fields in each.
ORG_SECTIONS: dict[str, tuple[str, ...]] = {
    "limits": ("scan_row_limit_default", "metrics_row_limit_default"),
    "email": app_settings_service.EMAIL_FIELDS,
    "ai": app_settings_service.AI_CHAT_FIELDS,
    "search": EMBEDDING_FIELDS,
}


#: The credential-group fields (SMTP relay, AI endpoint, embedding endpoint,
#: with their secrets) and the group switches (whether search text goes to the
#: embedding endpoint at all). In the OPERATOR scope these are instance-wide:
#: password-reset and invitation mail go through the operator relay, and other
#: organizations inherit the rest.
CREDENTIAL_FIELDS: frozenset[str] = frozenset(
    {
        *(field for group in CREDENTIAL_GROUPS for field in group),
        *GROUP_SWITCHES.values(),
    }
)

#: The Celery task a change of an organization's embedding identity enqueues.
ORG_REINDEX_TASK = "tripl.worker.tasks.search.reindex_org_search_documents"

#: The operator's embedding endpoint is ``SEARCH_EMBEDDING_BASE_URL``, never a
#: stored value; a self-hosted default organization writes the operator scope.
OPERATOR_EMBEDDING_URL_READ_ONLY = (
    "The operator's embedding endpoint is set by SEARCH_EMBEDDING_BASE_URL, not in settings."
)

#: The 403 detail of :func:`require_operator_credential_writer`; the same text as
#: ``api.deps.PLATFORM_ADMIN_REQUIRED`` (services do not import the API layer).
PLATFORM_ADMIN_REQUIRED = "Platform admin required"

#: The operator's identifiers an organization admin is not shown when it merely
#: INHERITS the group: some providers use a key id as the SMTP username, and the
#: embedding endpoint is the operator's infrastructure (the legacy view has
#: always withheld it from organization admins).
_REDACTED_INHERITED_FIELDS: tuple[str, ...] = ("smtp_username", "search_embedding_base_url")


def require_operator_credential_writer(
    changes: Mapping[str, Any], *, is_platform_admin: bool
) -> None:
    """403 unless a platform admin writes the OPERATOR scope's credential groups.

    Reached through a self-hosted default organization (the operator alias) or
    the legacy ``/settings``: an owner/admin of that organization may change the
    operator's limits and prompts, but not the relay that carries every user's
    password-reset mail nor the endpoint other organizations' AI text goes to.
    """
    if not is_platform_admin and not CREDENTIAL_FIELDS.isdisjoint(changes):
        raise HTTPException(status_code=403, detail=PLATFORM_ADMIN_REQUIRED)


def flatten_changes(sections: Mapping[str, Any]) -> dict[str, Any]:
    """``{"email": {"smtp_host": ...}}`` -> ``{"smtp_host": ...}`` (explicitly set keys only)."""
    changes: dict[str, Any] = {}
    for value in sections.values():
        if isinstance(value, dict):
            changes.update(value)
    return changes


def _redact_inherited(
    values: Mapping[str, Any], sources: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """``values`` with the operator's inherited identifiers blanked.

    ``sources`` ``None``: every value is inherited (the ``inherited`` block);
    otherwise only a field whose source is the operator's (not ``org``,
    ``default`` or ``disabled``) is blanked.
    """
    redacted = dict(values)
    for field in _REDACTED_INHERITED_FIELDS:
        if sources is None or sources.get(field) not in {"org", "default", "disabled"}:
            redacted[field] = ""
    return redacted


def _section_values(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "limits": {
            "scan_row_limit_default": values["scan_row_limit_default"],
            "metrics_row_limit_default": values["metrics_row_limit_default"],
        },
        "email": {
            "smtp_host": values["smtp_host"],
            "smtp_port": values["smtp_port"],
            "smtp_username": values["smtp_username"],
            "smtp_password_configured": bool(values["smtp_password"]),
            "smtp_security": values["smtp_security"],
            "smtp_from_address": values["smtp_from_address"],
        },
        "ai": {
            "ai_enabled": values["ai_enabled"],
            "ai_base_url": values["ai_base_url"],
            "ai_model": values["ai_model"],
            "ai_api_key_configured": bool(values["ai_api_key"]),
            "ai_timeout_seconds": values["ai_timeout_seconds"],
            "ai_max_output_tokens": values["ai_max_output_tokens"],
            "describe_system_prompt": values["describe_system_prompt"],
            "ask_system_prompt": values["ask_system_prompt"],
            "alert_explanation_system_prompt": values["alert_explanation_system_prompt"],
        },
        "search": {
            "search_embeddings_enabled": values["search_embeddings_enabled"],
            "search_embedding_provider": values["search_embedding_provider"],
            "search_embedding_model": values["search_embedding_model"],
            "search_embedding_base_url": values["search_embedding_base_url"],
            "search_embedding_api_key_configured": bool(values["search_embedding_api_key"]),
            "search_embedding_dimensions": settings.search_embedding_dimensions,
        },
    }


@dataclass(frozen=True)
class OrgSettingsView:
    resolved: ResolvedSettings
    inherited: ResolvedSettings
    operator: ResolvedSettings


async def _view(session: AsyncSession, org_id: uuid.UUID) -> OrgSettingsView:
    operator_overrides = await app_settings_service.get_service_overrides(session)
    scope = app_settings_service.settings_scope_for(org_id)
    operator = app_settings_service.resolve_settings(operator_overrides, None)
    if scope is None:
        return OrgSettingsView(resolved=operator, inherited=operator, operator=operator)
    org_overrides = await app_settings_service.get_org_overrides(session, scope)
    return OrgSettingsView(
        resolved=app_settings_service.resolve_settings(
            operator_overrides, org_overrides, org_scope=scope
        ),
        inherited=app_settings_service.resolve_settings(operator_overrides, {}, org_scope=scope),
        operator=operator,
    )


async def org_settings_payload(
    session: AsyncSession, *, org_id: uuid.UUID, org_slug: str
) -> dict[str, Any]:
    """The organization's settings view; the body of ``OrgSettingsResponse``."""
    view = await _view(session, org_id)
    resolved = view.resolved
    if resolved.org_scope is None:
        own_values: Mapping[str, Any] = resolved.values
        inherited_values: Mapping[str, Any] = view.inherited.values
    else:
        # An organization is told THAT it inherits the operator's relay, not
        # the operator's login to it (the ``*_configured`` flags already stand
        # in for the secrets).
        own_values = _redact_inherited(resolved.values, resolved.sources)
        inherited_values = _redact_inherited(view.inherited.values)
    sources = {
        f"{section}.{field}": resolved.sources[field]
        for section, section_fields in ORG_SECTIONS.items()
        for field in section_fields
    }
    return {
        "organization": org_slug,
        "scope": "operator" if resolved.org_scope is None else "organization",
        "operator_fallback": settings.org_settings_operator_fallback,
        **_section_values(own_values),
        "inherited": _section_values(inherited_values),
        "ceilings": {field: view.operator.values[field] for field in CEILING_FIELDS},
        "overridden_fields": [f for f in resolved.overridden_fields if f in ORG_FIELDS],
        "sources": sources,
    }


def _check_public_url(value: Any, field: str) -> None:
    if not value:
        return
    hostname = urlparse(str(value)).hostname
    if not hostname:
        raise ValueError(f"{field} must name a host")
    reject_private_host(hostname, field=field)


def _check_public_hosts(changes: Mapping[str, Any]) -> None:
    """422 when an organization points AI, embeddings or SMTP at a private address.

    Blocking (DNS): called in a worker thread.
    """
    _check_public_url(changes.get("ai_base_url"), "AI base URL")
    _check_public_url(changes.get("search_embedding_base_url"), "Search embedding base URL")
    smtp_host = changes.get("smtp_host")
    if smtp_host:
        reject_private_host(str(smtp_host).strip(), field="SMTP host")


def _check_ceilings(changes: Mapping[str, Any], operator: ResolvedSettings) -> None:
    for field in CEILING_FIELDS:
        value = changes.get(field)
        if value is None:
            continue
        ceiling = int(operator.values[field])
        if int(value) > ceiling:
            raise HTTPException(
                status_code=422,
                detail=f"{field} may not exceed the operator's limit of {ceiling}.",
            )


async def validate_org_changes(
    session: AsyncSession, scope: uuid.UUID, changes: Mapping[str, Any]
) -> None:
    """The save-time checks of an ORGANIZATION scope write (not the operator alias).

    Hosts must be public in every organization scope, whatever the deployment
    mode: on a self-hosted instance only the default organization is the
    operator's own team (and it is the operator scope, never checked here); any
    other organization is a tenant as on a hosted instance.
    """
    operator = app_settings_service.resolve_settings(
        await app_settings_service.get_service_overrides(session), None
    )
    _check_ceilings(changes, operator)
    try:
        await asyncio.to_thread(_check_public_hosts, changes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not frozenset(EMBEDDING_FIELDS).isdisjoint(changes):
        await _check_embedding_dimensions(session, scope, changes)


async def _check_embedding_dimensions(
    session: AsyncSession, scope: uuid.UUID, changes: Mapping[str, Any]
) -> None:
    """422 unless the organization's own embedding model fits the index (F20 PR10).

    Resolves what the organization WOULD run with after this save and, when
    that is its own endpoint with embeddings on, embeds one short text with it.
    ``search_embedding_dimensions`` is the operator's (vector(1536) for
    everyone): a model answering with another width, or not answering, would
    leave every document ``failed``. An organization with embeddings off is not
    checked. One that inherits the operator's endpoint is not probed over HTTP
    (the operator's infrastructure), but switching embeddings on still needs a
    config that can embed at all — a supported provider and a key — or its
    whole corpus would go ``pending`` and be chased every beat for nothing.
    """
    prospective = app_settings_service.apply_org_override_changes(
        await app_settings_service.get_org_overrides(session, scope), changes
    )
    resolved = app_settings_service.resolve_settings(
        await app_settings_service.get_service_overrides(session), prospective, org_scope=scope
    )
    config = app_settings_service.ai_config_for(resolved)
    if not config.search_embeddings_enabled:
        return
    if not any(field in prospective for field in EMBEDDING_GROUP):
        if not embedding_service.can_embed(config):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Semantic search cannot be switched on with the inherited embedding "
                    "settings (no API key or an unsupported provider): set this "
                    "organization's own endpoint, model and key."
                ),
            )
        return
    problem = await asyncio.to_thread(embedding_service.probe_embedding_dimensions, config)
    if problem is not None:
        raise HTTPException(status_code=422, detail=problem)


async def embedding_identity(session: AsyncSession, org_id: uuid.UUID) -> tuple[bool, str]:
    """Whether an organization's semantic search is on, and in which vector space."""
    config = await app_settings_service.get_embedding_config(session, org_id=org_id)
    return config.search_embeddings_enabled, embedding_service.embedding_provenance(config)


async def enqueue_org_search_reindex(org_id: uuid.UUID) -> bool:
    """Queue the reindex of ONE organization's projects; never raises."""
    try:
        from tripl.worker.celery_app import celery_app

        await dispatch(celery_app.send_task, ORG_REINDEX_TASK, args=[str(org_id)])
    except Exception:
        logger.exception("Failed to queue the search reindex of organization %s", org_id)
        return False
    return True


async def reindex_if_embeddings_moved(
    session: AsyncSession, org_id: uuid.UUID, before: tuple[bool, str] | None
) -> bool:
    """Enqueue ``org_id``'s reindex when its embedding identity differs from ``before``.

    ``before`` is ``None`` when the save did not touch an embedding field.
    """
    if before is None:
        return False
    if await embedding_identity(session, org_id) == before:
        return False
    return await enqueue_org_search_reindex(org_id)


def touches_embeddings(changes: Mapping[str, Any]) -> bool:
    return not frozenset(EMBEDDING_FIELDS).isdisjoint(changes)


async def write_org_changes(
    session: AsyncSession,
    org_id: uuid.UUID,
    changes: dict[str, Any],
    *,
    is_platform_admin: bool,
) -> uuid.UUID | None:
    """Save ``changes`` (ORG_FIELDS only) for ``org_id``; returns the scope written.

    ``None`` means the operator document: a self-hosted default organization,
    whose credential groups only a platform admin may write, and whose embedding
    endpoint is ``SEARCH_EMBEDDING_BASE_URL`` (422). A save that moves the
    organization's embedding identity queues the reindex of its projects.
    """
    unknown = sorted(set(changes) - ORG_FIELDS)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail="Not an organization setting: " + ", ".join(unknown),
        )
    scope = app_settings_service.settings_scope_for(org_id)
    before = await embedding_identity(session, org_id) if touches_embeddings(changes) else None
    if scope is None:
        if "search_embedding_base_url" in changes:
            raise HTTPException(status_code=422, detail=OPERATOR_EMBEDDING_URL_READ_ONLY)
        require_operator_credential_writer(changes, is_platform_admin=is_platform_admin)
        await app_settings_service.update_service_overrides(session, changes)
    else:
        await validate_org_changes(session, scope, changes)
        await app_settings_service.update_org_overrides(session, scope, changes)
    await reindex_if_embeddings_moved(session, org_id, before)
    return scope


def audit_scope_payload(changed: list[str], scope: uuid.UUID | None) -> dict[str, Any]:
    """The ``settings.update`` audit payload: what changed, and in which scope."""
    if scope is None:
        return {"changed_fields": changed, "scope": "platform"}
    return {"changed_fields": changed, "scope": "organization", "organization_id": str(scope)}
