"""Retire the variables a project no longer refers to, at the end of a scan.

The scan is what mints variables, so the scan is where they should stop
accumulating. Without this the catalog only ever grows: production's
``acme-ios`` reached 1517 variables of which 1296 were referenced by nothing,
1279 of them minted from the keys of one JSON map column (tripl-10h4).

The predicate is :mod:`tripl.core.variable_retirement`, shared verbatim with the
owner-facing danger-zone endpoint. Only the queries differ — that module runs on
an ``AsyncSession`` and this one on the worker's sync ``Session`` — and that
split is the same one every other pair in this package lives with.

**This does not undo the run that just finished.** A variable minted by that run
had its token written into at least one event's field value by the same run — a
path enters ``all_paths`` only by appearing in a row, and that row's event
stores ``${col.path}`` — so the reference check keeps it. What the sweep removes
is the row whose token has since disappeared from every stored value, which is
exactly the fossil.

One case does mint and sweep in the same run, and it is the right answer rather
than an exception to apologise for: a path carried *only* by an ARCHIVED event.
A scan deliberately leaves an archived row's field values alone (tripl-rsei), so
the token is never written and nothing live refers to the variable.

Runs after the scan's ``session.commit()`` and before the search reindex, so the
reindex sees the retired set and the deletions cannot be rolled back by a later
failure in the same task.

**"Nothing refers to this" is judged against field values the run has just
rewritten, and the rewrite is where a variable's evidence is lost — not here.**
The run's catalog pass reads its window, re-plans every event's stored values
from what that window held, and ``_upsert_field_values`` overwrites any
non-authored value that differs; ``delete_variable_contexts_for_event_type``
then drops the field's contexts because the field was rewritten. A variable
whose token was in the old value and is not in the new one is left with no
stored token and no context — this predicate's fossil — the moment that
rewrite commits, whether or not a sweep follows. What the sweep adds is the
loss of the ROW: the next run that sees the token again mints a new id, runs
``derive_display_name`` afresh, and the catalog shows a retired/created pair
where nothing changed in the warehouse.

How wide that flicker is depends on what the variable was minted from, and
that is the split ``include_scalar_derived`` carries (tripl-bwo8):

- A SCALAR column's variable is one ``${token}`` per column, and it vanishes
  on the cardinality of the window: ``event_plan.plan_column_meta`` sets
  ``meta['is_low']`` from the window it was given (non-JSON branch only), and
  ``plan_events`` then writes a LITERAL where the template stood. A column that
  is high over the table can read low over one quiet hour, and then the whole
  column flips at once, on every event carrying it. Recycling that row is
  churn an operator will notice, so the scheduled caller still asks for a
  DECLARED ``scan_lookback_hours`` before it sweeps these — the operator has
  said which window represents the plan, and the flip is theirs to own.
- A JSON column's variables are its discovered paths, one per key, and the
  ``is_low`` arm cannot reach them: a JSON column has no template and no
  literal fallback. What a narrow window does to a JSON variable is drop a key
  that did not arrive this interval from the stored value rebuilt for that
  event (``build_json_value`` from the window's row). That is the "key that
  stopped arriving" the sweep exists for, seen over one hour instead of one
  table, and recycling a key nothing refers to is the point rather than a
  hazard. Measured 2026-09-03, 1929 of production's 1931 variables are
  JSON-derived (``acme-ios`` 1805/1807, ``android`` 77/77, ``web`` 47/47), so
  a gate on the whole call left the whole growth unswept for the sake of two
  rows.

The sweep covers the whole project, while each caller sees only one config's
warehouse query. If any project config has no declared lookback, its scheduled
catalog pass may have replaced a scalar token using a narrow interval. Neither
a sibling config's full-table manual scan nor its declared lookback can prove
that token obsolete, so both callers defer all scalar-derived variables until
every config declares a representative window.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import Select, delete, select
from sqlalchemy.orm import InstrumentedAttribute, Session, lazyload

from tripl.core.variable_retirement import is_json_derived, plan_retirement, referenced_tokens
from tripl.models.domain_enums import FieldDefinitionType
from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_meta_value import EventMetaValue
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.scan_config import ScanConfig
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import VariableEventValueOverride
from tripl.models.variable_value import VariableValue
from tripl.models.variable_value_drift import VariableValueDrift

logger = logging.getLogger(__name__)

# Mirrors ``variable_retirement_service._DELETE_BATCH``: PostgreSQL caps a
# statement at 65535 bind parameters and the retirable set is unbounded by
# construction — it is the population this code exists because nothing bounded.
_DELETE_BATCH = 1000


def retired_details_line(count: int) -> str:
    """The one sentence both worker call sites report a sweep with.

    Lives here rather than at either call site because ``collect_metrics`` now
    says it TWICE — once on the stub it stamps the moment the delete is durable,
    once in the full summary it assembles ~400 lines later — and ``run_scan``
    says it a third time. An operator comparing a scheduled run against a manual
    one must not be left deciding whether two phrasings mean the same thing.
    """
    return f"Retired {count} unused variables no event refers to"


def _ids_with_rows(
    session: Session,
    column: InstrumentedAttribute[uuid.UUID],
    scoped_variable_ids: Select[tuple[uuid.UUID]],
) -> set[uuid.UUID]:
    """See ``variable_retirement_service._variable_ids_with_rows``.

    A SUBQUERY, not the loaded id list: the population is unbounded by
    construction and PostgreSQL caps a statement at 65535 bind parameters, so a
    literal ``IN`` here would put on the reads exactly the ceiling
    ``_DELETE_BATCH`` keeps off the writes.
    """
    return set(
        session.execute(select(column).where(column.in_(scoped_variable_ids)).distinct()).scalars()
    )


def _json_column_names(
    session: Session,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
) -> set[str]:
    """The FieldDefinition names that are JSON-typed on EVERY type of the branch.

    A name is what ``is_json_derived`` matches a variable's dotted prefix
    against, and a variable is per branch while a FieldDefinition is per event
    type, so one name can carry several types. A name that is ``json`` on one
    type and ``string`` on another is treated as scalar: the variable's token
    then stands in a scalar column's stored value somewhere on the branch, and
    that value is the one the window can flip to a literal.

    Reads the stored type rather than the warehouse's because the sweep has no
    adapter — and does not want one: the catalog sync wrote this type from the
    warehouse's own when it created the row
    (``generation._ensure_event_type_with_fields``). It never revisits it, and
    a field can also be authored through the API, so this is that decision as
    last recorded rather than as the warehouse would answer it today. The drift
    errs toward deferring — a column recorded ``string`` keeps its variables
    waiting for a declared window — which is the safe direction here.
    """
    scope = [EventType.project_id == project_id]
    if branch_id is not None:
        scope.append(EventType.branch_id == branch_id)
    only_json: dict[str, bool] = {}
    for name, field_type in session.execute(
        select(FieldDefinition.name, FieldDefinition.field_type)
        .join(EventType, FieldDefinition.event_type_id == EventType.id)
        .where(*scope)
        .distinct()
    ):
        only_json[name] = only_json.get(name, True) and field_type == FieldDefinitionType.json
    return {name for name, is_json in only_json.items() if is_json}


def retire_unused_variables(
    session: Session,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    include_scalar_derived: bool = True,
) -> int:
    """Delete the project branch's unreferenced scan-created variables.

    ``include_scalar_derived=False`` restricts the pass to variables minted
    from a path inside a JSON-typed column (:func:`is_json_derived` against the
    branch's FieldDefinitions) and DEFERS the rest — leaves them in place,
    unjudged, for a caller that can defend the window (the module docstring
    has the split). Even when a caller permits scalars, an unset lookback on
    any project config defers them: this sweep is project-wide and cannot prove
    which config rewrote a given variable's stored values.

    Returns the number of rows this call ACTUALLY deleted, for the scan's own
    report — summed from the statements' rowcounts, not from the length of the
    plan. The two differ whenever a concurrent run got there first, and that is
    not a rare shape: the sweep is project-wide but this task is per-config, and
    ``check_metrics_due`` dispatches every due config as an independent Celery
    task with no project-level lock (its advisory lock serialises the DISPATCH
    loop, and ``_get_active_scan_jobs`` is checked per config). Two configs of
    one project on the same interval therefore run in parallel, both compute the
    same retirable set, and the second ``DELETE`` matches zero rows — so
    reporting ``len(plan.retirable)`` had both runs claim the full count, in the
    summary and in the log.

    Commits only when it had something to delete, so a scan over a healthy
    catalog costs six reads — the variables, both value tables, and one
    anti-join each for contexts, drifts and overrides — seven when deferring,
    for the branch's FieldDefinition types, and no write at all.
    """
    scope = [Variable.project_id == project_id]
    value_scope = [Event.project_id == project_id]
    if branch_id is not None:
        scope.append(Variable.branch_id == branch_id)
        value_scope.append(Event.branch_id == branch_id)

    # ``lazyload`` for the same reason as ``catalog_sync`` and the two request-path
    # selects: ``Variable.value_contexts`` is ``lazy="selectin"`` and each context
    # then selectin-loads its FieldDefinition, so hydrating these plainly pulls the
    # project's entire context table into a sweep that only ever reads ids, names
    # and provenance. The contexts are answered by the anti-join below instead
    # (tripl-xkbb).
    variables = list(
        session.execute(
            select(Variable).where(*scope).options(lazyload(Variable.value_contexts))
        ).scalars()
    )
    if not variables:
        return 0

    if include_scalar_derived:
        unbounded_config = session.execute(
            select(ScanConfig.id)
            .where(
                ScanConfig.project_id == project_id,
                ScanConfig.scan_lookback_hours.is_(None),
            )
            .limit(1)
        ).scalar_one_or_none()
        include_scalar_derived = unbounded_config is None

    deferred = 0
    if not include_scalar_derived:
        json_columns = _json_column_names(session, project_id=project_id, branch_id=branch_id)
        candidates = [v for v in variables if is_json_derived(v, json_columns)]
        deferred = len(variables) - len(candidates)
        variables = candidates
        if not variables:
            return 0

    # BOTH value tables — see the note in ``variable_retirement_service``. A
    # ``${token}`` is legal in an event's field values and in its META values,
    # and only the first produces a ``VariableValue`` context, so reading field
    # values alone would retire a variable referenced solely from a meta value.
    values = [
        *session.execute(
            select(EventFieldValue.value)
            .join(Event, EventFieldValue.event_id == Event.id)
            .where(*value_scope)
        ).scalars(),
        *session.execute(
            select(EventMetaValue.value)
            .join(Event, EventMetaValue.event_id == Event.id)
            .where(*value_scope)
        ).scalars(),
    ]

    scoped_ids = select(Variable.id).where(*scope)
    plan = plan_retirement(
        variables,
        referenced=referenced_tokens(values),
        with_contexts=_ids_with_rows(session, VariableValue.variable_id, scoped_ids),
        with_drifts=_ids_with_rows(session, VariableValueDrift.variable_id, scoped_ids),
        with_overrides=_ids_with_rows(session, VariableEventValueOverride.variable_id, scoped_ids),
    )
    if not plan.retirable:
        return 0

    # Batching is unchanged — PostgreSQL's 65535-bind-parameter ceiling is the
    # reason for it and that has not moved. What changed is that each statement's
    # rowcount is now read and summed, so a batch that raced another run and
    # matched nothing contributes nothing.
    retired = 0
    for start in range(0, len(plan.retirable), _DELETE_BATCH):
        batch = plan.retirable[start : start + _DELETE_BATCH]
        result = session.execute(
            delete(Variable)
            .where(Variable.id.in_(batch))
            .execution_options(synchronize_session=False)
        )
        rowcount = getattr(result, "rowcount", 0)
        retired += int(rowcount or 0)
    session.commit()
    # Both numbers, always: ``planned`` short of ``retired`` is the signature of
    # the concurrent sibling config described in the docstring, and a log line
    # carrying only one of them cannot tell that apart from a quiet catalog.
    # ``deferred`` is the scalar-derived rows a scheduled run left unjudged —
    # 0 whenever the caller swept everything — so a line reporting a few
    # retired JSON keys is not read as a full pass over the catalog. Emitted,
    # as before, whenever the PLAN had something to delete, which is not the
    # same as having deleted it: the raced run whose ``retired`` is 0 against a
    # non-zero ``planned`` is exactly the case the two numbers exist to show. A
    # run that planned nothing — nothing retirable, or everything deferred —
    # returns 0 above and says nothing at all.
    logger.info(
        "Retired unreferenced scan-created variables",
        extra={
            "project_id": str(project_id),
            "retired": retired,
            "planned": len(plan.retirable),
            "scanned": plan.scanned,
            "deferred": deferred,
        },
    )
    return retired
