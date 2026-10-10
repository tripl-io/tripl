"""Get-or-create for the one-row-per-project settings tables.

Branch merge policy, the issue tracker config and anomaly detection each keep at
most one row per project, under a unique constraint on ``project_id``. The row
is written the first time a project saves the settings. Only a write path may
call this: a read of a project without a row answers with the defaults instead,
because GETs must not mutate the database.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.base import Base


async def get_or_create_project_row[RowT: Base](
    session: AsyncSession, model: type[RowT], project_id: uuid.UUID
) -> RowT:
    """The project's ``model`` row, inserted and committed with defaults if missing.

    Two first writes can race to insert; the loser's commit trips the unique
    constraint, and the winner's row is the one to return, not a 500.
    """
    row = await session.scalar(select(model).filter_by(project_id=project_id))
    if row is not None:
        return row

    row = model(project_id=project_id)
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        winner = await session.scalar(select(model).filter_by(project_id=project_id))
        if winner is None:  # pragma: no cover — row vanished between commit and re-read
            raise
        return winner
    await session.refresh(row)
    return row
