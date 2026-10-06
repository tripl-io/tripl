"""The "Update from main" writer, pointed at another branch's rows.

``_plan_branch_update_apply.apply_update_plan`` writes items of MAIN's snapshot
onto a branch, and reads main's own rows by the items' ids where the snapshot
spells a reference by name (a successor, an override, a value context). A
transfer hands it items of the SOURCE branch instead, so every such id is a
source row's, which the default writer would look up among main's copies and
never find. ``_TransferApplier`` answers those lookups for the source, and
stamps each row it creates with the main row the source row stands for.

Nothing here commits, like the writer it extends.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.services._plan_branch_three_way_model import Op
from tripl.services._plan_branch_update_apply import _Applier


class _TransferRefusal(Exception):
    """A write the planner should have refused, caught by the writer itself.

    Carries one ``BranchTransferConflict``-shaped dict; the service rolls the
    transaction back and answers it as a ``transfer_conflicts`` 409.
    """

    def __init__(self, conflict: dict[str, Any]) -> None:
        super().__init__(conflict["message"])
        self.conflict = conflict


def _refusal(op: Op, message: str, *, field: str | None = None) -> _TransferRefusal:
    item = op.main or op.branch or {}
    name = str(item.get("name", ""))
    parent: str | None = None
    if op.entity_type == "field_definition":
        parent = str(item.get("_et", ""))
    elif op.entity_type == "event":
        parent = str(item.get("event_type_name", ""))
    elif op.entity_type == "relation":
        name = (
            f"{item.get('source_event_type_name')}.{item.get('source_field_name')}"
            f" → {item.get('target_event_type_name')}.{item.get('target_field_name')}"
        )
    return _TransferRefusal(
        {
            "entity_type": op.entity_type,
            "name": name,
            "parent": parent,
            "field": field,
            "reason": "target_missing",
            "message": message,
        }
    )


class _TransferApplier(_Applier):
    """Writes source items onto the target branch.

    * ``create`` refuses where the default writer would silently skip (a
      parent the target lacks) or drop (a value whose definition it lacks),
      and gives the new event or relation the MAIN origin of the source row —
      None for one the source itself added — never the source row's id, which
      main never held and a merge would read as main having deleted it. The
      item keeps the source id: ``copy_photos``, ``write_successor`` and
      ``write_overrides`` read the source rows by it.
    * ``branch_event_for`` maps a source event id to the target's row: the
      one this transfer created, else the target copy of the same main row.
    """

    def __init__(
        self,
        session: AsyncSession,
        project_id: uuid.UUID,
        target_branch_id: uuid.UUID,
        source_payload: dict[str, Any],
        *,
        source_branch_id: uuid.UUID,
        target_origins_complete: bool,
        source_origins_complete: bool,
        target_name: str,
    ) -> None:
        super().__init__(
            session, project_id, target_branch_id, source_payload, target_origins_complete
        )
        self.source_branch_id = source_branch_id
        self.source_origins_complete = source_origins_complete
        self.target_name = target_name
        self.created: dict[uuid.UUID, Event] = {}
        self._creating: Op | None = None

    async def create(self, op: Op) -> Any:
        self._creating = op
        try:
            row = await super().create(op)
        finally:
            self._creating = None
        if row is None:
            raise _refusal(
                op,
                f"What '{(op.main or {}).get('name')}' hangs off is not on '{self.target_name}'. "
                "Add it there first, or include the change that adds it.",
            )
        item = op.main or {}
        if op.entity_type in ("event", "relation"):
            origin = item.get("origin_id")
            row.origin_id = uuid.UUID(str(origin)) if origin else None
        if op.entity_type == "event":
            self.created[uuid.UUID(str(item["id"]))] = row
        return row

    async def fit_values(self, item: dict[str, Any], event_type_name: str) -> dict[str, Any]:
        """The default fitting, refusing instead of dropping while an event is created.

        Only then: every event write runs through here, whatever keys it
        carries, and a write of a description must not fail over a value it
        never writes. A value write the target cannot hold was refused while
        planning (``_plan_branch_transfer_deps._missing_parents``).
        """
        fitted = await super().fit_values(item, event_type_name)
        if self._creating is None:
            return fitted
        dropped = sorted(
            {
                str(value.get("field_name"))
                for value in item.get("field_values") or []
                if value not in fitted["field_values"]
            }
            | {
                str(value.get("meta_field_name"))
                for value in item.get("meta_values") or []
                if value not in fitted["meta_values"]
            }
        )
        if dropped:
            raise _refusal(
                self._creating,
                f"'{item.get('name')}' has values for {', '.join(dropped)}, which "
                f"'{self.target_name}' does not define.",
                field=dropped[0],
            )
        return fitted

    async def branch_event_for(self, main_event_id: uuid.UUID) -> Event | None:
        """The target's row for the SOURCE event ``main_event_id``, or None.

        The parameter keeps the default writer's name; here it is a source
        row's id. The row this transfer created for it; else the target copy
        of the main row the source row stands for, by ``origin_id``; by name,
        among target rows no origin claims, when either branch predates origin
        ids or the source row is one the source added (an equal copy the
        target already held, which the transfer skipped). Never a refusal: a
        reference that cannot be placed is left out, as the target would leave
        out one into a row it does not hold.
        """
        made = self.created.get(main_event_id)
        if made is not None:
            return made
        source_event = await self.session.get(Event, main_event_id)
        if source_event is None or source_event.branch_id != self.source_branch_id:
            return None
        if source_event.origin_id is not None:
            copies = list(
                (
                    await self.session.execute(
                        select(Event).where(
                            Event.branch_id == self.branch_id,
                            Event.origin_id == source_event.origin_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            if len(copies) == 1:
                return copies[0]
            if copies or (self.origins_complete and self.source_origins_complete):
                return None
        type_name = await self.session.scalar(
            select(EventType.name).where(EventType.id == source_event.event_type_id)
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
                        Event.name == source_event.name,
                    )
                )
            )
            .scalars()
            .all()
        )
        return namesakes[0] if len(namesakes) == 1 else None
