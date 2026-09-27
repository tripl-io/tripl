from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from tripl.api.deps import SessionDep, get_owner_user
from tripl.schemas.audit import AuditActionCatalog, AuditEntryDetailResponse, AuditListResponse
from tripl.schemas.text_filters import FreeTextFilter
from tripl.services import audit_actions, audit_service

# Org owner/admin-only (``get_owner_user``, a browser session): this feed was the
# back door around two other owner-only gates.
#
# Every entry carries the request payload that produced it — since tripl-5ydt on
# ``GET /audit/{entry_id}`` alone, not on every list row — and the router had
# nothing but the shared auth dependency, so any authenticated user could read:
#
#   * ``data_source.create`` / ``.update`` payloads — the warehouse host, port,
#     database and username that data_sources.py:69 deliberately BLANKS for
#     non-admins on every direct read of the same source;
#   * ``scan_config.create`` payloads — ``base_query``, the free-text SQL that
#     authoring a scan is org owner/admin-only to protect (api/v1/scans.py:36), since it
#     reads whatever the warehouse credential can.
#
# ``project_slug`` is a filter, not a scope, so the reach is every project of
# the organization. The feed IS scoped to the request's organization (F20 PR4):
# list and detail read only rows whose ``organization_id`` is the bound org, so
# one organization's admin never reads another's payloads. Passwords were never
# exposed; audit_service._redact strips them (tripl-jfm3.110).
router = APIRouter(prefix="/audit", tags=["audit"], dependencies=[Depends(get_owner_user)])


@router.get("", response_model=AuditListResponse)
async def list_audit(
    session: SessionDep,
    project_slug: Annotated[FreeTextFilter | None, Query()] = None,
    action: Annotated[FreeTextFilter | None, Query()] = None,
    user_id: Annotated[uuid.UUID | None, Query()] = None,
    # FreeTextFilter: binds into a LIKE, so a NUL aborts inside asyncpg before
    # SQL runs (tripl-8wez).
    user_email: Annotated[FreeTextFilter | None, Query()] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AuditListResponse:
    return await audit_service.list_entries(
        session,
        project_slug=project_slug,
        action=action,
        user_id=user_id,
        user_email=user_email,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )


# Declared before ``/{entry_id}``, which would otherwise claim the segment and
# answer 422 for a UUID that does not parse. Same owner gate as the log itself.
@router.get("/actions", response_model=AuditActionCatalog)
async def list_audit_actions() -> AuditActionCatalog:
    """Every action the log records, grouped for the filter; see audit_actions."""
    return audit_actions.action_catalog()


# The payload half of an entry, which the list rows no longer carry. Owner-only
# by the router dependency above, i.e. by the same gate as the list: the fields
# quoted there (warehouse host/port/database/username, ``base_query``) live in
# the payload, so this route is the one that actually hands them out.
@router.get("/{entry_id}", response_model=AuditEntryDetailResponse)
async def get_audit_entry(
    session: SessionDep,
    entry_id: uuid.UUID,
) -> AuditEntryDetailResponse:
    entry = await audit_service.get_entry(session, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Audit entry not found")
    return entry
