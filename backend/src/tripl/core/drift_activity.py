"""Which drift rows still count: open, or snoozed until an instant that has passed.

One definition for schema, value and property drift, and for every reader of
them: the drift lists, the badge counts, the health score, the open-signals
feed, the rule replay, the live alert dispatch, the weekly digest and the
retention sweep. They must agree, or a drift shows on one surface and not on
another, or pages someone about a row the UI has already aged out.

Two separate rules, applied separately on purpose:

* **Active** (:func:`active_drift_clauses`): status ``open``, or ``snoozed``
  with the snooze over. Says nothing about age.
* **Retention** (:func:`retention_cutoff`): a row not refreshed for
  :data:`DRIFT_RETENTION_DAYS` days is out of view; the writer upserts in place,
  so a drift that clears on its own simply ages out. The replay and the digest
  bound rows by their own window instead, so they take only the active clauses.

The alert-scope readiness probe (``services._alerting_scope_readiness``) takes
the statuses and the retention but not the snooze expiry: it asks whether a
scope can ever fire, not whether it fires now.

The clauses are plain SQLAlchemy over the model, so they run on an async or a
sync session alike; callers add their own scope.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy.sql.elements import ColumnElement

from tripl.models.property_drift import PropertyDrift
from tripl.models.schema_drift import (
    SCHEMA_DRIFT_STATUS_OPEN,
    SCHEMA_DRIFT_STATUS_SNOOZED,
    SchemaDrift,
)
from tripl.models.variable_value_drift import VariableValueDrift

#: Days a drift row stays in view after it was last detected.
DRIFT_RETENTION_DAYS: Final = 30

#: The statuses a drift can be active in. ``accepted`` and ``false_positive``
#: are decisions, kept only for history.
ACTIVE_DRIFT_STATUSES: Final = (SCHEMA_DRIFT_STATUS_OPEN, SCHEMA_DRIFT_STATUS_SNOOZED)

type DriftModel = type[SchemaDrift] | type[VariableValueDrift] | type[PropertyDrift]


def retention_cutoff(now: datetime | None = None) -> datetime:
    """The oldest ``detected_at`` still in view at ``now`` (default: the current time)."""
    return (now or datetime.now(UTC)) - timedelta(days=DRIFT_RETENTION_DAYS)


def active_drift_clauses(model: DriftModel, now: datetime) -> list[ColumnElement[bool]]:
    """Open, or snoozed until an instant at or before ``now``. Retention is not applied.

    A snoozed row with no ``snoozed_until`` counts as active, as it always has.
    """
    return [
        model.status.in_(ACTIVE_DRIFT_STATUSES),
        (model.status != SCHEMA_DRIFT_STATUS_SNOOZED)
        | (model.snoozed_until.is_(None))
        | (model.snoozed_until <= now),
    ]
