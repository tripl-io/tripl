"""Which alembic revision this instance's database is actually stamped with.

The deployment is safe by construction — compose runs a ``migrate`` one-shot and
the app waits on ``service_completed_successfully`` — but that is an INFERENCE
drawn from the compose file, not an observation of the database. A
constraint-only migration changes nothing a probe can see, so a hand-rolled
deploy that skipped ``alembic upgrade head`` looked exactly like a correct one.

Every read here degrades to an honest unknown instead of raising, in the spirit
of ``apply_startup_service_overrides``: this backs the owner-only System panel,
and the settings page must never 500 because a revision could not be read.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

import tripl

logger = logging.getLogger(__name__)

#: ``backend/alembic`` — src/tripl/__init__.py -> src/tripl -> src -> backend.
#: Derived package-relatively, the same way ``tests/relevance/conftest.py`` does
#: it, and deliberately NOT from alembic.ini's ``script_location = alembic``:
#: that path resolves against the process's cwd, which only happens to be /app
#: under the container's entry command. The runtime image carries the directory
#: (backend/Dockerfile copies ``alembic/`` into the runtime stage).
_ALEMBIC_DIR = Path(tripl.__file__).resolve().parents[2] / "alembic"


@dataclass(frozen=True)
class MigrationStatus:
    """What the database is at, what this build ships, and whether they agree."""

    #: Stamped in ``alembic_version``. ``None`` is an honest unknown: no row, no
    #: table, or the database could not be reached.
    applied_revision: str | None
    #: The single head of the migration graph this build carries.
    head_revision: str | None
    #: ``None`` whenever either side above is unknown — never guessed.
    up_to_date: bool | None


UNKNOWN = MigrationStatus(None, None, None)


def _status(applied: str | None, head: str | None) -> MigrationStatus:
    up_to_date = None if applied is None or head is None else applied == head
    return MigrationStatus(applied, head, up_to_date)


@functools.cache
def head_revision() -> str | None:
    """The migration head this BUILD ships, read once per process.

    Cached because it is a property of the build and not of the instance:
    walking every file under ``alembic/versions`` on each settings request would
    be pure waste. The cost of that choice is local — a dev server with
    ``--reload`` that gains a new migration keeps reporting the old head until
    the process restarts.

    Lazy rather than computed at import: Celery workers import this package and
    never ask the question, and an import-time raise is precisely the failure
    mode ``apply_startup_service_overrides`` exists to avoid.

    Built by constructing :class:`ScriptDirectory` directly rather than through
    ``Config(alembic.ini)``, so the serving process neither depends on its cwd
    nor parses the ini (whose ``fileConfig`` would reconfigure logging).
    """
    try:
        heads = ScriptDirectory(str(_ALEMBIC_DIR)).get_heads()
    except Exception:  # noqa: BLE001
        logger.warning(
            "Could not read the alembic script directory at %s", _ALEMBIC_DIR, exc_info=True
        )
        return None
    if len(heads) != 1:
        # test_alembic_revision_graph_has_single_head pins one head, so this is a
        # build carrying an unmerged branch. Reporting either head would be a guess.
        logger.warning("Expected exactly one alembic head, found %d", len(heads))
        return None
    return heads[0]


def _read_applied(sync_session: Session) -> str | None:
    connection = sync_session.connection()
    # Probe the catalog rather than blind-SELECTing a table that may not exist.
    # On PostgreSQL a failed statement aborts the request-scoped transaction, so
    # a missing table would poison whatever this session touched next; and in the
    # test suite the table never exists at all, because that schema is built by
    # ``Base.metadata.create_all`` and never runs the migration chain.
    if not sa.inspect(connection).has_table("alembic_version"):
        return None
    rows = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all()
    if len(rows) != 1:
        # 0 = never stamped; >1 = several heads stamped, which cannot honestly be
        # summarised as one revision.
        logger.warning("alembic_version holds %d rows; expected exactly one", len(rows))
        return None
    return str(rows[0])


async def get_migration_status(session: AsyncSession) -> MigrationStatus:
    """Applied revision vs. the head this build ships. Never raises.

    The applied revision is read live on purpose while the head is cached: an
    operator can run the ``migrate`` one-shot against a running instance, and
    watching that land is the whole point of reporting this.
    """
    try:
        applied = await session.run_sync(_read_applied)
    except Exception:  # noqa: BLE001
        logger.warning("Could not read the applied alembic revision", exc_info=True)
        applied = None
    return _status(applied, await asyncio.to_thread(head_revision))
