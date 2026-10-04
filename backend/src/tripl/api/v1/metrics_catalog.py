import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from tripl.api.deps import EditorUserDep, SessionDep, get_editor_user
from tripl.models.domain_enums import MetricKind, MetricStatus
from tripl.models.metric_definition import MetricDefinition
from tripl.schemas.metric_definition import (
    FactOperand,
    FactOperandPreviewResponse,
    MetricCollectNowResponse,
    MetricDefinitionBulkUpdate,
    MetricDefinitionCreate,
    MetricDefinitionDetailResponse,
    MetricDefinitionListResponse,
    MetricDefinitionMove,
    MetricDefinitionReorder,
    MetricDefinitionResponse,
    MetricDefinitionUpdate,
    MetricGeneratedSqlResponse,
    MetricPreviewRequest,
    MetricPreviewResponse,
    MetricSeriesPreviewRequest,
)
from tripl.schemas.metric_series import (
    MetricBreakdownsResponse,
    MetricSeriesResponse,
    MetricVersionSeriesResponse,
)
from tripl.schemas.text_filters import FreeTextFilter
from tripl.services import (
    audit_service,
    metric_definition_service,
    metric_preview_service,
    metric_series_service,
)

router = APIRouter(prefix="/projects/{slug}/metrics", tags=["metrics-catalog"])
_editor_required = [Depends(get_editor_user)]

TimeFrom = Annotated[datetime | None, Query(alias="from")]
TimeTo = Annotated[datetime | None, Query(alias="to")]

# Same ceiling events.py puts on its bulk samples, for the same reason: the
# catalog's "select all" is unbounded, and an audit row should stay readable.
_BULK_SAMPLE = 200


def _bulk_update_audit_payload(
    metric_ids: list[uuid.UUID], data: MetricDefinitionBulkUpdate
) -> dict[str, object]:
    """The ``metric_definition.bulk_update`` payload: count, id sample, changes.

    Deliberately the shape of ``events.bulk_event_audit_payload`` — ``count`` is
    the true number of metrics and ``metric_ids`` is only a sample, so a reader
    can tell a 3-metric archive from a 300-metric one even when the list is cut
    off. Pass the DEDUPLICATED ids: the service updates ``set(data.metric_ids)``,
    so auditing the raw list would file ``count: 3`` for a 2-metric change.

    ``exclude_unset`` mirrors the service's own ``update_values``, so the row
    reports exactly the columns the statement wrote — including an explicit
    ``owner_id: null``, which unassigns the owner rather than meaning "unset".
    The fixed keys are written last so a request field can never shadow them.
    """
    return {
        **data.model_dump(mode="json", exclude_unset=True, exclude={"metric_ids"}),
        "count": len(metric_ids),
        "metric_ids": [str(metric_id) for metric_id in metric_ids[:_BULK_SAMPLE]],
        "truncated": len(metric_ids) > _BULK_SAMPLE,
    }


@router.get("", response_model=MetricDefinitionListResponse)
async def list_metric_definitions(
    session: SessionDep,
    slug: str,
    status: Annotated[list[MetricStatus] | None, Query()] = None,
    kind: MetricKind | None = None,
    # FreeTextFilter: binds into an ILIKE, so a NUL aborts inside asyncpg
    # before SQL runs.
    search: FreeTextFilter | None = None,
    reviewed: bool | None = None,
    owner_id: uuid.UUID | None = None,
    # Metrics that read this fact table, as either operand (F7).
    fact_table_id: uuid.UUID | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
) -> MetricDefinitionListResponse:
    items, total = await metric_definition_service.list_metric_definitions_enriched(
        session,
        slug,
        status=status,
        kind=kind,
        search=search,
        reviewed=reviewed,
        owner_id=owner_id,
        fact_table_id=fact_table_id,
        offset=offset,
        limit=limit,
    )
    active_total = await metric_definition_service.count_active_metric_definitions(
        session, slug, kind=kind, search=search
    )
    return MetricDefinitionListResponse(items=items, total=total, active_total=active_total)


