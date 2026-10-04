"""PostgreSQL wiring for the search-relevance harness.

WHY A SECOND DATABASE AT ALL
----------------------------
The backend suite runs on in-memory SQLite (``tests/conftest.py``), and search
carries a Python fallback (``_search_query.sqlite_search``) for exactly that
reason. So the production ranking SQL — ``ts_rank_cd``, the trigram tiers, the
``tripl_search`` text-search configuration, ``merge_results`` — has never once
been executed by a test, and every weight in it is unfalsifiable. Nothing short
of a real PostgreSQL can execute it.

TWO RULES, BORROWED FROM THE CONFORMANCE GATES
----------------------------------------------
**A developer without Docker must not be blocked.** An unreachable server SKIPS,
so ``uv run pytest`` on a laptop stays green and the SQLite suite is unaffected.

**CI must not be allowed to skip.** ``TRIPL_RELEVANCE_REQUIRED=1`` (CI sets it)
turns "server unreachable" into a hard FAILURE, and the CI job additionally
asserts from the JUnit XML that tests actually ran and that none of them skipped
for real. A gate that silently no-ops reports green while testing nothing.

WHY ALEMBIC AND NOT ``Base.metadata.create_all``
------------------------------------------------
``create_all`` would give a schema, but it would NOT give the object under test.
``tripl_search`` is created by a migration, and it was created as
``COPY = simple`` — i.e. with no stemming — which was itself one of the faults the
harness is here to pin (see :mod:`tripl.tests.relevance.cases`, fault B). A
hand-rolled configuration in this file would have tested a copy of the bug, and
the migration that fixed it would not have flipped a single assertion here.
Running the real revision chain is what makes "the fix landed" and "the harness
went green" the same event — and that is not hypothetical any more: fault B was
fixed by ``a7c3e1b9d5f2``, a migration and nothing but a migration, so the four
cases it flipped are green here only because this fixture runs ``alembic upgrade
head``. It also gets ``pgvector`` and ``pg_trgm`` set up the way production has
them, which is why the CI service container is the pgvector image rather than
stock postgres.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import NoReturn

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

import tripl
from tripl.services import app_settings_service
from tripl.services.search_service import reindex_project_branch
from tripl.tests.relevance.corpus import Corpus, seed_corpus

# Connection settings. The defaults match the CI service container. The database
# name is deliberately one the dev compose stack does NOT create, so a stray
# local ``pytest`` connects to nothing and skips instead of dropping the schema
# of a database someone was using.
_PG_HOST = os.environ.get("TRIPL_RELEVANCE_PG_HOST", "localhost")
_PG_PORT = os.environ.get("TRIPL_RELEVANCE_PG_PORT", "5432")
_PG_DB = os.environ.get("TRIPL_RELEVANCE_PG_DB", "tripl_relevance")
_PG_USER = os.environ.get("TRIPL_RELEVANCE_PG_USER", "tripl")
_PG_PASSWORD = os.environ.get("TRIPL_RELEVANCE_PG_PASSWORD", "tripl")

_REQUIRED = os.environ.get("TRIPL_RELEVANCE_REQUIRED") == "1"

DATABASE_URL = f"postgresql+asyncpg://{_PG_USER}:{_PG_PASSWORD}@{_PG_HOST}:{_PG_PORT}/{_PG_DB}"

#: ``backend/alembic`` — src/tripl/__init__.py -> src/tripl -> src -> backend.
_ALEMBIC_DIR = Path(tripl.__file__).resolve().parents[2] / "alembic"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark everything in this package ``relevance``.

    Done here rather than per-module so a new test file cannot forget the marker
    and quietly start running (and failing, for want of a database) in the fast
    local suite.
    """
    for item in items:
        if "tests/relevance/" in item.nodeid or "tests\\relevance\\" in item.nodeid:
            item.add_marker(pytest.mark.relevance)


def unavailable(reason: str) -> NoReturn:
    """Skip locally, FAIL in CI. See the module docstring."""
    if _REQUIRED:
        pytest.fail(
            f"TRIPL_RELEVANCE_REQUIRED=1 but PostgreSQL is unreachable: {reason}. "
            "The relevance harness is the only thing that executes the production "
            "ranking SQL — a skip here means the ranking went untested. Fix the "
            "container, do not relax this."
        )
    pytest.skip(f"relevance harness needs PostgreSQL: {reason}")


