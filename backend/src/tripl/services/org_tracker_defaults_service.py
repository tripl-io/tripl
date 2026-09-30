"""An organization's issue-tracker defaults, and how a project's config overrides them (F20 PR12).

An organization owner/admin sets Jira and Linear defaults once — the Jira site,
the account and API token it authenticates with, a default Jira project; a
Linear API key and default team — and every project of the organization uses
them unless its own ``project_tracker_config`` says otherwise. A project still
opts in itself (``enabled``, ``tracker_type`` stay per project): an
organization default never starts creating tickets for a project nobody wired.

Stored as one ``app_settings`` document per organization
(key :data:`~tripl.models.app_setting.TRACKER_DEFAULTS_KEY`), secrets
encrypted like every other tracker credential and never returned. There is no
operator layer beneath it: a self-hosted default organization stores its own
document like any other.

Resolution (:func:`effective_jira`, :func:`effective_linear`), per field:

* the project's value when it has one, else the organization's default;
* EXCEPT the Jira endpoint group — site URL, account e-mail, API token — which
  is one unit (critique #13): a project that names its own Jira site or account
  never has the organization's token sent there. Setting any of the three in
  the project makes all three the project's.
* Linear's host is fixed, so its key and team resolve independently.

Every organization value is checked on save (the Jira site through
``validate_jira_base_url``, which refuses private addresses) and again right
before each outbound call by the worker, as a project's own values are.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from typing import Any, Literal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from tripl import crypto
from tripl.alerting_validation import (
    validate_jira_api_token,
    validate_jira_auth_email,
    validate_jira_base_url,
    validate_jira_project_key,
    validate_linear_api_key,
    validate_linear_team_id,
)
from tripl.models.app_setting import TRACKER_DEFAULTS_KEY, AppSetting
from tripl.models.project import Project
from tripl.models.project_tracker_config import ProjectTrackerConfig

logger = logging.getLogger(__name__)

TRACKER_LINEAR = "linear"

#: Where an effective value came from.
TrackerSource = Literal["project", "org", "default"]

JIRA_FIELDS: tuple[str, ...] = (
    "jira_base_url",
    "jira_auth_email",
    "jira_api_token",
    "jira_project_key",
)
LINEAR_FIELDS: tuple[str, ...] = ("linear_api_key", "linear_team_id")
TRACKER_DEFAULT_FIELDS: frozenset[str] = frozenset((*JIRA_FIELDS, *LINEAR_FIELDS))
SECRET_FIELDS: frozenset[str] = frozenset({"jira_api_token", "linear_api_key"})

#: Pure string checks; the Jira site is checked separately (it resolves DNS).
_VALIDATORS: dict[str, Callable[[str | None], str]] = {
    "jira_auth_email": validate_jira_auth_email,
    "jira_api_token": validate_jira_api_token,
    "jira_project_key": validate_jira_project_key,
    "linear_api_key": validate_linear_api_key,
    "linear_team_id": validate_linear_team_id,
}


@dataclass(frozen=True)
class OrgTrackerDefaults:
    """An organization's defaults, secrets decrypted. Empty means "not set"."""

    jira_base_url: str = ""
    jira_auth_email: str = ""
    jira_api_token: str = ""
    jira_project_key: str = ""
    linear_api_key: str = ""
    linear_team_id: str = ""


NO_DEFAULTS = OrgTrackerDefaults()


def _decrypt(field: str, value: Any) -> str:
    try:
        return crypto.decrypt_value(str(value))
    except crypto.InvalidToken:
        logger.warning("Cannot decrypt the stored %s tracker default; ignoring it", field)
        return ""


def defaults_from_stored(stored: Mapping[str, Any]) -> OrgTrackerDefaults:
    """The decrypted defaults of a stored document (unknown keys ignored)."""
    values: dict[str, str] = {}
    for field in TRACKER_DEFAULT_FIELDS:
        raw = stored.get(field)
        if not raw:
            continue
        values[field] = _decrypt(field, raw) if field in SECRET_FIELDS else str(raw)
    return OrgTrackerDefaults(**values)