@router.post(
    "",
    response_model=MetricDefinitionResponse,
    status_code=201,
)
async def create_metric_definition(
    session: SessionDep,
    slug: str,
    data: MetricDefinitionCreate,
    current_user: EditorUserDep,
) -> MetricDefinition:
    metric = await metric_definition_service.create_metric_definition(session, slug, data)
    await audit_service.record(
        session,
        user=current_user,
        action="metric_definition.create",
        target_type="metric_definition",
        target_id=metric.id,
        target_name=metric.name,
        project_slug=slug,
        payload=data.model_dump(),
    )
    return metric


@router.post("/bulk-update", status_code=204)
async def bulk_update_metric_definitions(
    session: SessionDep,
    slug: str,
    data: MetricDefinitionBulkUpdate,
    current_user: EditorUserDep,
) -> None:
    # ``current_user`` rather than the bare ``_editor_required`` dependency
    # because the row below needs the actor: this route stops collection and
    # anomaly detection for a whole selection, and it used to be the only bulk
    # mutation in the group invisible in the Audit log — its ``events`` and
    # ``variables`` siblings both file one. The gate is the same
    # ``get_editor_user`` either way.
    #
    # A comment and not a docstring: FastAPI publishes a route docstring as the
    # operation ``description``, and this operation has none in the checked-in
    # ``backend/openapi.json``.
    await metric_definition_service.bulk_update_metric_definitions(session, slug, data)
    await audit_service.record(
        session,
        user=current_user,
        action="metric_definition.bulk_update",
        target_type="metric_definition",
        # No single target: one row stands for the whole selection, which is why
        # the ids live in the payload.
        target_id=None,
        project_slug=slug,
        payload=_bulk_update_audit_payload(list(dict.fromkeys(data.metric_ids)), data),
    )


@router.post("/preview", response_model=MetricPreviewResponse)
async def preview_metric_sql(
    session: SessionDep,
    slug: str,
    data: MetricPreviewRequest,
    current_user: EditorUserDep,
) -> MetricPreviewResponse:
    """Stateless dry-run of a sql-kind metric SELECT (editor-gated).

    Validates the SQL with the same safety gate the worker uses and executes it
    against the data source over the last 50 buckets of the requested interval
    (hard-capped at 200 rows); nothing is persisted. Expected user mistakes —
    bad SQL, missing time/value columns, warehouse errors — return 200 with
    ``error`` set so the editor can render them inline. A data source this
    project may not use is a 404, whether the id is unknown or belongs to
    another project — the same status and sentence the fact-table doors answer.
    """
    # Audited: see the note on ``fact_tables.preview_fact_table``. This and the
    # fact-operand preview below are the metrics catalog's two doors where an
    # editor's SQL reaches a warehouse credential without leaving a stored
    # object behind, so they are the two that needed a trail.
    await audit_service.record(
        session,
        user=current_user,
        action="metric.preview",
        target_type="metric_definition",
        target_id=None,
        project_slug=slug,
        payload={"data_source_id": str(data.data_source_id), "sql": data.sql},
    )
    return await metric_preview_service.preview_sql_metric(session, slug, data)


