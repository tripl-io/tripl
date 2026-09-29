"""Load the typed properties of one event type and turn them into contracts (F23, #306).

The I/O half of ``core.property_contracts``: the type's events, their
property-list entries and the listed variables. See that module for the rules.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection

from sqlalchemy import select
from sqlalchemy.orm import Session

from tripl.core.adapters.base import FieldContractExpectation
from tripl.core.property_contracts import (
    TypedProperty,
    property_contract_expectations,
    typed_properties_by_type,
)
from tripl.models.event import Event, EventStatus
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import VariableEventValueOverride


def load_typed_properties(
    session: Session,
    *,
    event_type_id: uuid.UUID,
    json_columns: Collection[str],
) -> list[TypedProperty]:
    """The properties of the type's events that live in one of ``json_columns``."""
    if not json_columns:
        return []
    events = {
        event_id: (event_type_id, threshold)
        for event_id, threshold in session.execute(
            select(Event.id, Event.required_presence_threshold).where(
                Event.event_type_id == event_type_id,
                Event.status != EventStatus.archived.value,
            )
        ).all()
    }
    if not events:
        return []
    listing_rows = [
        (variable_id, event_id, bool(required), values)
        for variable_id, event_id, required, values in session.execute(
            select(
                VariableEventValueOverride.variable_id,
                VariableEventValueOverride.event_id,
                VariableEventValueOverride.required,
                VariableEventValueOverride.values,
            ).where(VariableEventValueOverride.event_id.in_(list(events)))
        ).all()
    ]
    if not listing_rows:
        return []
    variables = session.execute(
        select(Variable).where(
            Variable.id.in_({row[0] for row in listing_rows}),
            # Excluding a variable from scans is an instruction to stop judging it.
            Variable.excluded_from_scans.is_(False),
        )
    ).scalars()
    return typed_properties_by_type(
        events=events,
        listing_rows=listing_rows,
        variables=variables,
        json_columns=json_columns,
    ).get(event_type_id, [])


def property_contract_expectations_for(
    session: Session,
    *,
    event_type_id: uuid.UUID,
    json_columns: Collection[str],
) -> list[FieldContractExpectation]:
    return property_contract_expectations(
        load_typed_properties(session, event_type_id=event_type_id, json_columns=json_columns)
    )
