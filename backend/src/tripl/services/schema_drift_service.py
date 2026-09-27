"""Read/write helpers for SchemaDrift records."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from tripl import cache
from tripl.core.warehouse_types import is_complex_type
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.schema_drift import SchemaDrift
from tripl.models.user import User
from tripl.schemas.schema_drift import (
    SchemaDriftActionRequest,
    SchemaDriftListResponse,
    SchemaDriftResponse,
)
from tripl.services.project_lookup import resolve_project_id
from tripl.services.scan_config_lookup import (
    name_format_conflict_detail,
    scan_configs_blocking_field_removal,
)
from tripl.services.search_service import reindex_project_branch

# Drift rows older than this are filtered out at read time. The writer
# upserts on (event_type_id, field_name, drift_type) so a drift that
# clears naturally just stops being refreshed and ages out of view.
DRIFT_RETENTION_DAYS = 30
ACTIVE_DRIFT_STATUSES = {"open", "snoozed"}


def _retention_cutoff(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now - timedelta(days=DRIFT_RETENTION_DAYS)


def _active_drift_predicates(now: datetime) -> list[ColumnElement[bool]]:
    return [
        SchemaDrift.status.in_(ACTIVE_DRIFT_STATUSES),
        (SchemaDrift.status != "snoozed")
        | (SchemaDrift.snoozed_until.is_(None))
        | (SchemaDrift.snoozed_until <= now),
    ]


# Public names for readers outside this module (the health score, F15 #268):
# they must judge "active" exactly as the drift lists do.
retention_cutoff = _retention_cutoff
active_drift_predicates = _active_drift_predicates


def _logical_type_from_observed(observed_type: str | None) -> str:
    # Match the detector's `_infer_logical_field_type`: accepts must write the
    # same auto-managed type that a fresh scan would infer from this column.
    return "json" if is_complex_type(observed_type or "") else "string"


async def _reject_if_name_format_needs(
    session: AsyncSession,
    *,
    event_type: EventType,
    field_name: str,
) -> None:
    """409 when deleting this field would leave a scan unable to name its events.

    Accepting a ``missing_field`` drift deletes the FieldDefinition, and
    ``generate_events`` assembles its format arguments ONLY from columns that
    still have one. So accepting a drift on a column a scan's
    ``event_name_format`` references kills every subsequent collection with
    "the event name format references unknown keys" — which is what took
    'Old events (iOS)' down for four days (tripl-3mmh, root cause of tripl-lpin),
    on a drift that was very likely a false positive.

    Refusing does not strand an operator whose column really did vanish: the
    scan is already dead in that case, because the query cannot supply a value
    the format needs whether or not the field exists. The only repair in both
    worlds is to edit the scan's event name format first, so the 409 is a
    redirect to the one action that works, not a wall. ``force`` covers the
    residual over-fire (a project-wide config that names the column but never
    scans this event type) and requires a note.
    """
    blocking = await scan_configs_blocking_field_removal(
        session,
        project_id=event_type.project_id,
        event_type_id=event_type.id,
        field_name=field_name,
    )
    if not blocking:
        return
    raise HTTPException(
        status_code=409,
        detail=name_format_conflict_detail(
            field_name=field_name,
            configs=blocking,
            lead="Cannot accept this drift: accepting deletes the field.",
            then="accept the drift",
        ),
    )


async def _apply_acceptance_to_plan(
    session: AsyncSession,
    *,
    drift: SchemaDrift,
    event_type: EventType,
    force: bool,
) -> bool:
    field = await session.scalar(
        select(FieldDefinition).where(
            FieldDefinition.event_type_id == event_type.id,
            FieldDefinition.name == drift.field_name,
        )
    )
    if drift.drift_type == "new_field":
        if field is not None:
            return False
        next_order = await session.scalar(
            select(func.max(FieldDefinition.order)).where(
                FieldDefinition.event_type_id == event_type.id
            )
        )
        session.add(
            FieldDefinition(
                event_type_id=event_type.id,
                name=drift.field_name,
                display_name=drift.field_name,
                field_type=_logical_type_from_observed(drift.observed_type),
                is_required=False,
                description="Accepted from schema drift",
                order=(next_order or 0) + 1,
            )
        )
        return True
    if drift.drift_type == "type_changed" and field is not None:
        field.field_type = _logical_type_from_observed(drift.observed_type)
        return True
    if drift.drift_type == "missing_field" and field is not None:
        # Guard here, not at the route: this is the exact point where the action
        # is accept, the drift is missing_field, and a FieldDefinition really
        # exists to delete. A missing_field drift whose field is already gone
        # stays a no-op accept rather than becoming a 409 (tripl-3mmh).
        if not force:
            await _reject_if_name_format_needs(
                session, event_type=event_type, field_name=drift.field_name
            )
        await session.delete(field)
        return True
    return False


async def list_drifts_for_event_type(
    session: AsyncSession,
    slug: str,
    event_type_id: uuid.UUID,
) -> SchemaDriftListResponse:
    project_id = await resolve_project_id(session, slug, detail=f"Project '{slug}' not found")

    event_type = await session.get(EventType, event_type_id)
    if event_type is None or event_type.project_id != project_id:
        raise HTTPException(status_code=404, detail="Event type not found")

    cutoff = _retention_cutoff()
    rows = (
        (
            await session.execute(
                select(SchemaDrift)
                .where(
                    SchemaDrift.event_type_id == event_type_id,
                    SchemaDrift.detected_at >= cutoff,
                )
                .order_by(SchemaDrift.detected_at.desc(), SchemaDrift.field_name)
            )
        )
        .scalars()
        .all()
    )

    items = [SchemaDriftResponse.model_validate(row) for row in rows]
    return SchemaDriftListResponse(items=items, total=len(items))


async def apply_drift_action(
    session: AsyncSession,
    slug: str,
    drift_id: uuid.UUID,
    data: SchemaDriftActionRequest,
    user: User,
) -> SchemaDriftResponse:
    project_id = await resolve_project_id(session, slug, detail=f"Project '{slug}' not found")
    row = (
        await session.execute(
            select(SchemaDrift, EventType)
            .join(EventType, EventType.id == SchemaDrift.event_type_id)
            .where(
                SchemaDrift.id == drift_id,
                EventType.project_id == project_id,
            )
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Schema drift not found")

    drift, event_type = row
    now = datetime.now(UTC)
    plan_changed = False
    if data.action == "accept":
        plan_changed = await _apply_acceptance_to_plan(
            session,
            drift=drift,
            event_type=event_type,
            force=data.force,
        )
        drift.status = "accepted"
        drift.resolved_at = now
        drift.resolved_by = user.id
        drift.snoozed_until = None
    elif data.action == "snooze":
        drift.status = "snoozed"
        drift.snoozed_until = data.snoozed_until
        drift.resolved_at = None
        drift.resolved_by = user.id
    elif data.action == "false_positive":
        drift.status = "false_positive"
        drift.resolved_at = now
        drift.resolved_by = user.id
        drift.snoozed_until = None
    elif data.action == "reopen":
        drift.status = "open"
        drift.resolved_at = None
        drift.resolved_by = None
        drift.snoozed_until = None
    else:
        raise HTTPException(status_code=422, detail="Unsupported schema drift action")

    # SEPARATE DEFECT (tripl-3mmh, found while reading the accept path): ``note``
    # is optional on every action, so assigning it unconditionally erased a note
    # an earlier action had stored — snooze-with-note then reopen-without wiped
    # it. ``reopen`` still clears it: a reopened drift has no resolution any more.
    #
    # OMITTED and EXPLICITLY NULL are different requests, and only
    # ``model_fields_set`` can tell them apart — ``data.note is None`` collapses
    # both. Without the distinction the first fix took away the only way to CLEAR
    # a note without reopening the drift, so a wrong note was permanent.
    if data.action == "reopen":
        drift.resolution_note = None
    elif "note" in data.model_fields_set:
        drift.resolution_note = data.note
    await session.commit()
    await session.refresh(drift)

    if plan_changed:
        await reindex_project_branch(
            session,
            project_id=event_type.project_id,
            branch_id=event_type.branch_id,
            slug=slug,
        )
        # Read the row's OWN branch, mirroring ``field_service._on_main``, which
        # is the one other place that writes these same FieldDefinition rows.
        # The guard this replaces asked ``event_type.branch_id is None``, and
        # ``event_types.branch_id`` has been NOT NULL since 4e5f60718293 — the
        # guard predates branches (aeb15a8d) — so it never fired and accepting a
        # drift left the 300 s ``GET /event-types`` cache serving a field list
        # the accept had just changed (tripl-0zpq.222). Not invalidated
        # unconditionally: drift rows are only ever written against scanned
        # (main) event types today, but the explicit main check stays correct if
        # that changes.
        branch = await session.get(PlanBranch, event_type.branch_id)
        if branch is not None and branch.kind == BranchKind.main.value:
            await cache.delete_prefix(cache.prefix_event_types(event_type.project_id))
    return SchemaDriftResponse.model_validate(drift)


async def get_drift_counts_by_event_type(
    session: AsyncSession,
    project_id: uuid.UUID,
    event_type_ids: list[uuid.UUID],
) -> dict[uuid.UUID, int]:
    if not event_type_ids:
        return {}
    now = datetime.now(UTC)
    cutoff = _retention_cutoff(now)
    rows = (
        await session.execute(
            select(SchemaDrift.event_type_id, func.count(SchemaDrift.id))
            .join(EventType, EventType.id == SchemaDrift.event_type_id)
            .where(
                EventType.project_id == project_id,
                SchemaDrift.event_type_id.in_(event_type_ids),
                SchemaDrift.detected_at >= cutoff,
                *_active_drift_predicates(now),
            )
            .group_by(SchemaDrift.event_type_id)
        )
    ).all()
    return {event_type_id: count for event_type_id, count in rows}
