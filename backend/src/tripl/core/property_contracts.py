"""Contracts on properties (F23, #306): what a typed property promises, as checks.

A field contract is declared by hand on a FieldDefinition. A property carries
its contract already: its variable's type and JSON Schema fragment, its
documented values, and the event property list that marks it required. This
module reads those into the same ``FieldContractExpectation``s a field
contract becomes, keyed on the property's warehouse path
(``<json_column>.<path>``), so the adapters count them in the same statement
and a violation becomes the same ``SchemaDrift`` row, alert and health fact.

What maps to what:

* **required** on every event of the type -> ``required_null_violation``. A
  missing path counts like a NULL. The allowed null rate is
  ``1 - presence threshold``, the lowest threshold among the type's events,
  so the contract fires where property drift would call the entry
  ``missing_required``.
* **documented values** (the variable's list, or an event's override) ->
  ``enum_violation``, for a scalar type.
* **``pattern``** in the schema -> ``regex_violation``.
* **``minimum`` / ``maximum``** in the schema -> ``range_violation``.

Contracts run per event type, the unit ``catalog_sync`` filters the warehouse
rows by; events are not a filter a scan can express in SQL. So an event-level
rule has to hold for the whole type before it becomes a check: a property is
required only when every event of the type requires it, and the enum is the
union of every listing event's values, and only exists when each of them
documents some. The per-event verdicts stay with property drift.

The documented rules are strict claims, so a property's enum, regex and range
contracts tolerate no bad rows (threshold 0): there is no per-property
``contract_max_bad_rate`` to loosen them with.

Pure: the worker loads the rows and calls in.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from tripl.core.adapters.base import FieldContractExpectation
from tripl.core.analyzers._event_generator_variables import VariableIndex
from tripl.core.property_drift import effective_threshold
from tripl.json_paths import split_property_field
from tripl.models.variable import Variable

#: How many properties of one event type get contracts in one check. Every
#: property adds up to four expectations to the shared contract statement, and
#: each one extracts the path per row; the cap keeps a type with a very wide
#: property list from multiplying the cost of the scan. Sorted by path, so the
#: same properties are checked on every run.
MAX_PROPERTY_CONTRACTS_PER_EVENT_TYPE = 50

# ``variable_type``s whose value is one scalar, and so can be an enum.
_SCALAR_TYPES = frozenset({"string", "number", "boolean", "date", "datetime"})


@dataclass(frozen=True)
class PropertyListing:
    """One event's entry for the property: its property-list row."""

    required: bool
    # The event's override of the allowed values; ``None``: the global list.
    values: Sequence[str] | None = None
    # The event's ``required_presence_threshold``; ``None``: the default.
    presence_threshold: float | None = None


@dataclass(frozen=True)
class TypedProperty:
    """A variable as a property of one event type's events."""

    # ``<json_column>.<path>``: where the value lives in the warehouse.
    source: str
    variable_type: str
    json_schema: Mapping[str, Any] | None = None
    allowed_values: Sequence[str] = ()
    listings: Sequence[PropertyListing] = field(default_factory=tuple)
    # How many (non-archived) events the type has, listing the property or not.
    type_event_count: int = 0


def property_source(variable: Variable, json_columns: Collection[str] | None) -> str | None:
    """The ``<json_column>.<path>`` a variable's value lives at, if any.

    The first of the variable's warehouse tokens (``source_name``, then its
    bindings, then its name) that is a well-formed path — of one of
    ``json_columns`` when given, the JSON columns a run read. The same tokens
    property drift judges presence through.
    """
    for token in VariableIndex.source_tokens_of(variable):
        try:
            prop = split_property_field(token)
        except ValueError:
            continue
        if prop is not None and (json_columns is None or prop[0] in json_columns):
            return token
    return None


# (variable_id, event_id, required, values) — one property-list row.
ListingRow = tuple[uuid.UUID, uuid.UUID, bool, Sequence[str] | None]


