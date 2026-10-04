"""Does a drift-style alert scope have any source data at all, project-wide?

Both drift scopes are downstream of configuration the AlertRule does not own, so
a rule can enable one and be structurally unable to fire: the flag only ever
NARROWS a candidate set, and when the producing side is empty there is no
candidate for it to narrow. This module answers only "is there
anything here to draw on", never "will it fire" — the second question belongs to
detection and dispatch, which run on a scan, not on a polling GET.

Kept out of ``_alerting_monitors`` for the same reason ``_alerting_health`` is
kept out of ``_alerting_destinations``: that module rolls rule state up into a
status, this is the read-side aggregation the responses are decorated with.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.distribution_drift import DistributionDrift
from tripl.models.domain_enums import DistributionDriftBand
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.scan_config import ScanConfig
from tripl.models.schema_drift import SCHEMA_DRIFT_STATUS_OPEN, SCHEMA_DRIFT_STATUS_SNOOZED
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import VariableEventValueOverride
from tripl.models.variable_value_drift import VariableValueDrift
from tripl.schemas.alerting import AlertScopeReadiness

# Restated rather than imported from ``variable_value_drift_service``, which
# would close a cycle: that module reaches ``alerting_service`` through
# search_service -> project_service, and ``alerting_service`` imports
# ``_alerting_monitors``, which imports this module. Only the model layer is
# safe to depend on from here. Both values track that service's
# ``DRIFT_RETENTION_DAYS`` / ``ACTIVE_DRIFT_STATUSES``; the readiness probe
# below is meaningless if they diverge.
_DRIFT_RETENTION_DAYS = 30
_ACTIVE_DRIFT_STATUSES = (SCHEMA_DRIFT_STATUS_OPEN, SCHEMA_DRIFT_STATUS_SNOOZED)


def _documents_values(column: sa.SQLColumnExpression[list[str] | None]) -> sa.ColumnElement[bool]:
    """Does this JSON list column hold anything at all?

    Compared as TEXT rather than through ``json_array_length``: these columns are
    ``sa.JSON`` (not JSONB), so on Postgres ``json = json`` has no operator and a
    plain ``!= []`` fails outright, while ``json_array_length`` ERRORS on a
    non-array scalar — a crash on an endpoint the Alerting tab polls. Every write
    goes through SQLAlchemy's JSON serializer, which renders the empty list as
    exactly ``[]``, and a cast can never raise. A value this misreads (raw SQL
    that wrote ``[ ]``, say) reads as documented and shows NO warning, which is
    the safe direction to be wrong in for an accusation.
    """
    return sa.cast(column, sa.Text).not_in(("[]", "null"))


async def load_scope_readiness(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> AlertScopeReadiness:
    """Whether each drift scope has any source data in this project.

    One round-trip: a FROM-less SELECT of two columns, each an OR of EXISTS
    probes on an indexed column. This runs on every monitors-summary poll, so it
    must stay one small query and must never write.

    Each scope asks the same pair of questions its candidate builder answers —
    is anything CONFIGURED to produce a candidate, and has anything already been
    COLLECTED that the builder would still pick up. Either alone is readiness;
    the two must stay symmetric across the scopes.
    """
    # Read-only, deliberately: ``plan_branch_service.ensure_main_branch_id``
    # CREATES the branch when it is missing, and this path is a GET. A project
    # with no main branch yields a NULL here, the ``branch_id ==`` comparisons
    # below match nothing, and readiness comes back False — which is the honest
    # answer, since a project with no main branch has no variables to document.
    main_branch = (
        sa.select(PlanBranch.id)
        .where(
            PlanBranch.project_id == project_id,
            PlanBranch.kind == BranchKind.main.value,
        )
        .limit(1)
        .scalar_subquery()
    )

    # Detection reads the MAIN branch only (core/analyzers/event_generator.py
    # resolves it before calling the detector), and skips variables tombstoned
    # out of scans (core/analyzers/_event_generator_variables.py) — an excluded
    # variable never enters the context map, so its documented list can never
    # produce a drift however full it is.
    documented_globally = (
        sa.select(sa.literal(1))
        .select_from(Variable)
        .where(
            Variable.project_id == project_id,
            Variable.branch_id == main_branch,
            Variable.excluded_from_scans.is_(False),
            _documents_values(Variable.allowed_values),
        )
        .exists()
    )
    # A per-event override REPLACES the variable's global list rather than
    # extending it (models/variable_event_value_override.py), so either source
    # alone is enough to give the scope something to drift against.
    documented_per_event = (
        sa.select(sa.literal(1))
        .select_from(VariableEventValueOverride)
        .join(Variable, Variable.id == VariableEventValueOverride.variable_id)
        .where(
            VariableEventValueOverride.project_id == project_id,
            VariableEventValueOverride.branch_id == main_branch,
            Variable.excluded_from_scans.is_(False),
            _documents_values(VariableEventValueOverride.values),
        )
        .exists()
    )
    # Rows on their own are enough here too, and for the SAME reason the
    # distribution scope needs its collected disjunct below: the candidate
    # builder (``_get_active_variable_value_drift_candidates`` in
    # worker/tasks/metrics/signals.py) selects VariableValueDrift ROWS and never
    # consults ``allowed_values`` or an override. Emptying a documented list
    # stops NEW rows but closes none of the old ones, so a project that emptied
    # its lists to quiet the noise keeps producing candidates from the survivors
    # for the rest of the retention window — and was being told the scope
    # "cannot fire" the whole time.
    #
    # Mirrors that builder's filters rather than probing rows blindly, because a
    # row it could never select is not readiness: the status set, the 30-day
    # window, the scan_config_id link (``ondelete="SET NULL"`` leaves rows behind
    # whose scan is gone, and the demo seeds rows with no scan at all), and the
    # exclusion flag on the ROW's variable — excluding no longer deletes the
    # drift a variable already had, so the builder asks the flag instead, and a
    # row it will skip is no more readiness than a resolved one. That is the
    # same flag the documented disjuncts above apply, which is what keeps a
    # project of excluded variables inert on every source of this scope.
    # The builder's snooze-EXPIRY clause is deliberately NOT mirrored: a
    # row snoozed until next week is a candidate this scope will produce, just
    # not today, and the question here is "ever", not "now".
    retention_cutoff = datetime.now(UTC) - timedelta(days=_DRIFT_RETENTION_DAYS)
    value_drift_collected = (
        sa.select(sa.literal(1))
        .select_from(VariableValueDrift)
        .join(Variable, Variable.id == VariableValueDrift.variable_id)
        .where(
            VariableValueDrift.project_id == project_id,
            VariableValueDrift.scan_config_id.in_(
                sa.select(ScanConfig.id).where(ScanConfig.project_id == project_id)
            ),
            Variable.excluded_from_scans.is_(False),
            VariableValueDrift.status.in_(_ACTIVE_DRIFT_STATUSES),
            VariableValueDrift.detected_at >= retention_cutoff,
        )
        .exists()
    )

    distribution_configured = (
        sa.select(sa.literal(1))
        .select_from(ScanConfig)
        .where(
            ScanConfig.project_id == project_id,
            _documents_values(ScanConfig.distribution_drift_fields),
        )
        .exists()
    )
    # Rows on their own are enough, and this disjunct is NOT redundant: the
    # candidate builder (``_get_active_distribution_drift_candidates`` in
    # worker/tasks/metrics/signals.py) reads DistributionDrift ROWS, not the
    # scan's configured field list. The seeded demo is exactly that shape — drift
    # rows collected against a config whose ``distribution_drift_fields`` is
    # empty (services/demo/builders/warehouse.py) — so dropping this would paint
    # a warning across the demo the demo exists to disprove.
    #
    # Mirrors that builder's BAND filter for the same reason the value-drift
    # probe above mirrors its own builder's filters: a row it could never select
    # is not readiness. Every scored bucket is persisted, not just the alarming
    # ones (worker/tasks/metrics/metric_rows.py bands each PSI and writes the
    # stable and minor rows too), and nothing prunes them outside the demo
    # reset — so a project that watched a column for months without ever
    # crossing the significant threshold, then cleared the field list, would
    # otherwise read as ready off a pile of rows no dispatch can ever turn into
    # a candidate. The demo survives the filter: its ladder is built to climb
    # stable -> minor -> significant (services/demo/noise.py), so its newest
    # bucket is a significant row.
    #
    # The builder's ``bucket == latest_bucket`` clause is deliberately NOT
    # mirrored, on the same reading that leaves the value-drift snooze expiry
    # alone above: the question here is "ever", not "now". It would also have to
    # be a per-scan_config correlated MAX — the builder takes the max per config,
    # not per project — turning a cheap indexed EXISTS into a correlated
    # aggregate on an endpoint the Alerting tab polls, against the "one small
    # query" rule in this module's docstring. What that leaves is one narrow
    # overclaim: a project whose only significant row sits behind a newer,
    # all-stable bucket reads as ready. That errs toward saying nothing, the
    # same direction ``_documents_values`` picks above, and it is the direction
    # to be wrong in for an accusation.
    distribution_collected = (
        sa.select(sa.literal(1))
        .select_from(DistributionDrift)
        .where(
            DistributionDrift.scan_config_id.in_(
                sa.select(ScanConfig.id).where(ScanConfig.project_id == project_id)
            ),
            DistributionDrift.band == DistributionDriftBand.significant.value,
        )
        .exists()
    )

    variable_value_drift, distribution_drift = (
        await session.execute(
            sa.select(
                sa.or_(documented_globally, documented_per_event, value_drift_collected),
                sa.or_(distribution_configured, distribution_collected),
            )
        )
    ).one()
    return AlertScopeReadiness(
        variable_value_drift=bool(variable_value_drift),
        distribution_drift=bool(distribution_drift),
    )
