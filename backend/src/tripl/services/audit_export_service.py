"""Exporting an organization's audit log as CSV or NDJSON (F20, GH #273).

``GET /orgs/{org}/audit/export`` streams every row of one organization in a
time range, oldest first, without loading them all: the rows are read in
keyset pages of :data:`CHUNK_SIZE` ordered by ``(created_at, id)`` (the
``ix_audit_log_organization_created`` index), each page formatted and handed
to the response before the next is read.

Which rows: those filed in the organization (``organization_id``) and those of
its projects, never a platform-scope row (``organization_id`` NULL) and never
another organization's.

CSV cells are written ``QUOTE_ALL`` and any cell starting with ``=``, ``+``,
``-``, ``@``, a tab or a carriage return gets a leading apostrophe (the OWASP
rule for CSV injection), so a spreadsheet opens the file as text. The payload
is a JSON string in CSV and an object in NDJSON.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from tripl.models.audit_log import AuditLog
from tripl.models.project import Project
from tripl.services.audit_rows import COLUMNS, row_record

ExportFormat = Literal["csv", "json"]

CHUNK_SIZE: Final = 1000
#: The widest range one export may cover.
MAX_RANGE: Final = timedelta(days=366)
#: The range when the caller names none: the last 30 days.
DEFAULT_RANGE: Final = timedelta(days=30)

MEDIA_TYPES: Final[dict[str, str]] = {
    "csv": "text/csv; charset=utf-8",
    "json": "application/x-ndjson",
}
_EXTENSIONS: Final[dict[str, str]] = {"csv": "csv", "json": "ndjson"}
# OWASP CSV injection: a cell a spreadsheet would read as a formula.
_FORMULA_PREFIXES: Final = ("=", "+", "-", "@", "\t", "\r")


class ExportRangeError(ValueError):
    """The requested range is empty, reversed or wider than :data:`MAX_RANGE`."""


@dataclass(frozen=True)
class ExportRange:
    start: datetime
    end: datetime


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def resolve_range(start: datetime | None, end: datetime | None, *, now: datetime) -> ExportRange:
    """``[start, end)`` in UTC; ``end`` defaults to now, ``start`` to 30 days before it.

    ``end`` is EXCLUSIVE: ``to=2026-09-30`` (midnight) leaves out September
    30th itself; a caller exporting whole days up to and including a last day
    sends the day after it (the settings page does). A naive datetime is read
    as UTC. Raises :class:`ExportRangeError`.
    """
    end_utc = _utc(end) if end is not None else _utc(now)
    start_utc = _utc(start) if start is not None else end_utc - DEFAULT_RANGE
    if start_utc >= end_utc:
        raise ExportRangeError("'from' must be before 'to'")
    if end_utc - start_utc > MAX_RANGE:
        raise ExportRangeError("The range may cover at most 366 days")
    return ExportRange(start=start_utc, end=end_utc)


def filename(org_slug: str, span: ExportRange, fmt: ExportFormat) -> str:
    """``audit-<org>-<from>-<to>.<ext>``, dates in UTC."""
    return (
        f"audit-{org_slug}-{span.start.strftime('%Y%m%d')}-{span.end.strftime('%Y%m%d')}"
        f".{_EXTENSIONS[fmt]}"
    )


def org_scope(org_id: uuid.UUID) -> ColumnElement[bool]:
    """The organization's rows and its projects' rows; never a platform-scope row."""
    return and_(
        AuditLog.organization_id.is_not(None),
        or_(
            AuditLog.organization_id == org_id,
            AuditLog.project_id.in_(select(Project.id).where(Project.organization_id == org_id)),
        ),
    )


async def iter_chunks(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    span: ExportRange,
    action: str | None = None,
    chunk_size: int = CHUNK_SIZE,
) -> AsyncIterator[Sequence[AuditLog]]:
    """The rows in keyset pages of ``chunk_size``, oldest first. One query per page.

    Keyset rather than OFFSET: page N costs the same as page 1, and a row
    written while the export runs cannot shift a page boundary. ``(created_at,
    id)`` is a total order (rows of one transaction share ``created_at``).
    Each page is its own short transaction, committed before it is yielded,
    so no connection is held while the consumer is busy.
    """
    base = select(AuditLog).where(
        org_scope(org_id),
        AuditLog.created_at >= span.start,
        AuditLog.created_at < span.end,
    )
    if action:
        base = base.where(AuditLog.action == action)
    after: tuple[datetime, uuid.UUID] | None = None
    while True:
        statement = base
        if after is not None:
            created, row_id = after
            statement = statement.where(
                or_(
                    AuditLog.created_at > created,
                    and_(AuditLog.created_at == created, AuditLog.id > row_id),
                )
            )
        rows = (
            (
                await session.execute(
                    statement.order_by(AuditLog.created_at, AuditLog.id).limit(chunk_size)
                )
            )
            .scalars()
            .all()
        )
        # Detach what was read: a long export must not grow the identity map.
        for row in rows:
            session.expunge(row)
        # End the page's transaction BEFORE handing the page on: the
        # connection goes back to the pool while a slow client downloads it,
        # instead of sitting "idle in transaction" for as long as the client
        # takes (and holding back vacuum). Keyset paging needs no snapshot
        # across pages. Nothing is pending here: the route's own writes (the
        # ``org.audit_export`` row) are committed before the stream starts.
        await session.commit()
        if not rows:
            return
        yield rows
        if len(rows) < chunk_size:
            return
        last = rows[-1]
        after = (last.created_at, last.id)


def escape_cell(value: str) -> str:
    """A leading apostrophe for a cell a spreadsheet would evaluate (OWASP)."""
    return f"'{value}" if value.startswith(_FORMULA_PREFIXES) else value


def _csv_cells(record: dict[str, Any]) -> list[str]:
    cells: list[str] = []
    for column in COLUMNS:
        value = record[column]
        if column == "payload":
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        else:
            text = "" if value is None else str(value)
        cells.append(escape_cell(text))
    return cells


def csv_header() -> str:
    buffer = io.StringIO()
    csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\r\n").writerow(COLUMNS)
    return buffer.getvalue()


def csv_lines(rows: Sequence[AuditLog], org_slug: str) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    for row in rows:
        writer.writerow(_csv_cells(row_record(row, org_slug)))
    return buffer.getvalue()


def ndjson_lines(rows: Sequence[AuditLog], org_slug: str) -> str:
    return "".join(
        json.dumps(row_record(row, org_slug), ensure_ascii=False, default=str) + "\n"
        for row in rows
    )


async def stream(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    org_slug: str,
    span: ExportRange,
    fmt: ExportFormat,
    action: str | None = None,
) -> AsyncIterator[bytes]:
    """The whole file, one page of rows per chunk (the CSV header first)."""
    if fmt == "csv":
        yield csv_header().encode("utf-8")
    async for rows in iter_chunks(session, org_id=org_id, span=span, action=action):
        text = csv_lines(rows, org_slug) if fmt == "csv" else ndjson_lines(rows, org_slug)
        yield text.encode("utf-8")