def typed_properties_by_type(
    *,
    events: Mapping[uuid.UUID, tuple[uuid.UUID, float | None]],
    listing_rows: Iterable[ListingRow],
    variables: Iterable[Variable],
    json_columns: Collection[str] | None = None,
) -> dict[uuid.UUID, list[TypedProperty]]:
    """Each event type's typed properties, from the rows that describe them.

    ``events``: event -> (event type, presence threshold), the type's
    non-archived events. ``variables``: the listed variables, excluded ones
    already left out. One path gets one set of checks per type, even when two
    variables are bound to it.
    """
    event_counts: dict[uuid.UUID, int] = {}
    for event_type_id, _threshold in events.values():
        event_counts[event_type_id] = event_counts.get(event_type_id, 0) + 1
    listings: dict[tuple[uuid.UUID, uuid.UUID], list[PropertyListing]] = {}
    for variable_id, event_id, required, values in listing_rows:
        if event_id not in events:
            continue
        event_type_id, threshold = events[event_id]
        listings.setdefault((event_type_id, variable_id), []).append(
            PropertyListing(
                required=bool(required),
                values=None if values is None else [str(value) for value in values],
                presence_threshold=threshold,
            )
        )
    # By name, so when two variables share a path the same one wins every run.
    ordered = sorted(variables, key=lambda variable: (variable.name, str(variable.id)))
    out: dict[uuid.UUID, dict[str, TypedProperty]] = {}
    for variable in ordered:
        source = property_source(variable, json_columns)
        if source is None:
            continue
        for (event_type_id, variable_id), entries in listings.items():
            if variable_id != variable.id:
                continue
            out.setdefault(event_type_id, {}).setdefault(
                source,
                TypedProperty(
                    source=source,
                    variable_type=str(variable.variable_type),
                    json_schema=variable.json_schema,
                    allowed_values=[str(value) for value in variable.allowed_values or []],
                    listings=tuple(entries),
                    type_event_count=event_counts.get(event_type_id, 0),
                ),
            )
    return {
        event_type_id: sorted(props.values(), key=lambda prop: prop.source)
        for event_type_id, props in out.items()
    }


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _enum_spellings(variable_type: str, value: str) -> list[str]:
    """The texts a documented value may come back as from the warehouse.

    Extraction yields a JSON number as text, and ``10`` may be documented as
    ``10.0`` (or the other way round), so a number is accepted in both its
    documented and its canonical spelling.
    """
    spellings = [value]
    if variable_type == "number":
        try:
            number = float(value)
        except ValueError:
            return spellings
        if math.isfinite(number):
            canonical = str(int(number)) if number.is_integer() else repr(number)
            if canonical not in spellings:
                spellings.append(canonical)
    return spellings


def _required_expectation(prop: TypedProperty) -> FieldContractExpectation | None:
    listings = prop.listings
    if not listings or len(listings) < prop.type_event_count:
        return None
    if not all(listing.required for listing in listings):
        return None
    presence = min(effective_threshold(listing.presence_threshold) for listing in listings)
    return FieldContractExpectation(
        field_name=prop.source,
        drift_type="required_null_violation",
        threshold=max(0.0, min(1.0, 1.0 - presence)),
    )


def _enum_expectation(prop: TypedProperty) -> FieldContractExpectation | None:
    if prop.variable_type not in _SCALAR_TYPES or not prop.listings:
        return None
    options: list[str] = []
    for listing in prop.listings:
        values = prop.allowed_values if listing.values is None else listing.values
        if not values:
            # One event leaves the property undocumented: nothing to hold the
            # type's rows to.
            return None
        for value in values:
            for spelling in _enum_spellings(prop.variable_type, str(value)):
                if spelling not in options:
                    options.append(spelling)
    return FieldContractExpectation(
        field_name=prop.source,
        drift_type="enum_violation",
        threshold=0.0,
        enum_options=tuple(options),
    )


def property_contract_expectations(
    properties: Sequence[TypedProperty],
) -> list[FieldContractExpectation]:
    """Every expectation the properties' types and lists imply, by path."""
    expectations: list[FieldContractExpectation] = []
    ordered = sorted(properties, key=lambda prop: prop.source)
    for prop in ordered[:MAX_PROPERTY_CONTRACTS_PER_EVENT_TYPE]:
        required = _required_expectation(prop)
        if required is not None:
            expectations.append(required)
        enum = _enum_expectation(prop)
        if enum is not None:
            expectations.append(enum)

        schema = prop.json_schema or {}
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and pattern:
            expectations.append(
                FieldContractExpectation(
                    field_name=prop.source,
                    drift_type="regex_violation",
                    threshold=0.0,
                    regex=pattern,
                )
            )
        minimum = _finite_number(schema.get("minimum"))
        maximum = _finite_number(schema.get("maximum"))
        if minimum is not None or maximum is not None:
            expectations.append(
                FieldContractExpectation(
                    field_name=prop.source,
                    drift_type="range_violation",
                    threshold=0.0,
                    min_value=minimum,
                    max_value=maximum,
                )
            )
    return expectations