def _document(org_id: uuid.UUID) -> Any:
    return select(AppSetting).where(
        AppSetting.key == TRACKER_DEFAULTS_KEY, AppSetting.organization_id == org_id
    )


def _stored(row: AppSetting | None) -> dict[str, Any]:
    if row is None or not isinstance(row.value, dict):
        return {}
    return {k: v for k, v in row.value.items() if k in TRACKER_DEFAULT_FIELDS}


async def get_stored_defaults(session: AsyncSession, org_id: uuid.UUID) -> dict[str, Any]:
    """The raw stored document (secrets still encrypted)."""
    return _stored(await session.scalar(_document(org_id)))


async def get_org_tracker_defaults(session: AsyncSession, org_id: uuid.UUID) -> OrgTrackerDefaults:
    return defaults_from_stored(await get_stored_defaults(session, org_id))


def _project_org_stmt(project_id: uuid.UUID) -> Any:
    return select(Project.organization_id).where(Project.id == project_id)


async def defaults_for_project(session: AsyncSession, project_id: uuid.UUID) -> OrgTrackerDefaults:
    """The defaults of the organization owning ``project_id``, read from the database.

    Fails CLOSED: an unreadable document means no defaults, so the project runs
    on its own values alone (and skips when they are incomplete).
    """
    try:
        org_id = await session.scalar(_project_org_stmt(project_id))
        if org_id is None:
            return NO_DEFAULTS
        return await get_org_tracker_defaults(session, org_id)
    except Exception:
        logger.warning("Tracker defaults unreadable for project %s; using none", project_id)
        return NO_DEFAULTS


def defaults_for_project_sync(session: Session, project_id: uuid.UUID) -> OrgTrackerDefaults:
    """Sync twin of :func:`defaults_for_project` (the scan's ticket comment path)."""
    try:
        org_id = session.scalar(_project_org_stmt(project_id))
        if org_id is None:
            return NO_DEFAULTS
        return defaults_from_stored(_stored(session.scalar(_document(org_id))))
    except Exception:
        logger.warning("Tracker defaults unreadable for project %s; using none", project_id)
        return NO_DEFAULTS


# ── resolution ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EffectiveJira:
    """What an outbound Jira call uses, before validation. Secrets in clear."""

    base_url: str
    auth_email: str
    api_token: str
    project_key: str
    issue_type: str
    sources: dict[str, TrackerSource]


@dataclass(frozen=True)
class EffectiveLinear:
    api_key: str
    team_id: str
    sources: dict[str, TrackerSource]


def _project_token(config: ProjectTrackerConfig) -> str:
    if not config.api_token_encrypted:
        return ""
    return _decrypt("project api_token", config.api_token_encrypted)


def _pick(own: str, default: str) -> tuple[str, TrackerSource]:
    if own:
        return own, "project"
    if default:
        return default, "org"
    return "", "default"


def effective_jira(config: ProjectTrackerConfig, defaults: OrgTrackerDefaults) -> EffectiveJira:
    """The Jira settings a project runs with: its own, else its organization's.

    The endpoint group (site, account, token) is taken whole from ONE side: the
    project's when it set any member, else the organization's.
    """
    own_token = _project_token(config)
    project_group = bool(config.base_url or config.auth_email or config.api_token_encrypted)
    sources: dict[str, TrackerSource] = {}
    if project_group:
        base_url, auth_email, api_token = config.base_url, config.auth_email, own_token
        for field in ("base_url", "auth_email", "api_token"):
            sources[field] = "project"
    else:
        base_url = defaults.jira_base_url
        auth_email = defaults.jira_auth_email
        api_token = defaults.jira_api_token
        for field, value in (
            ("base_url", base_url),
            ("auth_email", auth_email),
            ("api_token", api_token),
        ):
            sources[field] = "org" if value else "default"
    project_key, sources["project_key"] = _pick(config.project_key, defaults.jira_project_key)
    return EffectiveJira(
        base_url=base_url,
        auth_email=auth_email,
        api_token=api_token,
        project_key=project_key,
        issue_type=config.issue_type or "Task",
        sources=sources,
    )


