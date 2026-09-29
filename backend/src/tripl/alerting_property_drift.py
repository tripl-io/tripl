"""Property drift as an alert candidate (F23, #306): the one mapping.

The live dispatch (``worker.tasks.metrics.signals``, sync) and the in-UI replay
(``services.alerting_service``, async) both turn ``PropertyDrift`` rows into
``DriftAlertCandidate``s. Value drift keeps two hand-maintained copies of that
field mapping and a docstring asking whoever edits one to edit the other
(tripl-0zpq.158); here there is one copy, and both loaders only choose rows.

The mapping rides the shared drift columns:

* ``drift_field``  — the property (variable) name;
* ``drift_type``   — the drift kind (``AlertDriftType.new_property`` /
  ``missing_required`` / ``type_change``, the ``PropertyDriftKind`` values);
* ``sample_value`` — what the scan saw, as a short clause;
* ``event_id``     — the event, so event and event-type filters apply. NULL for
  a type change, which is per property: such a candidate passes an event
  filter like the event-less schema drift does;
* ``actual_count`` / ``expected_count`` — the presence rate and the event's
  threshold in percent for the two per-event kinds, ``1`` vs ``0`` for a type
  change. The drift scopes bypass the numeric thresholds, so these only make
  the audit row readable.

The rows a loader may choose are the "active" ones by the same rule as value
drift (open, or snoozed until a past instant) whose property is still scanned
— :func:`active_property_drift_filters` — within the value-drift retention.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy.sql.elements import ColumnElement

from tripl.alerting_matching import SCOPE_PROPERTY_DRIFT, DriftAlertCandidate
from tripl.models.property_drift import PropertyDrift, PropertyDriftKind
from tripl.models.variable import Variable

# Property drift is always reported as a spike: "something the plan did not
# say", like the other drift scopes, so a rule's direction toggle reads the
# same for all of them.
PROPERTY_DRIFT_DIRECTION = "spike"


def active_property_drift_filters(now: datetime) -> list[ColumnElement[bool]]:
    """Open, or snoozed until an instant that has passed, on a scanned property.

    Needs ``Variable`` joined on ``PropertyDrift.variable_id``. Excluding a
    property from scans stops new drift being detected, but the rows it already
    had outlive the exclusion; the flag is asked here so nobody keeps being
    paged — or counted against — about a property they took out of scanning.
    """
    return [
        PropertyDrift.status.in_(("open", "snoozed")),
        (PropertyDrift.status != "snoozed")
        | (PropertyDrift.snoozed_until.is_(None))
        | (PropertyDrift.snoozed_until <= now),
        Variable.excluded_from_scans.is_(False),
    ]


def _percent(value: object) -> float:
    try:
        return round(float(value) * 100.0, 1)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return 0.0


def _format_percent(value: float) -> str:
    return f"{value:.0f}%" if float(value).is_integer() else f"{value:.1f}%"


def property_drift_sample(kind: str, detail: Mapping[str, Any]) -> str:
    """What the scan saw, as the clause an alert quotes after the property name."""
    if kind == PropertyDriftKind.type_change.value:
        observed = detail.get("observed_type") or "?"
        expected = detail.get("expected_type") or "?"
        return f"observed {observed}, typed {expected}"
    rate = _format_percent(_percent(detail.get("presence_rate")))
    if kind == PropertyDriftKind.missing_required.value:
        threshold = _format_percent(_percent(detail.get("threshold")))
        return f"on {rate} of rows, required on {threshold}"
    return f"on {rate} of rows, not on the event's property list"


def _counts(kind: str, detail: Mapping[str, Any]) -> tuple[float, float]:
    if kind == PropertyDriftKind.type_change.value:
        return 1.0, 0.0
    actual = _percent(detail.get("presence_rate"))
    if kind == PropertyDriftKind.missing_required.value:
        return actual, _percent(detail.get("threshold"))
    return actual, 0.0


def property_drift_candidate(
    drift: PropertyDrift,
    *,
    variable_name: str,
    scan_config_id: uuid.UUID | None,
) -> DriftAlertCandidate:
    """The candidate one ``PropertyDrift`` row becomes, on either path."""
    kind = str(drift.kind)
    detail = drift.detail or {}
    actual, expected = _counts(kind, detail)
    return DriftAlertCandidate(
        id=drift.id,
        scan_config_id=scan_config_id,
        scope_type=SCOPE_PROPERTY_DRIFT,
        scope_ref=str(drift.id),
        event_id=drift.event_id,
        event_type_id=None,
        bucket=drift.detected_at,
        direction=PROPERTY_DRIFT_DIRECTION,
        actual_count=actual,
        expected_count=expected,
        drift_field=variable_name,
        drift_type=kind,
        sample_value=property_drift_sample(kind, detail),
    )


def property_drift_scope_name(event_name: str | None, variable_name: str) -> str:
    """``"signup.plan"`` for a per-event drift, ``"All events.plan"`` for a type."""
    return f"{event_name or 'All events'}.{variable_name}"
