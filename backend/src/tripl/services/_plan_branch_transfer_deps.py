"""What moving or copying a selection of branch changes takes, decided on snapshots.

A transfer replays rows of one branch's diff onto another branch with the
"Update from main" writer, so what it plans is a list of ``Op`` exactly like
the three-way planner's — only the items come from the SOURCE branch (their
ids are its rows'), and the base every precondition reads is the source's own
base, which the service has already proven to be the target's too.

Three sides, all flattened the way ``_plan_branch_three_way`` flattens them
(``_flatten_fields`` adds ``_et`` to a field definition):

* ``source`` — the source branch now (S);
* ``base`` — the source's base snapshot (base_S, main ids);
* ``target`` — the target branch now (T).

The closure (``transfer_closure``) adds what a selected row cannot land
without — its new event type, the new field its value uses, the variable its
``${token}`` names — and, on a move, what the source would lose when the
selected row is undone there. ``entry_to_op`` then turns each row into an op,
a skip (the target already says the same) or a refusal; a refusal is never a
question, the user fixes the target and retries.

Pure: snapshot dicts and diff entries in, ops and words out.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from tripl.core.variable_retirement import referenced_tokens
from tripl.schemas.plan_revision import PlanDiffEntry
from tripl.services._plan_branch_three_way_model import Op, entity_key, fields_of
from tripl.services._plan_diff_housekeeping import _tokens_of
from tripl.services.plan_branch_conflicts import _flatten_fields, comparable_field

UnitKind = Literal["added", "changed", "removed", "renamed"]
Mode = Literal["move", "copy"]

# Deleting one of these cascades into rows the diff never lists (an event
# type's events, a field's values, a meta field's values), target-only rows
# included, so a removal of them is refused rather than replayed.
NOT_TRANSFERABLE_REMOVALS = frozenset({"event_type", "field_definition", "meta_field"})

_SETS: dict[str, str] = {
    "event_type": "event_types",
    "meta_field": "meta_fields",
    "variable": "variables",
    "event": "events",
    "relation": "relations",
}

# Rows reverted on the source after a move, in the order that keeps every
# lookup valid: an addition's children before its parent, so a parent's
# cascade finds nothing left; changes; then the removals put back, the rows
# others point at first (a restored variable's overrides name events).
_ADDED_REVERT_ORDER = (
    "relation",
    "event",
    "field_definition",
    "meta_field",
    "variable",
    "event_type",
)
_REMOVED_REVERT_ORDER = ("event", "relation", "variable")

# Catalog position: cosmetic, and left out of the plan diff, so it is never
# carried and never compared.
_IGNORED_KEYS = frozenset({"order"})

_SHOWN_MAX = 80


@dataclass(frozen=True)
class Sides:
    source: dict[str, Any]
    base: dict[str, Any]
    target: dict[str, Any]
    source_origins_complete: bool = True
    target_origins_complete: bool = True
    target_name: str = "the target"
    source_name: str = "the source"


@dataclass(eq=False)
class Unit:
    """One thing a transfer writes: a diff row, or both halves of a rename.

    ``uid`` is the entries' positions in the diff, the unit's identity.
    ``item`` is the source's row (an addition, a change, a rename's new half);
    ``base`` the base's (a change, a removal, a rename's old half).
    """

    uid: tuple[int, ...]
    entity_type: str
    kind: UnitKind
    entries: tuple[PlanDiffEntry, ...]
    item: dict[str, Any] | None
    base: dict[str, Any] | None

    @property
    def label(self) -> str:
        if self.kind == "renamed":
            return f"{self.entries[0].name} → {self.entries[1].name}"
        return self.entries[0].name

    @property
    def parent(self) -> str | None:
        return self.entries[0].parent


@dataclass
class Closure:
    selected: list[Unit]
    # (unit, the label of the row it is needed by)
    carried: list[tuple[Unit, str]]
    warnings: list[str] = field(default_factory=list)


@dataclass
class UnitOutcome:
    op: Op | None = None
    skipped: str | None = None
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    not_transferable: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class TransferPlan:
    closure: Closure
    ops: list[Op] = field(default_factory=list)
    applied: list[Unit] = field(default_factory=list)
    carried: list[tuple[Unit, str]] = field(default_factory=list)
    skipped: list[tuple[Unit, str]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    not_transferable: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --- sides --------------------------------------------------------------------


def flat_items(payload: Mapping[str, Any], entity_type: str) -> list[dict[str, Any]]:
    """One entity type's rows of a snapshot, in the planner's shape."""
    if entity_type == "field_definition":
        return _flatten_fields(dict(payload))
    return list(payload.get(_SETS[entity_type], []))


def entry_name(entity_type: str, item: Mapping[str, Any]) -> str:
    """The name the plan diff gives ``item`` (``compute_plan_diff_entries``)."""
    if entity_type == "relation":
        return (
            f"{item['source_event_type_name']}.{item['source_field_name']}"
            f" → {item['target_event_type_name']}.{item['target_field_name']}"
        )
    return str(item["name"])


def entry_parent(entity_type: str, item: Mapping[str, Any]) -> str | None:
    if entity_type == "field_definition":
        return str(item["_et"])
    if entity_type == "event":
        return str(item.get("event_type_name") or "")
    return None


def _named(
    items: Iterable[dict[str, Any]], entity_type: str, name: str, parent: str | None
) -> list[dict[str, Any]]:
    return [
        item
        for item in items
        if entry_name(entity_type, item) == name and entry_parent(entity_type, item) == parent
    ]


def _by_id(items: Iterable[dict[str, Any]], row_id: str | None) -> dict[str, Any] | None:
    if row_id is None:
        return None
    return next((item for item in items if str(item.get("id")) == row_id), None)


def _locate(
    items: list[dict[str, Any]], entity_type: str, entry: PlanDiffEntry
) -> dict[str, Any] | None:
    """The row ``entry`` describes: by its id, else by the only row under its name."""
    found = _by_id(items, entry.entity_id)
    if found is not None:
        return found
    named = _named(items, entity_type, entry.name, entry.parent)
    return named[0] if len(named) == 1 else None


def _base_of(
    entity_type: str, item: Mapping[str, Any], base_items: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The base row a source row of a ``changed`` entry stands for."""
    origin = item.get("origin_id")
    if origin is not None:
        found = _by_id(base_items, str(origin))
        if found is not None:
            return found
    named = _named(
        base_items, entity_type, entry_name(entity_type, item), entry_parent(entity_type, item)
    )
    return named[0] if len(named) == 1 else None


