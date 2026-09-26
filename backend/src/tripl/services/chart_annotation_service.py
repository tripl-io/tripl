"""CRUD service for chart annotations (deploy/release markers)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.chart_annotation import ChartAnnotation
from tripl.models.domain_enums import ChartAnnotationSource
from tripl.services.project_service import get_project_id_by_slug


async def list_annotations(
    session: AsyncSession,
    slug: str,
    *,
    scope_type: str | None = None,
    scope_ref: str | None = None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
) -> list[ChartAnnotation]:
    """Return annotations visible for the requested chart scope.

    Project-wide markers (scope_type IS NULL) are always included; scoped
    markers are filtered to the given scope_type/ref pair when provided.
    """
    project_id = await get_project_id_by_slug(session, slug)

    conditions = [ChartAnnotation.project_id == project_id]
    if scope_type is not None and scope_ref is not None:
        conditions.append(
            or_(
                ChartAnnotation.scope_type.is_(None),
                and_(
                    ChartAnnotation.scope_type == scope_type,
                    ChartAnnotation.scope_ref == scope_ref,
                ),
            )
        )
    if time_from is not None:
        conditions.append(ChartAnnotation.bucket >= time_from)
    if time_to is not None:
        conditions.append(ChartAnnotation.bucket <= time_to)

    rows = await session.execute(
        select(ChartAnnotation).where(*conditions).order_by(ChartAnnotation.bucket.asc())
    )
    return list(rows.scalars().all())


# A CI job retried, or two deploy steps posting the same marker, must not stack
# identical labels on the chart: an ``api`` marker repeating its (project,
# source, label) inside this window returns the existing row. Release markers
# are unique for good instead (``uq_chart_annotation_release_label``): a version
# ships once. Manual markers are never de-duplicated — a person adding the same
# note twice meant to.
ANNOTATION_DEDUP_WINDOW = timedelta(hours=24)

_WINDOWED_DEDUP_SOURCES = frozenset({ChartAnnotationSource.api.value})
_PERMANENT_DEDUP_SOURCES = frozenset({ChartAnnotationSource.release.value})


def is_deduplicated_source(source: str) -> bool:
    """Whether a create with this source can return an existing row instead."""
    return source in _WINDOWED_DEDUP_SOURCES or source in _PERMANENT_DEDUP_SOURCES


async def find_duplicate_annotation(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    source: str,
    label: str,
    now: datetime | None = None,
) -> ChartAnnotation | None:
    """The existing annotation a create with this (source, label) would repeat.

    Any age for ``release``; for ``api`` only one created inside
    :data:`ANNOTATION_DEDUP_WINDOW`; never for ``manual``. The newest match wins
    when several exist.
    """
    if not is_deduplicated_source(source):
        return None
    conditions = [
        ChartAnnotation.project_id == project_id,
        ChartAnnotation.source == source,
        ChartAnnotation.label == label,
    ]
    if source in _WINDOWED_DEDUP_SOURCES:
        cutoff = (now or datetime.now(UTC)) - ANNOTATION_DEDUP_WINDOW
        conditions.append(ChartAnnotation.created_at >= cutoff)
    existing: ChartAnnotation | None = await session.scalar(
        select(ChartAnnotation)
        .where(*conditions)
        .order_by(ChartAnnotation.created_at.desc())
        .limit(1)
    )
    return existing


async def _lock_dedup_key(
    session: AsyncSession, project_id: uuid.UUID, *, source: str, label: str
) -> None:
    """Serialize concurrent creates of one (project, source, label) on PostgreSQL.

    Two CI retries racing each other would both miss the lookup and both
    insert; a transaction-scoped advisory lock on the key makes the second wait
    for the first to commit, then find its row. Released at commit/rollback. A
    no-op on SQLite, which serializes writers anyway.
    """
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"{project_id}{source}{label}"},
    )


async def create_annotation(
    session: AsyncSession,
    slug: str,
    *,
    bucket: datetime,
    label: str,
    description: str | None,
    color: str,
    scope_type: str | None,
    scope_ref: str | None,
    user_id: uuid.UUID | None,
    source: str = ChartAnnotationSource.manual.value,
    url: str | None = None,
) -> tuple[ChartAnnotation, bool]:
    """Create an annotation, or return the one it would duplicate.

    Returns ``(annotation, created)``; ``created`` is False when an existing row
    with the same (project, source, label) was returned instead (see
    :func:`find_duplicate_annotation`), so the route can answer 200 rather
    than 201 and skip the audit row for a write that did not happen. A manual
    create always writes.
    """
    project_id = await get_project_id_by_slug(session, slug)
    clean_label = label.strip()

    if is_deduplicated_source(source):
        await _lock_dedup_key(session, project_id, source=source, label=clean_label)
        existing = await find_duplicate_annotation(
            session, project_id, source=source, label=clean_label
        )
        if existing is not None:
            # Ends the transaction, so the advisory lock is released here too.
            await session.commit()
            return existing, False

    annotation = ChartAnnotation(
        project_id=project_id,
        scope_type=scope_type,
        scope_ref=scope_ref,
        bucket=bucket,
        label=clean_label,
        description=description.strip() if description else None,
        color=color,
        source=source,
        url=url,
        created_by_user_id=user_id,
    )
    session.add(annotation)
    await session.commit()
    await session.refresh(annotation)
    return annotation, True


async def delete_annotation(
    session: AsyncSession,
    slug: str,
    annotation_id: uuid.UUID,
) -> None:
    project_id = await get_project_id_by_slug(session, slug)
    annotation = await session.get(ChartAnnotation, annotation_id)
    if annotation is None or annotation.project_id != project_id:
        raise HTTPException(status_code=404, detail="Annotation not found")
    await session.delete(annotation)
    await session.commit()
