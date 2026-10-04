import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from tripl.api.deps import BranchIdDep, EditorUserDep, SessionDep, get_editor_user
from tripl.models.event import Event, EventStatus
from tripl.schemas.event import (
    MAX_IDENTITY_LOOKUP_NAMES,
    EventBulkDelete,
    EventBulkUpdate,
    EventChangeResponse,
    EventCreate,
    EventIdentityHoldersResponse,
    EventListResponse,
    EventMove,
    EventMutationResponse,
    EventReorder,
    EventResponse,
    EventUpdate,
)
from tripl.schemas.text_filters import FreeTextFilter
from tripl.schemas.variable import EventPropertyResponse
from tripl.services import audit_service, event_service, variable_service

router = APIRouter(prefix="/projects/{slug}/events", tags=["events"])
# Kept for the two routes that only permute ``Event.order`` and are deliberately
# not audited (see reorder_events / move_event). The six routes that DO record
# take the same gate as a ``current_user: EditorUserDep`` parameter instead,
# because the audit row needs the user object — the shape event_types.py and
# variables.py already use. Both spellings land in ``route.dependant.dependencies``,
# which is what tests/test_rbac.py checks, so the write gate is unchanged.
_editor_required = [Depends(get_editor_user)]

# How many ids/names one bulk audit payload carries.
#
# ONE ROW PER BULK REQUEST, not one per event — the call the closest sibling
# already made (``variable.bulk_update`` / ``variable.bulk_delete`` in
# variables.py). ``EventBulkDelete.event_ids`` and ``EventBulkUpdate.event_ids``
# carry ``min_length=1`` and NO upper bound, so a row per event would let one API
# call write an unbounded number of audit rows. The lists inside the payload are
# sampled for the same reason: ``audit_log.payload`` is an uncapped JSON column
# and nobody reads the 201st id off a compliance row.
_BULK_SAMPLE = 200


