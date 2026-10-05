"""A project's audit history: the Audit tab of its settings.

Org owner/admin only (``get_owner_user``, a browser session), like every read
of the log: an entry carries the request payload that produced it, and some
payloads hold what other routes blank for non-admins (a warehouse's host and
username on ``data_source.*``, the SQL of ``scan_config.create``).

Scoped to the project in the path and to the request's organization. The
organization-wide log, its search across projects, export and the audit
webhook are part of the Enterprise edition.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from tripl.api.deps import SessionDep, get_owner_user
from tripl.schemas.audit import AuditActionCatalog, AuditEntryDetailResponse, AuditListResponse
from tripl.schemas.text_filters import FreeTextFilter
from tripl.services import audit_actions, audit_service

router = APIRouter(
    prefix="/projects/{slug}/audit", tags=["audit"], dependencies=[Depends(get_owner_user)]
)


@router.get("", response_model=AuditListResponse)
async def list_project_audit(
    session: SessionDep,
    slug: str,
    action: Annotated[FreeTextFilter | None, Query()] = None,
    user_id: Annotated[uuid.UUID | None, Query()] = None,
    # FreeTextFilter: binds into a LIKE, so a NUL aborts inside asyncpg before
    # SQL runs.
    user_email: Annotated[FreeTextFilter | None, Query()] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AuditListResponse:
    """The project's entries, newest first. A deleted project's rows stay
    reachable by its last slug while no live project answers to it."""
    return await audit_service.list_entries(
        session,
        project_slug=slug,
        action=action,
        user_id=user_id,
        user_email=user_email,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )


# Declared before ``/{entry_id}``, which would otherwise claim the segment and
# answer 422.
@router.get("/actions", response_model=AuditActionCatalog)
async def list_project_audit_actions(slug: str) -> AuditActionCatalog:
    """Every action the log records, grouped for the filter; see audit_actions.
    A project's entries carry the ``project`` group's actions."""
    return audit_actions.action_catalog()


@router.get("/{entry_id}", response_model=AuditEntryDetailResponse)
async def get_project_audit_entry(
    session: SessionDep, slug: str, entry_id: uuid.UUID
) -> AuditEntryDetailResponse:
    """One entry with its payload. 404 for an entry of another project."""
    entry = await audit_service.get_entry(session, entry_id, project_slug=slug)
    if entry is None:
        raise HTTPException(status_code=404, detail="Audit entry not found")
    return entry
