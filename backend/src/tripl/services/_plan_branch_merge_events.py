"""The merge's event-type and event arms: the two that delete main events.

Both collect the photo blobs of the main events they delete, and clear the
references other rows hold to those events, before the rows go: the attachments
leave by a database cascade nothing in Python sees (``_blob_keys_of``).
``_plan_branch_merge_state`` lists where these arms sit in the merge.

Nothing here commits.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from tripl.models.event import Event
from tripl.models.event import EventStatus as _ES
from tripl.models.event import event_status_rank as _rank
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_meta_value import EventMetaValue
from tripl.models.event_photo import EventPhoto
from tripl.models.event_tag import EventTag
from tripl.models.event_type import EventType
from tripl.services._event_reference_cleanup import drop_dangling_event_references
from tripl.services._plan_branch_merge_state import (
    MergeContext,
    MergedEvents,
    MergedEventTypes,
    MergedFields,
    MergedMetaFields,
)
from tripl.services._plan_merge_slots import merge_slots
from tripl.services.event_photo_service import PHOTO_KIND_PHOTO, BlobRef
from tripl.services.plan_branch_conflicts import _ET_CHANGE_KEYS, main_keeps
from tripl.services.scan_config_lookup import (
    event_type_binding_conflict_detail,
    scan_configs_binding_event_types,
)


async def _blob_keys_of(session: AsyncSession, event_ids: Sequence[uuid.UUID]) -> set[BlobRef]:
    """Every uploaded blob the given events' attachments point at.

    Read BEFORE the rows go, because they go by FK cascade: ``EventPhoto``
    declares ``ondelete="CASCADE"`` on ``event_id`` and ``Event`` carries no
    ``photos`` relationship, so deleting an event takes its attachments at the
    database level with nothing in Python seeing them leave.
    The merge deletes events on two paths — a removed event type takes its
    events, and a removed event goes on its own — and neither reached the photo
    reconciliation that fills ``released_blobs``, so a screenshot on an event
    the branch deleted stayed in storage with no row left to find it by.

    Over-collecting is safe: ``delete_unreferenced_blobs`` re-checks every key
    against the rows that survived the commit and skips the ones still in use.
    """
    if not event_ids:
        return set()
    rows = await session.execute(
        select(
            EventPhoto.storage_backend, EventPhoto.storage_key, EventPhoto.storage_config_id
        ).where(
            EventPhoto.event_id.in_(event_ids),
            EventPhoto.kind == PHOTO_KIND_PHOTO,
            EventPhoto.storage_backend.is_not(None),
            EventPhoto.storage_key.is_not(None),
        )
    )
    # The query already drops NULL keys; the guard only tells the type checker so.
    return {
        (str(backend), key, config_id) for backend, key, config_id in rows.all() if key is not None
    }


async def apply_event_types(ctx: MergeContext) -> MergedEventTypes:
    """Event types: three-way per metadata field, honouring ``ctx.resolutions``.

    A matched type is updated in place; one new on the branch is created, and
    one main deleted after the cut stays deleted; one the branch removed is
    deleted from main with its events, and the whole merge is refused when a
    scan is bound to it.
    """
    session, project_id, branch_id = ctx.session, ctx.project_id, ctx.branch_id
    main_branch_id, resolutions = ctx.main_branch_id, ctx.resolutions
    base_et_by_name: dict[str, dict[str, Any]] = {
        e["name"]: e for e in ctx.base_payload.get("event_types", [])
    }
    main_ets = list(
        (
            await session.execute(
                select(EventType)
                .where(EventType.project_id == project_id, EventType.branch_id == main_branch_id)
                .options(selectinload(EventType.field_definitions))
            )
        )
        .scalars()
        .all()
    )
    branch_ets = list(
        (
            await session.execute(
                select(EventType)
                .where(EventType.project_id == project_id, EventType.branch_id == branch_id)
                .options(selectinload(EventType.field_definitions))
            )
        )
        .scalars()
        .all()
    )
    main_et_by_name = {et.name: et for et in main_ets}
    branch_et_by_name = {et.name: et for et in branch_ets}

    for name, b_et in branch_et_by_name.items():
        m_et = main_et_by_name.get(name)
        if m_et is not None:
            # 3-way per-field merge. Falls back to branch-wins when no base
            # snapshot is available (legacy path).
            b_dict = base_et_by_name.get(name)
            for field in _ET_CHANGE_KEYS:
                choice = resolutions.get(("event_type", name, field))
                if choice == "ours":
                    continue
                if choice == "theirs":
                    setattr(m_et, field, getattr(b_et, field))
                    continue
                if main_keeps(field, b_dict, getattr(m_et, field)):
                    continue
                if b_dict is None:
                    setattr(m_et, field, getattr(b_et, field))
                    continue
                base_v = b_dict.get(field)
                theirs_v = getattr(b_et, field)
                # Branch changed this field → take it; otherwise keep main's
                # current value (which may include main-side edits).
                if theirs_v != base_v:
                    setattr(m_et, field, theirs_v)
        else:
            # The entity existed in the base but is now absent on main: that is
            # a main-only deletion. An unchanged branch must not resurrect it.
            # Divergent branch edits are rejected by conflict detection.
            if name in base_et_by_name:
                continue
            session.add(
                EventType(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    branch_id=main_branch_id,
                    name=b_et.name,
                    display_name=b_et.display_name,
                    description=b_et.description,
                    color=b_et.color,
                    order=b_et.order,
                )
            )
    removed_main_ets = [
        m_et
        for name, m_et in main_et_by_name.items()
        if name in base_et_by_name and name not in branch_et_by_name
    ]
    # The second door onto ``scan_configs.event_type_id``'s ON DELETE SET NULL,
    # after ``event_type_service.delete_event_type``: merging a branch that
    # removed an event type deletes main's copy, and every scan bound to it goes
    # on running against an empty binding, collecting nothing.
    # Refused for the reason ``_reject_removals_a_scan_names_events_by`` — the
    # field-removal guard the field-definition arm awaits next — gives in its
    # docstring: a merge refuses whole rather than skipping the deletion.
    if removed_main_ets:
        binding = await scan_configs_binding_event_types(
            session,
            project_id=project_id,
            event_type_ids=[m_et.id for m_et in removed_main_ets],
        )
        blocked_types = [
            event_type_binding_conflict_detail(
                configs=binding[m_et.id],
                lead=f"Cannot merge this branch: merging deletes '{m_et.name}' from main.",
                then="merge the branch",
            )
            for m_et in removed_main_ets
            if binding.get(m_et.id)
        ]
        if blocked_types:
            raise HTTPException(status_code=409, detail=" ".join(blocked_types))
    if removed_main_ets:
        # BEFORE the delete, and this placement is the substance of the fix.
        # Deleting the event type takes its events with it through the database
        # cascade at the flush below — EventType maps no ``events`` relationship,
        # so no service ever sees those rows go. Their dangling references have
        # to be cleared here or nowhere.
        #
        # There is no survivor to re-point at: these main events lose their event
        # type outright, so the rule is DROP, exactly as on the three CRUD delete
        # doors.
        doomed_event_ids = list(
            (
                await session.execute(
                    select(Event.id).where(
                        Event.event_type_id.in_([m_et.id for m_et in removed_main_ets])
                    )
                )
            )
            .scalars()
            .all()
        )
        ctx.released_blobs.update(await _blob_keys_of(session, doomed_event_ids))
        await drop_dangling_event_references(
            session, project_id=project_id, event_ids=doomed_event_ids
        )
    for name, m_et in list(main_et_by_name.items()):
        if name in base_et_by_name and name not in branch_et_by_name:
            await session.delete(m_et)
            del main_et_by_name[name]
    await session.flush()

    # Re-load main event types so name→id mapping reflects new inserts.
    main_ets_after = list(
        (
            await session.execute(
                select(EventType).where(
                    EventType.project_id == project_id, EventType.branch_id == main_branch_id
                )
            )
        )
        .scalars()
        .all()
    )
    main_et_name_to_id = {et.name: et.id for et in main_ets_after}
    branch_et_id_to_name = {et.id: et.name for et in branch_ets}
    return MergedEventTypes(
        base_et_by_name=base_et_by_name,
        branch_ets=branch_ets,
        branch_et_by_name=branch_et_by_name,
        main_ets_after=main_ets_after,
        main_et_name_to_id=main_et_name_to_id,
        branch_et_id_to_name=branch_et_id_to_name,
    )


async def apply_events(
    ctx: MergeContext,
    event_types: MergedEventTypes,
    fields: MergedFields,
    meta_fields: MergedMetaFields,
) -> MergedEvents:
    """Events: each branch row onto the main row it pairs with, ids preserved.

    Field values, meta values and tags are replayed onto main per event, each
    only when the branch changed it from the base; the successor pointer is
    translated last (``_translate_successors``). Returns where every branch row
    landed, and which of main's events are still standing afterwards.
    """
    session, project_id, branch_id = ctx.session, ctx.project_id, ctx.branch_id
    main_branch_id, base_payload = ctx.main_branch_id, ctx.base_payload
    branch_origins_complete = ctx.branch_origins_complete
    branch_snapshot_payload = ctx.branch_snapshot_payload
    main_ets_after = event_types.main_ets_after
    main_et_name_to_id = event_types.main_et_name_to_id
    branch_et_id_to_name = event_types.branch_et_id_to_name
    main_field_by_key = fields.main_field_by_key
    branch_field_by_id = fields.branch_field_by_id
    main_meta_field_id = meta_fields.main_meta_field_id
    main_events = list(
        (
            await session.execute(
                select(Event)
                .where(Event.project_id == project_id, Event.branch_id == main_branch_id)
                .options(
                    selectinload(Event.field_values),
                    selectinload(Event.meta_values),
                    selectinload(Event.tags),
                )
            )
        )
        .scalars()
        .all()
    )
    branch_events = list(
        (
            await session.execute(
                select(Event)
                .where(Event.project_id == project_id, Event.branch_id == branch_id)
                .options(
                    selectinload(Event.field_values),
                    selectinload(Event.meta_values),
                    selectinload(Event.tags),
                )
            )
        )
        .scalars()
        .all()
    )
    main_et_id_to_name = {et.id: et.name for et in main_ets_after}
    # Events whose parent event_type was just removed are orphans on main; in
    # Postgres they'd cascade-delete via the FK, SQLite (test env) doesn't, so
    # we delete them explicitly to keep the in-memory lookup consistent.
    for e in main_events:
        if e.event_type_id not in main_et_id_to_name:
            await session.delete(e)
    await session.flush()
    main_events = [e for e in main_events if e.event_type_id in main_et_id_to_name]
    # The subscript below was unguarded, and it is the FIRST place a branch event
    # parented by another branch's type lands — the pre-refusal row shape
    # ``event_service`` now blocks, with no migration sweeping the ones already
    # stored. A KeyError here is the same bare 500 on the merge,
    # naming nothing, that ``deep_copy_plan_to_branch`` now answers 409 for.
    #
    # Deliberately NOT the deletion main's events get a few lines above: there
    # the type is missing because THIS merge removed it, so its events are doomed
    # by the merge itself. Here the type is simply on another branch, and
    # dropping the event would carry onto main a deletion nobody made.
    mis_parented = [e for e in branch_events if e.event_type_id not in branch_et_id_to_name]
    if mis_parented:
        raise HTTPException(
            status_code=409,
            detail=" ".join(
                f"Cannot merge this branch: event '{e.name}' is parented by "
                f"{e.event_type_id}, which is not an event type of this branch, so it "
                "has no type on main to land under. Delete the event, then merge."
                for e in mis_parented
            ),
        )

    def main_event_key(event: Event) -> tuple[str, str]:
        return (main_et_id_to_name[event.event_type_id], event.name)

    def branch_event_key(event: Event) -> tuple[str, str]:
        return (branch_et_id_to_name[event.event_type_id], event.name)

    def base_event_key(event: dict[str, Any]) -> tuple[str, str]:
        return (event["event_type_name"], event["name"])

    branch_event_snapshot_by_id: dict[str, dict[str, Any]] = {
        event["id"]: event for event in branch_snapshot_payload.get("events", [])
    }
    # Row by row, not key by key. Main's rows pair with the
    # base by their own ids, which the base recorded; the branch's by the main
    # row each copy was made from. Keyed by (type, name) instead, two namesakes
    # collapsed to one on every side: deleting one of them on the branch left
    # both on main, and each copy's edits landed on whichever main namesake the
    # dict kept. The natural key still pairs the rows the ids
    # do not place — see ``merge_slots``.
    #
    # A slot whose branch copy moved to another name is a rename, and main's row
    # takes the new name in place. Event has no ``display_name`` — its machine
    # name is the one on screen, so renaming an event is routine editing — and
    # replacing the row would take its ``variable_values``, their drift rows and
    # its ``event_changes`` with it through the FK cascade, and leave the
    # ``event_metrics`` series holding a NULL ``event_id`` (that FK is SET NULL).
    # Only the name can differ: a copy under another TYPE is a removal plus an
    # addition, as the natural-key merge always read it. Events need no parking
    # pass: nothing is unique on the name, and ``uq_event_scan_identity`` is on
    # ``(event_type_id, source_name)``, which a name-only write never touches.
    event_slots = merge_slots(
        list(base_payload.get("events", [])),
        main_events,
        branch_events,
        base_key=base_event_key,
        main_key=main_event_key,
        branch_key=branch_event_key,
        main_ref=lambda event: str(event.id),
        branch_ref=lambda event: str(event.origin_id or event.id),
        follows_rename=lambda old_key, new_key: old_key[0] == new_key[0],
        branch_origins_complete=branch_origins_complete,
        identities=(
            lambda event: event.get("source_name"),
            lambda event: event.source_name,
            lambda event: event.source_name,
        ),
    )
    # (branch row, the main row it lands on, its base state) for every branch
    # row the merge carries onto main, created or matched. Every later pass that
    # follows a branch row onto main — the successor pointer, the photos, the
    # discussion and the variable overrides — reads it from here, so none of
    # them can pick a different namesake than the attribute writes did.
    landings: list[tuple[Event, Event, dict[str, Any] | None]] = []
    doomed_main_events: list[Event] = []
    event_attrs = (
        "source_name",
        "title",
        "description",
        "sunset_at",
        "order",
        "owner_id",
        "reviewed",
        "metric_breakdown_columns",
        "required_presence_threshold",
    )
    for slot in event_slots.slots:
        m_ev, b_ev, base_event = slot.main, slot.branch, slot.base
        if m_ev is None:
            # Main deleted the row after the cut (or never had it): main's
            # deletion stands, and divergent branch edits are the conflict
            # scan's to refuse.
            continue
        if b_ev is None:
            # The branch deleted THIS row — the very main namesake its copy
            # came from, not whichever shares the key.
            if base_event is not None and slot.branch_known:
                doomed_main_events.append(m_ev)
            continue
        if (
            base_event is not None
            and branch_event_key(b_ev) != base_event_key(base_event)
            and main_event_key(m_ev) == base_event_key(base_event)
        ):
            m_ev.name = b_ev.name
        landings.append((b_ev, m_ev, base_event))
        branch_event_snapshot = branch_event_snapshot_by_id[str(b_ev.id)]
        for attr in event_attrs:
            if main_keeps(attr, base_event, getattr(m_ev, attr)):
                continue
            if base_event is None or branch_event_snapshot.get(attr) != base_event.get(attr):
                branch_value = getattr(b_ev, attr)
                if attr == "metric_breakdown_columns":
                    branch_value = list(branch_value or [])
                setattr(m_ev, attr, branch_value)
        if base_event is None or branch_event_snapshot.get("status") != base_event.get("status"):
            b_status = _ES(b_ev.status)
            m_status = _ES(m_ev.status)
            if b_status != _ES.archived:
                m_ev.status = b_status if _rank(b_status) >= _rank(m_status) else m_status

        if base_event is None or branch_event_snapshot.get("field_values") != base_event.get(
            "field_values"
        ):
            await session.execute(
                delete(EventFieldValue).where(EventFieldValue.event_id == m_ev.id)
            )
            for fv in b_ev.field_values:
                bf_et, bf_name = branch_field_by_id[fv.field_definition_id]
                session.add(
                    EventFieldValue(
                        id=uuid.uuid4(),
                        event_id=m_ev.id,
                        field_definition_id=main_field_by_key[(bf_et, bf_name)],
                        value=fv.value,
                        is_authored=fv.is_authored,
                    )
                )
        if base_event is None or branch_event_snapshot.get("meta_values") != base_event.get(
            "meta_values"
        ):
            await session.execute(delete(EventMetaValue).where(EventMetaValue.event_id == m_ev.id))
            for mv in b_ev.meta_values:
                session.add(
                    EventMetaValue(
                        id=uuid.uuid4(),
                        event_id=m_ev.id,
                        meta_field_definition_id=main_meta_field_id(b_ev, mv),
                        value=mv.value,
                    )
                )
        if base_event is None or branch_event_snapshot.get("tags") != base_event.get("tags"):
            await session.execute(delete(EventTag).where(EventTag.event_id == m_ev.id))
            for tag in b_ev.tags:
                session.add(EventTag(id=uuid.uuid4(), event_id=m_ev.id, name=tag.name))

    for b_ev in event_slots.created:
        et_name = branch_et_id_to_name[b_ev.event_type_id]
        if et_name not in main_et_name_to_id:
            continue
        new_ev_id = uuid.uuid4()
        created_event = Event(
            id=new_ev_id,
            project_id=project_id,
            branch_id=main_branch_id,
            event_type_id=main_et_name_to_id[et_name],
            name=b_ev.name,
            title=b_ev.title,
            source_name=b_ev.source_name,
            description=b_ev.description,
            order=b_ev.order,
            status=b_ev.status,
            sunset_at=b_ev.sunset_at,
            last_seen_at=b_ev.last_seen_at,
            metric_breakdown_columns=list(b_ev.metric_breakdown_columns or []),
            required_presence_threshold=b_ev.required_presence_threshold,
            owner_id=b_ev.owner_id,
            reviewed=b_ev.reviewed,
            # superseded_by_event_id is deliberately absent: the value on
            # the branch row is a BRANCH event id, meaningless on main.
            # ``_translate_successors``, after the flush below, sets it.
        )
        session.add(created_event)
        landings.append((b_ev, created_event, None))
        for fv in b_ev.field_values:
            bf_et, bf_name = branch_field_by_id[fv.field_definition_id]
            session.add(
                EventFieldValue(
                    id=uuid.uuid4(),
                    event_id=new_ev_id,
                    field_definition_id=main_field_by_key[(bf_et, bf_name)],
                    value=fv.value,
                    is_authored=fv.is_authored,
                )
            )
        for mv in b_ev.meta_values:
            session.add(
                EventMetaValue(
                    id=uuid.uuid4(),
                    event_id=new_ev_id,
                    meta_field_definition_id=main_meta_field_id(b_ev, mv),
                    value=mv.value,
                )
            )
        for tag in b_ev.tags:
            session.add(EventTag(id=uuid.uuid4(), event_id=new_ev_id, name=tag.name))
    main_target_by_branch_id: dict[uuid.UUID, Event] = {
        b_ev.id: target for b_ev, target, _ in landings
    }
    # Collect, clear, then delete. These are main events the branch removed on
    # purpose, so again there is no survivor and the rule is DROP.
    if doomed_main_events:
        doomed_main_event_ids = [m_ev.id for m_ev in doomed_main_events]
        ctx.released_blobs.update(await _blob_keys_of(session, doomed_main_event_ids))
        await drop_dangling_event_references(
            session,
            project_id=project_id,
            event_ids=doomed_main_event_ids,
        )
    for m_ev in doomed_main_events:
        await session.delete(m_ev)
    await session.flush()

    _translate_successors(landings, branch_event_snapshot_by_id, main_target_by_branch_id)

    # Which of main's events are still standing after every delete above: the
    # photos, the discussion and the overrides land only on those.
    main_events_after = list(
        (
            await session.execute(
                select(Event).where(
                    Event.project_id == project_id, Event.branch_id == main_branch_id
                )
            )
        )
        .scalars()
        .all()
    )
    surviving_main_ids = {e.id for e in main_events_after}
    return MergedEvents(
        branch_event_snapshot_by_id=branch_event_snapshot_by_id,
        landings=landings,
        main_target_by_branch_id=main_target_by_branch_id,
        surviving_main_ids=surviving_main_ids,
    )


def _translate_successors(
    landings: list[tuple[Event, Event, dict[str, Any] | None]],
    branch_event_snapshot_by_id: dict[str, dict[str, Any]],
    main_target_by_branch_id: dict[uuid.UUID, Event],
) -> None:
    """The successor pointer, translated rather than copied.

    ``apply_events``' ``event_attrs`` copies raw ORM values, which for this
    column would write a BRANCH event id onto a main row; the snapshot carries
    the successor as a natural key for exactly that reason, so the branch's own
    pointer is re-resolved against main here — through the landings, so a
    successor with a namesake resolves to the main row ITS copy landed on rather
    than the last row under the key. It runs after ``apply_events``' flush
    because a successor may be an event this very merge created, and because
    the FK is immediate.
    """
    for b_ev, target, base_event in landings:
        snapshot = branch_event_snapshot_by_id.get(str(b_ev.id), {})
        # Untouched on the branch: leave main's own answer alone, the same
        # three-way rule every attribute write in ``apply_events`` follows.
        if base_event is not None and snapshot.get("superseded_by") == base_event.get(
            "superseded_by"
        ):
            continue
        successor = (
            main_target_by_branch_id.get(b_ev.superseded_by_event_id)
            if b_ev.superseded_by_event_id is not None
            else None
        )
        # A successor the merge cannot place on main clears the pointer rather
        # than leaving a branch id behind: "replaced by something that is not
        # here" is not a fact worth keeping.
        target.superseded_by_event_id = successor.id if successor is not None else None
