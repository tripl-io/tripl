"""Refresh the demo warehouse (beat, every five minutes).

Writes the rows of the demo warehouse model up to the end of the current
five-minute slice, backfills after downtime, and keeps the database inside its
retention and size bounds (:mod:`tripl.core.demo_warehouse.writer`). A no-op
when the warehouse is not provisioned or ``DEMO_WAREHOUSE_MODE=memory``.
"""

from __future__ import annotations

from tripl.core.demo_warehouse import writer
from tripl.worker.celery_app import celery_app

TASK_NAME = "tripl.worker.tasks.demo_warehouse.write_demo_warehouse"


@celery_app.task(  # type: ignore[untyped-decorator]
    name=TASK_NAME,
    # A first run backfills the retention window (~28k rows a day, ~850k for the
    # default 30 days); every later run writes five minutes of rows.
    soft_time_limit=15 * 60,
    time_limit=20 * 60,
)
def write_demo_warehouse() -> dict[str, object]:
    run = writer.write()
    if run is None:
        return {"skipped": True}
    return {
        "rows_written": run.rows_written,
        "written_through": run.written_through.isoformat(),
        "dropped_days": run.dropped_days,
    }
