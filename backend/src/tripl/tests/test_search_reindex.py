"""Regression tests for the Celery->async search-reindex bridge.

The bridge runs inside long-lived prefork workers via a fresh ``asyncio.run()``
per task. Reusing the module-global pooled async engine across those loops makes
asyncpg raise "got Future attached to a different loop" from the second reindex
onward (deterministic on CPython 3.14). These tests pin the fix: every call
builds its own short-lived NullPool engine and disposes it inside the same loop.

``reindex_main_branch_from_worker`` short-circuits on non-postgresql binds, so
the production path is exercised here with a fake postgresql-dialect session and
patched async deps — no real database or event-loop reuse needed. The function
is synchronous (it owns the ``asyncio.run`` call), so these tests are sync too.

The single-branch Celery entry point into the same bridge
(``tasks.search.reindex_search_branch``) is covered at the bottom.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.pool import NullPool

from tripl.worker import search_reindex
from tripl.worker.tasks import search as search_tasks


class _FakeAsyncSession:
    async def __aenter__(self) -> MagicMock:
        return MagicMock(name="async_db")

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeEngine:
    def __init__(self, poolclass: object) -> None:
        self.poolclass = poolclass
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


def _fake_pg_session(project: object, branch_id: uuid.UUID) -> MagicMock:
    session = MagicMock()
    session.bind.dialect.name = "postgresql"
    session.get.return_value = project
    session.scalar.return_value = branch_id
    return session


def _patch_async_deps(monkeypatch):
    created: list[_FakeEngine] = []

    def fake_create_async_engine(url: object, **kwargs: object) -> _FakeEngine:
        engine = _FakeEngine(poolclass=kwargs.get("poolclass"))
        created.append(engine)
        return engine

    def fake_async_sessionmaker(engine: object, **kwargs: object):
        return lambda: _FakeAsyncSession()

    reindex = AsyncMock(return_value=1)

    monkeypatch.setattr(search_reindex, "create_async_engine", fake_create_async_engine)
    monkeypatch.setattr(search_reindex, "async_sessionmaker", fake_async_sessionmaker)
    monkeypatch.setattr(search_reindex, "reindex_project_branch", reindex)
    return created, reindex


def test_each_call_builds_and_disposes_a_fresh_nullpool_engine(monkeypatch) -> None:
    created, reindex = _patch_async_deps(monkeypatch)
    project = MagicMock(slug="demo")
    project_id, branch_id = uuid.uuid4(), uuid.uuid4()

    # Two sequential calls emulate a long-lived worker reindexing twice. The
    # bug would surface on the second call; the fix makes both independent.
    for _ in range(2):
        search_reindex.reindex_main_branch_from_worker(
            _fake_pg_session(project, branch_id), project_id
        )

    assert len(created) == 2, "each call must build its own short-lived engine"
    assert all(e.poolclass is NullPool for e in created), "engine must use NullPool"
    assert all(e.disposed for e in created), "each engine must be disposed in-loop"
    assert reindex.await_count == 2


def test_non_postgresql_bind_short_circuits_without_touching_async(monkeypatch) -> None:
    created, reindex = _patch_async_deps(monkeypatch)
    session = MagicMock()
    session.bind.dialect.name = "sqlite"

    search_reindex.reindex_main_branch_from_worker(session, uuid.uuid4())

    assert created == [], "no async engine should be built for non-postgresql binds"
    assert reindex.await_count == 0
    session.get.assert_not_called()


def test_reindex_search_branch_task_parses_its_ids_and_closes_its_session(monkeypatch) -> None:
    """The read path enqueues this with STRING ids, as Celery's JSON payload requires.

    ``reindex_branch_from_worker`` compares and queries on ``uuid.UUID`` values —
    handing it the raw strings selects nothing and silently reindexes an empty
    branch, which looks exactly like success.
    """
    session = MagicMock()
    monkeypatch.setattr(search_tasks, "_get_sync_session", lambda: session)
    reindexed: list[tuple[object, object, object]] = []
    monkeypatch.setattr(
        search_tasks,
        "reindex_branch_from_worker",
        lambda db, project_id, branch_id: reindexed.append((db, project_id, branch_id)),
    )
    project_id, branch_id = uuid.uuid4(), uuid.uuid4()

    result = search_tasks.reindex_search_branch(str(project_id), str(branch_id))

    assert result == {"branches_reindexed": 1}
    assert reindexed == [(session, project_id, branch_id)]
    session.close.assert_called_once()