def effective_linear(config: ProjectTrackerConfig, defaults: OrgTrackerDefaults) -> EffectiveLinear:
    """The Linear key and team a project runs with (the team rides ``project_key``)."""
    sources: dict[str, TrackerSource] = {}
    api_key, sources["api_token"] = _pick(_project_token(config), defaults.linear_api_key)
    team_id, sources["team_id"] = _pick(config.project_key, defaults.linear_team_id)
    return EffectiveLinear(api_key=api_key, team_id=team_id, sources=sources)


def inherited_fields(config: ProjectTrackerConfig, defaults: OrgTrackerDefaults) -> list[str]:
    """The project-config fields whose effective value is the organization's default."""
    if config.tracker_type == TRACKER_LINEAR:
        sources: Mapping[str, TrackerSource] = effective_linear(config, defaults).sources
    else:
        sources = effective_jira(config, defaults).sources
    return sorted(field for field, source in sources.items() if source == "org")


# ── the organization's view and write ───────────────────────────────────────


def payload(org_slug: str, defaults: OrgTrackerDefaults) -> dict[str, Any]:
    """The body of ``OrgTrackerDefaultsResponse``; secrets only as ``*_configured``."""
    sources: dict[str, str] = {}
    for field in fields(OrgTrackerDefaults):
        section, name = field.name.split("_", 1)
        sources[f"{section}.{name}"] = "org" if getattr(defaults, field.name) else "default"
    return {
        "organization": org_slug,
        "jira": {
            "base_url": defaults.jira_base_url,
            "auth_email": defaults.jira_auth_email,
            "api_token_configured": bool(defaults.jira_api_token),
            "project_key": defaults.jira_project_key,
        },
        "linear": {
            "api_key_configured": bool(defaults.linear_api_key),
            "team_id": defaults.linear_team_id,
        },
        "sources": sources,
    }


def _validated(field: str, value: str) -> str:
    """Normalized ``value`` (blocking for the Jira site: DNS); ValueError when bad."""
    if field == "jira_base_url":
        return validate_jira_base_url(value)
    return _VALIDATORS[field](value)


async def update_org_tracker_defaults(
    session: AsyncSession, org_id: uuid.UUID, changes: Mapping[str, Any]
) -> list[str]:
    """Apply ``changes`` (flattened ``jira_*`` / ``linear_*``); returns the fields changed.

    ``None`` or ``""`` clears a field (the organization then has no default for
    it); anything else is validated — the Jira site refused when private — and
    stored, secrets encrypted. 422 on the first invalid value, nothing written.
    """
    unknown = sorted(set(changes) - TRACKER_DEFAULT_FIELDS)
    if unknown:
        raise HTTPException(status_code=422, detail="Not a tracker default: " + ", ".join(unknown))
    row = await session.scalar(_document(org_id))
    stored = _stored(row)
    for field in sorted(changes):
        value = changes[field]
        if value is None or str(value).strip() == "":
            stored.pop(field, None)
            continue
        try:
            if field == "jira_base_url":
                normalized = await asyncio.to_thread(_validated, field, str(value))
            else:
                normalized = _validated(field, str(value))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        stored[field] = crypto.encrypt_value(normalized) if field in SECRET_FIELDS else normalized
    if row is None:
        session.add(AppSetting(key=TRACKER_DEFAULTS_KEY, value=stored, organization_id=org_id))
    else:
        row.value = stored
    await session.commit()
    return sorted(changes)
