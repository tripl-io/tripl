"""Plan-reality reconciliation: shadow event inbox, dead events, coverage."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.coverage_metric import CoverageMetric
from tripl.models.event import Event, EventStatus
from tripl.models.event_type import EventType
from tripl.models.scan_config import ScanConfig
from tripl.models.shadow_event_candidate import (
    SHADOW_STATUS_ACCEPTED,
    SHADOW_STATUS_DISMISSED,
    SHADOW_STATUS_NEW,
    ShadowEventCandidate,
)
from tripl.schemas.event import EventBulkUpdate, EventCreate
from tripl.schemas.reconciliation import (
    CoverageBucket,
    CoverageResponse,
    CoverageSummary,
    DeadEventItem,
    DeadEventListResponse,
    ShadowEventAcceptRequest,
    ShadowEventAcceptResponse,
    ShadowEventBatchItemResult,
    ShadowEventBatchRequest,
    ShadowEventCandidateResponse,
    ShadowEventDismissResponse,
    ShadowEventListResponse,
)
from tripl.services import event_service
from tripl.services.plan_branch_service import resolve_branch_id
from tripl.services.project_lookup import resolve_project_id

DEFAULT_DEAD_EVENT_DAYS = 30
DEFAULT_COVERAGE_DAYS = 14

# Terminal lifecycle states a dead event may be retired into. Kept here (rather
# than in tripl.schemas.reconciliation) so the bulk-archive request/response
# travel with the service that owns the behaviour.
_ARCHIVE_STATUSES = (EventStatus.deprecated, EventStatus.archived)


class DeadEventArchiveRequest(BaseModel):
    """Bulk-retire request for dead events.

    ``status`` is constrained to the two terminal lifecycle states so the
    action can only deprecate or archive — never resurrect — an event.
    """

    event_ids: list[uuid.UUID] = Field(min_length=1)
    status: EventStatus = EventStatus.archived

    @field_validator("status")
    @classmethod
    def _validate_terminal_status(cls, value: EventStatus) -> EventStatus:
        if value not in _ARCHIVE_STATUSES:
            raise ValueError("status must be 'archived' or 'deprecated'")
        return value


class DeadEventArchiveResponse(BaseModel):
    event_ids: list[uuid.UUID]
    status: EventStatus
    archived_count: int


def _not_an_archived_identity(project_id: uuid.UUID) -> ColumnElement[bool]:
    """Anti-join excluding candidates whose identity belongs to an archived event.

    Archiving means "put it away", so the identity is in the plan and by
    definition not an unmapped event. The collector stopped writing these,
    but rows written before that shipped would otherwise sit in the
    inbox forever: accepting one only 409s on the duplicate source identity, so
    there is no way for the user to clear it.

    Correlated on ``event_type_id`` as well: a scan identity is
    one per event TYPE (``uq_event_scan_identity``), and the collector's archived
    set is keyed per type (``_archived_identities_by_event_type``), so an
    archived type-A event with identity X must not hide a type-B candidate X the
    collector keeps upserting. The candidate's type id is the one the scan
    resolved on main, so in practice this matches main's archived rows, which
    is exactly the population the collector consults. A candidate whose type
    is NULL keeps the project-wide match.
    """
    return ~(
        select(Event.id)
        .where(
            Event.project_id == project_id,
            Event.status == EventStatus.archived,
            # A candidate with no resolved type (every row written before the
            # collector began recording one) keeps the project-wide match; those
            # legacy rows are the population this filter exists for.
            or_(
                ShadowEventCandidate.event_type_id.is_(None),
                Event.event_type_id == ShadowEventCandidate.event_type_id,
            ),
            # `source_name or name`, matching how the collector builds the
            # archived identity set (generation.py
            # `_archived_identities_by_event_type`) and how `events_by_name` is
            # keyed. A hand-created event has no source_name, so comparing the
            # column alone silently failed to match exactly the rows a user is
            # most likely to archive by hand: the two halves of one rule have to
            # agree, or the inbox keeps showing a candidate the collector has
            # already stopped writing.
            func.coalesce(Event.source_name, Event.name) == ShadowEventCandidate.event_name,
        )
        .exists()
    )


async def list_shadow_events(
    session: AsyncSession,
    slug: str,
    *,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> ShadowEventListResponse:
    project_id = await resolve_project_id(session, slug)
    not_archived = _not_an_archived_identity(project_id)

    query = (
        # display_name is what every other surface labels an event type with
        # (activity_service.py:76, alert_payload.py:58) — projecting the internal
        # `name` here made Reconciliation the odd one out.
        select(
            ShadowEventCandidate,
            ScanConfig.name,
            func.coalesce(EventType.display_name, EventType.name),
        )
        .join(ScanConfig, ScanConfig.id == ShadowEventCandidate.scan_config_id)
        .outerjoin(EventType, EventType.id == ShadowEventCandidate.event_type_id)
        .where(ShadowEventCandidate.project_id == project_id, not_archived)
        # ``id`` breaks ties so that pages are stable: many candidates share
        # an observed count, and an unordered tie could show one row on two
        # pages and another on none.
        .order_by(ShadowEventCandidate.observed_count.desc(), ShadowEventCandidate.id)
        .offset(offset)
        .limit(limit)
    )
    if status:
        query = query.where(ShadowEventCandidate.status == status)

    rows = (await session.execute(query)).all()
    total = (
        await session.scalar(
            select(func.count(ShadowEventCandidate.id)).where(
                ShadowEventCandidate.project_id == project_id,
                not_archived,
                *((ShadowEventCandidate.status == status,) if status else ()),
            )
        )
    ) or 0
    new_count = (
        await session.scalar(
            select(func.count(ShadowEventCandidate.id)).where(
                ShadowEventCandidate.project_id == project_id,
                ShadowEventCandidate.status == SHADOW_STATUS_NEW,
                not_archived,
            )
        )
    ) or 0

    items = [
        ShadowEventCandidateResponse(
            id=candidate.id,
            scan_config_id=candidate.scan_config_id,
            scan_config_name=scan_name,
            event_type_id=candidate.event_type_id,
            event_type_name=event_type_name,
            event_name=candidate.event_name,
            observed_count=candidate.observed_count,
            first_seen_at=candidate.first_seen_at,
            last_seen_at=candidate.last_seen_at,
            status=candidate.status,
            accepted_event_id=candidate.accepted_event_id,
            sample_properties=candidate.sample_properties or [],
        )
        for candidate, scan_name, event_type_name in rows
    ]
    return ShadowEventListResponse(items=items, total=int(total), new_count=int(new_count))


async def _get_candidate(
    session: AsyncSession,
    project_id: uuid.UUID,
    candidate_id: uuid.UUID,
) -> ShadowEventCandidate:
    candidate = await session.scalar(
        select(ShadowEventCandidate).where(
            ShadowEventCandidate.id == candidate_id,
            ShadowEventCandidate.project_id == project_id,
        )
    )
    if candidate is None:
        raise HTTPException(status_code=404, detail="Shadow event candidate not found")
    return candidate


async def _event_type_on_branch(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    event_type_id: uuid.UUID,
) -> uuid.UUID:
    """The counterpart of ``event_type_id`` on ``branch_id``, matched by name.

    A no-op when the id already belongs to that branch, which is every accept on
    main — a scan resolves its types against main's plan, so ``branch_id`` is
    main's in the ordinary case and the SELECT returns the row unchanged.
    ``uq_event_type_project_name`` is per branch, so the counterpart is unique.

    422 rather than a silent fallback to the id the scan gave: the branch was
    deep-copied from main, so a missing counterpart means the branch deleted
    that event type, and accepting a candidate onto a type the branch says is
    gone is not a thing the operator asked for.
    """
    row = await session.execute(
        select(EventType.branch_id, EventType.name).where(
            EventType.id == event_type_id, EventType.project_id == project_id
        )
    )
    found = row.first()
    if found is None:
        raise HTTPException(status_code=404, detail="Event type not found")
    detected_branch_id, name = found
    if detected_branch_id == branch_id:
        return event_type_id
    counterpart = await session.scalar(
        select(EventType.id).where(
            EventType.project_id == project_id,
            EventType.branch_id == branch_id,
            EventType.name == name,
        )
    )
    if counterpart is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Event type '{name}' does not exist on this branch. The scan detected "
                "this candidate against the main plan; accept it on main, or name an "
                "event type that exists on this branch."
            ),
        )
    return counterpart


# --- inbox resolutions: what the route answers, and what its audit row needs ---
#
# Both resolutions are recorded, and the audit row is written in the ROUTER, never
# here. That is not style. The sync Celery worker imports from ``services`` —
# app_settings_service, search_service, embedding_service, the demo builders — and
# never from ``api``, so keeping ``audit_service.record`` on the router side of
# that line is what makes "a scan writes no audit rows" structural rather than a
# promise; tests/test_audit.py freezes it by reading imports. So the facts the row
# needs have to LEAVE the service, the same division of labour
# ``event_service.bulk_delete_events`` uses when it hands the router back
# (id, name) pairs for the delete row.


@dataclass(frozen=True)
class ShadowEventAcceptResult:
    """The accept route's answer plus the facts its audit row names.

    ``event_create`` is the schema object this service actually passed to
    ``event_service.create_event``, not a reconstruction of it from the created
    row. ``event.create`` is one action filed through two doors — POST /events and
    this one — and one action must not grow two payload shapes; re-deriving the
    input from the output is how the two quietly stop matching.

    ``event`` is carried separately because ``event.name`` is not always
    ``event_create.name``: a governing scan rule can rename it, and the row has to
    name the event that exists.
    """

    response: ShadowEventAcceptResponse
    event: Event
    event_create: EventCreate
    candidate: ShadowEventCandidate


@dataclass(frozen=True)
class ShadowEventDismissResult:
    """The dismiss route's answer plus the candidate it retired.

    Nothing is created here, so there is no event to point an audit row at — the
    candidate is the target, and the payload is the traffic the reader is choosing
    to stop being told about.
    """

    response: ShadowEventDismissResponse
    candidate: ShadowEventCandidate


async def accept_shadow_event(
    session: AsyncSession,
    slug: str,
    candidate_id: uuid.UUID,
    data: ShadowEventAcceptRequest,
    *,
    user_id: uuid.UUID,
    branch_id: uuid.UUID | None,
) -> ShadowEventAcceptResult:
    project_id = await resolve_project_id(session, slug)
    candidate = await _get_candidate(session, project_id, candidate_id)
    if candidate.status != SHADOW_STATUS_NEW:
        raise HTTPException(
            status_code=409,
            detail=f"Candidate already {candidate.status}",
        )

    event_type_id = data.event_type_id or candidate.event_type_id
    if event_type_id is None:
        raise HTTPException(
            status_code=422,
            detail="event_type_id is required: the candidate has no detected event type",
        )

    resolved_branch_id = await resolve_branch_id(session, project_id, branch_id)
    if data.event_type_id is None:
        # The candidate's type id was resolved by the SCAN, and a scan reads
        # main's plan, so it is always a MAIN event type id. Writing it onto a
        # row on a working branch would give that row main's identity, which
        # ``create_event`` now refuses outright — and with it
        # the whole branch accept flow. Translated by NAME to the branch's own
        # copy, which is the pairing ``load_governing_scan_configs_by_type`` and
        # ``services/_branch_counterparts`` already use in the other direction.
        # Only for the DETECTED id: an id the operator picked came from a list
        # scoped to the branch they are accepting on.
        event_type_id = await _event_type_on_branch(
            session,
            project_id=project_id,
            branch_id=resolved_branch_id,
            event_type_id=event_type_id,
        )
    # Per event type, like every other statement of the identity rule
    # (``uq_event_scan_identity``, ``_guard_scan_identity``): a type-A event
    # holding identity X does not stop a type-B event from taking it.
    existing = await session.scalar(
        select(Event.id).where(
            Event.project_id == project_id,
            Event.branch_id == resolved_branch_id,
            Event.event_type_id == event_type_id,
            Event.source_name == candidate.event_name,
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail="An event with this source identity already exists (possibly archived)",
        )

    event_create = EventCreate(
        event_type_id=event_type_id,
        name=data.name or candidate.event_name,
        status="live",
    )
    # The scan identity is what the metrics collector matches on — without it the
    # accepted event would never attach to warehouse data. It is passed IN rather
    # than assigned after the call for two reasons: create_event would otherwise
    # try to derive a name from a governing event_name_format, and a candidate
    # carries no field values, so every placeholder reads as missing and the
    # accept 422s on any rule-governed event type; and assigning
    # it afterwards wrote the identity in a second transaction, after the search
    # index for this event had already been built without it.
    # ``user_id`` names the accepting editor in the event's own 'created' history
    # row, the way POST /events does. Without it the row was anonymous and the
    # docs' claim that an accepted candidate is indistinguishable from one you
    # typed was false on the History tab.
    event = await event_service.create_event(
        session,
        slug,
        event_create,
        branch_id=branch_id,
        scan_identity=candidate.event_name,
        user_id=user_id,
    )

    candidate.status = SHADOW_STATUS_ACCEPTED
    candidate.accepted_event_id = event.id
    candidate.resolved_by = user_id
    candidate.resolved_at = datetime.now(UTC)
    await session.commit()

    return ShadowEventAcceptResult(
        response=ShadowEventAcceptResponse(
            candidate_id=candidate.id,
            event_id=event.id,
            status=SHADOW_STATUS_ACCEPTED,
        ),
        event=event,
        event_create=event_create,
        candidate=candidate,
    )


async def dismiss_shadow_event(
    session: AsyncSession,
    slug: str,
    candidate_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
) -> ShadowEventDismissResult:
    project_id = await resolve_project_id(session, slug)
    candidate = await _get_candidate(session, project_id, candidate_id)
    if candidate.status != SHADOW_STATUS_NEW:
        raise HTTPException(
            status_code=409,
            detail=f"Candidate already {candidate.status}",
        )

    candidate.status = SHADOW_STATUS_DISMISSED
    candidate.resolved_by = user_id
    candidate.resolved_at = datetime.now(UTC)
    await session.commit()

    return ShadowEventDismissResult(
        response=ShadowEventDismissResponse(
            candidate_id=candidate.id,
            status=SHADOW_STATUS_DISMISSED,
        ),
        candidate=candidate,
    )


async def batch_shadow_events(
    session: AsyncSession,
    slug: str,
    data: ShadowEventBatchRequest,
    *,
    user_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    on_accepted: Callable[[ShadowEventAcceptResult], Awaitable[None]],
    on_dismissed: Callable[[ShadowEventDismissResult], Awaitable[None]],
) -> list[ShadowEventBatchItemResult]:
    """Accept or dismiss many inbox rows, each on its own.

    Every row runs through the single route's service call and commits on its
    own, so one refused row (already resolved, no event type, an identity that
    exists) is reported beside the others instead of undoing them — the same
    outcome the page got from one request per row, in one round trip.

    ``on_accepted``/``on_dismissed`` run right after each row commits, with the
    result the single route audits, so a batch files the same ``event.create``
    / ``shadow_event.dismiss`` rows one click at a time would. Right after, not
    at the end: a later row's rollback expires every loaded object, and the
    earlier results could no longer be read without IO.
    """
    results: list[ShadowEventBatchItemResult] = []
    for item in data.items:
        try:
            if data.action == "accept":
                accepted = await accept_shadow_event(
                    session,
                    slug,
                    item.candidate_id,
                    ShadowEventAcceptRequest(event_type_id=item.event_type_id, name=item.name),
                    user_id=user_id,
                    branch_id=branch_id,
                )
                results.append(
                    ShadowEventBatchItemResult(
                        candidate_id=item.candidate_id,
                        ok=True,
                        status=accepted.response.status,
                        event_id=accepted.response.event_id,
                    )
                )
                await on_accepted(accepted)
            else:
                dismissed = await dismiss_shadow_event(
                    session, slug, item.candidate_id, user_id=user_id
                )
                results.append(
                    ShadowEventBatchItemResult(
                        candidate_id=item.candidate_id,
                        ok=True,
                        status=dismissed.response.status,
                    )
                )
                await on_dismissed(dismissed)
        except HTTPException as exc:
            # Whatever the refused row had staged goes; the rows before it
            # committed on their own.
            await session.rollback()
            results.append(
                ShadowEventBatchItemResult(
                    candidate_id=item.candidate_id,
                    ok=False,
                    error=str(exc.detail),
                    error_status=exc.status_code,
                )
            )
    return results


async def list_dead_events(
    session: AsyncSession,
    slug: str,
    *,
    days: int = DEFAULT_DEAD_EVENT_DAYS,
) -> DeadEventListResponse:
    project_id = await resolve_project_id(session, slug)
    main_branch_id = await resolve_branch_id(session, project_id, None)
    cutoff = datetime.now(UTC) - timedelta(days=days)

    rows = (
        await session.execute(
            select(Event, func.coalesce(EventType.display_name, EventType.name))
            .join(EventType, EventType.id == Event.event_type_id)
            .where(
                Event.project_id == project_id,
                Event.branch_id == main_branch_id,
                Event.status.in_(["implemented", "live"]),
                # An event that HAS been seen and then went quiet is dead
                # regardless of when its plan row was written. The grace period
                # covers only the never-seen case, where a freshly authored
                # event legitimately has no data yet. Gating both cases on
                # created_at hid genuinely stale events behind a young plan row
                # and made every backdated demo event permanently unflaggable.
                (
                    (Event.last_seen_at.is_(None) & (Event.created_at < cutoff))
                    | (Event.last_seen_at < cutoff)
                ),
            )
            .order_by(Event.last_seen_at.asc().nulls_first(), Event.name)
        )
    ).all()

    items = [
        DeadEventItem(
            event_id=event.id,
            name=event.name,
            event_type_id=event.event_type_id,
            event_type_name=event_type_name,
            last_seen_at=event.last_seen_at,
            created_at=event.created_at,
        )
        for event, event_type_name in rows
    ]
    return DeadEventListResponse(items=items, total=len(items), days=days)


async def archive_dead_events(
    session: AsyncSession,
    slug: str,
    data: DeadEventArchiveRequest,
    *,
    user_id: uuid.UUID,
    branch_id: uuid.UUID | None,
) -> DeadEventArchiveResponse:
    """Bulk-retire dead events into a terminal lifecycle state.

    Delegates to the canonical ``event_service.bulk_update_events`` status path:
    it resolves the branch, asserts every id belongs to it (404 otherwise),
    records change history, reindexes search and busts the project cache inside a
    single transaction. ``status`` is already validated to be deprecated/archived
    by ``DeadEventArchiveRequest``.
    """
    # Dedup while preserving order so a repeated id is counted (and echoed) once.
    unique_ids = list(dict.fromkeys(data.event_ids))
    await event_service.bulk_update_events(
        session,
        slug,
        EventBulkUpdate(event_ids=unique_ids, status=data.status),
        branch_id=branch_id,
        user_id=user_id,
    )
    return DeadEventArchiveResponse(
        event_ids=unique_ids,
        status=data.status,
        archived_count=len(unique_ids),
    )


async def get_coverage(
    session: AsyncSession,
    slug: str,
    *,
    days: int = DEFAULT_COVERAGE_DAYS,
    scan_config_id: uuid.UUID | None = None,
) -> CoverageResponse:
    project_id = await resolve_project_id(session, slug)
    time_from = datetime.now(UTC) - timedelta(days=days)

    query = (
        select(
            CoverageMetric.bucket,
            func.sum(CoverageMetric.total_count),
            func.sum(CoverageMetric.matched_count),
        )
        .join(ScanConfig, ScanConfig.id == CoverageMetric.scan_config_id)
        .where(
            ScanConfig.project_id == project_id,
            CoverageMetric.bucket >= time_from,
        )
        .group_by(CoverageMetric.bucket)
        .order_by(CoverageMetric.bucket)
    )
    if scan_config_id is not None:
        query = query.where(CoverageMetric.scan_config_id == scan_config_id)

    rows = (await session.execute(query)).all()
    items = [
        CoverageBucket(
            bucket=bucket,
            total_count=int(total or 0),
            matched_count=int(matched or 0),
        )
        for bucket, total, matched in rows
    ]
    total_sum = sum(item.total_count for item in items)
    matched_sum = sum(item.matched_count for item in items)
    coverage_pct = (matched_sum / total_sum * 100) if total_sum else 0.0
    return CoverageResponse(
        items=items,
        summary=CoverageSummary(
            total_count=total_sum,
            matched_count=matched_sum,
            coverage_pct=round(coverage_pct, 2),
        ),
        days=days,
    )
