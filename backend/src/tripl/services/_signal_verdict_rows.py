"""Deleting signal verdict rows (F01, #254).

One helper for every path that takes a verdict row away — the verdict service
replacing or clearing a verdict, and the alert inbox dropping the rows an
incident move made stale — so each one also removes the ``Expected`` chart
annotation an ``expected`` row wrote. Kept free of service imports so the
alerting services can use it without an import cycle.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.chart_annotation import ChartAnnotation
from tripl.models.signal_triage import SignalTriage


async def delete_verdict_rows(
    session: AsyncSession, project_id: uuid.UUID, rows: Iterable[SignalTriage]
) -> int:
    """Delete ``rows`` and the chart annotation each one wrote. DOES NOT COMMIT."""
    deleted = 0
    for row in rows:
        if row.annotation_id is not None:
            await session.execute(
                delete(ChartAnnotation).where(
                    ChartAnnotation.id == row.annotation_id,
                    ChartAnnotation.project_id == project_id,
                )
            )
        await session.delete(row)
        deleted += 1
    return deleted
