import uuid
from collections.abc import Callable, Mapping, Sequence
from functools import partial

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.data_source import DataSource
from tripl.models.domain_enums import MetricKind
from tripl.models.fact_table import FactTable
from tripl.models.metric_definition import MetricDefinition
from tripl.schemas.fact_table import FactTableCreate, FactTableListItem, FactTableUpdate
from tripl.services.data_source_scope import (
    DATA_SOURCE_NOT_AVAILABLE,
    data_source_out_of_project_scope,
    project_organization_id,
    scanning_project_ids_for,
)
from tripl.services.fact_table_dependents import (
    fact_table_conflict_detail,
    metrics_depending_on,
    metrics_needing_column,
    metrics_needing_filter,
)
from tripl.services.plan_branch_service import resolve_branch_id
from tripl.services.project_lookup import resolve_project_id
from tripl.services.search_service import reindex_project_branch

# Defensive cap on the list query; realistic projects have well under this many
# fact tables.
_LIST_HARD_CAP = 1000


async def _refresh_main_search_index(
    session: AsyncSession, project_id: uuid.UUID, slug: str
) -> None:
    """Refresh the search index after a fact-table mutation.

    Fact tables are global (project-scoped, not branched), so only the MAIN
    branch index is refreshed eagerly; feature-branch indexes pick the change
    up on their next rebuild.
    """
    main_branch_id = await resolve_branch_id(session, project_id, None)
    await reindex_project_branch(
        session, project_id=project_id, branch_id=main_branch_id, slug=slug
    )


async def _verify_data_source(
    session: AsyncSession, project_id: uuid.UUID, data_source_id: uuid.UUID
) -> None:
    """Assert this project may use the data source before persisting a link.

    Blocks an editor from binding a fact table to another project's warehouse by
    supplying a foreign data source id (multi-tenant isolation).

    The rule is OWNERSHIP, shared verbatim with the fact-table preview door and
    with the ``sql``-metric save and preview doors — see
    ``services/data_source_scope``. It used to be "at least one ``ScanConfig``
    links the two", which answered the same question differently from the metric
    doors and refused a workspace-global warehouse nobody scans (tripl-0zpq.177).
    """
    data_source = await session.get(DataSource, data_source_id)
    if data_source is None:
        raise HTTPException(status_code=404, detail=DATA_SOURCE_NOT_AVAILABLE)
    if data_source_out_of_project_scope(
        data_source,
        project_id=project_id,
        project_organization_id=await project_organization_id(session, project_id),
        scanning_project_ids=await scanning_project_ids_for(session, data_source),
    ):
        raise HTTPException(status_code=404, detail=DATA_SOURCE_NOT_AVAILABLE)


def _stored_names(rows: object) -> list[str]:
    """The ``name`` of every well-formed descriptor in a stored JSON list.

    ``row_filters`` and ``columns`` are both JSON columns of ``{"name": ...}``
    descriptors, so a legacy row can be any shape; anything without a string
    ``name`` — up to and including the whole value not being a list — is skipped
    rather than raising, because a referential guard must never turn an unrelated
    edit into a 500.
    """
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return []
    return [
        row["name"] for row in rows if isinstance(row, Mapping) and isinstance(row.get("name"), str)
    ]


def _stored_filter_names(fact_table: FactTable) -> list[str]:
    """The named row filters currently stored on the table, in stored order."""
    return _stored_names(fact_table.row_filters)


def _stored_column_names(fact_table: FactTable) -> list[str]:
    """The introspected columns currently stored on the table, in stored order."""
    return _stored_names(fact_table.columns)


def _removed_names(replacement: object, stored: Sequence[str]) -> list[str]:
    """Which ``stored`` names the PATCH's whole-list replacement drops.

    ``replacement`` is untyped at this boundary (it arrives from
    ``model_dump(exclude_unset=True)``), and ``_stored_names`` narrows it: a value
    that is not a list of named descriptors leaves the surviving set empty, which
    reports every stored name as removed and makes the guard refuse the update
    rather than let a malformed payload strand a metric silently.
    """
    surviving = set(_stored_names(replacement))
    return [name for name in stored if name not in surviving]


def _reject_stranding_removals(
    *,
    dependents: Sequence[MetricDefinition],
    removed: Sequence[str],
    needing: Callable[[Sequence[MetricDefinition], str], list[MetricDefinition]],
    noun: str,
    nouns: str,
    then_one: str,
    then_many: str,
) -> None:
    """One 409 naming EVERY removed name a saved metric still reads, or nothing.

    Raising on the first blocked name would cost the operator one
    edit/save/409 round trip per name in a payload the server already examined in
    full: a form that prunes three filters used by three metrics teaches them one
    of the three facts at a time, and each message reads as though it were the
    only obstruction. ``fact_table_conflict_detail``'s ten-then-count truncation
    already bounds the body, so naming them all together costs nothing.
    """
    blocking_ids: set[uuid.UUID] = set()
    blocked: list[str] = []
    for name in removed:
        metrics = needing(dependents, name)
        if not metrics:
            continue
        blocked.append(name)
        blocking_ids.update(metric.id for metric in metrics)
    if not blocked:
        return
    one = len(blocked) == 1
    named = ", ".join(repr(name) for name in blocked)
    raise HTTPException(
        status_code=409,
        detail=fact_table_conflict_detail(
            # Ordered by ``dependents`` rather than by discovery order, so the
            # same PATCH always produces the same sentence.
            metrics=[metric for metric in dependents if metric.id in blocking_ids],
            lead=f"Cannot remove or rename the {noun if one else nouns} {named}.",
            reason=f"That {noun} is used by" if one else f"Those {nouns} are used by",
            then=then_one if one else then_many,
        ),
    )


