"""Retire the variables a scan minted and nothing ever referenced (danger zone).

The predicate lives in :mod:`tripl.core.variable_retirement` and is shared with
the scan worker, which runs the same pass at the end of every run so a catalog
stops accumulating fossils in the first place. This module is the operator's
door to it: one deliberate, owner-only, audited pass over a whole project, for
the backlog that built up before the worker started sweeping — 1296 rows on the
project this was written for.

Shaped on :mod:`tripl.services.detection_reset_service`, the other danger-zone
service: bulk statements with ``synchronize_session=False``, commit, return
counts, idempotent on a second call.

``dry_run`` is the default and it is not decoration. The counts it returns are
broken down by *why* each row was kept, so an operator sees "1280 retirable, 18
kept because a live event still names them, 219 kept because a human edited
them" before anything is deleted.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, lazyload

from tripl.core.variable_retirement import RetirementPlan, plan_retirement, referenced_tokens
from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_meta_value import EventMetaValue
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import VariableEventValueOverride
from tripl.models.variable_value import VariableValue
from tripl.models.variable_value_drift import VariableValueDrift
from tripl.services.plan_branch_service import resolve_branch_id
from tripl.services.search_service import reindex_project_branch

# How many ids go into one ``IN (...)`` when the retirable set is deleted.
#
# PostgreSQL refuses a statement carrying more than 65535 bind parameters, and
# the retirable set is unbounded by construction — it is exactly the population
# this module exists because nothing bounded. 1000 keeps the statement small
# enough to plan well and leaves two orders of magnitude of headroom.
_DELETE_BATCH = 1000


async def _variable_ids_with_rows(
    session: AsyncSession,
    column: InstrumentedAttribute[uuid.UUID],
    scoped_variable_ids: Select[tuple[uuid.UUID]],
) -> set[uuid.UUID]:
    """Which of the branch's variables have at least one row on *column*.

    A ``DISTINCT`` over an indexed FK, not a per-variable ``EXISTS``: all three
    tables index ``variable_id`` (``ix_variable_values_variable`` and the
    ``index=True`` declarations on the drift and override models).

    ``scoped_variable_ids`` is a SUBQUERY, not the list of ids already loaded
    above, and that is the whole point: PostgreSQL refuses a statement carrying
    more than 65535 bind parameters, and the population this module exists to
    clean up is unbounded by construction. Passing the literal list would have
    put the same ceiling on these three reads that ``_DELETE_BATCH`` is there to
    keep off the writes — guarding one and not the other is worse than guarding
    neither, because it reads as though the problem was handled.
    """
    rows = await session.execute(select(column).where(column.in_(scoped_variable_ids)).distinct())
    return set(rows.scalars().all())


async def plan_project_retirement(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
) -> RetirementPlan:
    """Compute the retirement plan for one project branch, writing nothing.

    Six reads, all bounded by the project: the variables, every stored field
    value, every stored meta value, and three anti-join id sets. The two value
    passes are the expensive ones and they are deliberately single streaming
    scans — the alternative, a ``LIKE '%${name}%'`` per variable, is one full
    pass per row of the catalog, which on the project this was written for is
    1517 of them.

    Six is the whole count only because the variable read declares its loader.
    ``Variable.value_contexts`` is ``lazy="selectin"`` and each context then
    selectin-loads its FieldDefinition, so the plain statement was two further
    reads that nothing here asked for and that no ``LIMIT`` bounds — the
    project's entire context table, hydrated to answer a question the indexed
    ``with_contexts`` anti-join below already answers by id. This
    runs on every ``GET /variables?usage=used|unused``, so it is a request path
    and not only the danger-zone one.
    """
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    scope = (Variable.project_id == project_id, Variable.branch_id == branch_id)
    variables = list(
        (
            await session.execute(
                select(Variable).where(*scope).options(lazyload(Variable.value_contexts))
            )
        )
        .scalars()
        .all()
    )
    if not variables:
        return RetirementPlan()

    # BOTH value tables. A ``${token}`` is legal in an event's field values and
    # in its META values, and only the first produces a ``VariableValue``
    # context (that model is keyed by ``field_definition_id``). Reading field
    # values alone would therefore retire a variable referenced solely from a
    # meta value — and the event naming it would immediately start rendering
    # "Unknown variable token", the exact failure this predicate exists to
    # avoid. ``event_service._attach_template_warnings`` reads both; so does
    # this.
    field_values = (
        (
            await session.execute(
                select(EventFieldValue.value)
                .join(Event, EventFieldValue.event_id == Event.id)
                .where(Event.project_id == project_id, Event.branch_id == branch_id)
            )
        )
        .scalars()
        .all()
    )
    meta_values = (
        (
            await session.execute(
                select(EventMetaValue.value)
                .join(Event, EventMetaValue.event_id == Event.id)
                .where(Event.project_id == project_id, Event.branch_id == branch_id)
            )
        )
        .scalars()
        .all()
    )
    values = [*field_values, *meta_values]

    scoped_ids = select(Variable.id).where(*scope)
    return plan_retirement(
        variables,
        referenced=referenced_tokens(values),
        with_contexts=await _variable_ids_with_rows(session, VariableValue.variable_id, scoped_ids),
        with_drifts=await _variable_ids_with_rows(
            session, VariableValueDrift.variable_id, scoped_ids
        ),
        with_overrides=await _variable_ids_with_rows(
            session, VariableEventValueOverride.variable_id, scoped_ids
        ),
    )


async def retire_unused_variables(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    slug: str,
    mode: str = "delete",
    dry_run: bool = True,
) -> dict[str, int]:
    """Apply (or preview) the retirement plan. Returns counts; commits when it acts.

    ``mode="delete"`` removes the rows, and it is the default because it is the
    one that helps: an *excluded* variable still renders in the Variables page's
    own "excluded from scans" section, so excluding 1280 fossils leaves the
    operator looking at the same 1280 rows under a different heading.
    ``mode="exclude"`` is for the case delete cannot serve — a binding still
    live in the warehouse, which the next scan would simply mint again, where
    the tombstone is the entire point.

    Reindexing runs once at the end, and only when something actually moved.
    """
    plan = await plan_project_retirement(session, project_id=project_id, branch_id=branch_id)
    counts = {"scanned": plan.scanned, "retirable": len(plan.retirable), "retired": 0}
    counts.update({f"kept_{reason}": count for reason, count in plan.kept.items()})
    if dry_run or not plan.retirable:
        return counts

    for start in range(0, len(plan.retirable), _DELETE_BATCH):
        batch = plan.retirable[start : start + _DELETE_BATCH]
        statement = (
            update(Variable).where(Variable.id.in_(batch)).values(excluded_from_scans=True)
            if mode == "exclude"
            else delete(Variable).where(Variable.id.in_(batch))
        )
        await session.execute(statement.execution_options(synchronize_session=False))

    counts["retired"] = len(plan.retirable)
    await session.commit()
    resolved_branch_id = await resolve_branch_id(session, project_id, branch_id)
    await reindex_project_branch(
        session, project_id=project_id, branch_id=resolved_branch_id, slug=slug
    )
    return counts
