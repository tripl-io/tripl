import uuid

from fastapi import APIRouter, HTTPException, Query
from pydantic import ValidationError

from tripl.api.deps import EditorUserDep, SessionDep
from tripl.models.fact_table import FactTable
from tripl.schemas.fact_table import (
    FactTableColumnSchema,
    FactTableCreate,
    FactTableListResponse,
    FactTablePreviewRequest,
    FactTablePreviewResponse,
    FactTableResponse,
    FactTableUpdate,
)
from tripl.schemas.pagination import Offset
from tripl.schemas.text_filters import FreeTextFilter
from tripl.services import audit_service, fact_table_service
from tripl.services.project_lookup import resolve_project_id

router = APIRouter(prefix="/projects/{slug}/fact-tables", tags=["fact-tables"])


@router.get("", response_model=FactTableListResponse)
async def list_fact_tables(
    session: SessionDep,
    slug: str,
    # FreeTextFilter: binds into an ILIKE, so a NUL aborts inside asyncpg
    # before SQL runs.
    search: FreeTextFilter | None = None,
    offset: Offset = 0,
    limit: int = Query(200, ge=1, le=1000),
) -> FactTableListResponse:
    items, total = await fact_table_service.list_fact_table_items(
        session,
        slug,
        search=search,
        offset=offset,
        limit=limit,
    )
    return FactTableListResponse(items=items, total=total)


@router.post("", response_model=FactTableResponse, status_code=201)
async def create_fact_table(
    session: SessionDep,
    slug: str,
    data: FactTableCreate,
    current_user: EditorUserDep,
) -> FactTable:
    fact_table = await fact_table_service.create_fact_table(session, slug, data)
    await audit_service.record(
        session,
        user=current_user,
        action="fact_table.create",
        target_type="fact_table",
        target_id=fact_table.id,
        target_name=fact_table.name,
        project_slug=slug,
        payload=data.model_dump(),
    )
    return fact_table


# Declared before the parametric ``/{fact_table_id}`` routes so the static
# ``/preview`` path is never shadowed.
@router.post("/preview", response_model=FactTablePreviewResponse)
async def preview_fact_table(
    session: SessionDep,
    slug: str,
    payload: FactTablePreviewRequest,
    current_user: EditorUserDep,
) -> FactTablePreviewResponse:
    """Read the column shape of a candidate fact-table SELECT (editor-gated).

    Reads the query's columns and identifier candidates; no rows are returned
    and nothing is persisted. The run is recorded in the audit log.
    """
    # Imported lazily: the introspection service is a sibling slice and may land
    # after this router. A function-local import keeps the app importable even
    # before that module exists on disk.
    from tripl.services.fact_table_introspection_service import (
        DataSourceNotAvailableError,
        FactTableIntrospectionError,
        introspect_fact_table,
    )

    project_id = await resolve_project_id(session, slug)
    # Audited, unlike most read-shaped routes, because this one and the metrics
    # catalog's previews are the only places an editor's own SQL reaches a
    # warehouse credential WITHOUT leaving a stored object behind. Saving a fact
    # table or a metric writes a row an owner can read back; a preview wrote
    # nothing at all, so the one capability it uniquely held over the saved
    # paths was being invisible afterwards. website/docs/run/security.md states
    # the boundary this sits on: an editor may run read-only SQL against their
    # own projects' data sources, and every such run is attributable.
    #
    # The row deliberately carries the SQL. It is the authored artefact — the
    # same text ``fact_table.create`` already records — and a trail saying only
    # that "someone previewed something" answers none of the questions an owner
    # would ask it.
    await audit_service.record(
        session,
        user=current_user,
        action="fact_table.preview",
        target_type="fact_table",
        target_id=None,
        project_slug=slug,
        payload={"data_source_id": str(payload.data_source_id), "sql": payload.sql},
    )
    try:
        result = await introspect_fact_table(
            session,
            project_id=project_id,
            data_source_id=payload.data_source_id,
            sql=payload.sql,
            timestamp_column=payload.timestamp_column,
        )
    except DataSourceNotAvailableError as exc:
        # 404, the same status and sentence the fact-table SAVE door and both
        # ``sql``-metric doors answer with for this exact cause. Caught before
        # its base class, which is the 400 case.
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FactTableIntrospectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Introspection hands back the warehouse's own strings, and ``name`` is bounded
    # at 255 characters by the response model. An over-long name is a
    # ``pydantic.ValidationError`` raised HERE, inside the handler — neither a
    # ``FactTableIntrospectionError`` nor an ``HTTPException`` nor a
    # ``RequestValidationError`` — so without this it reaches ``main``'s catch-all
    # and the user gets a blank 500 for a query they can actually fix. It is not
    # truncated the way ``native_type`` is (see NATIVE_TYPE_MAX_LEN): a column name
    # is a dict KEY that the saved column allowlist is later built from, and a
    # truncated key silently stops matching the warehouse's own column.
    try:
        columns = [
            FactTableColumnSchema(
                name=column.name,
                type=column.type,
                native_type=column.native_type,
            )
            for column in result.columns
        ]
    except ValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                "This query projects a column tripl cannot describe; alias it to a shorter name."
            ),
        ) from exc
    return FactTablePreviewResponse(
        columns=columns,
        identifier_candidates=list(result.identifier_candidates),
    )


@router.get("/{fact_table_id}", response_model=FactTableResponse)
async def get_fact_table(
    session: SessionDep,
    slug: str,
    fact_table_id: uuid.UUID,
) -> FactTable:
    return await fact_table_service.get_fact_table(session, slug, fact_table_id)


@router.patch("/{fact_table_id}", response_model=FactTableResponse)
async def update_fact_table(
    session: SessionDep,
    slug: str,
    fact_table_id: uuid.UUID,
    data: FactTableUpdate,
    current_user: EditorUserDep,
) -> FactTable:
    fact_table = await fact_table_service.update_fact_table(session, slug, fact_table_id, data)
    await audit_service.record(
        session,
        user=current_user,
        action="fact_table.update",
        target_type="fact_table",
        target_id=fact_table.id,
        target_name=fact_table.name,
        project_slug=slug,
        payload=data.model_dump(exclude_unset=True),
    )
    return fact_table


@router.delete("/{fact_table_id}", status_code=204)
async def delete_fact_table(
    session: SessionDep,
    slug: str,
    fact_table_id: uuid.UUID,
    current_user: EditorUserDep,
) -> None:
    existing = await fact_table_service.get_fact_table(session, slug, fact_table_id)
    name = existing.name
    await fact_table_service.delete_fact_table(session, slug, fact_table_id)
    await audit_service.record(
        session,
        user=current_user,
        action="fact_table.delete",
        target_type="fact_table",
        target_id=fact_table_id,
        target_name=name,
        project_slug=slug,
    )