async def _reject_updates_that_strand_a_metric(
    session: AsyncSession,
    fact_table: FactTable,
    update_data: dict[str, object],
) -> None:
    """409 when this PATCH would leave a saved fact metric pointing at nothing.

    Three edits are referential, not presentational:

    * dropping or renaming a named row filter — metrics store the NAME, and
      ``_fact_conditions._resolve_named_filter_fragment`` raises at collection
      time when it no longer resolves;
    * dropping or renaming an introspected column — metrics store the NAME of the
      column they aggregate, break down by, or filter on, and ``_fact_conditions``
      rejects it against ``allowed_columns`` at collection time. A re-preview
      against a query that no longer projects the column sends exactly this
      PATCH;
    * unbinding the data source — ``metric_collect`` raises "FactTable ... has no
      data source bound" for every metric on the table.

    ``exclude_unset`` is what makes the two list arms safe to write this way: a
    PATCH that does not mention ``row_filters`` (or ``columns``) has no key here
    and cannot trip the guard, while an explicit ``[]`` does, because removing all
    of them is exactly the destructive case. Same for ``data_source_id``: the key
    is present only when the client actually sent it, so ``None`` here always
    means "unbind", never "left alone".

    The dependents query runs only when one of those three keys is present AND
    something was actually dropped, so an ordinary rename of ``display_name`` —
    or a re-preview that only ADDS a column — still costs nothing.
    """
    unbinding = "data_source_id" in update_data and update_data["data_source_id"] is None
    removed_filters = (
        _removed_names(update_data["row_filters"], _stored_filter_names(fact_table))
        if "row_filters" in update_data
        else []
    )
    removed_columns = (
        _removed_names(update_data["columns"], _stored_column_names(fact_table))
        if "columns" in update_data
        else []
    )
    if not unbinding and not removed_filters and not removed_columns:
        return

    dependents = await metrics_depending_on(
        session, project_id=fact_table.project_id, fact_table_id=fact_table.id
    )
    if not dependents:
        return

    # Both predicates are scoped to THIS table: a cross-table ratio metric names
    # its other operand's filters and columns in the same ``config``, and those
    # references are not this edit's business.
    _reject_stranding_removals(
        dependents=dependents,
        removed=removed_filters,
        needing=partial(metrics_needing_filter, fact_table_id=fact_table.id),
        noun="row filter",
        nouns="row filters",
        then_one="change the filter",
        then_many="change those filters",
    )
    _reject_stranding_removals(
        dependents=dependents,
        removed=removed_columns,
        needing=partial(metrics_needing_column, fact_table_id=fact_table.id),
        noun="column",
        nouns="columns",
        then_one="change the column",
        then_many="change those columns",
    )
    if unbinding:
        raise HTTPException(
            status_code=409,
            detail=fact_table_conflict_detail(
                metrics=dependents,
                lead="Cannot unbind this fact table's data source.",
                reason="This fact table is read by",
                then="unbind it",
            ),
        )


async def _next_order(session: AsyncSession, project_id: uuid.UUID) -> int:
    """Append-at-end order value: one past the project's current maximum."""
    max_order = await session.scalar(
        select(func.max(FactTable.order)).where(FactTable.project_id == project_id)
    )
    return 0 if max_order is None else int(max_order) + 1


async def list_fact_tables(
    session: AsyncSession,
    slug: str,
    *,
    search: str | None = None,
    offset: int = 0,
    limit: int = 200,
) -> tuple[list[FactTable], int]:
    project_id = await resolve_project_id(session, slug)
    query = select(FactTable).where(FactTable.project_id == project_id)
    count_query = select(func.count(FactTable.id)).where(FactTable.project_id == project_id)

    if search:
        search_clause = or_(
            FactTable.name.ilike(f"%{search}%"),
            FactTable.display_name.ilike(f"%{search}%"),
            FactTable.description.ilike(f"%{search}%"),
        )
        query = query.where(search_clause)
        count_query = count_query.where(search_clause)

    total = (await session.execute(count_query)).scalar() or 0
    result = await session.execute(
        query.order_by(
            FactTable.order.asc(),
            FactTable.created_at.desc(),
            FactTable.id.asc(),
        )
        .offset(offset)
        .limit(min(limit, _LIST_HARD_CAP))
    )
    return list(result.scalars().all()), int(total)


