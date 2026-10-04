"""Detection of observed variable values outside the documented lists.

Runs inside the scan's ``generate_events`` right after variable contexts are
rebuilt. Purely additive to the plan: rows are upserted on
``(variable_id, event_id)`` refreshing ``observed_values``/``detected_at``.

Because the key is the (variable, event) pair and the evidence is a mutable
payload, a resolved row would otherwise absorb every future novel value while
staying resolved — unlike ``SchemaDrift``, which keys on content and so gets a
fresh row per distinct finding. ``accepted`` rows therefore freeze: scans stop
refreshing them, and a scan reopens the row as soon as it observes a value
outside the resolved set.

The resolved set is ``observed_values`` INTERSECTED with what is documented,
not ``observed_values`` alone. Acceptance documents the values it accepts, and
absorption never did — so on any instance that ran an older build, the stored
evidence holds values nobody accepted, and reading the column at face value
would keep suppressing exactly the rows this freeze exists to fix. The
intersection is against the pair's documented list UNION the variable's global
list, because accepting globally on a pair that has an override writes to
``allowed_values`` while the override still decides what counts as novel.
Consequence worth stating: a value removed from the documented list by hand
comes back as a drift, which is the honest reading — a row cannot claim to be
resolved while holding a value the plan does not document.

``snoozed`` (time-boxed, expires on its own) and ``false_positive`` ("this
detector is wrong for this pair") stay as they were: refreshed in place,
never reopened by a scan.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from tripl.models.schema_drift import SCHEMA_DRIFT_STATUS_ACCEPTED, SCHEMA_DRIFT_STATUS_OPEN
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import VariableEventValueOverride
from tripl.models.variable_value_drift import VariableValueDrift

NOVEL_VALUE_SAMPLE_LIMIT = 20


def _accepted_sets_by_pair(
    session: Session,
    *,
    project_id: uuid.UUID,
    variable_ids: set[uuid.UUID],
    event_ids: set[uuid.UUID],
) -> dict[tuple[uuid.UUID, uuid.UUID], tuple[uuid.UUID, set[str]]]:
    """Row id + accepted value set for every accepted pair in scope.

    Selected as columns rather than ORM entities so the later bulk upsert,
    which bypasses the identity map, cannot be shadowed by stale instances.
    """
    return {
        (variable_id, event_id): (drift_id, set(observed or []))
        for drift_id, variable_id, event_id, observed in session.execute(
            select(
                VariableValueDrift.id,
                VariableValueDrift.variable_id,
                VariableValueDrift.event_id,
                VariableValueDrift.observed_values,
            ).where(
                VariableValueDrift.project_id == project_id,
                VariableValueDrift.variable_id.in_(variable_ids),
                VariableValueDrift.event_id.in_(event_ids),
                VariableValueDrift.status == SCHEMA_DRIFT_STATUS_ACCEPTED,
            )
        ).all()
    }


def detect_variable_value_drifts(
    session: Session,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    scan_config_id: uuid.UUID | None,
    contexts: dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID], dict[str, Any]],
) -> int:
    """Upsert drift rows for observed values missing from documented lists.

    ``contexts`` is the in-memory (variable_id, event_id, field_definition_id)
    → payload map that ``generate_events`` just persisted. The documented list
    for a (variable, event) pair is the per-event override when one exists,
    else the variable's global ``allowed_values``; an empty documented list
    means "no contract" and produces no drift. Returns the number of
    (variable, event) pairs recorded this run — pairs whose novel values are
    all covered by an existing acceptance are skipped and not counted.
    """
    if not contexts:
        return 0

    variable_ids = {variable_id for variable_id, _, _ in contexts}
    event_ids = {event_id for _, event_id, _ in contexts}

    variables_by_id: dict[uuid.UUID, Variable] = {
        variable.id: variable
        for variable in session.execute(
            select(Variable).where(Variable.id.in_(variable_ids))
        ).scalars()
    }
    override_query = select(VariableEventValueOverride).where(
        VariableEventValueOverride.project_id == project_id,
        VariableEventValueOverride.variable_id.in_(variable_ids),
        VariableEventValueOverride.event_id.in_(event_ids),
    )
    if branch_id is not None:
        override_query = override_query.where(VariableEventValueOverride.branch_id == branch_id)
    overrides: dict[tuple[uuid.UUID, uuid.UUID], list[str]] = {
        (override.variable_id, override.event_id): list(override.values)
        for override in session.execute(override_query).scalars()
        # A property entry without an override leaves the global list in force.
        if override.values is not None
    }

    novel_by_pair: dict[tuple[uuid.UUID, uuid.UUID], list[str]] = {}
    resolved_by_pair: dict[tuple[uuid.UUID, uuid.UUID], set[str]] = {}
    for (variable_id, event_id, _), context in contexts.items():
        variable = variables_by_id.get(variable_id)
        if variable is None:
            continue
        documented = overrides.get((variable_id, event_id), list(variable.allowed_values or []))
        if not documented:
            continue
        documented_set = set(documented)
        # What an acceptance can be trusted to have resolved: the pair's own
        # documented list, plus the variable's global list. The union covers the
        # one case where they differ — accepting globally on a pair that has an
        # override writes to ``allowed_values`` while the override still governs
        # what counts as novel.
        resolved_by_pair[(variable_id, event_id)] = documented_set | set(
            variable.allowed_values or []
        )
        novel = novel_by_pair.setdefault((variable_id, event_id), [])
        for value in context.get("values") or []:
            if value not in documented_set and value not in novel:
                novel.append(value)

    accepted_by_pair = _accepted_sets_by_pair(
        session,
        project_id=project_id,
        variable_ids=variable_ids,
        event_ids=event_ids,
    )

    rows: list[dict[str, Any]] = []
    reopen_ids: list[uuid.UUID] = []
    now = datetime.now(UTC)
    for (variable_id, event_id), novel in novel_by_pair.items():
        if not novel:
            continue
        accepted = accepted_by_pair.get((variable_id, event_id))
        if accepted is not None:
            drift_id, accepted_values = accepted
            # An accepted row's stored evidence only stands for "the set the user
            # accepted" once it is intersected with what the acceptance actually
            # documented. Builds before this guard existed refreshed
            # ``observed_values`` on accepted rows, so rows already on disk carry
            # values nobody ever accepted — silently absorbed, never documented,
            # never alerted. Those are documented nowhere, so the intersection
            # drops them and the row reopens, which is the whole point of the
            # fix. Without this the fix would be inert for exactly the population
            # the bug corrupted, and no migration can tell the two apart after
            # the fact: acceptance documents, absorption does not.
            accepted_values &= resolved_by_pair.get((variable_id, event_id), set())
            if set(novel) <= accepted_values:
                # Still inside the resolved set: leave the row untouched so its
                # evidence (and its retention clock) stay frozen at acceptance.
                continue
            reopen_ids.append(drift_id)
        rows.append(
            {
                "id": uuid.uuid4(),
                "project_id": project_id,
                "variable_id": variable_id,
                "event_id": event_id,
                "scan_config_id": scan_config_id,
                "observed_values": novel[:NOVEL_VALUE_SAMPLE_LIMIT],
                "detected_at": now,
            }
        )
    if not rows:
        return 0

    refresh_columns = ("observed_values", "detected_at")

    def _refresh(stmt: Any) -> dict[str, Any]:
        # The incoming ``scan_config_id`` wins, falling back to the stored one
        # only when this caller has none. Detection and dispatch run in the same
        # collect_metrics task for the same config, so the run that just saw the
        # drift must be the one the alert query (which filters on this column)
        # then matches — freezing the first writer would strand the row on a
        # config that may never dispatch again. COALESCE rather than a plain
        # refresh so a caller without a scan config still cannot blank an
        # attribution, and so a row an older build left NULL heals.
        assignments: dict[str, Any] = {
            column: getattr(stmt.excluded, column) for column in refresh_columns
        }
        assignments["scan_config_id"] = func.coalesce(
            stmt.excluded.scan_config_id, VariableValueDrift.scan_config_id
        )
        return assignments

    if session.bind is not None and session.bind.dialect.name == "sqlite":
        sqlite_stmt = sqlite_insert(VariableValueDrift).values(rows)
        sqlite_stmt = sqlite_stmt.on_conflict_do_update(
            index_elements=["variable_id", "event_id"],
            set_=_refresh(sqlite_stmt),
        )
        session.execute(sqlite_stmt)
    else:
        pg_stmt = pg_insert(VariableValueDrift).values(rows)
        pg_stmt = pg_stmt.on_conflict_do_update(
            constraint="uq_variable_value_drift_context",
            set_=_refresh(pg_stmt),
        )
        session.execute(pg_stmt)

    if reopen_ids:
        # The upsert just replaced the evidence with values the acceptance does
        # not cover; drop the resolution (note included — it described the
        # accepted set) so review and alerting pick the row up again.
        session.execute(
            update(VariableValueDrift)
            .where(VariableValueDrift.id.in_(reopen_ids))
            .values(
                status=SCHEMA_DRIFT_STATUS_OPEN,
                resolution_note=None,
                resolved_at=None,
                resolved_by=None,
                snoozed_until=None,
            )
        )
    return len(rows)