def build_units(
    entries: Sequence[PlanDiffEntry],
    renames: Iterable[tuple[int, int]],
    sides: Sides,
) -> list[Unit]:
    """Every diff row as a unit, a rename's two halves (removed, added) as one."""
    paired: dict[int, tuple[int, int]] = {}
    for removed_at, added_at in renames:
        paired[removed_at] = (removed_at, added_at)
        paired[added_at] = (removed_at, added_at)
    units: list[Unit] = []
    seen: set[int] = set()
    source_sets: dict[str, list[dict[str, Any]]] = {}
    base_sets: dict[str, list[dict[str, Any]]] = {}
    for index, entry in enumerate(entries):
        if index in seen:
            continue
        entity_type = entry.entity_type
        if entity_type not in source_sets:
            source_sets[entity_type] = flat_items(sides.source, entity_type)
            base_sets[entity_type] = flat_items(sides.base, entity_type)
        source_items = source_sets[entity_type]
        base_items = base_sets[entity_type]
        if index in paired:
            removed_at, added_at = paired[index]
            seen.update((removed_at, added_at))
            removed, added = entries[removed_at], entries[added_at]
            units.append(
                Unit(
                    uid=(removed_at, added_at),
                    entity_type=entity_type,
                    kind="renamed",
                    entries=(removed, added),
                    item=_locate(source_items, entity_type, added),
                    base=_locate(base_items, entity_type, removed),
                )
            )
            continue
        seen.add(index)
        item: dict[str, Any] | None = None
        base: dict[str, Any] | None = None
        if entry.kind == "added":
            item = _locate(source_items, entity_type, entry)
        elif entry.kind == "removed":
            base = _locate(base_items, entity_type, entry)
        else:
            item = _locate(source_items, entity_type, entry)
            base = _base_of(entity_type, item, base_items) if item is not None else None
        units.append(
            Unit(
                uid=(index,),
                entity_type=entity_type,
                kind=entry.kind,
                entries=(entry,),
                item=item,
                base=base,
            )
        )
    return units