def event_create_audit_payload(
    data: EventCreate,
    *,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """What an ``event.create`` row stores.

    Deliberately NOT ``data.model_dump()``, the shape every other router uses:
    one ``EventFieldValueIn.value`` may be 100 000 characters and one
    ``EventMetaValueIn.value`` 8000, and NEITHER list has an upper bound, so
    copying the body verbatim would put megabytes into a single
    ``audit_log.payload``. (8000 is the payload guard; what the service will
    actually STORE is capped at ``META_VALUE_MAX_BYTES``.)
    The counts record that values were written; the values themselves live on the
    event.

    The two value lists are the whole target. ``description`` and ``tags`` are
    also uncapped and DO stay in the payload, because event_types.py and
    variables.py already dump their own uncapped ``description`` verbatim and no
    writer in the repo bloats one — a length cap for those belongs in
    ``audit_service``, where it would cover all six routers at once, not here.

    Public, and with an ``extra`` escape hatch, for the same reason
    ``bulk_event_audit_payload`` is: reconciliation.py files ``event.create`` too
    when an editor admits a shadow-event candidate into the plan, and one action
    must not have two payload shapes. ``extra`` is merged FIRST,
    so a caller cannot shadow a field of the event that was actually created.
    """
    return {
        **(extra or {}),
        **data.model_dump(mode="json", exclude={"field_values", "meta_values"}),
        "field_value_count": len(data.field_values),
        "meta_value_count": len(data.meta_values),
    }


def _update_payload(data: EventUpdate) -> dict[str, object]:
    """Same size guard as ``event_create_audit_payload``, keeping ``exclude_unset`` so the
    row still reports exactly the fields the client sent — and two booleans so it
    still says the field or meta values were rewritten."""
    return {
        **data.model_dump(mode="json", exclude_unset=True, exclude={"field_values", "meta_values"}),
        "field_values_replaced": data.field_values is not None,
        "meta_values_replaced": data.meta_values is not None,
    }


def bulk_event_audit_payload(
    event_ids: list[uuid.UUID],
    *,
    event_names: list[str] | None = None,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """One bulk route's payload: the true count, a sample of the ids and (for
    deletes) of the names, plus whatever the request itself said.

    ``count`` is the real number of events; the two lists are samples, so a
    reader can always tell a 3-event delete from a 3000-event one even when the
    ids are cut off. That makes ``event_ids`` the caller's contract: pass the
    events the service actually touched, not the ids the request listed — see
    ``bulk_update_events`` below, the one route where those differ.

    The fixed keys are written last so a request field can never shadow them.

    Public, not ``_``-prefixed, because reconciliation.py's dead-event archive
    files the same ``event.bulk_update`` action and one action must not have two
    payload shapes.
    """
    return {
        **(extra or {}),
        "count": len(event_ids),
        "event_ids": [str(event_id) for event_id in event_ids[:_BULK_SAMPLE]],
        "truncated": len(event_ids) > _BULK_SAMPLE,
        **({} if event_names is None else {"event_names": event_names[:_BULK_SAMPLE]}),
    }


@router.get("", response_model=EventListResponse)
async def list_events(
    session: SessionDep,
    slug: str,
    branch_id: BranchIdDep,
    event_type_id: uuid.UUID | None = None,
    # FreeTextFilter (not str): these four bind straight into a Postgres
    # parameter — three ILIKEs, an equality — and a NUL in any of them aborts
    # inside asyncpg before SQL runs, so ?search=%00 was a 500.
    search: FreeTextFilter | None = None,
    # EventStatus (not list[str]): the column is a native Postgres enum, so an
    # out-of-enum value used to reach the driver and surface as a 500. FastAPI
    # now 422s it up front, like the order_by Literal below already did.
    status: Annotated[list[EventStatus] | None, Query()] = None,
    tag: FreeTextFilter | None = None,
    silent_since_days: int | None = Query(None, ge=0, le=3650),
    # `reviewed` is an axis of its own — an event can be marked reviewed and
    # still carry status=in_review — and it had no filter at all, so the UI's
    # "Mark reviewed" wrote a flag nobody could isolate afterwards.
    # Omit for "any".
    reviewed: bool | None = None,
    # "Which events are waiting on an answer?" — the question the discussion
    # could not be asked until threads could be resolved. True
    # keeps only events with an unanswered thread, False only those with none;
    # omit for "any". A thread whose snooze has lapsed counts as unanswered
    # again. Twin-aware: the discussion hangs on the event's main row, so this
    # answers correctly on a branch too.
    has_open_questions: bool | None = None,
    field_value: FreeTextFilter | None = None,
    meta_value: FreeTextFilter | None = None,
    # Events whose property list carries this property (F23.8): its id or name
    # on the branch read. A ref that names no property matches nothing.
    property: Annotated[
        FreeTextFilter | None,
        Query(description="Property id or name: only events whose property list carries it."),
    ] = None,
    offset: int = Query(0, ge=0),
    # Ceiling is 10000 because frontend/src/pages/events/useEventsQuery.ts pages
    # the whole match set at EVENTS_ID_FETCH_PAGE_SIZE = 10000 for bulk "select
    # all matching" and CSV export. That constant is hardcoded to this value, so
    # LOWERING THIS CAP 422s the export unless the frontend changes in the same
    # commit. The rows must stay full EventListItemResponse too: the CSV writes
    # tags, field values and meta values. (The old note here named
    # ProjectAlertingTab.tsx, which no longer fetches the roster at all.)
    limit: int = Query(200, ge=1, le=10000),
    # Review-queue ordering: "catalog" keeps the manual/creation order; "volume"
    # sorts busiest-first by 24h EventMetric volume; "health" sorts least healthy
    # first (F15, #268; main plan only, 400 on a branch). Literal → FastAPI 422s
    # any other value.
    order_by: Literal["catalog", "volume", "health"] = Query("catalog"),
) -> EventListResponse:
    items, total = await event_service.list_events(
        session,
        slug,
        event_type_id,
        search,
        [member.value for member in status] if status else None,
        tag,
        offset,
        limit,
        silent_since_days=silent_since_days,
        field_value=field_value,
        meta_value=meta_value,
        reviewed=reviewed,
        has_open_questions=has_open_questions,
        branch_id=branch_id,
        order_by=order_by,
        property_ref=property,
    )
    return EventListResponse(items=items, total=total)


@router.get("/tags", response_model=list[str])
async def list_tags(session: SessionDep, slug: str, branch_id: BranchIdDep) -> list[str]:
    return await event_service.list_tags(session, slug, branch_id)


@router.get("/by-names", response_model=EventIdentityHoldersResponse)
async def lookup_events_by_names(
    session: SessionDep,
    slug: str,
    branch_id: BranchIdDep,
    event_type_id: uuid.UUID,
    # Exact identities, repeated: ``?names=a&names=b``. An authoring form's
    # "is this name taken" check, answered with the rule create enforces
    # (EVT-37); a substring ``search`` per name was the only way to ask before.
    names: Annotated[
        list[FreeTextFilter],
        Query(min_length=1, max_length=MAX_IDENTITY_LOOKUP_NAMES),
    ],
) -> EventIdentityHoldersResponse:
    return await event_service.identity_holders(
        session, slug, event_type_id=event_type_id, names=names, branch_id=branch_id
    )


@router.post("", response_model=EventMutationResponse, status_code=201)
async def create_event(
    session: SessionDep,
    slug: str,
    data: EventCreate,
    branch_id: BranchIdDep,
    current_user: EditorUserDep,
) -> Event:
    event = await event_service.create_event(
        session, slug, data, branch_id, user_id=current_user.id
    )
    # ``event.name``, not ``data.name``: a governing scan rule can generate the
    # name, and the audit row has to name the event that exists.
    await audit_service.record(
        session,
        user=current_user,
        action="event.create",
        target_type="event",
        target_id=event.id,
        target_name=event.name,
        project_slug=slug,
        payload=event_create_audit_payload(data),
    )
    return event


@router.post("/bulk", response_model=list[EventResponse], status_code=201)
async def bulk_create_events(
    session: SessionDep,
    slug: str,
    data: list[EventCreate],
    branch_id: BranchIdDep,
    current_user: EditorUserDep,
) -> list[Event]:
    created = await event_service.bulk_create_events(
        session, slug, data, branch_id, user_id=current_user.id
    )
    # Ids only: these rows are alive, so their names are one GET away.
    await audit_service.record(
        session,
        user=current_user,
        action="event.bulk_create",
        target_type="event",
        target_id=None,
        project_slug=slug,
        payload=bulk_event_audit_payload([event.id for event in created]),
    )
    return created


@router.post("/bulk-delete", status_code=204)
async def bulk_delete_events(
    session: SessionDep,
    slug: str,
    data: EventBulkDelete,
    branch_id: BranchIdDep,
    current_user: EditorUserDep,
) -> None:
    # The service validates the ids, raises the 404 when one is missing, and
    # hands back (id, name) per deleted row — the division of labour
    # variables.py:delete_variable documents.
    deleted = await event_service.bulk_delete_events(session, slug, data, branch_id)
    await audit_service.record(
        session,
        user=current_user,
        action="event.bulk_delete",
        target_type="event",
        target_id=None,
        project_slug=slug,
        # Names, not just ids. After this request the ids resolve to nothing and
        # each event's change history went with it (FK ondelete=CASCADE), so the
        # names are the only readable trace of what was deleted.
        payload=bulk_event_audit_payload(
            [event_id for event_id, _ in deleted],
            event_names=[name for _, name in deleted],
        ),
    )


@router.post("/bulk-update", status_code=204)
async def bulk_update_events(
    session: SessionDep,
    slug: str,
    data: EventBulkUpdate,
    branch_id: BranchIdDep,
    current_user: EditorUserDep,
) -> None:
    await event_service.bulk_update_events(session, slug, data, branch_id, user_id=current_user.id)
    await audit_service.record(
        session,
        user=current_user,
        action="event.bulk_update",
        target_type="event",
        target_id=None,
        project_slug=slug,
        payload=bulk_event_audit_payload(
            # Deduplicated, order-preserving — the idiom
            # reconciliation_service.archive_dead_events already uses. Unlike the
            # two routes above, this one has no rows to count: the service
            # updates ``set(data.event_ids)`` and validates against that set, so
            # ``{"event_ids": [A, A, B]}`` succeeds having changed two events.
            # Auditing the raw list would file ``count: 3`` for a 2-event change.
            # bulk_delete deliberately 404s the same body, so
            # only this route can see a duplicate at all.
            list(dict.fromkeys(data.event_ids)),
            # ``exclude_unset``, the same key the service updates by: an
            # explicit ``owner_id: null`` unassigns across the selection, and
            # ``exclude_none`` filed that change as a row saying nothing about
            # owners at all. Unsent fields still stay out, so
            # the row keeps reporting exactly what the request said.
            extra=data.model_dump(mode="json", exclude_unset=True, exclude={"event_ids"}),
        ),
    )


# NOT audited, deliberately: reorder_events and move_event only permute
# ``Event.order``, which is display ordering with no plan semantics, and the
# events table's drag-to-reorder would file one audit row per drag.
@router.patch(
    "/reorder",
    response_model=list[EventResponse],
    dependencies=_editor_required,
)
async def reorder_events(
    session: SessionDep, slug: str, data: EventReorder, branch_id: BranchIdDep
) -> list[Event]:
    return await event_service.reorder_events(session, slug, data, branch_id)


@router.get("/{event_id}", response_model=EventResponse)
async def get_event(
    session: SessionDep, slug: str, event_id: uuid.UUID, branch_id: BranchIdDep
) -> Event:
    return await event_service.get_event(session, slug, event_id, branch_id, strict_branch=False)


@router.get("/{event_id}/properties", response_model=list[EventPropertyResponse])
async def list_event_properties(
    session: SessionDep, slug: str, event_id: uuid.UUID, branch_id: BranchIdDep
) -> list[EventPropertyResponse]:
    """The event's property list (F23). Edit an entry through
    ``PUT /variables/{variable_id}/event-overrides/{event_id}``."""
    return await variable_service.list_event_properties(session, slug, event_id, branch_id)


@router.get("/{event_id}/history", response_model=list[EventChangeResponse])
async def get_event_history(
    session: SessionDep, slug: str, event_id: uuid.UUID, branch_id: BranchIdDep
) -> list[dict[str, object]]:
    return await event_service.get_event_history(session, slug, event_id, branch_id)


@router.patch(
    "/{event_id}",
    response_model=EventMutationResponse,
)
async def update_event(
    session: SessionDep,
    slug: str,
    event_id: uuid.UUID,
    data: EventUpdate,
    branch_id: BranchIdDep,
    current_user: EditorUserDep,
) -> Event:
    event = await event_service.update_event(
        session, slug, event_id, data, branch_id, user_id=current_user.id
    )
    # This row and the per-event history are different surfaces, not duplicates:
    # ``_record_changes`` covers the before/after values of the fields in
    # ``event_service._TRACKED_FIELDS`` and dies with the event, this one covers
    # who/what/when/which-branch and outlives it. The count is deliberately not
    # spelled out here — it was written as "four fields" and the tuple has since
    # grown to six.
    await audit_service.record(
        session,
        user=current_user,
        action="event.update",
        target_type="event",
        target_id=event.id,
        target_name=event.name,
        project_slug=slug,
        payload=_update_payload(data),
    )
    return event


# Not audited, for the reason given above reorder_events: display order only.
@router.patch(
    "/{event_id}/move",
    response_model=EventResponse,
    dependencies=_editor_required,
)
async def move_event(
    session: SessionDep,
    slug: str,
    event_id: uuid.UUID,
    data: EventMove,
    branch_id: BranchIdDep,
) -> Event:
    return await event_service.move_event(session, slug, event_id, data, branch_id)


@router.delete("/{event_id}", status_code=204)
async def delete_event(
    session: SessionDep,
    slug: str,
    event_id: uuid.UUID,
    branch_id: BranchIdDep,
    current_user: EditorUserDep,
) -> None:
    # The service does the branch-scoped lookup, raises the 404 when the event is
    # missing, and hands back the name for the audit record. This is the row the
    # whole issue is about: ``event_changes`` is FK ondelete=CASCADE, so the
    # event's own history is destroyed by the very delete it would need to
    # record, and without this the deletion leaves no trace anywhere.
    name = await event_service.delete_event(session, slug, event_id, branch_id)
    await audit_service.record(
        session,
        user=current_user,
        action="event.delete",
        target_type="event",
        target_id=event_id,
        target_name=name,
        project_slug=slug,
    )
