from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.api.deps import BranchIdDep, EditorUserDep, SessionDep
from tripl.api.v1.events import bulk_event_audit_payload, event_create_audit_payload
from tripl.models.domain_enums import ShadowEventStatus
from tripl.models.user import User
from tripl.schemas.pagination import Offset
from tripl.schemas.reconciliation import (
    CoverageResponse,
    DeadEventListResponse,
    ShadowEventAcceptRequest,
    ShadowEventAcceptResponse,
    ShadowEventBatchRequest,
    ShadowEventBatchResponse,
    ShadowEventDismissResponse,
    ShadowEventListResponse,
)
from tripl.services import audit_service, reconciliation_service
from tripl.services.reconciliation_service import (
    DeadEventArchiveRequest,
    DeadEventArchiveResponse,
    ShadowEventAcceptResult,
    ShadowEventDismissResult,
)

router = APIRouter(prefix="/projects/{slug}/reconciliation", tags=["reconciliation"])


@router.get("/shadow-events", response_model=ShadowEventListResponse)
async def list_shadow_events(
    session: SessionDep,
    slug: str,
    status: Annotated[ShadowEventStatus | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    # True paging for the inbox; rows are ordered busiest first, ties
    # by id, so consecutive pages neither repeat nor skip a row.
    offset: Offset = 0,
) -> ShadowEventListResponse:
    return await reconciliation_service.list_shadow_events(
        session,
        slug,
        status=status,
        limit=limit,
        offset=offset,
    )


async def _audit_accept(
    session: AsyncSession, current_user: User, slug: str, result: ShadowEventAcceptResult
) -> None:
    candidate = result.candidate
    # Filed as ``event.create`` — the action POST /events files — because that is
    # what happened: a catalog event now exists, on this branch, because a person
    # decided it should. The scan only PROPOSED the identity; accepting it is an
    # editor authoring a plan row, so "events written by a scan are not audited"
    # never covered this, and reading it as if it did left a whole door into the
    # catalog unrecorded.
    #
    # Not a reconciliation-specific action, deliberately. An owner asking "which
    # events did people create?" filters ``event.create``; an action of its own
    # would answer that question with a subset and look complete — the same hole
    # in a new shape. ``accepted_from`` in the payload is what tells the two
    # doors apart, and ``archive_dead_events`` in this module sets the
    # precedent: same write, same action, the context in the payload.
    await audit_service.record(
        session,
        user=current_user,
        action="event.create",
        target_type="event",
        target_id=result.event.id,
        # ``result.event.name``, not the requested name: a governing scan rule can
        # rename it, and the row has to name the event that exists.
        target_name=result.event.name,
        project_slug=slug,
        payload=event_create_audit_payload(
            result.event_create,
            extra={
                "accepted_from": {
                    "shadow_candidate_id": candidate.id,
                    # The scan identity the event now carries as ``source_name``.
                    # It is the whole reason this row is not a plain create: it
                    # says the plan was changed to match observed traffic, and
                    # names the traffic.
                    "source_name": candidate.event_name,
                    "observed_count": candidate.observed_count,
                    "scan_config_id": candidate.scan_config_id,
                }
            },
        ),
    )


async def _audit_dismiss(
    session: AsyncSession, current_user: User, slug: str, result: ShadowEventDismissResult
) -> None:
    candidate = result.candidate
    # Its own action, because nothing was created: this records a judgement that
    # observed traffic does not belong in the plan, and it is terminal through the
    # API — both accept and dismiss require ``status == new``, so no route takes
    # the click back. The candidate is still readable under the inbox's dismissed
    # tab, but it can never be accepted again, and the collector deliberately
    # leaves ``status`` alone on re-observation (metric_rows.py), so the next scan
    # will not resurrect it either.
    #
    # The candidate's own row already stores ``resolved_by``/``resolved_at``, and
    # that is NOT a substitute: it is ``ondelete="CASCADE"`` on both project and
    # scan config, so deleting the scan that found the traffic erases every record
    # of who waved it away. Same failure the event history had —
    # the trace dies with the thing it describes.
    #
    # No branch: this route declares no ``BranchIdDep`` because a candidate has no
    # branch column, so the contextvar is unbound and the row records none. That
    # is the honest answer, not a gap — see ``audit_service.record``.
    await audit_service.record(
        session,
        user=current_user,
        action="shadow_event.dismiss",
        target_type="shadow_event_candidate",
        target_id=candidate.id,
        # The scan identity, which is all this candidate ever was.
        target_name=candidate.event_name,
        project_slug=slug,
        payload={
            # What makes a dismissal reviewable once the candidate row is gone.
            # ``observed_count`` is the LATEST collection window's count, not a
            # total over the seen-span below it — the collector re-collects
            # windows, so summing would double-count (see the column's own
            # comment). The pair still answers "was this busy, and for how
            # long", which is the question a reader has.
            "observed_count": candidate.observed_count,
            "first_seen_at": candidate.first_seen_at,
            "last_seen_at": candidate.last_seen_at,
            "scan_config_id": candidate.scan_config_id,
            "event_type_id": candidate.event_type_id,
        },
    )


@router.post(
    "/shadow-events/{candidate_id}/accept",
    response_model=ShadowEventAcceptResponse,
)
async def accept_shadow_event(
    session: SessionDep,
    slug: str,
    candidate_id: uuid.UUID,
    payload: ShadowEventAcceptRequest,
    branch_id: BranchIdDep,
    current_user: EditorUserDep,
) -> ShadowEventAcceptResponse:
    result = await reconciliation_service.accept_shadow_event(
        session,
        slug,
        candidate_id,
        payload,
        user_id=current_user.id,
        branch_id=branch_id,
    )
    await _audit_accept(session, current_user, slug, result)
    return result.response


@router.post(
    "/shadow-events/{candidate_id}/dismiss",
    response_model=ShadowEventDismissResponse,
)
async def dismiss_shadow_event(
    session: SessionDep,
    slug: str,
    candidate_id: uuid.UUID,
    current_user: EditorUserDep,
) -> ShadowEventDismissResponse:
    result = await reconciliation_service.dismiss_shadow_event(
        session,
        slug,
        candidate_id,
        user_id=current_user.id,
    )
    await _audit_dismiss(session, current_user, slug, result)
    return result.response


@router.post("/shadow-events/batch", response_model=ShadowEventBatchResponse)
async def batch_shadow_events(
    session: SessionDep,
    slug: str,
    payload: ShadowEventBatchRequest,
    branch_id: BranchIdDep,
    current_user: EditorUserDep,
) -> ShadowEventBatchResponse:
    """Accept or dismiss many inbox rows in one request.

    Each row is handled and audited exactly as its single route would, and a
    refused row is reported in ``results`` without stopping the rest.
    """

    # A refused row rolls the session back, which expires every loaded object,
    # the signed-in user included; the audit row reads it, so it is re-read
    # first rather than lazy-loaded from async code.
    async def audit_user() -> User:
        if sa_inspect(current_user).expired_attributes:
            await session.refresh(current_user)
        return current_user

    async def on_accepted(result: ShadowEventAcceptResult) -> None:
        await _audit_accept(session, await audit_user(), slug, result)

    async def on_dismissed(result: ShadowEventDismissResult) -> None:
        await _audit_dismiss(session, await audit_user(), slug, result)

    results = await reconciliation_service.batch_shadow_events(
        session,
        slug,
        payload,
        user_id=current_user.id,
        branch_id=branch_id,
        on_accepted=on_accepted,
        on_dismissed=on_dismissed,
    )
    succeeded = sum(1 for row in results if row.ok)
    return ShadowEventBatchResponse(
        results=results, succeeded=succeeded, failed=len(results) - succeeded
    )


@router.get("/dead-events", response_model=DeadEventListResponse)
async def list_dead_events(
    session: SessionDep,
    slug: str,
    days: Annotated[int, Query(ge=1, le=365)] = reconciliation_service.DEFAULT_DEAD_EVENT_DAYS,
) -> DeadEventListResponse:
    return await reconciliation_service.list_dead_events(session, slug, days=days)


@router.post("/dead-events/archive", response_model=DeadEventArchiveResponse)
async def archive_dead_events(
    session: SessionDep,
    slug: str,
    payload: DeadEventArchiveRequest,
    branch_id: BranchIdDep,
    current_user: EditorUserDep,
) -> DeadEventArchiveResponse:
    result = await reconciliation_service.archive_dead_events(
        session,
        slug,
        payload,
        user_id=current_user.id,
        branch_id=branch_id,
    )
    # Audited as ``event.bulk_update``, the same action POST /events/bulk-update
    # files, because it is the same write: archive_dead_events delegates to
    # ``event_service.bulk_update_events`` to move events into a terminal
    # lifecycle state. Without this row an editor could retire 40 events from
    # Reconciliation and the audit log — which the docs describe as covering
    # every event edit — would say nothing. Recorded in the
    # router, not the service: the sync worker imports from ``services`` and
    # never from ``api``, so that line is what keeps the scan pipeline — which
    # constructs its ``Event(...)`` rows directly — structurally unable to write
    # audit rows. The branch comes off the contextvar ``BranchIdDep`` above
    # already bound.
    await audit_service.record(
        session,
        user=current_user,
        action="event.bulk_update",
        target_type="event",
        target_id=None,
        project_slug=slug,
        # ``result.event_ids`` is the service's deduplicated list, so ``count``
        # is the number of events actually moved and matches what the response
        # echoed to the caller.
        payload=bulk_event_audit_payload(
            result.event_ids,
            extra={"status": result.status.value},
        ),
    )
    return result


@router.get("/coverage", response_model=CoverageResponse)
async def get_coverage(
    session: SessionDep,
    slug: str,
    days: Annotated[int, Query(ge=1, le=180)] = reconciliation_service.DEFAULT_COVERAGE_DAYS,
    scan_config_id: Annotated[uuid.UUID | None, Query()] = None,
) -> CoverageResponse:
    return await reconciliation_service.get_coverage(
        session,
        slug,
        days=days,
        scan_config_id=scan_config_id,
    )
