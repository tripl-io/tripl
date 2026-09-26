"""API for chart annotations (deploy/release markers on metric charts)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from tripl.api.deps import EditorUserDep, SessionDep
from tripl.models.domain_enums import ChartAnnotationScopeType
from tripl.schemas.chart_annotation import ChartAnnotationCreate, ChartAnnotationResponse
from tripl.schemas.text_filters import FreeTextFilter
from tripl.services import audit_service, chart_annotation_service

router = APIRouter(
    prefix="/projects/{slug}/annotations",
    tags=["chart-annotations"],
)

OptionalDateTimeQuery = Annotated[datetime | None, Query()]


@router.get("", response_model=list[ChartAnnotationResponse])
async def list_chart_annotations(
    session: SessionDep,
    slug: str,
    # ChartAnnotationScopeType (not str): the filter binds against the native
    # ``chart_annotation_scope_type`` column, so an out-of-enum value reached the
    # driver as a 500. Note this is the annotation scope enum, NOT MetricScopeType
    # — annotations only ever hang off project_total/event_type/event/metric,
    # which is exactly what the POST body already validates (tripl-57g0). Moved
    # into Annotated[] because ruff's B008 only exempts call-defaults whose
    # annotation it can prove immutable, and an enum class isn't on that list.
    scope_type: Annotated[ChartAnnotationScopeType | None, Query()] = None,
    # Into Annotated[] for the same reason scope_type above is: ruff's B008
    # exempts a call-default only when it can prove the annotation immutable,
    # and FreeTextFilter is not on that list.
    scope_ref: Annotated[FreeTextFilter | None, Query()] = None,
    time_from: OptionalDateTimeQuery = None,
    time_to: OptionalDateTimeQuery = None,
) -> list[ChartAnnotationResponse]:
    rows = await chart_annotation_service.list_annotations(
        session,
        slug,
        scope_type=scope_type,
        scope_ref=scope_ref,
        time_from=time_from,
        time_to=time_to,
    )
    return [ChartAnnotationResponse.model_validate(row) for row in rows]


@router.post(
    "",
    response_model=ChartAnnotationResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_200_OK: {
            "model": ChartAnnotationResponse,
            "description": (
                "A source=api annotation with the same label was already created in "
                "this project within the last 24 hours; it is returned instead of a "
                "duplicate. Manual annotations are never de-duplicated."
            ),
        }
    },
)
async def create_chart_annotation(
    session: SessionDep,
    slug: str,
    data: ChartAnnotationCreate,
    current_user: EditorUserDep,
    response: Response,
) -> ChartAnnotationResponse:
    # EditorUserDep admits a write-scoped API key (``tk_w_``) bound to this
    # project or to none, so CI can post deploy markers with ``source=api``.
    annotation, created = await chart_annotation_service.create_annotation(
        session,
        slug,
        bucket=data.bucket,
        label=data.label,
        description=data.description,
        color=data.color,
        scope_type=data.scope_type,
        scope_ref=data.scope_ref,
        user_id=current_user.id,
        source=data.source,
        url=data.url,
    )
    if not created:
        # A retried CI step or a double click: nothing was written, so there is
        # nothing to audit either.
        response.status_code = status.HTTP_200_OK
        return ChartAnnotationResponse.model_validate(annotation)
    await audit_service.record(
        session,
        user=current_user,
        action="chart_annotation.create",
        target_type="chart_annotation",
        target_id=annotation.id,
        target_name=annotation.label,
        project_slug=slug,
        payload={"source": annotation.source, "url": annotation.url},
    )
    return ChartAnnotationResponse.model_validate(annotation)


@router.delete("/{annotation_id}", status_code=204)
async def delete_chart_annotation(
    session: SessionDep,
    slug: str,
    annotation_id: uuid.UUID,
    current_user: EditorUserDep,
) -> None:
    await chart_annotation_service.delete_annotation(session, slug, annotation_id)
    await audit_service.record(
        session,
        user=current_user,
        action="chart_annotation.delete",
        target_type="chart_annotation",
        target_id=annotation_id,
        project_slug=slug,
    )