def _alembic_upgrade_head(database_url: str) -> None:
    """Run ``alembic upgrade head`` against ``database_url``, in this thread.

    ``alembic/env.py`` takes its URL from ``tripl.config.settings.database_url``
    and overwrites whatever the config object carries, so the setting is the only
    way in; it is restored immediately afterwards. ``Config()`` is built without
    an ini file on purpose — reading ``alembic.ini`` would run ``fileConfig`` and
    reconfigure logging for the whole pytest session.

    env.py calls ``asyncio.run``, which cannot run inside the session event loop,
    which is why the caller dispatches this to a worker thread.
    """
    from alembic import command
    from alembic.config import Config

    from tripl.config import settings

    if not _ALEMBIC_DIR.is_dir():
        msg = f"alembic directory not found at {_ALEMBIC_DIR}"
        raise RuntimeError(msg)

    previous_url = settings.database_url
    settings.database_url = database_url
    try:
        config = Config()
        config.set_main_option("script_location", str(_ALEMBIC_DIR))
        command.upgrade(config, "head")
    finally:
        settings.database_url = previous_url


async def _reset_schema(engine: AsyncEngine) -> None:
    """Drop and recreate ``public`` so the revision chain starts from empty.

    Idempotent re-runs matter more than speed here: a half-migrated leftover from
    an interrupted run would otherwise make the next run's failure a mystery.
    """
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))


@pytest.fixture(scope="session")
async def relevance_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    # Any connect failure at all means "no server here", which is a skip locally
    # and a hard failure in CI — never a test error.
    except Exception as exc:
        await engine.dispose()
        unavailable(f"{_PG_HOST}:{_PG_PORT}/{_PG_DB}: {exc!r}")

    await _reset_schema(engine)
    await asyncio.to_thread(_alembic_upgrade_head, DATABASE_URL)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
async def relevance_sessions(
    relevance_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(relevance_engine, expire_on_commit=False)


@pytest.fixture(scope="session")
async def seeded_corpus(relevance_sessions: async_sessionmaker[AsyncSession]) -> Corpus:
    """Seed the fixed corpus once and index it with the production reindexer.

    The index is built by ``reindex_project_branch``, not by hand: that is what
    populates ``text_vector`` through ``to_tsvector('tripl_search', ...)`` and
    what production runs, so the harness ranks the same rows a user would.
    """
    async with relevance_sessions() as session:
        ai_config = await app_settings_service.get_embedding_config(session, org_id=None)
        if ai_config.search_embeddings_enabled:
            pytest.fail(
                "search embeddings are enabled in this environment, so the semantic "
                "leg would run against a live provider and rankings would not be "
                "reproducible. The case table pins the LEXICAL leg — it does not "
                "de-weight the semantic one, it just does not call it. The semantic "
                "leg's own guarantees (the cosine floor, the confidence it reports) "
                "are covered by test_semantic_floor.py, which builds its vectors by "
                "hand and needs no provider — so nothing is lost by turning this off. "
                "Unset SEARCH_EMBEDDINGS_ENABLED / OPENAI_API_KEY for this run."
            )
        corpus = await seed_corpus(session)
        await reindex_project_branch(
            session,
            project_id=corpus.project_id,
            branch_id=corpus.branch_id,
            slug=corpus.slug,
            # No Celery in the harness; the semantic leg is not under measurement.
            schedule_embeddings=False,
        )
    return corpus


@pytest.fixture
async def relevance_session(
    relevance_sessions: async_sessionmaker[AsyncSession],
    seeded_corpus: Corpus,
) -> AsyncIterator[AsyncSession]:
    """A read-only session per test against the already-seeded corpus."""
    async with relevance_sessions() as session:
        yield session


@pytest.fixture
async def unseeded_session(
    relevance_sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """A session on the migrated database with NOTHING seeded into it.

    Deliberately not ``relevance_session``: that fixture depends on
    ``seeded_corpus``, and a corpus is the one thing the invariant modules must
    not have. The engine fixture underneath still runs ``alembic upgrade head``,
    so both text search configurations are the ones the migrations create —
    asserting against a hand-rolled configuration would test a copy of the code
    rather than the code.

    It lived in ``test_stemming_invariants.py`` until a SECOND corpus-free module
    needed it (``test_coverage_invariants.py``). Importing a fixture
    across test modules is how two subtly different versions of it appear, and a
    hand-rolled copy in the new file would have been free to drop the "no corpus"
    property that is the whole point.
    """
    async with relevance_sessions() as session:
        yield session
