"""The vocabulary of "Update from main": sides, slots, ops, row names.

What ``_plan_branch_three_way`` plans in: the entity types in the order they
are visited, the fields each one compares, the ``Slot`` that lines one entity
up across base, main and the branch, the ``Op`` a plan writes, and how an
entity is keyed and named in conflict rows.

Pure, like ``_plan_branch_three_way``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from tripl.services.plan_branch_conflicts import (
    _ET_CHANGE_KEYS,
    _EV_CHANGE_KEYS,
    _FD_CHANGE_KEYS,
    _MF_CHANGE_KEYS,
    _REL_CHANGE_KEYS,
    _VAR_CHANGE_KEYS,
    comparable_field,
)

PRESENCE_FIELD = "@presence"
PRESENT = "present"
ABSENT = "absent"

# Parents before children, the order conflicts are listed in and creations run.
ENTITY_TYPES: tuple[str, ...] = (
    "event_type",
    "meta_field",
    "variable",
    "field_definition",
    "event",
    "relation",
)

CHANGE_KEYS: dict[str, tuple[str, ...]] = {
    "event_type": _ET_CHANGE_KEYS,
    "field_definition": _FD_CHANGE_KEYS,
    "event": _EV_CHANGE_KEYS,
    "variable": _VAR_CHANGE_KEYS,
    "meta_field": _MF_CHANGE_KEYS,
    "relation": _REL_CHANGE_KEYS,
}

# Entities whose name is editable while the row stays the same row. For the
# others the name is the identity, so another name is another entity.
_RENAMEABLE = frozenset({"event", "variable"})

# An event's value collections: keyed per definition, so a definition whose
# existence differs between the sides is spliced out of them (``_values_fate``).
_VALUE_FIELDS: dict[str, str] = {"field_values": "field_name", "meta_values": "meta_field_name"}

# ``plan_branch_merge_resolutions.entity_name`` is ``String(255)``; a dotted
# event or relation name can be longer (an event name alone may be 500).
_MAX_CONFLICT_NAME = 255
_NAME_DIGEST_LENGTH = 16

# Keys that describe WHICH row, never WHAT it says. Stripped before two
# dependency states are compared, together with the photo discussion, which is
# not plan content (``comparable_field``).
_NOT_CONTENT = frozenset(
    {
        "id",
        "origin_id",
        "event_type_id",
        "source_event_type_id",
        "target_event_type_id",
        "source_field_id",
        "target_field_id",
        "comments",
    }
)

Choice = Literal["ours", "theirs"]
OpKind = Literal["delete", "create", "write", "origin", "link"]


@dataclass
class Slot:
    """One entity as the base, main and the branch each hold it (None: absent)."""

    entity_type: str
    base: dict[str, Any] | None
    main: dict[str, Any] | None = None
    branch: dict[str, Any] | None = None


@dataclass(frozen=True)
class Op:
    """One write "Update from main" makes on the branch.

    * ``delete`` — remove ``branch`` (``cascade``: an event type or field with
      everything hanging off it on the branch);
    * ``create`` — build main's ``main`` item on the branch;
    * ``write`` — copy ``fields`` of ``main`` onto the branch row ``branch``;
    * ``origin`` — point the branch row at main's ``main`` row, or clear the
      link when ``main`` is None (a kept row main deleted is the branch's own);
    * ``link`` — put back the references main's row ``main`` holds to rows this
      update recreates (``targets``: their main ids) onto the branch row
      ``branch``, leaving its other references alone: an override of a variable
      on a recreated event (``fields=("event_value_overrides",)``) or an
      event's successor (``fields=("superseded_by",)``).

    ``keep`` (a ``write`` of a variable's ``event_value_overrides``): branch
    event ids whose overrides the write leaves as they are — events the branch
    kept against main's deletion, whose overrides main lost only with them.
    """

    kind: OpKind
    entity_type: str
    branch: dict[str, Any] | None = None
    main: dict[str, Any] | None = None
    fields: tuple[str, ...] = ()
    cascade: bool = False
    targets: tuple[str, ...] = ()
    keep: tuple[str, ...] = ()


@dataclass
class ThreeWay:
    rows: list[dict[str, Any]] = field(default_factory=list)
    ops: list[Op] = field(default_factory=list)
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    main_changes: dict[str, dict[str, int]] = field(default_factory=dict)
    # Reasons the update cannot be applied at all, whatever is chosen:
    # ``{kind, entity_type, name, message}``. ``ambiguous`` — main changed a
    # row the branch's pairing cannot place (a namesake on a branch opened
    # before origin ids); ``identity_clash`` — main's rows and the branch's
    # own would end up sharing a name or ``source_name``.
    blockers: list[dict[str, Any]] = field(default_factory=list)


# --- naming -----------------------------------------------------------------


def entity_key(entity_type: str, item: Mapping[str, Any]) -> tuple[str, ...]:
    if entity_type == "field_definition":
        return (str(item["_et"]), str(item["name"]))
    if entity_type == "event":
        return (str(item.get("event_type_name") or ""), str(item["name"]))
    if entity_type == "relation":
        return (
            str(item["source_event_type_name"]),
            str(item["source_field_name"]),
            str(item["target_event_type_name"]),
            str(item["target_field_name"]),
        )
    return (str(item["name"]),)


def conflict_name(entity_type: str, item: Mapping[str, Any]) -> str:
    """The name a conflict row and its stored resolution are keyed by.

    Dotted the way ``_detect_merge_conflicts`` names the same entities, so the
    two lists read alike. Namesake events share one — the documented limit a
    resolution by name has, the same one a revert by name has.

    Bounded to the stored resolution's 255 characters: a longer name (an event
    name alone may be 500) keeps its head and ends in a digest of the whole,
    so it stays unique and resolvable. Display reads ``conflict_label``.
    """
    key = entity_key(entity_type, item)
    name = f"{key[0]}.{key[1]}->{key[2]}.{key[3]}" if entity_type == "relation" else ".".join(key)
    if len(name) <= _MAX_CONFLICT_NAME:
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:_NAME_DIGEST_LENGTH]
    head = name[: _MAX_CONFLICT_NAME - _NAME_DIGEST_LENGTH - 1]
    return f"{head}~{digest}"


def conflict_label(entity_type: str, item: Mapping[str, Any]) -> str:
    key = entity_key(entity_type, item)
    if entity_type == "relation":
        return f"{key[0]}.{key[1]} → {key[2]}.{key[3]}"
    return key[-1]


def conflict_parent(entity_type: str, item: Mapping[str, Any]) -> str | None:
    if entity_type in ("field_definition", "event"):
        return entity_key(entity_type, item)[0]
    return None


def fields_of(entity_type: str) -> tuple[str, ...]:
    keys = CHANGE_KEYS[entity_type]
    return ("name", *keys) if entity_type in _RENAMEABLE else keys


def _value(item: Mapping[str, Any], field_name: str) -> Any:
    return comparable_field(dict(item), field_name)


def _clean(value: Any) -> Any:
    """``value`` without row identities or discussion, lists in content order.

    Re-sorted after stripping, as ``photos_without_comments`` does: the
    snapshot orders photos by JSON that includes their comments, and namesake
    events by their ids, so the order of the stripped lists would otherwise
    still carry what was stripped — a comment on main or a branch copy's new
    id would read as a change.
    """
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if k not in _NOT_CONTENT}
    if isinstance(value, list):
        cleaned = [_clean(v) for v in value]
        if cleaned and all(isinstance(v, dict) for v in cleaned):
            cleaned.sort(key=lambda v: json.dumps(v, sort_keys=True, default=str))
        return cleaned
    return value


def _canonical_list(values: Sequence[Any]) -> list[str]:
    return sorted(json.dumps(v, sort_keys=True, default=str) for v in values)


def _canonical(value: Any) -> str:
    return json.dumps(_clean(value), sort_keys=True, separators=(",", ":"), default=str)
