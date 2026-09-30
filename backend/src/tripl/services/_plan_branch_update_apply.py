"""The writes of "Update from main" (PL-8), split out of ``plan_branch_update_service``.

``_plan_branch_three_way`` decides WHAT to write — a list of ``Op`` — and this
module writes it onto the branch's rows in the order that keeps every identity
free when it is needed. Nothing here commits: the caller owns the transaction,
the new base revision and the answer to a constraint the database refuses.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload

from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_meta_value import EventMetaValue
from tripl.models.event_photo import EventPhoto
from tripl.models.event_photo_comment import EventPhotoComment
from tripl.models.event_type import EventType
from tripl.models.event_type_relation import EventTypeRelation
from tripl.models.field_definition import FieldDefinition
from tripl.models.meta_field_definition import MetaFieldDefinition
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import (
    VariableEventValueOverride,
    copy_override_values,
)
from tripl.schemas.plan_branch import EntityChangeCount
from tripl.services._plan_branch_three_way_model import ENTITY_TYPES, Op
from tripl.services._plan_branch_update_contexts import copy_value_contexts
from tripl.services.plan_branch_merge_service import rename_variables_with_parking
from tripl.services.plan_branch_revert_service import (
    delete_branch_entity,
    recreate_from_snapshot,
    write_snapshot_fields,
)
from tripl.services.variable_service import rewrite_variable_token_references

_MODELS: dict[str, Any] = {
    "event_type": EventType,
    "field_definition": FieldDefinition,
    "meta_field": MetaFieldDefinition,
    "variable": Variable,
    "event": Event,
    "relation": EventTypeRelation,
}

# Written after every creation has landed: a successor or an override may name
# an event this very update creates.
_DEFERRED_FIELDS = frozenset({"superseded_by", "event_value_overrides"})

# A ``${token}`` parked between the two passes of a rename cycle. Outside what a
# variable name admits, so a value that ever escaped would be unmistakable, and
# free of ``_`` / ``%``, which the rewrite's LIKE would read as wildcards.
_TOKEN_PARKING_PREFIX = "PARKED-RENAME-"


def _photo_identity(photo: EventPhoto) -> tuple[Any, ...]:
    return (photo.kind, photo.storage_key, photo.external_url)


def _counts_list(counts: dict[str, dict[str, int]]) -> list[EntityChangeCount]:
    return [
        EntityChangeCount(entity_type=entity_type, **counts[entity_type])
        for entity_type in ENTITY_TYPES
        if entity_type in counts and any(counts[entity_type].values())
    ]


# --- apply --------------------------------------------------------------------


class _Applier:
    def __init__(
        self,
        session: AsyncSession,
        project_id: uuid.UUID,
        branch_id: uuid.UUID,
        main_payload: dict[str, Any],
        origins_complete: bool = True,
    ) -> None:
        self.session = session
        self.project_id = project_id
        self.branch_id = branch_id
        self.main_payload = main_payload
        self.origins_complete = origins_complete
        self.counts: dict[str, dict[str, int]] = {}

    def bump(self, entity_type: str, kind: str) -> None:
        counts = self.counts.setdefault(
            entity_type, {"added": 0, "changed": 0, "removed": 0, "renamed": 0}
        )
        counts[kind] += 1

    async def row(self, entity_type: str, item: dict[str, Any] | None) -> Any:
        if item is None or item.get("id") is None:
            return None
        row = await self.session.get(_MODELS[entity_type], uuid.UUID(str(item["id"])))
        if row is None or getattr(row, "branch_id", self.branch_id) != self.branch_id:
            return None
        return row

    # -- deletions -----------------------------------------------------------

    async def delete_events(self, events: Iterable[Event]) -> None:
        events = list(events)
        if not events:
            return
        event_ids = [event.id for event in events]
        # Photos carry no ORM relationship, so SQLite (no FK cascade) would
        # keep them. Their blobs stay: main's rows point at the same objects.
        photo_ids = select(EventPhoto.id).where(EventPhoto.event_id.in_(event_ids))
        await self.session.execute(
            delete(EventPhotoComment).where(EventPhotoComment.photo_id.in_(photo_ids))
        )
        await self.session.execute(delete(EventPhoto).where(EventPhoto.event_id.in_(event_ids)))
        await self.session.execute(
            delete(VariableEventValueOverride).where(
                VariableEventValueOverride.event_id.in_(event_ids)
            )
        )
        for event in events:
            await delete_branch_entity(
                self.session, project_id=self.project_id, entity_type="event", entity=event
            )

    async def relations_touching(
        self, *, event_type_id: uuid.UUID | None = None, field_id: uuid.UUID | None = None
    ) -> list[EventTypeRelation]:
        clause = (
            or_(
                EventTypeRelation.source_event_type_id == event_type_id,
                EventTypeRelation.target_event_type_id == event_type_id,
            )
            if event_type_id is not None
            else or_(
                EventTypeRelation.source_field_id == field_id,
                EventTypeRelation.target_field_id == field_id,
            )
        )
        rows = await self.session.execute(
            select(EventTypeRelation).where(EventTypeRelation.branch_id == self.branch_id, clause)
        )
        return list(rows.scalars().all())

    async def delete(self, op: Op) -> None:
        entity_type = op.entity_type
        row = await self.row(entity_type, op.branch)
        if row is None:
            return
        self.bump(entity_type, "removed")
        if entity_type == "event_type":
            for relation in await self.relations_touching(event_type_id=row.id):
                await self.session.delete(relation)
            events = await self.session.execute(select(Event).where(Event.event_type_id == row.id))
            await self.delete_events(events.scalars().all())
            await self.session.flush()
            await delete_branch_entity(
                self.session, project_id=self.project_id, entity_type="event_type", entity=row
            )
        elif entity_type == "field_definition":
            for relation in await self.relations_touching(field_id=row.id):
                await self.session.delete(relation)
            await self.session.execute(
                delete(EventFieldValue).where(EventFieldValue.field_definition_id == row.id)
            )
            await self.session.delete(row)
        elif entity_type == "meta_field":
            await self.session.execute(
                delete(EventMetaValue).where(EventMetaValue.meta_field_definition_id == row.id)
            )
            await self.session.delete(row)
        elif entity_type == "event":
            await self.delete_events([row])
        elif entity_type == "variable":
            await self.session.execute(
                delete(VariableEventValueOverride).where(
                    VariableEventValueOverride.variable_id == row.id
                )
            )
            await self.session.delete(row)
        else:
            await self.session.delete(row)

    # -- item fitting ----------------------------------------------------------

    async def fit_values(self, item: dict[str, Any], event_type_name: str) -> dict[str, Any]:
        """Main's event item with values only for definitions the branch has.

        A definition the branch deleted and kept deleted (the user chose to
        keep the branch's side) cannot hold values; the revert's writers
        refuse such values outright, which here would make the kept choice
        impossible to apply.
        """
        field_names = set(
            (
                await self.session.execute(
                    select(FieldDefinition.name)
                    .join(EventType, EventType.id == FieldDefinition.event_type_id)
                    .where(
                        EventType.branch_id == self.branch_id,
                        EventType.name == event_type_name,
                    )
                )
            )
            .scalars()
            .all()
        )
        meta_names = set(
            (
                await self.session.execute(
                    select(MetaFieldDefinition.name).where(
                        MetaFieldDefinition.branch_id == self.branch_id
                    )
                )
            )
            .scalars()
            .all()
        )
        return {
            **item,
            "field_values": [
                value
                for value in item.get("field_values") or []
                if value.get("field_name") in field_names
            ],
            "meta_values": [
                value
                for value in item.get("meta_values") or []
                if value.get("meta_field_name") in meta_names
            ],
        }

    async def relation_ends_exist(self, item: dict[str, Any]) -> bool:
        wanted = {
            (item["source_event_type_name"], item["source_field_name"]),
            (item["target_event_type_name"], item["target_field_name"]),
        }
        rows = await self.session.execute(
            select(EventType.name, FieldDefinition.name)
            .join(FieldDefinition, FieldDefinition.event_type_id == EventType.id)
            .where(
                EventType.branch_id == self.branch_id,
                EventType.name.in_([name for name, _ in wanted]),
            )
        )
        return wanted <= {(et_name, fd_name) for et_name, fd_name in rows.all()}

    async def event_type_exists(self, name: str) -> bool:
        found = await self.session.scalar(
            select(EventType.id).where(
                EventType.branch_id == self.branch_id, EventType.name == name
            )
        )
        return found is not None

    # -- creations ---------------------------------------------------------

    async def create(self, op: Op) -> Any:
        entity_type = op.entity_type
        item = op.main
        assert item is not None
        parent: str | None = None
        if entity_type == "variable":
            # Overrides name events, and an event this update creates may not
            # exist yet: they are written with the deferred fields.
            item = {**item, "event_value_overrides": []}
        elif entity_type == "field_definition":
            parent = str(item["_et"])
            if not await self.event_type_exists(parent):
                return None
        elif entity_type == "event":
            parent = str(item["event_type_name"])
            if not await self.event_type_exists(parent):
                return None
            item = await self.fit_values(item, parent)
            # The successor is written with the deferred fields, by main's id:
            # the rebuild's own lookup reads the dotted name, which a rename
            # or a namesake on the branch sends astray.
            item = {**item, "superseded_by": None}
        elif entity_type == "relation" and not await self.relation_ends_exist(item):
            return None
        row = await recreate_from_snapshot(
            self.session,
            project_id=self.project_id,
            branch_id=self.branch_id,
            entity_type=entity_type,
            item=item,
            payload=self.main_payload,
            parent=parent,
        )
        self.bump(entity_type, "added")
        return row

    async def copy_photos(self, target_event_id: uuid.UUID, source_event_id: uuid.UUID) -> None:
        """Make the branch event's attachments main's, keeping the branch's discussion.

        Rows only: the storage keys are shared with main's rows, exactly as the
        branch deep copy shares them, so no blob is copied or deleted. A photo
        both sides hold (same kind, storage key and URL) keeps its branch row,
        and with it the branch's comments; one only the branch held goes, with
        its thread; one only main holds is added bare. Discussion is not plan
        content: main's is never imported, the branch's is never overwritten.
        """
        existing = list(
            (
                await self.session.execute(
                    select(EventPhoto)
                    .where(EventPhoto.event_id == target_event_id)
                    .order_by(EventPhoto.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        photos = (
            (
                await self.session.execute(
                    select(EventPhoto)
                    .where(EventPhoto.event_id == source_event_id)
                    .order_by(EventPhoto.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        spare: dict[tuple[Any, ...], list[EventPhoto]] = {}
        for photo in existing:
            spare.setdefault(_photo_identity(photo), []).append(photo)
        for photo in photos:
            same = spare.get(_photo_identity(photo))
            if same:
                kept = same.pop(0)
                kept.original_filename = photo.original_filename
                kept.content_type = photo.content_type
                kept.size_bytes = photo.size_bytes
                kept.storage_backend = photo.storage_backend
                kept.storage_org_id = photo.storage_org_id
                kept.storage_config_id = photo.storage_config_id
                kept.sort_order = photo.sort_order
                kept.uploaded_by_user_id = photo.uploaded_by_user_id
                continue
            self.session.add(
                EventPhoto(
                    id=uuid.uuid4(),
                    project_id=photo.project_id,
                    event_id=target_event_id,
                    uploaded_by_user_id=photo.uploaded_by_user_id,
                    original_filename=photo.original_filename,
                    content_type=photo.content_type,
                    size_bytes=photo.size_bytes,
                    kind=photo.kind,
                    external_url=photo.external_url,
                    storage_backend=photo.storage_backend,
                    storage_key=photo.storage_key,
                    storage_org_id=photo.storage_org_id,
                    storage_config_id=photo.storage_config_id,
                    sort_order=photo.sort_order,
                )
            )
        gone = [photo.id for rest in spare.values() for photo in rest]
        if gone:
            await self.session.execute(
                delete(EventPhotoComment).where(EventPhotoComment.photo_id.in_(gone))
            )
            await self.session.execute(delete(EventPhoto).where(EventPhoto.id.in_(gone)))

    # -- references by main's id -------------------------------------------------
    #
    # A successor and an override name another event. The snapshot spells that
    # event by name, and names move: the branch may have renamed it, or hold a
    # namesake of it. The revert's writers look the name up and refuse (or
    # clear) what they cannot place, which is right for a revert and wrong
    # here: main's row names its target by id, and the branch copy of that
    # target carries the id as ``origin_id``. So these read main's own rows.

    async def branch_event_for(self, main_event_id: uuid.UUID) -> Event | None:
        """The branch copy of main's event ``main_event_id``, or None when there is none.

        By ``origin_id``; by name only on a branch opened before origin ids,
        and then only among rows no origin claims. None means the branch no
        longer holds the event (it deleted it and kept that): a reference into
        it goes the way the branch's own deletion took its references.
        """
        copies = list(
            (
                await self.session.execute(
                    select(Event).where(
                        Event.branch_id == self.branch_id, Event.origin_id == main_event_id
                    )
                )
            )
            .scalars()
            .all()
        )
        if len(copies) == 1:
            return copies[0]
        if copies or self.origins_complete:
            return None
        main_event = await self.session.get(Event, main_event_id)
        if main_event is None:
            return None
        type_name = await self.session.scalar(
            select(EventType.name).where(EventType.id == main_event.event_type_id)
        )
        namesakes = list(
            (
                await self.session.execute(
                    select(Event)
                    .join(EventType, EventType.id == Event.event_type_id)
                    .where(
                        Event.branch_id == self.branch_id,
                        Event.origin_id.is_(None),
                        EventType.name == type_name,
                        Event.name == main_event.name,
                    )
                )
            )
            .scalars()
            .all()
        )
        if len(namesakes) > 1:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Main points at the event '{type_name}.{main_event.name}', which this "
                    "branch holds more than once under that name. The branch predates origin "
                    "tracking, so there is no telling which copy is meant. Rename one of them "
                    "on the branch, then update from main again."
                ),
            )
        return namesakes[0] if namesakes else None

    async def write_successor(self, event: Event, item: dict[str, Any]) -> None:
        """Point the branch ``event`` at the copy of main's successor for ``item``."""
        main_event = await self.session.get(Event, uuid.UUID(str(item["id"])))
        successor_id = main_event.superseded_by_event_id if main_event is not None else None
        target = await self.branch_event_for(successor_id) if successor_id is not None else None
        event.superseded_by_event_id = (
            target.id if target is not None and target.id != event.id else None
        )

    async def write_overrides(
        self,
        variable: Variable,
        item: dict[str, Any],
        only: Iterable[str] | None = None,
        keep: Iterable[str] = (),
    ) -> None:
        """Copy main's overrides of ``item`` onto the branch ``variable``.

        ``only`` (main event ids): just the overrides on those events, the rest
        of the branch's left as they are. ``keep`` (branch event ids): events
        kept against main's deletion, whose branch overrides a whole write
        leaves in place. An override on an event the branch no
        longer holds is dropped, as ``fit_values`` drops a value whose
        definition the branch no longer holds.
        """
        stmt = select(VariableEventValueOverride).where(
            VariableEventValueOverride.variable_id == uuid.UUID(str(item["id"]))
        )
        if only is not None:
            stmt = stmt.where(
                VariableEventValueOverride.event_id.in_([uuid.UUID(str(i)) for i in only])
            )
        main_overrides = list((await self.session.execute(stmt)).scalars().all())
        placed: list[tuple[uuid.UUID, list[Any] | None, bool]] = []
        for override in main_overrides:
            target = await self.branch_event_for(override.event_id)
            if target is not None:
                placed.append((target.id, copy_override_values(override.values), override.required))
        clear = delete(VariableEventValueOverride).where(
            VariableEventValueOverride.variable_id == variable.id,
            VariableEventValueOverride.branch_id == self.branch_id,
        )
        if only is not None:
            clear = clear.where(
                VariableEventValueOverride.event_id.in_([event_id for event_id, _, _ in placed])
            )
        kept = [uuid.UUID(str(event_id)) for event_id in keep]
        if kept:
            clear = clear.where(VariableEventValueOverride.event_id.not_in(kept))
            placed = [entry for entry in placed if entry[0] not in kept]
        await self.session.execute(clear)
        for event_id, values, required in placed:
            self.session.add(
                VariableEventValueOverride(
                    id=uuid.uuid4(),
                    project_id=self.project_id,
                    branch_id=self.branch_id,
                    variable_id=variable.id,
                    event_id=event_id,
                    values=values,
                    required=required,
                )
            )

    async def write_references(
        self,
        entity_type: str,
        row: Any,
        item: dict[str, Any],
        fields: Iterable[str],
        keep: Iterable[str] = (),
    ) -> None:
        for field in fields:
            if entity_type == "event" and field == "superseded_by":
                await self.write_successor(row, item)
            elif entity_type == "variable" and field == "event_value_overrides":
                await self.write_overrides(row, item, keep=keep)
            else:  # pragma: no cover - _DEFERRED_FIELDS names only these two
                raise ValueError(f"{entity_type}.{field} is not a reference field")

    async def quiet_branch_event_for(self, main_event_id: uuid.UUID) -> Event | None:
        """``branch_event_for`` without its refusal: an unplaceable context is skipped."""
        try:
            return await self.branch_event_for(main_event_id)
        except HTTPException:
            return None

    # -- source names ----------------------------------------------------------

    async def park_source_names(self, ops: list[Op]) -> None:
        """Clear every ``source_name`` main moves, before any is written or created.

        Main can move a scan identity from one row to another, or swap two.
        Written one row at a time, the first UPDATE would meet the old holder
        under ``uq_variable_project_source_name`` / ``uq_event_scan_identity``,
        so each moving row lets go first (NULL, which neither constraint
        counts), and step 4 writes the final values — the parking the variable
        renames get, for the other unique column.
        """
        parked = False
        for op in ops:
            if (
                op.kind != "write"
                or "source_name" not in op.fields
                or op.entity_type not in ("event", "variable")
            ):
                continue
            row = await self.row(op.entity_type, op.branch)
            if row is not None and row.source_name is not None:
                row.source_name = None
                parked = True
        if parked:
            await self.session.flush()

    # -- renames -------------------------------------------------------------

    async def rename_variables(self, renames: dict[str, str]) -> None:
        rows = await self.session.execute(
            select(Variable)
            .where(Variable.project_id == self.project_id, Variable.branch_id == self.branch_id)
            .options(lazyload(Variable.value_contexts))
        )
        by_name = {variable.name: variable for variable in rows.scalars().all()}
        renames = {old: new for old, new in renames.items() if old in by_name}
        if renames:
            await rename_variables_with_parking(self.session, by_name, renames)

    async def rewrite_tokens(self, renames: dict[str, str]) -> None:
        """Carry ``${old}`` to ``${new}`` in the branch's values, cycles included.

        Runs before main's values are written or created: those already name
        the new tokens, and main may have re-used an old name for a new
        variable. Through a parking token, because a swap rewritten one pair at
        a time would fuse both tokens into one.
        """
        parked = {old: f"{_TOKEN_PARKING_PREFIX}{index}" for index, old in enumerate(renames)}
        for old, park in parked.items():
            await rewrite_variable_token_references(
                self.session,
                project_id=self.project_id,
                branch_id=self.branch_id,
                old_name=old,
                new_name=park,
            )
        for old, park in parked.items():
            await rewrite_variable_token_references(
                self.session,
                project_id=self.project_id,
                branch_id=self.branch_id,
                old_name=park,
                new_name=renames[old],
            )


async def apply_update_plan(
    session: AsyncSession,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    ops: list[Op],
    main_payload: dict[str, Any],
    *,
    origins_complete: bool = True,
) -> list[EntityChangeCount]:
    """Write the plan onto the branch; commit nothing.

    In the order that keeps every identity free when it is needed (design §2):
    main's deletions first and flushed — SQLAlchemy saves before it deletes, so
    a name or ``source_name`` main moved onto another row would otherwise still
    be held — then main's renames through the parking pass and the branch's
    ``${token}`` rewrite, then every moving ``source_name`` parked, then
    additions, parents first, then field writes, and last the fields that name
    other rows.
    """
    applier = _Applier(session, project_id, branch_id, main_payload, origins_complete)

    # 1. Deletions, children first.
    for entity_type in ("relation", "event", "field_definition", "meta_field", "variable"):
        for op in ops:
            if op.kind == "delete" and op.entity_type == entity_type:
                await applier.delete(op)
        await session.flush()
    for op in ops:
        if op.kind == "delete" and op.entity_type == "event_type":
            await applier.delete(op)
    await session.flush()

    # 2. Renames.
    variable_renames: dict[str, str] = {}
    for op in ops:
        if op.kind != "write" or "name" not in op.fields or op.main is None:
            continue
        assert op.branch is not None
        if op.entity_type == "variable":
            variable_renames[str(op.branch["name"])] = str(op.main["name"])
        else:
            row = await applier.row(op.entity_type, op.branch)
            if row is None:
                continue
            row.name = op.main["name"]
        applier.bump(op.entity_type, "renamed")
    if variable_renames:
        await applier.rename_variables(variable_renames)
    await session.flush()
    # Carry ``${old}`` to ``${new}`` now, while every value on the branch is
    # still the branch's own: main's values, written and created below,
    # already name the new tokens — and one naming a variable main then
    # created under an old name must not be re-pointed. A value collection
    # step 4 rebuilds from a snapshot was re-tokened by the planner
    # (``_Planner.branch_renames``), so it does not put an old token back.
    if variable_renames:
        await applier.rewrite_tokens(variable_renames)
        await session.flush()
    await applier.park_source_names(ops)

    # 3. Additions, parents first.
    deferred: list[tuple[str, Any, dict[str, Any], list[str], tuple[str, ...]]] = []
    created: dict[str, list[tuple[Any, dict[str, Any]]]] = {"event": [], "variable": []}
    for group in (
        ("event_type", "meta_field", "variable"),
        ("field_definition",),
        ("event",),
        ("relation",),
    ):
        for op in ops:
            if op.kind != "create" or op.entity_type not in group or op.main is None:
                continue
            row = await applier.create(op)
            if row is None:
                continue
            item = op.main
            if op.entity_type in created:
                created[op.entity_type].append((row, item))
            if op.entity_type == "event":
                await session.flush()
                await applier.copy_photos(row.id, uuid.UUID(str(item["id"])))
                if item.get("superseded_by"):
                    deferred.append(("event", row, item, ["superseded_by"], ()))
            elif op.entity_type == "variable" and item.get("event_value_overrides"):
                deferred.append(("variable", row, item, ["event_value_overrides"], ()))
        await session.flush()
    await copy_value_contexts(
        session, project_id, branch_id, created, applier.quiet_branch_event_for
    )

    # 4. Field writes on paired rows, and the origin links.
    for op in ops:
        if op.kind != "write" or op.main is None:
            continue
        fields = [field for field in op.fields if field != "name"]
        if not fields:
            continue
        row = await applier.row(op.entity_type, op.branch)
        if row is None:
            continue
        item = op.main
        if op.entity_type == "event":
            item = await applier.fit_values(item, str(item["event_type_name"]))
        now = [field for field in fields if field not in _DEFERRED_FIELDS and field != "photos"]
        if now:
            await write_snapshot_fields(
                session,
                project_id=project_id,
                branch_id=branch_id,
                entity_type=op.entity_type,
                entity=row,
                item=item,
                payload=main_payload,
                fields=now,
                parent=item.get("event_type_name") if op.entity_type == "event" else None,
            )
        if "photos" in fields:
            await applier.copy_photos(row.id, uuid.UUID(str(item["id"])))
        later = [field for field in fields if field in _DEFERRED_FIELDS]
        if later:
            deferred.append((op.entity_type, row, item, later, op.keep))
        applier.bump(op.entity_type, "changed")
    for op in ops:
        if op.kind != "origin":
            continue
        row = await applier.row(op.entity_type, op.branch)
        if row is not None:
            row.origin_id = uuid.UUID(str(op.main["id"])) if op.main is not None else None
    await session.flush()

    # 5. Fields that name other rows, now that every row exists — by main's
    # ids, so a target the branch renamed or holds a namesake of is still found.
    for entity_type, row, item, later, keep in deferred:
        await applier.write_references(entity_type, row, item, later, keep)
    for op in ops:
        if op.kind != "link" or op.main is None:
            continue
        row = await applier.row(op.entity_type, op.branch)
        if row is None:
            continue
        if op.entity_type == "variable":
            await applier.write_overrides(row, op.main, only=op.targets)
        elif row.superseded_by_event_id is None:
            await applier.write_successor(row, op.main)
        applier.bump(op.entity_type, "changed")
    await session.flush()
    return _counts_list(applier.counts)
