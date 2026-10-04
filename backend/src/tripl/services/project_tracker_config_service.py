from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.alerting_validation import (
    validate_jira_api_token,
    validate_jira_auth_email,
    validate_jira_base_url,
    validate_jira_issue_type,
    validate_jira_project_key,
    validate_linear_api_key,
    validate_linear_team_id,
)
from tripl.crypto import encrypt_value
from tripl.models.project_tracker_config import ProjectTrackerConfig
from tripl.schemas.project_tracker_config import (
    ProjectTrackerConfigResponse,
    ProjectTrackerConfigUpdate,
)
from tripl.services.org_tracker_defaults_service import (
    NO_DEFAULTS,
    OrgTrackerDefaults,
    defaults_for_project,
    inherited_fields,
)
from tripl.services.project_lookup import resolve_project_id

DEFAULT_ENABLED = False
DEFAULT_TRACKER_TYPE = "jira"
TRACKER_JIRA = "jira"
TRACKER_LINEAR = "linear"
# Jira-only fields: ignored while the project is on Linear. ``project_key`` is
# among them because the Linear team id is stored in that column (see
# ``_to_response``), so a stray Jira key must not overwrite it.
_JIRA_ONLY_FIELDS = frozenset({"base_url", "project_key", "auth_email", "issue_type"})
DEFAULT_ISSUE_TYPE = "Task"

# Jira scalar fields validated on update when a non-null value is supplied. The
# validators normalize (uppercase the key, ...) and raise ValueError on bad
# input, which we surface as HTTP 422.
#
# Two fields are deliberately absent. ``api_token`` because it is encrypted at
# rest and never echoed back, so it needs its own arm below. ``base_url``
# because it is the only validator here that touches the NETWORK: everything in
# this map is pure string work, and keeping the one blocking validator out of it
# is what makes the loop below safe to run inline. See its branch for the rest.
_JIRA_FIELD_VALIDATORS: dict[str, Callable[[str | None], str]] = {
    "auth_email": validate_jira_auth_email,
    "project_key": validate_jira_project_key,
    "issue_type": validate_jira_issue_type,
}