async def list_fact_table_items(
    session: AsyncSession,
    slug: str,
    *,
    search: str | None = None,
    offset: int = 0,
    limit: int = 200,
) -> tuple[list[FactTableListItem], int]:
    """The catalog page with its per-row rollups (MT-30).

    ``metric_count`` counts every fact metric that READS the table, the way
    ``fact_table_dependents.metrics_depending_on`` decides it: the metric's own
    ``fact_table_id`` plus both ratio operands in ``config``. A cross-table
    ratio keeps its denominator's table only in that JSON, so a grouped count on
    the column alone would call a denominator-only table unused. The operand ids
    are not indexable, so this is one query for the project's fact metrics and
    the tally in Python (bounded by the project's metric count, never a query
    per row); the column and identifier counts come off the row's own JSON.
    """
    # Function-local for the reason ``metrics_depending_on`` gives: a
    # module-level import of the metrics service here would close an import loop.
    from tripl.services.metric_definition_service import _metric_fact_table_ids

    fact_tables, total = await list_fact_tables(
        session, slug, search=search, offset=offset, limit=limit
    )
    metric_counts: dict[uuid.UUID, int] = {}
    if fact_tables:
        listed_ids = {ft.id for ft in fact_tables}
        fact_metrics = (
            (
                await session.execute(
                    select(MetricDefinition).where(
                        MetricDefinition.project_id == fact_tables[0].project_id,
                        MetricDefinition.kind == MetricKind.fact,
                    )
                )
            )
            .scalars()
            .all()
        )
        for metric in fact_metrics:
            for fact_table_id in _metric_fact_table_ids(metric) & listed_ids:
                metric_counts[fact_table_id] = metric_counts.get(fact_table_id, 0) + 1
    items = [
        FactTableListItem.model_validate(fact_table).model_copy(
            update={
                "metric_count": metric_counts.get(fact_table.id, 0),
                "column_count": len(fact_table.columns or []),
                "identifier_count": len(fact_table.identifier_columns or []),
            }
        )
        for fact_table in fact_tables
    ]
    return items, total


async def get_fact_table(session: AsyncSession, slug: str, fact_table_id: uuid.UUID) -> FactTable:
    project_id = await resolve_project_id(session, slug)
    result = await session.execute(
        select(FactTable).where(
            FactTable.id == fact_table_id,
            FactTable.project_id == project_id,
        )
    )
    fact_table = result.scalar_one_or_none()
    if fact_table is None:
        raise HTTPException(status_code=404, detail="Fact table not found")
    return fact_table


async def create_fact_table(session: AsyncSession, slug: str, data: FactTableCreate) -> FactTable:
    project_id = await resolve_project_id(session, slug)

    existing = await session.scalar(
        select(FactTable.id).where(
            FactTable.project_id == project_id,
            FactTable.name == data.name,
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=409, detail="Fact table with this name already exists in project"
        )

    if data.data_source_id is not None:
        await _verify_data_source(session, project_id, data.data_source_id)

    order = await _next_order(session, project_id)
    fact_table = FactTable(project_id=project_id, order=order, **data.to_create_values())
    session.add(fact_table)
    await session.flush()
    await session.commit()
    await session.refresh(fact_table)
    await _refresh_main_search_index(session, project_id, slug)
    return fact_table


async def update_fact_table(
    session: AsyncSession,
    slug: str,
    fact_table_id: uuid.UUID,
    data: FactTableUpdate,
) -> FactTable:
    fact_table = await get_fact_table(session, slug, fact_table_id)
    update_data = data.model_dump(exclude_unset=True)

    new_data_source_id = update_data.get("data_source_id")
    if "data_source_id" in update_data and new_data_source_id is not None:
        await _verify_data_source(session, fact_table.project_id, new_data_source_id)

    # Before the blind ``setattr`` loop below: the loop has no referential
    # validation of any kind, so anything that could strand a saved metric has to
    # be refused here or not at all.
    await _reject_updates_that_strand_a_metric(session, fact_table, update_data)

    for key, value in update_data.items():
        setattr(fact_table, key, value)
    await session.commit()
    await session.refresh(fact_table)
    await _refresh_main_search_index(session, fact_table.project_id, slug)
    return fact_table


async def delete_fact_table(session: AsyncSession, slug: str, fact_table_id: uuid.UUID) -> None:
    fact_table = await get_fact_table(session, slug, fact_table_id)
    project_id = fact_table.project_id
    # ``metric_definitions.fact_table_id`` is ``ON DELETE SET NULL``, so the
    # database accepts this silently and leaves a fact metric with no table to
    # read — and a ratio operand's id lives in another metric's ``config`` JSON,
    # which no FK reaches at all. Refusing here is the only place the user is
    # still in a position to do something about it.
    dependents = await metrics_depending_on(
        session, project_id=project_id, fact_table_id=fact_table.id
    )
    if dependents:
        raise HTTPException(
            status_code=409,
            detail=fact_table_conflict_detail(
                metrics=dependents,
                lead="Cannot delete this fact table.",
                reason="This fact table is read by",
                then="delete the fact table",
            ),
        )
    await session.delete(fact_table)
    await session.commit()
    await _refresh_main_search_index(session, project_id, slug)