@router.post("/fact-preview", response_model=FactOperandPreviewResponse)
async def preview_fact_operand(
    session: SessionDep,
    slug: str,
    data: FactOperand,
    current_user: EditorUserDep,
) -> FactOperandPreviewResponse:
    """Stateless dry-run of ONE fact operand's row filter (editor-gated).

    The body is the operand a save would send. Its filters are compiled by the
    worker's own resolver for the fact table's data-source dialect and the
    resulting query is executed with a 1-row cap over a bounded recent window;
    nothing is persisted. Expected user mistakes — an unknown named filter, SQL
    the warehouse rejects, a measure column the filtered query does not project —
    return 200 with ``error`` set so the filter editor can render them inline.
    An unknown project or fact table is a 404.
    """
    # The SQL here is a FRAGMENT the server compiles against a SAVED fact
    # table's query, not free-text the caller hands to the warehouse whole — but
    # it still selects rows under that table's credential, and it still stores
    # nothing, so it gets the same trail for the same reason.
    await audit_service.record(
        session,
        user=current_user,
        action="metric.fact_preview",
        target_type="fact_table",
        target_id=data.fact_table_id,
        project_slug=slug,
        payload=data.model_dump(mode="json"),
    )
    return await metric_preview_service.preview_fact_operand(session, slug, data)


@router.post("/series-preview", response_model=MetricPreviewResponse)
async def preview_metric_series(
    session: SessionDep,
    slug: str,
    data: MetricSeriesPreviewRequest,
    current_user: EditorUserDep,
) -> MetricPreviewResponse:
    """Stateless dry-run of a fact or event-composition metric's series (editor-gated).

    The body is the definition a save would send. A fact metric is aggregated by
    the collector's own code over its last closed buckets (up to 50, capped at
    a month of wall clock); an event
    composition is composed from already-collected event counts on its newest
    scan grid. Nothing is persisted. Expected mistakes and warehouse errors
    return 200 with ``error`` set; an unknown fact table or data source is a
    404, an event outside the project a 422.
    """
    # Audited with the other previews: a fact draft's filters run under a
    # warehouse credential and leave no stored object behind.
    await audit_service.record(
        session,
        user=current_user,
        action="metric.series_preview",
        target_type="metric_definition",
        target_id=None,
        project_slug=slug,
        payload=data.model_dump(mode="json"),
    )
    return await metric_preview_service.preview_metric_series(session, slug, data)


@router.patch(
    "/reorder",
    response_model=list[MetricDefinitionResponse],
    dependencies=_editor_required,
)
async def reorder_metric_definitions(
    session: SessionDep,
    slug: str,
    data: MetricDefinitionReorder,
) -> list[MetricDefinition]:
    return await metric_definition_service.reorder_metric_definitions(session, slug, data)


@router.get("/{metric_id}", response_model=MetricDefinitionDetailResponse)
async def get_metric_definition(
    session: SessionDep,
    slug: str,
    metric_id: uuid.UUID,
) -> MetricDefinitionDetailResponse:
    return await metric_definition_service.get_metric_definition_enriched(session, slug, metric_id)


@router.get("/{metric_id}/generated-sql", response_model=MetricGeneratedSqlResponse)
async def get_metric_generated_sql(
    session: SessionDep,
    slug: str,
    metric_id: uuid.UUID,
) -> MetricGeneratedSqlResponse:
    """Return a saved fact metric's primary dependency-batch SQL without running it.

    Same gate as ``GET /{metric_id}``: anyone who can read the metric. The SQL is
    compiled from config that read already returns and nothing executes.
    """
    return await metric_preview_service.get_saved_fact_metric_sql(
        session,
        slug,
        metric_id,
    )


# Series reads live under the two-segment ``/{metric_id}/...`` templates, which
# never overlap the single-segment ``/{metric_id}`` CRUD route or the static
# ``/reorder`` / ``/bulk-update`` routes.
@router.get("/{metric_id}/series", response_model=MetricSeriesResponse)
async def get_metric_series(
    session: SessionDep,
    slug: str,
    metric_id: uuid.UUID,
    time_from: TimeFrom = None,
    time_to: TimeTo = None,
) -> MetricSeriesResponse:
    return await metric_series_service.get_metric_series(
        session,
        slug,
        metric_id,
        time_from=time_from,
        time_to=time_to,
    )


