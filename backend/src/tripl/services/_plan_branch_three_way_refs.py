"""Comparable forms of what a snapshot spells by name, for "Update from main".

A snapshot names other rows by their display names: an event's values name
variables as ``${token}``s, a variable's overrides name events by
``(event type, event name)``, and an event names its successor as
``"type.name"``. Names move. A rename on one side rewrites every such
reference on that side, so compared raw, a rename on one side next to an
unrelated edit on the other reads as both sides editing the same field (a
false conflict), and a rename alone reads as an edit (a false presence row).

So before three sides are compared, every such reference is rewritten to one
identity per row, the same on all three sides: the row's name where every side
spells it alike, else an opaque marker for its slot. The comparable forms are
used only to decide; what is written and what a conflict row shows stay the
raw snapshot values.

Pure, like ``_plan_branch_three_way``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

SIDES: tuple[str, ...] = ("base", "main", "branch")

# A ``${token}``, spelled the way ``rewrite_variable_token_references`` matches it.
_TOKEN = re.compile(r"\$\{([^{}]*)\}")

_VALUE_FIELDS = ("field_values", "meta_values")


class _SlotLike(Protocol):
    base: dict[str, Any] | None
    main: dict[str, Any] | None
    branch: dict[str, Any] | None


def retoken_values(values: Sequence[Any], renames: Mapping[str, str]) -> list[dict[str, Any]]:
    """``values`` with every ``${old}`` in ``renames`` turned into ``${new}``.

    One pass per value, so a swap (``a`` <-> ``b``) never fuses two tokens —
    the reason the database rewrite goes through parking tokens.
    """
    out = [dict(v) for v in values]
    if not renames:
        return out
    for v in out:
        text = v.get("value")
        if isinstance(text, str):
            v["value"] = _TOKEN.sub(
                lambda match: "${" + renames.get(match.group(1), match.group(1)) + "}", text
            )
    return out


def dotted(item: Mapping[str, Any]) -> str:
    """How a successor names an event: ``"type.name"``."""
    return f"{item.get('event_type_name') or ''}.{item.get('name')}"


def _side_items(slot: _SlotLike) -> list[tuple[str, dict[str, Any]]]:
    return [
        (side, item)
        for side, item in (("base", slot.base), ("main", slot.main), ("branch", slot.branch))
        if item is not None
    ]


@dataclass
class References:
    """Per row identities, and each side's payload with references rewritten to them."""

    # id(raw item) -> the row's identity, for events and variables of every side.
    ident_of: dict[int, str] = field(default_factory=dict)
    # id(raw item) -> the comparable copy of that item.
    comparable: dict[int, dict[str, Any]] = field(default_factory=dict)
    # side -> the comparable payload.
    payloads: dict[str, dict[str, Any]] = field(default_factory=dict)

    def of(self, item: dict[str, Any] | None) -> dict[str, Any] | None:
        if item is None:
            return None
        found: dict[str, Any] = self.comparable.get(id(item), item)
        return found


def _identities(
    slots: Sequence[_SlotLike], prefix: str, name_of: Any
) -> tuple[dict[str, dict[str, str]], dict[int, str]]:
    """Each side's name -> identity map, and each raw item's identity.

    A row's identity is its BASE name where it has a base, so a reference the
    base spells, and a side that left the name alone, reads exactly as it is
    written (a dangling ``${token}`` included). A row without a base is its
    name where both sides spell it alike and no base row owns that name, else
    an opaque marker for its slot.
    """
    by_side: dict[str, dict[str, list[str]]] = {side: {} for side in SIDES}
    ident_of: dict[int, str] = {}
    base_names = {name_of(slot.base) for slot in slots if slot.base is not None}
    for index, slot in enumerate(slots):
        present = _side_items(slot)
        spellings = {name_of(item) for _side, item in present}
        if slot.base is not None:
            ident = name_of(slot.base)
        elif len(spellings) == 1 and not spellings & base_names:
            ident = next(iter(spellings))
        else:
            ident = f"\x00{prefix}{index}"
        for side, item in present:
            ident_of[id(item)] = ident
            by_side[side].setdefault(name_of(item), []).append(ident)
    # A name two slots share on one side (namesake events) names no one row:
    # it stays as it is spelled.
    names = {
        side: {name: idents[0] if len(set(idents)) == 1 else name for name, idents in m.items()}
        for side, m in by_side.items()
    }
    return names, ident_of


def build_references(
    payloads: Mapping[str, Mapping[str, Any]],
    variable_slots: Sequence[_SlotLike],
    event_slots: Sequence[_SlotLike],
) -> References:
    """Rewrite every reference of the three ``payloads`` to the rows' identities."""
    variable_names, variable_ident = _identities(
        variable_slots, "v", lambda item: str(item.get("name"))
    )
    event_names, event_ident = _identities(event_slots, "e", dotted)
    refs = References(ident_of={**variable_ident, **event_ident})
    for side in SIDES:
        payload = payloads[side]
        tokens = variable_names[side]
        events = event_names[side]
        out_events: list[dict[str, Any]] = []
        for event in payload.get("events", []):
            copy = dict(event)
            for f in _VALUE_FIELDS:
                if isinstance(event.get(f), list):
                    copy[f] = retoken_values(event[f], tokens)
            successor = event.get("superseded_by")
            if successor:
                copy["superseded_by"] = events.get(str(successor), str(successor))
            refs.comparable[id(event)] = copy
            out_events.append(copy)
        out_variables: list[dict[str, Any]] = []
        for variable in payload.get("variables", []):
            copy = dict(variable)
            overrides = variable.get("event_value_overrides")
            if isinstance(overrides, list):
                copy["event_value_overrides"] = sorted(
                    (
                        {
                            "event": events.get(
                                f"{o.get('event_type_name') or ''}.{o.get('event_name')}",
                                f"{o.get('event_type_name') or ''}.{o.get('event_name')}",
                            ),
                            "values": o.get("values"),
                            "required": bool(o.get("required", False)),
                        }
                        for o in overrides
                    ),
                    key=lambda o: json.dumps(o, sort_keys=True, default=str),
                )
            refs.comparable[id(variable)] = copy
            out_variables.append(copy)
        refs.payloads[side] = {**payload, "events": out_events, "variables": out_variables}
    return refs


def references_into(refs: References, payload: Mapping[str, Any], idents: set[str]) -> list[str]:
    """What on one side points at the events ``idents``: overrides and successors.

    ``payload`` is the side's RAW payload; the comparison runs on identities,
    so a rename of the variable or of the pointing event is not a change.
    """
    out: list[str] = []
    for variable in payload.get("variables", []):
        who = refs.ident_of.get(id(variable), str(variable.get("name")))
        comparable = refs.of(variable) or {}
        for override in comparable.get("event_value_overrides") or []:
            if override.get("event") in idents:
                out.append(json.dumps(["override", who, override], sort_keys=True, default=str))
    for event in payload.get("events", []):
        comparable = refs.of(event) or {}
        if comparable.get("superseded_by") in idents:
            who = refs.ident_of.get(id(event), dotted(event))
            out.append(json.dumps(["successor", who, comparable["superseded_by"]]))
    return sorted(out)
