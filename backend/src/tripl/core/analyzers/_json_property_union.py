"""Fold an event's breakdown rows into one property set (F23, #306).

A breakdown row is one combination of values, and for a JSON column one
combination of the keys present. Several rows collapse into one event whenever
the name format does not depend on the JSON, and the event keeps one value per
field. Before F23 the busiest row's JSON won, so a key that appears only on
some occurrences — an optional property — vanished from the plan.

Here the JSON of every row of an identity is merged into one template carrying
the UNION of the keys, and each ``${column.path}`` token gets a presence rate:
the share of the identity's rows, weighted by their counts, that carried it.
On a leaf two rows disagree about (a kept literal value), the busier row wins,
the rule ``_rows_most_frequent_last`` already applies to every other field.

Pure, no session: ``generate_events`` calls it before writing anything.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import replace
from typing import Any

from tripl.core.analyzers.event_plan import PlannedEvent
from tripl.json_paths import flatten_json_paths

_TOKEN = re.compile(r"^\$\{([^}{]*)\}$")

# event identity -> raw token -> presence rate in [0, 1].
Presence = dict[str, dict[str, float]]


def _merge(into: dict[str, Any], other: Mapping[str, Any]) -> None:
    """Deep-merge ``other`` into ``into``; ``other`` wins a disagreeing leaf."""
    for key, value in other.items():
        current = into.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            _merge(current, value)
        else:
            into[key] = value


def _tokens(document: Mapping[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for _path, leaf in flatten_json_paths(document):
        if isinstance(leaf, str):
            match = _TOKEN.match(leaf)
            if match:
                tokens.add(match.group(1))
    return tokens


def _parse(value: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(value)
    except TypeError, ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def fold_json_properties(
    ordered: Sequence[PlannedEvent], json_columns: Collection[str]
) -> tuple[list[PlannedEvent], Presence]:
    """``ordered`` with every row's JSON replaced by its identity's union.

    ``ordered`` is ``_rows_most_frequent_last``'s output: each identity's rows
    ascending by count, so merging in order lets the busiest row win a leaf.
    Every row of an identity gets the same union, so whichever row is written
    last — and the ``max_events`` cap can stop anywhere — carries all the keys.

    Presence is reported only for identities whose rows all carry a count; a
    hand-built analysis without counts says nothing about how often.
    """
    if not json_columns:
        return list(ordered), {}
    columns = frozenset(json_columns)
    unions: dict[tuple[str, str], dict[str, Any]] = {}
    carried: dict[tuple[str, str], int] = {}
    totals: dict[str, int] = {}
    uncounted: set[str] = set()
    for planned in ordered:
        name = planned.name
        if planned.row_count is None:
            uncounted.add(name)
        count = planned.row_count or 0
        totals[name] = totals.get(name, 0) + count
        for _fd_id, column, value in planned.field_values:
            if column not in columns:
                continue
            document = _parse(value)
            if document is None:
                continue
            _merge(unions.setdefault((name, column), {}), document)
            for token in _tokens(document):
                carried[(name, token)] = carried.get((name, token), 0) + count

    folded = [
        replace(
            planned,
            field_values=tuple(
                (
                    fd_id,
                    column,
                    json.dumps(unions[(planned.name, column)], ensure_ascii=False, sort_keys=True)
                    if (planned.name, column) in unions
                    else value,
                )
                for fd_id, column, value in planned.field_values
            ),
        )
        for planned in ordered
    ]
    presence: Presence = {}
    for (name, token), count in carried.items():
        if name not in uncounted and totals.get(name):
            presence.setdefault(name, {})[token] = count / totals[name]
    return folded, presence