@router.get("/{metric_id}/breakdowns", response_model=MetricBreakdownsResponse)
async def get_metric_breakdowns(
    session: SessionDep,
    slug: str,
    metric_id: uuid.UUID,
    column: FreeTextFilter | None = None,
    time_from: TimeFrom = None,
    time_to: TimeTo = None,
) -> MetricBreakdownsResponse:
    return await metric_series_service.get_metric_breakdowns(
        session,
        slug,
        metric_id,
        column=column,
        time_from=time_from,
        time_to=time_to,
    )


@router.get("/{metric_id}/versions", response_model=MetricVersionSeriesResponse)
async def get_metric_version_series(
    session: SessionDep,
    slug: str,
    metric_id: uuid.UUID,
    time_from: TimeFrom = None,
    time_to: TimeTo = None,
) -> MetricVersionSeriesResponse:
    return await metric_series_service.get_metric_version_series(
        session,
        slug,
        metric_id,
        time_from=time_from,
        time_to=time_to,
    )


@router.post(
    "/{metric_id}/collect",
    response_model=MetricCollectNowResponse,
    status_code=202,
)
async def collect_metric_now(
    session: SessionDep,
    slug: str,
    metric_id: uuid.UUID,
    current_user: EditorUserDep,
) -> MetricCollectNowResponse:
    """Trigger an immediate backfill collection from one metric (editor-gated).

    SQL/event-composition metrics dispatch alone. A fact metric refreshes every
    fact table reachable through its operands and recalculates all active fact
    metrics using those tables in shared batches (the clicked metric is included
    even when draft). Returns 202 once queued; warehouse queries run in workers.
    Unknown metric -> 404.
    """
    result = await metric_definition_service.trigger_metric_collection(session, slug, metric_id)
    # Re-read for the NAME alone. The 202 body carries ids and a window, and this
    # row was filed with an empty ``target_name`` — so the Audit log listed a
    # collect against a bare UUID, while every other row in this router names its
    # metric. After the trigger, so the 404 path pays nothing.
    collected = await metric_definition_service.get_metric_definition(session, slug, metric_id)
    await audit_service.record(
        session,
        user=current_user,
        action="metric_definition.collect",
        target_type="metric_definition",
        target_id=metric_id,
        target_name=collected.name,
        project_slug=slug,
        payload={
            "window_from": result.window_from.isoformat() if result.window_from else None,
            "window_to": result.window_to.isoformat() if result.window_to else None,
        },
    )
    return result


@router.patch("/{metric_id}", response_model=MetricDefinitionResponse)
async def update_metric_definition(
    session: SessionDep,
    slug: str,
    metric_id: uuid.UUID,
    data: MetricDefinitionUpdate,
    current_user: EditorUserDep,
) -> MetricDefinition:
    metric = await metric_definition_service.update_metric_definition(
        session, slug, metric_id, data
    )
    await audit_service.record(
        session,
        user=current_user,
        action="metric_definition.update",
        target_type="metric_definition",
        target_id=metric.id,
        target_name=metric.name,
        project_slug=slug,
        payload=data.model_dump(exclude_unset=True),
    )
    return metric


@router.patch(
    "/{metric_id}/move",
    response_model=MetricDefinitionResponse,
    dependencies=_editor_required,
)
async def move_metric_definition(
    session: SessionDep,
    slug: str,
    metric_id: uuid.UUID,
    data: MetricDefinitionMove,
) -> MetricDefinition:
    return await metric_definition_service.move_metric_definition(session, slug, metric_id, data)


@router.delete("/{metric_id}", status_code=204)
async def delete_metric_definition(
    session: SessionDep,
    slug: str,
    metric_id: uuid.UUID,
    current_user: EditorUserDep,
) -> None:
    existing = await metric_definition_service.get_metric_definition(session, slug, metric_id)
    name = existing.name
    await metric_definition_service.delete_metric_definition(session, slug, metric_id)
    await audit_service.record(
        session,
        user=current_user,
        action="metric_definition.delete",
        target_type="metric_definition",
        target_id=metric_id,
        target_name=name,
        project_slug=slug,
    )
