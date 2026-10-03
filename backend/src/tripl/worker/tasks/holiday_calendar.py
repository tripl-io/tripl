"""Nightly refresh of every project's holiday calendar (F18, #271).

A calendar covers last year, this year and next year, so the run on the first
night of a new year adds the year after it. A project whose country was cleared
loses its leftover holiday rows here too.
"""

from __future__ import annotations

import logging

from tripl.services.holiday_calendar import projects_with_a_calendar, sync_project_holidays
from tripl.worker.celery_app import celery_app
from tripl.worker.db import _get_sync_session

logger = logging.getLogger(__name__)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="tripl.worker.tasks.holiday_calendar.sync_holiday_calendars",
)
def sync_holiday_calendars() -> dict[str, int]:
    changed = 0
    with _get_sync_session() as session:
        project_ids = projects_with_a_calendar(session)
        for project_id in project_ids:
            try:
                if sync_project_holidays(session, project_id):
                    changed += 1
                session.commit()
            except Exception:
                session.rollback()
                logger.exception("could not sync the holiday calendar of project %s", project_id)
    return {"projects": len(project_ids), "changed": changed}