def _to_response(
    config: ProjectTrackerConfig, defaults: OrgTrackerDefaults = NO_DEFAULTS
) -> ProjectTrackerConfigResponse:
    """Explicit build — ``api_token_set`` is derived, so ``model_validate`` from
    the ORM row would miss it (and we must never surface the token itself).

    ``inherited_fields`` names what the project takes from its organization's
    tracker defaults (F20 PR12) because it left the field empty."""
    is_linear = config.tracker_type == TRACKER_LINEAR
    return ProjectTrackerConfigResponse(
        id=config.id,
        project_id=config.project_id,
        enabled=config.enabled,
        tracker_type=config.tracker_type,
        base_url=config.base_url,
        # A Linear config keeps its team id in the ``project_key`` column — one
        # per-project "where do tickets go" slot, no second column for it — so
        # the response splits the column by tracker rather than showing a team
        # id as a Jira key.
        project_key="" if is_linear else config.project_key,
        auth_email=config.auth_email,
        issue_type=config.issue_type,
        team_id=config.project_key if is_linear else "",
        api_token_set=bool(config.api_token_encrypted),
        inherited_fields=inherited_fields(config, defaults),
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


def _defaults_response(
    project_id: uuid.UUID, defaults: OrgTrackerDefaults = NO_DEFAULTS
) -> ProjectTrackerConfigResponse:
    blank = ProjectTrackerConfig(
        project_id=project_id,
        enabled=DEFAULT_ENABLED,
        tracker_type=DEFAULT_TRACKER_TYPE,
        base_url="",
        project_key="",
        auth_email="",
        api_token_encrypted="",
        issue_type=DEFAULT_ISSUE_TYPE,
    )
    return ProjectTrackerConfigResponse(
        project_id=project_id,
        enabled=DEFAULT_ENABLED,
        tracker_type=DEFAULT_TRACKER_TYPE,
        base_url="",
        project_key="",
        auth_email="",
        issue_type=DEFAULT_ISSUE_TYPE,
        api_token_set=False,
        inherited_fields=inherited_fields(blank, defaults),
    )


async def _ensure_config(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> ProjectTrackerConfig:
    config = await session.scalar(
        select(ProjectTrackerConfig).where(ProjectTrackerConfig.project_id == project_id)
    )
    if config is not None:
        return config

    config = ProjectTrackerConfig(project_id=project_id)
    session.add(config)
    try:
        await session.commit()
    except IntegrityError:
        # Lost a concurrent first-write race on uq_project_tracker_config_project;
        # the winner's row is what we want.
        await session.rollback()
        winner: ProjectTrackerConfig | None = await session.scalar(
            select(ProjectTrackerConfig).where(ProjectTrackerConfig.project_id == project_id)
        )
        if winner is None:  # pragma: no cover — row vanished between commit and re-read
            raise
        return winner
    await session.refresh(config)
    return config


async def get_project_tracker_config(
    session: AsyncSession,
    slug: str,
) -> ProjectTrackerConfigResponse:
    """Read-only: projects that never configured a tracker get the defaults back
    without a row being written (GETs must not mutate the database)."""
    project_id = await resolve_project_id(session, slug)
    config = await session.scalar(
        select(ProjectTrackerConfig).where(ProjectTrackerConfig.project_id == project_id)
    )
    defaults = await defaults_for_project(session, project_id)
    if config is None:
        return _defaults_response(project_id, defaults)
    return _to_response(config, defaults)


async def update_project_tracker_config(
    session: AsyncSession,
    slug: str,
    data: ProjectTrackerConfigUpdate,
) -> ProjectTrackerConfigResponse:
    project_id = await resolve_project_id(session, slug)
    config = await _ensure_config(session, project_id)
    payload = data.model_dump(exclude_unset=True)

    # Resolve the tracker FIRST: it decides how the token and the destination
    # fields below are validated and where they are stored.
    previous_tracker = config.tracker_type or DEFAULT_TRACKER_TYPE
    requested_tracker = payload.pop("tracker_type", None)
    tracker = requested_tracker or previous_tracker
    team_id = payload.pop("team_id", None)
    if tracker != previous_tracker:
        # Switching vendors drops the stored credential and destination: a Jira
        # token must never be sent to Linear or the other way round, and a Jira
        # project key is not a Linear team. A new token in this same request is
        # applied just below.
        config.api_token_encrypted = ""
        config.project_key = ""
        config.tracker_type = tracker

    # api_token is encrypted at rest and never stored raw. ""/clears the token;
    # None/omitted leaves it unchanged; a real value is validated then encrypted.
    token_validator = (
        validate_linear_api_key if tracker == TRACKER_LINEAR else validate_jira_api_token
    )
    if "api_token" in payload:
        raw_token = payload.pop("api_token")
        if raw_token is not None:
            if raw_token == "":
                config.api_token_encrypted = ""
            else:
                config.api_token_encrypted = encrypt_value(_validate(token_validator, raw_token))

    if tracker == TRACKER_LINEAR:
        # Linear talks to its fixed public API host, so none of the Jira fields
        # (base_url in particular, the one with an SSRF check) take effect.
        for key in _JIRA_ONLY_FIELDS:
            payload.pop(key, None)
        if team_id is not None:
            config.project_key = (
                "" if team_id == "" else _validate(validate_linear_team_id, team_id)
            )

    for key, value in payload.items():
        if value is None:
            # An explicit JSON null is not a valid value for a NOT NULL column —
            # treat it the same as omitting the field.
            continue
        if key == "base_url":
            # ``validate_jira_base_url`` ends in ``reject_private_host`` ->
            # ``socket.getaddrinfo``, a blocking resolver call. This function is
            # ``async`` and runs on the request's event loop, so calling it
            # inline — as it was, through the map above — held the loop, and
            # with it every other request on this uvicorn worker, for however
            # long the lookup took. The whole validator is
            # offloaded rather than split: unlike the destination schemas, there
            # is no pydantic layer here that wants the syntactic half earlier.
            value = await _validate_async(validate_jira_base_url, value)
        else:
            validator = _JIRA_FIELD_VALIDATORS.get(key)
            if validator is not None:
                value = _validate(validator, value)
        setattr(config, key, value)

    await session.commit()
    await session.refresh(config)
    return _to_response(config, await defaults_for_project(session, project_id))


def _validate(validator: Callable[[str | None], str], value: str) -> str:
    try:
        return validator(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _validate_async(validator: Callable[[str | None], str], value: str) -> str:
    """``_validate`` for a validator that does network IO.

    ``asyncio.to_thread`` keeps a blocking resolver off the request's event
    loop. The ValueError -> 422 translation is character-for-character the one
    above, so moving a validator between the two changes where it runs and
    nothing a caller can observe — including the normalization it returns.
    """
    try:
        return await asyncio.to_thread(validator, value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
