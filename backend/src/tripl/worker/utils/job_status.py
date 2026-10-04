"""Who is allowed to close a ``ScanJob``, and how a running worker finds out.

Lives in ``worker.utils`` rather than in ``worker.tasks.metrics._helpers``
because both the scan tasks and ``collect_metrics`` need it, and importing a
metrics task module from ``worker.tasks.scan`` would drag the whole
``collect_metrics`` task graph into that module's import path — the same
reasoning that put ``event_types`` and ``reserved_columns`` in this package.

``worker.tasks.metrics._helpers`` still *declares* a ``TERMINAL_SCAN_JOB_STATUSES``
of its own because that name sits in its published ``__all__`` and removing it is
a wider change than this one. It has no production consumer left — the task
module imports this copy — and ``test_batch3_a3`` pins the two equal so the
leftover cannot drift.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from tripl.models.scan_job import ScanJob, ScanJobStatus

# Statuses from which a job never runs again, and which a finishing run must
# never overwrite. ``running`` is deliberately NOT here: with ``task_acks_late``
# a redelivered message legitimately re-enters its OWN running job. Terminal
# means somebody else already closed the row — the user cancelled it, the stale
# reaper stamped it failed, or its ``completed`` ack was lost — and re-running it
# would redo the work and, worse, overwrite that verdict.
TERMINAL_SCAN_JOB_STATUSES = (
    ScanJobStatus.completed.value,
    ScanJobStatus.failed.value,
    ScanJobStatus.cancelled.value,
)


def read_job_status(session: Session, job_id: uuid.UUID) -> str | None:
    """The row's status THROUGH the database, never the in-session attribute.

    The worker sessionmaker is ``expire_on_commit=False`` (``worker.db``), so a
    task's in-memory ``job.status`` is frozen at whatever that task itself last
    wrote and can never observe a cancel or a reap that landed in another
    session. Reading the attribute instead of issuing this SELECT is the one way
    to ship a guard that looks right and is silently always false.
    """
    return session.execute(select(ScanJob.status).where(ScanJob.id == job_id)).scalar()


def closed_by_someone_else(session: Session, job_id: uuid.UUID) -> str | None:
    """The terminal status another writer stamped on the job, or ``None``.

    Call it immediately before a closing write. Deliberately reads only the
    status column and assigns nothing back: the closing writer owns ``status``,
    ``completed_at`` and ``error_message``, and a re-assignment here would flush
    this run's stale copies over theirs — the ``completed`` beside "Cancelled by
    user" bug.

    NOT a lock, and not sold as one. ``ScanJob`` declares no ``version_id_col``
    and no caller takes ``FOR UPDATE``, so a cancel landing between this SELECT
    and the caller's COMMIT still loses. What this closes is the seconds-wide
    window a long tail opens — a full main-branch reindex, a variable sweep, the
    last chunk of a replay — not the microseconds around the commit itself.
    Narrowing that last sliver needs a row lock or a version column on both
    writers, which is a schema change, and no test on the in-memory SQLite suite
    could tell you whether it worked.
    """
    current = read_job_status(session, job_id)
    if current is not None and current in TERMINAL_SCAN_JOB_STATUSES:
        return str(current)
    return None


def job_is_cancelled(session: Session, job_id: uuid.UUID) -> bool:
    """``True`` when the user has stopped this job since the task last wrote.

    Narrower than :func:`closed_by_someone_else` on purpose: a mid-run checkpoint
    asks "may I still throw this work away?", and only a cancel answers yes. The
    stale reaper cannot reach a live catalog run anyway — its threshold (75 min)
    exceeds Celery's hard ``task_time_limit`` (60 min), so the run is killed
    first.
    """
    return read_job_status(session, job_id) == ScanJobStatus.cancelled.value