# --- content ------------------------------------------------------------------


def _keys(entity_type: str) -> list[str]:
    return [key for key in fields_of(entity_type) if key not in _IGNORED_KEYS]


def same_content(entity_type: str, a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    return all(
        comparable_field(dict(a), key) == comparable_field(dict(b), key)
        for key in _keys(entity_type)
    )


def _first_difference(entity_type: str, a: Mapping[str, Any], b: Mapping[str, Any]) -> str | None:
    for key in _keys(entity_type):
        if comparable_field(dict(a), key) != comparable_field(dict(b), key):
            return key
    return None


def rename_write_fields(
    new_item: Mapping[str, Any], base_old: Mapping[str, Any], entity_type: str | None = None
) -> list[str]:
    """What a rename pair writes: the name, and every other key the source edited.

    Neither half of the pair carries ``field_changes`` (the diff shows a
    removal and an addition), so the keys are read off the two rows. Only
    variables and events pair as renames; without ``entity_type`` the row's
    shape says which.
    """
    if entity_type is None:
        entity_type = "variable" if "variable_type" in new_item else "event"
    return [
        key
        for key in _keys(entity_type)
        if key == "name"
        or comparable_field(dict(new_item), key) != comparable_field(dict(base_old), key)
    ]


def _shown(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    return text if len(text) <= _SHOWN_MAX else f"{text[: _SHOWN_MAX - 1]}…"


# --- the target ---------------------------------------------------------------


def _target_for(
    entity_type: str, base_item: Mapping[str, Any], sides: Sides
) -> tuple[dict[str, Any] | None, bool]:
    """The target's copy of the base row ``base_item``, and whether it is ambiguous.

    Events and relations by origin: the base row's id is main's, and every
    branch copy names it as ``origin_id``. By name only when either branch
    predates origin ids, and then among target rows no origin claims, the rule
    ``_Applier.branch_event_for`` reads. Every other type by its key, which a
    uniqueness constraint holds to one row per branch.
    """
    target_items = flat_items(sides.target, entity_type)
    if entity_type in ("event", "relation"):
        origin = str(base_item.get("id"))
        copies = [item for item in target_items if str(item.get("origin_id")) == origin]
        if len(copies) == 1:
            return copies[0], False
        if copies:
            return None, True
        if sides.source_origins_complete and sides.target_origins_complete:
            return None, False
        key = entity_key(entity_type, base_item)
        namesakes = [
            item
            for item in target_items
            if item.get("origin_id") is None and entity_key(entity_type, item) == key
        ]
        if len(namesakes) > 1:
            return None, True
        return (namesakes[0] if namesakes else None), False
    key = entity_key(entity_type, base_item)
    same = [item for item in target_items if entity_key(entity_type, item) == key]
    return (same[0] if same else None), False


def _target_namesakes(
    entity_type: str, item: Mapping[str, Any], sides: Sides
) -> list[dict[str, Any]]:
    key = entity_key(entity_type, item)
    return [
        row for row in flat_items(sides.target, entity_type) if entity_key(entity_type, row) == key
    ]


# --- dependencies -------------------------------------------------------------


def _changed_keys(unit: Unit) -> list[str]:
    """A ``changed`` entry's keys, in the planner's terms.

    The diff lists a variable's ``json_schema`` on its own; the planner and
    the writer read it folded into ``variable_type`` (``comparable_field``),
    one value in two columns, so it is compared and written as that.
    """
    keys: list[str] = []
    for change in unit.entries[0].field_changes:
        key = (
            "variable_type"
            if unit.entity_type == "variable" and change.field == "json_schema"
            else change.field
        )
        if key not in keys:
            keys.append(key)
    return keys


def _written(unit: Unit) -> set[str] | None:
    """The keys the unit writes on the target; None for a whole new row."""
    if unit.kind == "added":
        return None
    if unit.kind == "changed":
        return set(_changed_keys(unit))
    if unit.kind == "renamed" and unit.item is not None and unit.base is not None:
        return set(rename_write_fields(unit.item, unit.base, unit.entity_type))
    return set()


def _writes(unit: Unit, key: str) -> bool:
    written = _written(unit)
    return written is None or key in written


def _key_of(unit: Unit) -> tuple[str, ...] | None:
    """The unit's key on the target once written (a rename's: its new one)."""
    source = unit.item if unit.item is not None else unit.base
    return entity_key(unit.entity_type, source) if source is not None else None


def _dotted(item: Mapping[str, Any]) -> str:
    return f"{item.get('event_type_name', '')}.{item.get('name', '')}"


def _value_strings(item: Mapping[str, Any], collection: str) -> list[str]:
    return [
        member["value"]
        for member in item.get(collection) or []
        if isinstance(member, dict) and isinstance(member.get("value"), str)
    ]


class _Index:
    """The diff's units, looked up the ways a dependency names them."""

    def __init__(self, units: Sequence[Unit]) -> None:
        self.units = list(units)
        # Additions and renames: rows the target lacks until they are written.
        self.new: dict[tuple[str, tuple[str, ...]], Unit] = {}
        # Changed definitions: a value may use the option or type they added.
        self.changed_definition: dict[tuple[str, tuple[str, ...]], Unit] = {}
        self.new_event_by_dotted: dict[str, Unit] = {}
        self.new_variables: list[tuple[Unit, set[str]]] = []
        for unit in self.units:
            key = _key_of(unit)
            if key is None or unit.item is None:
                continue
            if unit.kind in ("added", "renamed"):
                self.new[(unit.entity_type, key)] = unit
                if unit.entity_type == "event":
                    self.new_event_by_dotted[_dotted(unit.item)] = unit
                if unit.entity_type == "variable":
                    self.new_variables.append((unit, _tokens_of(unit.item)))
            elif unit.kind == "changed" and unit.entity_type in ("field_definition", "meta_field"):
                self.changed_definition[(unit.entity_type, key)] = unit

    def definition(self, entity_type: str, key: tuple[str, ...]) -> Unit | None:
        return self.new.get((entity_type, key)) or self.changed_definition.get((entity_type, key))

    def variables_named_by(self, values: list[str]) -> list[Unit]:
        named = referenced_tokens(values)
        return [unit for unit, tokens in self.new_variables if tokens & named]


def _forward(unit: Unit, index: _Index) -> list[Unit]:
    """What ``unit`` cannot land on the target without."""
    item = unit.item
    if item is None or unit.kind == "removed":
        return []
    out: list[Unit | None] = []
    if unit.entity_type == "field_definition" and unit.kind == "added":
        out.append(index.new.get(("event_type", (str(item["_et"]),))))
    elif unit.entity_type == "event":
        type_name = str(item.get("event_type_name") or "")
        if unit.kind == "added":
            out.append(index.new.get(("event_type", (type_name,))))
        values: list[str] = []
        if _writes(unit, "field_values"):
            for value in item.get("field_values") or []:
                out.append(
                    index.definition("field_definition", (type_name, str(value.get("field_name"))))
                )
            values.extend(_value_strings(item, "field_values"))
        if _writes(unit, "meta_values"):
            for value in item.get("meta_values") or []:
                out.append(index.definition("meta_field", (str(value.get("meta_field_name")),)))
            values.extend(_value_strings(item, "meta_values"))
        out.extend(index.variables_named_by(values))
        successor = item.get("superseded_by")
        if successor and _writes(unit, "superseded_by"):
            out.append(index.new_event_by_dotted.get(str(successor)))
    elif unit.entity_type == "relation" and unit.kind == "added":
        for side in ("source", "target"):
            type_name = str(item[f"{side}_event_type_name"])
            out.append(index.new.get(("event_type", (type_name,))))
            out.append(
                index.definition("field_definition", (type_name, str(item[f"{side}_field_name"])))
            )
    elif unit.entity_type == "variable" and _writes(unit, "event_value_overrides"):
        # An override hangs off its event: one the source added comes along,
        # or the target write would drop it and the move's revert lose it.
        for override in item.get("event_value_overrides") or []:
            dotted = f"{override.get('event_type_name', '')}.{override.get('event_name', '')}"
            event = index.new_event_by_dotted.get(dotted)
            if event is not None and event.kind == "added":
                out.append(event)
    return [dep for dep in out if dep is not None and dep is not unit]


def _uses_definition(event: Unit, entity_type: str, key: tuple[str, ...]) -> bool:
    item = event.item
    if item is None:
        return False
    if entity_type == "field_definition":
        type_name, field_name = key
        return item.get("event_type_name") == type_name and any(
            value.get("field_name") == field_name for value in item.get("field_values") or []
        )
    return any(value.get("meta_field_name") == key[0] for value in item.get("meta_values") or [])


def _reverse(unit: Unit, index: _Index) -> list[Unit]:
    """What the source loses, staying behind, when ``unit`` is undone there.

    Undoing an addition deletes the row, and the database takes with it what
    hangs off it (``ondelete=CASCADE`` / ``SET NULL``); undoing a changed
    definition takes back the option or type a remaining value may use. Each
    such row moves too, so nothing disappears from both branches.
    """
    item = unit.item
    key = _key_of(unit)
    if item is None or key is None or unit.kind not in ("added", "changed"):
        return []
    out: list[Unit] = []
    live = [other for other in index.units if other.kind in ("added", "changed") and other.item]
    if unit.entity_type == "event_type" and unit.kind == "added":
        name = key[0]
        for other in live:
            other_item = other.item or {}
            if (
                other.entity_type == "field_definition"
                and other_item.get("_et") == name
                or other.entity_type == "event"
                and other_item.get("event_type_name") == name
                or other.entity_type == "relation"
                and name
                in (
                    other_item.get("source_event_type_name"),
                    other_item.get("target_event_type_name"),
                )
            ):
                out.append(other)
    elif unit.entity_type in ("field_definition", "meta_field"):
        drops_relations = unit.kind == "added" and unit.entity_type == "field_definition"
        for other in live:
            if (
                other.entity_type == "event"
                and _uses_definition(other, unit.entity_type, key)
                or (
                    drops_relations
                    and other.entity_type == "relation"
                    and _relation_touches(other.item or {}, key)
                )
            ):
                out.append(other)
    elif unit.entity_type == "event" and unit.kind == "added":
        dotted = _dotted(item)
        for other in live:
            other_item = other.item or {}
            if (
                other.entity_type == "event"
                and other_item.get("superseded_by") == dotted
                or other.entity_type == "variable"
                and any(
                    f"{o.get('event_type_name', '')}.{o.get('event_name', '')}" == dotted
                    for o in other_item.get("event_value_overrides") or []
                )
            ):
                out.append(other)
    elif unit.entity_type == "variable" and unit.kind == "added":
        tokens = _tokens_of(item)
        for other in live:
            if other.entity_type != "event":
                continue
            values = _value_strings(other.item or {}, "field_values") + _value_strings(
                other.item or {}, "meta_values"
            )
            if tokens & referenced_tokens(values):
                out.append(other)
    return [other for other in out if other is not unit]


def _relation_touches(item: Mapping[str, Any], key: tuple[str, ...]) -> bool:
    type_name, field_name = key
    return (item.get("source_event_type_name"), item.get("source_field_name")) == (
        type_name,
        field_name,
    ) or (item.get("target_event_type_name"), item.get("target_field_name")) == (
        type_name,
        field_name,
    )


def transfer_closure(selected: Sequence[Unit], units: Sequence[Unit], *, mode: Mode) -> Closure:
    """The selection plus everything it takes along, each with why.

    Forward on both modes; reverse (what the source would lose) on a move.
    """
    index = _Index(units)
    included: dict[tuple[int, ...], Unit] = {unit.uid: unit for unit in selected}
    carried: list[tuple[Unit, str]] = []
    queue = list(selected)
    while queue:
        unit = queue.pop(0)
        deps = _forward(unit, index)
        if mode == "move":
            deps += _reverse(unit, index)
        for dep in deps:
            if dep.uid in included:
                continue
            included[dep.uid] = dep
            carried.append((dep, unit.label))
            queue.append(dep)
    return Closure(selected=list(selected), carried=carried)


# --- entry to op ----------------------------------------------------------------


def _conflict(
    unit: Unit, reason: str, message: str, *, field_name: str | None = None
) -> dict[str, Any]:
    return {
        "entity_type": unit.entity_type,
        "name": unit.entries[-1].name if unit.kind == "renamed" else unit.entries[0].name,
        "parent": unit.parent,
        "field": field_name,
        "reason": reason,
        "message": message,
    }


class _Present:
    """What exists on the target once the transfer's new rows are written."""

    def __init__(self, sides: Sides, incoming: Iterable[Unit]) -> None:
        self.types = {str(item["name"]) for item in sides.target.get("event_types", [])}
        self.fields = {
            (str(item["_et"]), str(item["name"]))
            for item in flat_items(sides.target, "field_definition")
        }
        self.meta = {str(item["name"]) for item in sides.target.get("meta_fields", [])}
        self.events = {_dotted(item) for item in sides.target.get("events", [])}
        for unit in incoming:
            if unit.item is None or unit.kind not in ("added", "renamed"):
                continue
            if unit.entity_type == "event_type":
                self.types.add(str(unit.item["name"]))
            elif unit.entity_type == "field_definition":
                self.fields.add((str(unit.item["_et"]), str(unit.item["name"])))
            elif unit.entity_type == "meta_field":
                self.meta.add(str(unit.item["name"]))
            elif unit.entity_type == "event":
                self.events.add(_dotted(unit.item))


def _missing_parents(unit: Unit, present: _Present, writes: set[str] | None) -> list[str]:
    """What ``unit`` needs on the target and will not find there, in words."""
    item = unit.item or {}
    missing: list[str] = []

    def need(key: str) -> bool:
        return writes is None or key in writes

    if unit.entity_type == "field_definition" and writes is None:
        if str(item["_et"]) not in present.types:
            missing.append(f"event type '{item['_et']}'")
    elif unit.entity_type == "event":
        type_name = str(item.get("event_type_name") or "")
        if writes is None and type_name not in present.types:
            missing.append(f"event type '{type_name}'")
        if need("field_values"):
            for value in item.get("field_values") or []:
                if (type_name, str(value.get("field_name"))) not in present.fields:
                    missing.append(f"field '{type_name}.{value.get('field_name')}'")
        if need("meta_values"):
            for value in item.get("meta_values") or []:
                if str(value.get("meta_field_name")) not in present.meta:
                    missing.append(f"meta field '{value.get('meta_field_name')}'")
        successor = item.get("superseded_by")
        if successor and need("superseded_by") and str(successor) not in present.events:
            missing.append(f"successor event '{successor}'")
    elif unit.entity_type == "relation" and writes is None:
        for side in ("source", "target"):
            type_name = str(item[f"{side}_event_type_name"])
            field_name = str(item[f"{side}_field_name"])
            if (type_name, field_name) not in present.fields:
                missing.append(f"field '{type_name}.{field_name}'")
    return sorted(set(missing))


def _field_writes(
    unit: Unit,
    target_item: Mapping[str, Any],
    keys: Iterable[str],
    sides: Sides,
    outcome: UnitOutcome,
) -> list[str]:
    """The keys still to write, each checked against what the target did itself.

    A key the target already holds at the source's value is left out. One the
    target moved off the base itself is refused: the base is read from the
    base snapshot, not from the diff's ``before`` (which strips ordering and
    child collections), so the comparison is like for like.
    """
    assert unit.item is not None and unit.base is not None
    write: list[str] = []
    for key in keys:
        target_value = comparable_field(dict(target_item), key)
        source_value = comparable_field(unit.item, key)
        base_value = comparable_field(unit.base, key)
        if target_value == source_value:
            continue
        if target_value != base_value:
            outcome.conflicts.append(
                _conflict(
                    unit,
                    "target_changed",
                    (
                        f"'{sides.target_name}' changed '{key}' of '{unit.label}' too: it holds "
                        f"{_shown(target_value)} there, the base had {_shown(base_value)}. "
                        f"Undo that edit on '{sides.target_name}', or leave this change out."
                    ),
                    field_name=key,
                )
            )
            continue
        write.append(key)
    return write


def entry_to_op(unit: Unit, sides: Sides, present: _Present, *, mode: Mode) -> UnitOutcome:
    """One unit as the op that writes it on the target, a skip, or refusals."""
    outcome = UnitOutcome()
    entity_type = unit.entity_type
    target = sides.target_name

    if unit.kind == "removed":
        if entity_type in NOT_TRANSFERABLE_REMOVALS:
            outcome.not_transferable = (
                f"Deleting '{unit.label}' on '{target}' would also delete every event, value "
                f"or relation that uses it there. Delete it on '{target}' directly."
            )
            return outcome
        if unit.base is None:
            outcome.conflicts.append(
                _conflict(
                    unit, "target_missing", f"The base snapshot does not describe '{unit.label}'."
                )
            )
            return outcome
        target_item, ambiguous = _target_for(entity_type, unit.base, sides)
        if ambiguous:
            outcome.conflicts.append(_ambiguous_on_target(unit, target))
            return outcome
        if target_item is None:
            outcome.skipped = f"already absent on '{target}'"
            return outcome
        changed = _first_difference(entity_type, target_item, unit.base)
        if changed is not None:
            outcome.conflicts.append(
                _conflict(
                    unit,
                    "target_changed",
                    (
                        f"'{unit.label}' was edited on '{target}' ('{changed}'), so deleting it "
                        "there would discard that edit. Delete it on the target directly."
                    ),
                    field_name=changed,
                )
            )
            return outcome
        if entity_type == "event" and mode == "move" and unit.base.get("photos"):
            outcome.warnings.append(
                f"'{unit.label}' had photos; after the move '{sides.source_name}' brings the "
                "event back without them, because undoing a deletion does not restore photos."
            )
        outcome.op = Op("delete", entity_type, branch=dict(target_item))
        return outcome

    if unit.item is None:
        outcome.conflicts.append(
            _conflict(unit, "target_missing", f"'{unit.label}' could not be read on the source.")
        )
        return outcome

    if unit.kind == "added":
        namesakes = _target_namesakes(entity_type, unit.item, sides)
        if namesakes:
            if any(same_content(entity_type, row, unit.item) for row in namesakes):
                outcome.skipped = f"already on '{target}'"
            else:
                outcome.conflicts.append(
                    _conflict(
                        unit,
                        "target_exists",
                        (
                            f"'{target}' already has '{unit.label}' with other content. Rename "
                            "or remove it there, or make the two the same."
                        ),
                    )
                )
            return outcome
        missing = _missing_parents(unit, present, None)
        if missing:
            outcome.conflicts.append(_missing(unit, missing, target))
            return outcome
        outcome.op = Op("create", entity_type, main=dict(unit.item))
        return outcome

    if unit.base is None:
        outcome.conflicts.append(
            _conflict(
                unit, "target_missing", f"The base snapshot does not describe '{unit.label}'."
            )
        )
        return outcome
    target_item, ambiguous = _target_for(entity_type, unit.base, sides)
    if ambiguous:
        outcome.conflicts.append(_ambiguous_on_target(unit, target))
        return outcome
    if target_item is None:
        outcome.conflicts.append(
            _conflict(
                unit,
                "target_missing",
                f"'{unit.base.get('name', unit.label)}' is not on '{target}' any more, so the "
                "change has nothing to land on.",
            )
        )
        return outcome

    if unit.kind == "renamed":
        keys = rename_write_fields(unit.item, unit.base, entity_type)
        others = [
            row
            for row in _target_namesakes(entity_type, unit.item, sides)
            if str(row.get("id")) != str(target_item.get("id"))
        ]
        if others:
            outcome.conflicts.append(
                _conflict(
                    unit,
                    "target_exists",
                    f"'{target}' already has a {entity_type.replace('_', ' ')} called "
                    f"'{unit.entries[1].name}', so the rename cannot land there.",
                )
            )
            return outcome
    else:
        keys = _changed_keys(unit)
    write = _field_writes(unit, target_item, keys, sides, outcome)
    if outcome.conflicts:
        return outcome
    if not write:
        outcome.skipped = f"already on '{target}'"
        return outcome
    missing = _missing_parents(unit, present, set(write))
    if missing:
        outcome.conflicts.append(_missing(unit, missing, target))
        return outcome
    outcome.op = Op(
        "write", entity_type, branch=dict(target_item), main=dict(unit.item), fields=tuple(write)
    )
    return outcome


def _missing(unit: Unit, missing: list[str], target: str) -> dict[str, Any]:
    return _conflict(
        unit,
        "target_missing",
        f"'{unit.label}' needs {', '.join(missing)}, which '{target}' does not have. Add it "
        "there first, or include the change that adds it.",
    )


def _ambiguous_on_target(unit: Unit, target: str) -> dict[str, Any]:
    return _conflict(
        unit,
        "target_missing",
        f"'{target}' holds '{unit.label}' more than once and predates origin tracking, so "
        "there is no telling which copy this change belongs to. Rename one of them there.",
    )


# --- the whole plan ---------------------------------------------------------------


def plan_transfer(
    selected: Sequence[Unit], units: Sequence[Unit], sides: Sides, *, mode: Mode
) -> TransferPlan:
    """The closure, then every unit of it as an op, a skip or a refusal."""
    closure = transfer_closure(selected, units, mode=mode)
    plan = TransferPlan(closure=closure, warnings=list(closure.warnings))
    every = [*closure.selected, *(unit for unit, _ in closure.carried)]
    present = _Present(sides, every)
    needed_by = {unit.uid: why for unit, why in closure.carried}
    for unit in every:
        outcome = entry_to_op(unit, sides, present, mode=mode)
        if unit.uid in needed_by:
            # A refusal of a row nobody ticked says why it is in the transfer.
            for conflict in outcome.conflicts:
                conflict["message"] += f" It comes along because '{needed_by[unit.uid]}' needs it."
        plan.conflicts.extend(outcome.conflicts)
        plan.warnings.extend(outcome.warnings)
        if outcome.not_transferable:
            plan.not_transferable.append(outcome.not_transferable)
            continue
        if outcome.skipped is not None:
            plan.skipped.append((unit, outcome.skipped))
            continue
        if outcome.op is None:
            continue
        plan.ops.append(outcome.op)
        if unit.uid in needed_by:
            plan.carried.append((unit, needed_by[unit.uid]))
        else:
            plan.applied.append(unit)
    plan.warnings.extend(_dropped_override_warnings(every, sides, present))
    return plan


def _dropped_override_warnings(units: Iterable[Unit], sides: Sides, present: _Present) -> list[str]:
    """Overrides a transferred variable has on events the target will not hold."""
    out: list[str] = []
    for unit in units:
        if unit.entity_type != "variable" or unit.item is None or unit.kind == "removed":
            continue
        if not _writes(unit, "event_value_overrides"):
            continue
        dropped = sorted(
            {
                f"{o.get('event_type_name', '')}.{o.get('event_name', '')}"
                for o in unit.item.get("event_value_overrides") or []
                if f"{o.get('event_type_name', '')}.{o.get('event_name', '')}" not in present.events
            }
        )
        if dropped:
            out.append(
                f"'{unit.label}' documents values for {', '.join(dropped)}, which "
                f"'{sides.target_name}' does not have; those overrides are not copied."
            )
    return out


def revert_order(units: Iterable[Unit]) -> list[Unit]:
    """The order a move undoes its units on the source in.

    Additions children first, so an event type's cascade finds nothing left;
    then changes; then renames put back and removals restored, the rows others
    point at first.
    """
    units = list(units)

    def of(kind: str, entity_type: str) -> list[Unit]:
        return [u for u in units if u.kind == kind and u.entity_type == entity_type]

    ordered: list[Unit] = []
    for entity_type in _ADDED_REVERT_ORDER:
        ordered.extend(of("added", entity_type))
    ordered.extend(u for u in units if u.kind == "changed")
    ordered.extend(u for u in units if u.kind == "renamed")
    for entity_type in _REMOVED_REVERT_ORDER:
        ordered.extend(of("removed", entity_type))
    return ordered
