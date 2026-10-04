from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from tripl.api.deps import OwnerUserDep, SessionDep, get_owner_user
from tripl.middleware.org_context import current_org, require_org_id
from tripl.middleware.rate_limit import audit_export_rate_limiter, enforce
from tripl.schemas.audit import AuditActionCatalog, AuditEntryDetailResponse, AuditListResponse
from tripl.schemas.text_filters import FreeTextFilter
from tripl.services import audit_actions, audit_export_service, audit_service

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
# exposed; audit_service._redact strips them.
router = APIRouter(prefix="/audit", tags=["audit"], dependencies=[Depends(get_owner_user)])


@router.get("", response_model=AuditListResponse)
async def list_audit(
    session: SessionDep,
    project_slug: Annotated[FreeTextFilter | None, Query()] = None,
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


# Declared before ``/{entry_id}`` for the same reason. Same gate as the feed
# (org owner/admin, browser session, API keys refused): the file carries every
# payload the feed does, for up to a year at once.
@router.get(
    "/export",
    response_class=StreamingResponse,
    # A year-long export reads for a while: its own small bucket.
    dependencies=[Depends(enforce(audit_export_rate_limiter))],
    responses={
        200: {
            "description": "The rows, oldest first: CSV (a header row) or NDJSON.",
            "content": {
                "text/csv": {"schema": {"type": "string"}},
                "application/x-ndjson": {"schema": {"type": "string"}},
            },
        }
    },
)
async def export_audit(
    session: SessionDep,
    current_user: OwnerUserDep,
    export_format: Annotated[audit_export_service.ExportFormat, Query(alias="format")] = "csv",
    start: Annotated[datetime | None, Query(alias="from")] = None,
    end: Annotated[datetime | None, Query(alias="to")] = None,
    action: Annotated[FreeTextFilter | None, Query()] = None,
) -> StreamingResponse:
    """Stream the organization's audit log for ``[from, to)`` (UTC; default the
    last 30 days, at most 366). ``to`` is exclusive: to include a whole last
    day, send the day after it. Rows of the organization and of its projects;
    never platform-scope rows. 422 for an empty, reversed or too wide range."""
    try:
        span = audit_export_service.resolve_range(start, end, now=datetime.now(UTC))
    except audit_export_service.ExportRangeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from None
    org_id = require_org_id()
    org = current_org()
    org_slug = org.slug if org is not None else ""
    # Recorded at the start: how many rows the file holds is not known yet.
    await audit_service.record(
        session,
        user=current_user,
        action="org.audit_export",
        target_type="organization",
        target_id=org_id,
        target_name=org_slug,
        payload={
            "format": export_format,
            "from": span.start.isoformat(),
            "to": span.end.isoformat(),
            "action": action or "",
        },
    )
    name = audit_export_service.filename(org_slug, span, export_format)
    return StreamingResponse(
        audit_export_service.stream(
            session,
            org_id=org_id,
            org_slug=org_slug,
            span=span,
            fmt=export_format,
            action=action,
        ),
        media_type=audit_export_service.MEDIA_TYPES[export_format],
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
        },
    )


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
