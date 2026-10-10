"""The state a branch merge's arms share.

``plan_branch_merge_service._apply_merge`` applies a branch onto main one
entity kind at a time, each in a coroutine of its own, in this order:

1. event types (``_plan_branch_merge_events.apply_event_types``);
2. field definitions (``_plan_branch_merge_fields.apply_field_definitions``);
3. meta fields (``_plan_branch_merge_fields.apply_meta_fields``);
4. variables (``_plan_branch_merge_variables.apply_variables``);
5. events, their successor pointers included
   (``_plan_branch_merge_events.apply_events``);
6. the photos on each event that landed, with their discussion
   (``_plan_branch_merge_photos.apply_photos``);
7. each event's own discussion and its watchers, which are not plan content
   (``plan_branch_merge_service._hand_over_event_discussions``);
8. variable value overrides
   (``_plan_branch_merge_variables.apply_value_overrides``);
9. relations (``_plan_branch_merge_fields.apply_relations``).

The order is part of the behaviour: an arm reads the ids and names the arms
before it wrote, some of them only once an earlier flush has sent them. Each
arm takes the ``MergeContext`` and the results of the arms it reads, so what
flows from one to the next is spelled out in ``_apply_merge``.

Nothing here commits.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.event import Event
from tripl.models.event_meta_value import EventMetaValue
from tripl.models.event_type import EventType
from tripl.models.variable import Variable
from tripl.services.event_photo_service import BlobRef


@dataclass(frozen=True)
class MergeContext:
    """One merge's inputs, read by every arm.

    ``base_payload`` is the plan snapshot at the branch's cut, ``{}`` when the
    caller had none, and ``branch_snapshot_payload`` the branch's own, built
    before any arm writes. ``released_blobs`` is the one field the arms add
    to: every uploaded photo the merge deleted from main, for ``merge_branch``
    to release once the merge has committed.
    """

    session: AsyncSession
    project_id: uuid.UUID
    main_branch_id: uuid.UUID
    branch_id: uuid.UUID
    # (entity_type, name, field) -> "ours" | "theirs", for event-type metadata.
    resolutions: dict[tuple[str, str, str], str]
    base_payload: dict[str, Any]
    branch_origins_complete: bool
    branch_snapshot_payload: dict[str, Any]
    released_blobs: set[BlobRef] = field(default_factory=set)


@dataclass(frozen=True)
class MergedEventTypes:
    """What the event-type arm leaves for the arms after it."""

    base_et_by_name: dict[str, dict[str, Any]]
    branch_ets: list[EventType]
    branch_et_by_name: dict[str, EventType]
    # Main's event types re-read after the arm's inserts and deletes.
    main_ets_after: list[EventType]
    main_et_name_to_id: dict[str, uuid.UUID]
    branch_et_id_to_name: dict[uuid.UUID, str]


@dataclass(frozen=True)
class MergedFields:
    """Field definitions keyed by ``(event type name, field name)``, after the field arm."""

    main_field_by_key: dict[tuple[str, str], uuid.UUID]
    branch_field_by_id: dict[uuid.UUID, tuple[str, str]]


@dataclass(frozen=True)
class MergedMetaFields:
    """Meta-field definitions by name, after the meta-field arm."""

    main_mf_name_to_id: dict[str, uuid.UUID]
    branch_mf_id_to_name: dict[uuid.UUID, str]

    def main_meta_field_id(self, event: Event, mv: EventMetaValue) -> uuid.UUID:
        """Translate a branch meta value onto main by NAME, or refuse the merge.

        ``branch_mf_id_to_name`` holds this branch's own definitions only, so a
        value pointing at another branch's definition was an unqualified
        subscript — the same bare 500, from the same pre-refusal rows, that
        ``deep_copy_plan_to_branch`` now answers 409 for.
        ``event_service._normalize_meta_values`` refuses to write one today; no
        migration sweeps the ones already stored, and the merge is the second
        place they surface.

        Refusing rather than skipping the value: ``apply_events`` DELETEs main's
        whole set for a matched event and rebuilds it from the branch, and a
        created event takes the branch's set as it is, so a skipped value is one
        that disappears from main with nothing said.

        ``main_mf_name_to_id`` needs no guard of its own — the meta-field arm
        builds it after its upsert, which gives main a definition for every
        name this branch has.
        """
        mf_name = self.branch_mf_id_to_name.get(mv.meta_field_definition_id)
        if mf_name is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot merge this branch: event '{event.name}' carries a meta "
                    f"value on {mv.meta_field_definition_id}, which is not a meta "
                    "field of this branch, so it has no name to carry onto main. "
                    "Edit the event to drop that value, then merge."
                ),
            )
        return self.main_mf_name_to_id[mf_name]


@dataclass(frozen=True)
class MergedVariables:
    """The branch's variables, and the base's entries re-keyed onto any renames."""

    branch_vars: list[Variable]
    base_var_by_name: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class MergedEvents:
    """Where each branch event landed on main, for the arms that follow one there."""

    branch_event_snapshot_by_id: dict[str, dict[str, Any]]
    # (branch row, the main row it lands on, its base state) for every branch
    # row the merge carried onto main, created or matched.
    landings: list[tuple[Event, Event, dict[str, Any] | None]]
    main_target_by_branch_id: dict[uuid.UUID, Event]
    # Main's event ids still standing once the events arm has run.
    surviving_main_ids: set[uuid.UUID]
