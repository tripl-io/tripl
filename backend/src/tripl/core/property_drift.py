"""Property drift (F23, #306): an event's property list against what a scan saw.

Three findings, each a ``PropertyDrift`` row keyed on (variable, event, kind):

* ``new_property`` — the event carried a JSON path whose variable is not on
  its property list. Only for events whose list names at least one property:
  an empty list means "not described yet", not "carries nothing".
* ``missing_required`` — a required entry's presence rate fell below the
  event's threshold, or the path was absent from every row of the event.
* ``type_change`` — the sampled values of a typed variable are of a kind its
  type does not admit. Per variable, ``event_id`` NULL: the sampler reads the
  whole scan config and cannot attribute a value to an event.

The scan never writes the property list (a user-owned table): drift is how it
reports disagreement. Rows are refreshed in place; ``status`` is never touched
except that an ``accepted`` row reopens when the finding comes back, since
accepting changes the plan so that the finding cannot recur on its own.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from tripl.core.analyzers._event_generator_variables import (
    SCAN_PROVENANCE_DESCRIPTION,
    VariableIndex,
)
from tripl.models.event import Event
from tripl.models.property_drift import PropertyDrift, PropertyDriftKind
from tripl.models.schema_drift import SCHEMA_DRIFT_STATUS_ACCEPTED, SCHEMA_DRIFT_STATUS_OPEN
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import VariableEventValueOverride

# Owner decision 6 sets the threshold per event; this is what an event that
# sets none gets. A property carried on 95% of occurrences is one the event is
# meant to carry and a tracking bug drops now and then.
DEFAULT_REQUIRED_PRESENCE = 0.95

# The observed type a variable type admits besides its own: a string may hold
# dates, a date-time may be sampled as a bare date, and ``json`` is any
# container.
_ALSO_ADMITS: dict[str, frozenset[str]] = {
    "string": frozenset({"date", "datetime"}),
    "datetime": frozenset({"date"}),
    "json": frozenset({"string_array", "number_array"}),
}


def effective_threshold(event_threshold: float | None) -> float:
    return DEFAULT_REQUIRED_PRESENCE if event_threshold is None else event_threshold


def type_admits(expected: str, observed: str) -> bool:
    return expected == observed or observed in _ALSO_ADMITS.get(expected, frozenset())


def is_typed_by_hand_or_scan(variable: Variable) -> bool:
    """Whether the variable's type is a claim worth checking values against.

    A scan-minted variable that is still ``string`` with no schema has no type
    anyone chose: F23.4 types it once from its first samples, and one observed
    before that keeps the placeholder. Reporting every number under it as a
    type change would bury the real ones.
    """
    return not (
        variable.variable_type == "string"
        and variable.json_schema is None
        and variable.description == SCAN_PROVENANCE_DESCRIPTION
    )


@dataclass(frozen=True)
class Finding:
    variable_id: uuid.UUID
    event_id: uuid.UUID | None
    kind: PropertyDriftKind
    detail: dict[str, Any]


def event_findings(
    *,
    presence: Mapping[tuple[uuid.UUID, uuid.UUID], float],
    measured_events: Collection[uuid.UUID],
    entries: Mapping[uuid.UUID, Mapping[uuid.UUID, bool]],
    thresholds: Mapping[uuid.UUID, float | None],
    json_variables: Collection[uuid.UUID],
) -> list[Finding]:
    """The per-event findings of one run. Pure.

    ``presence``: (variable, event) -> rate this run measured.
    ``measured_events``: events whose rows all carried counts, so absence
    from ``presence`` means the path was carried by none of its rows.
    ``entries``: event -> {variable -> required}, the property list.
    ``json_variables``: variables bound to a JSON path of a column this run
    read; a required entry for any other variable cannot be judged here.
    """
    findings: list[Finding] = []
    for event_id in sorted(measured_events, key=str):
        listed = entries.get(event_id, {})
        threshold = effective_threshold(thresholds.get(event_id))
        if listed:
            for (variable_id, observed_event), rate in sorted(
                presence.items(), key=lambda item: (str(item[0][0]), str(item[0][1]))
            ):
                if observed_event == event_id and variable_id not in listed:
                    findings.append(
                        Finding(
                            variable_id,
                            event_id,
                            PropertyDriftKind.new_property,
                            {"presence_rate": rate},
                        )
                    )
        for variable_id, required in sorted(listed.items(), key=lambda item: str(item[0])):
            if not required or variable_id not in json_variables:
                continue
            rate = presence.get((variable_id, event_id), 0.0)
            if rate < threshold:
                findings.append(
                    Finding(
                        variable_id,
                        event_id,
                        PropertyDriftKind.missing_required,
                        {"presence_rate": rate, "threshold": threshold},
                    )
                )
    return findings


def upsert_findings(
    session: Session,
    *,
    project_id: uuid.UUID,
    scan_config_id: uuid.UUID | None,
    findings: Iterable[Finding],
) -> int:
    """Write the findings, refreshing a row that exists. Returns how many."""
    findings = list(findings)
    if not findings:
        return 0
    variable_ids = {finding.variable_id for finding in findings}
    existing = {
        (row.variable_id, row.event_id, row.kind): row
        for row in session.execute(
            select(PropertyDrift).where(
                PropertyDrift.project_id == project_id,
                PropertyDrift.variable_id.in_(variable_ids),
            )
        ).scalars()
    }
    now = datetime.now(UTC)
    for finding in findings:
        row = existing.get((finding.variable_id, finding.event_id, finding.kind.value))
        if row is None:
            row = PropertyDrift(
                project_id=project_id,
                variable_id=finding.variable_id,
                event_id=finding.event_id,
                kind=finding.kind.value,
            )
            session.add(row)
            existing[(finding.variable_id, finding.event_id, finding.kind.value)] = row
        elif row.status == SCHEMA_DRIFT_STATUS_ACCEPTED:
            # Acceptance changed the plan so this finding could not recur;
            # that it did is new.
            row.status = SCHEMA_DRIFT_STATUS_OPEN
            row.resolved_at = None
            row.resolved_by = None
        row.detail = dict(finding.detail)
        row.scan_config_id = scan_config_id
        row.detected_at = now
    return len(findings)


def detect_event_property_drifts(
    session: Session,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    scan_config_id: uuid.UUID | None,
    contexts: Mapping[tuple[uuid.UUID, uuid.UUID, uuid.UUID], Mapping[str, Any]],
    measured_events: Collection[uuid.UUID],
    json_columns: Collection[str],
) -> int:
    """Run ``event_findings`` over one ``generate_events`` pass and store them."""
    if not measured_events:
        return 0
    entries: dict[uuid.UUID, dict[uuid.UUID, bool]] = {}
    for variable_id, event_id, required in session.execute(
        select(
            VariableEventValueOverride.variable_id,
            VariableEventValueOverride.event_id,
            VariableEventValueOverride.required,
        ).where(VariableEventValueOverride.event_id.in_(list(measured_events)))
    ):
        entries.setdefault(event_id, {})[variable_id] = required

    # Also what narrows ``measured_events`` to events that still exist: a
    # group rule may have merged one away after its presence was measured.
    thresholds: dict[uuid.UUID, float | None] = {
        event_id: threshold
        for event_id, threshold in session.execute(
            select(Event.id, Event.required_presence_threshold).where(
                Event.id.in_(list(measured_events))
            )
        )
    }
    measured_events = set(thresholds)
    presence: dict[tuple[uuid.UUID, uuid.UUID], float] = {}
    for (variable_id, event_id, _field_id), context in contexts.items():
        rate = context.get("presence_rate")
        if rate is None or event_id not in measured_events:
            continue
        key = (variable_id, event_id)
        presence[key] = max(presence.get(key, 0.0), float(rate))

    required_ids = {
        variable_id for listed in entries.values() for variable_id, req in listed.items() if req
    }
    json_variables: set[uuid.UUID] = set()
    if required_ids:
        columns = frozenset(json_columns)
        # An excluded variable gets no contexts, so it would read as never
        # carried; the exclusion is an instruction to stop judging it.
        query = select(Variable).where(
            Variable.id.in_(required_ids), Variable.excluded_from_scans.is_(False)
        )
        if branch_id is not None:
            query = query.where(Variable.branch_id == branch_id)
        for variable in session.execute(query).scalars():
            if any(
                token.partition(".")[0] in columns and token.partition(".")[2]
                for token in VariableIndex.source_tokens_of(variable)
            ):
                json_variables.add(variable.id)

    findings = event_findings(
        presence=presence,
        measured_events=measured_events,
        entries=entries,
        thresholds=thresholds,
        json_variables=json_variables,
    )
    clear_stale_findings(
        session,
        project_id=project_id,
        kinds=(PropertyDriftKind.new_property, PropertyDriftKind.missing_required),
        scope=PropertyDrift.event_id.in_(list(measured_events)),
        still_found={(f.variable_id, f.event_id, f.kind.value) for f in findings},
    )
    return upsert_findings(
        session, project_id=project_id, scan_config_id=scan_config_id, findings=findings
    )


def clear_stale_findings(
    session: Session,
    *,
    project_id: uuid.UUID,
    kinds: Collection[PropertyDriftKind],
    scope: ColumnElement[bool],
    still_found: Collection[tuple[uuid.UUID, uuid.UUID | None, str]],
) -> int:
    """Delete untriaged rows this run measured and no longer finds.

    An ``open`` row with no note carries nothing a person wrote, so when its
    condition has gone — the property reached its threshold, or someone put
    it on the list by hand — it goes too, rather than counting as open for
    the retention window. Snoozed, false-positive and accepted rows are
    triage, and stay. ``scope`` is what this run measured; rows outside it
    are left alone.
    """
    stale = [
        row
        for row in session.execute(
            select(PropertyDrift).where(
                PropertyDrift.project_id == project_id,
                PropertyDrift.kind.in_([kind.value for kind in kinds]),
                PropertyDrift.status == SCHEMA_DRIFT_STATUS_OPEN,
                PropertyDrift.resolution_note.is_(None),
                scope,
            )
        ).scalars()
        if (row.variable_id, row.event_id, row.kind) not in still_found
    ]
    for row in stale:
        session.delete(row)
    return len(stale)


def type_findings(
    variables: Iterable[Variable],
    observed: Mapping[str, tuple[str, dict[str, Any] | None]],
) -> list[Finding]:
    """``type_change`` findings for variables whose samples disagree. Pure.

    ``observed``: raw token (``column.path``) -> the inferred type pair.
    """
    findings: list[Finding] = []
    for variable in variables:
        if variable.excluded_from_scans or not is_typed_by_hand_or_scan(variable):
            continue
        inferred = next(
            (observed[t] for t in VariableIndex.source_tokens_of(variable) if t in observed),
            None,
        )
        if inferred is None or type_admits(variable.variable_type, inferred[0]):
            continue
        findings.append(
            Finding(
                variable.id,
                None,
                PropertyDriftKind.type_change,
                {
                    "expected_type": variable.variable_type,
                    "observed_type": inferred[0],
                    "observed_schema": inferred[1],
                },
            )
        )
    return findings
