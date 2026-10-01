"""Batch 18 merge-time races: tripl-0zpq.288, .290 and .294.

One mechanism closes all three (``services/_plan_branch_locks.py``): a plan
write holds its branch's ``plan_branches`` row ``FOR SHARE`` in its own
transaction, and a merge holds the merged branch's row ``FOR UPDATE`` and
main's ``FOR NO KEY UPDATE`` from before it reads either plan to its commit.

The locks exist only on PostgreSQL, so the interleavings live in the
``postgres`` tests at the bottom: the real app, on a real database, with two
requests in flight at once and the merge held open at a chosen point. They skip
without ``TRIPL_TEST_PG_URL`` and fail when ``TRIPL_TEST_PG_REQUIRED=1`` finds
no database, like every other PostgreSQL gate (see
``test_alert_digest_concurrency_pg``). The SQLite tests at the top pin what can
be shown without concurrency: which requests take which lock, that the merge
takes main's before it reads main, and that the helpers are no-ops off
PostgreSQL.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request

from tripl.api import deps
from tripl.database import get_session
from tripl.main import app
from tripl.models import Base
from tripl.models.event import Event
from tripl.models.event_photo_comment import EventPhotoComment
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.user import User
from tripl.services import _plan_branch_locks, event_comment_service, plan_branch_merge_service
from tripl.tests._pg_url import asyncpg_url
from tripl.tests._project_ids import project_id_by_slug
from tripl.tests.conftest import TestSessionLocal, engine
from tripl.tests.test_alert_digest_concurrency_pg import _engine_or_skip
from tripl.tests.test_batch18_branches_a import _ask, _branch_track_id, _create_event
from tripl.tests.test_plan_branches import (
    _create_branch,
    _main_branch_id,
    _seed_plan,
    _transition,
)
from tripl.tests.test_rbac import iter_api_routes

# Long enough that a request which is NOT blocked has finished by then on any
# CI runner; the blocked ones are released explicitly, so this is no timeout.
_SETTLE_SECONDS = 1.0
# The ceiling on anything that must complete; a hang fails instead of stalling CI.
_DEADLINE_SECONDS = 30.0


def _handler(route: APIRoute) -> str:
    return f"{route.endpoint.__module__}.{route.endpoint.__qualname__}"


async def _event_id(client: AsyncClient, slug: str, name: str, branch_id: str | None) -> str:
    query = f"?branch={branch_id}" if branch_id else ""
    listed = await client.get(f"/api/v1/projects/{slug}/events{query}")
    assert listed.status_code == 200, listed.text
    return str(next(e["id"] for e in listed.json()["items"] if e["name"] == name))


class _Calls:
    """Records each call of a wrapped coroutine function, then runs it."""

    def __init__(self, wrapped: Callable[..., Awaitable[Any]]) -> None:
        self.wrapped = wrapped
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return await self.wrapped(*args, **kwargs)


# --- SQLite: who takes which lock --------------------------------------------


def test_the_lock_free_write_handlers_name_real_routes() -> None:
    """A renamed AI route would silently start holding the plan lock again."""
    handlers = {_handler(route) for _path, route in iter_api_routes()}
    assert handlers >= deps._LOCK_FREE_WRITE_HANDLERS


@pytest.mark.asyncio
async def test_only_a_plan_write_on_a_branch_holds_the_branch_row(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tripl-0zpq.288: the dependency locks for writes, not for reads."""
    slug = "b18-lock-branch-writes"
    await _seed_plan(client, slug)
    branch_id = await _create_branch(client, slug)
    event_id = await _event_id(client, slug, "purchase:success", branch_id)
    held = _Calls(deps.hold_branch_for_plan_write)
    monkeypatch.setattr(deps, "hold_branch_for_plan_write", held)

    listed = await client.get(f"/api/v1/projects/{slug}/events?branch={branch_id}")
    assert listed.status_code == 200
    assert held.calls == []

    # Refused (AI is off) after the dependency ran: it writes nothing, so it
    # does not hold the branch across what would be an LLM call.
    await client.post(
        f"/api/v1/projects/{slug}/ai/describe-event?branch={branch_id}",
        json={"event_id": event_id},
    )
    assert held.calls == []

    patched = await client.patch(
        f"/api/v1/projects/{slug}/events/{event_id}?branch={branch_id}",
        json={"description": "edited"},
    )
    assert patched.status_code == 200, patched.text
    assert [args[1] for args, _ in held.calls] == [uuid.UUID(branch_id)]


@pytest.mark.asyncio
async def test_only_a_plan_write_on_main_holds_mains_row(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tripl-0zpq.294: main writes, with or without main's id, take main's lock."""
    slug = "b18-lock-main-writes"
    await _seed_plan(client, slug)
    main_id = await _main_branch_id()
    event_id = await _event_id(client, slug, "purchase:success", None)
    held = _Calls(deps.hold_main_plan_for_write)
    monkeypatch.setattr(deps, "hold_main_plan_for_write", held)
    # The dependency skips the lock off PostgreSQL; pretend rows lock here so
    # the call is observable (the real helper still no-ops on SQLite).
    monkeypatch.setattr(deps, "locks_rows", lambda _session: True)

    assert (await client.get(f"/api/v1/projects/{slug}/events")).status_code == 200
    await client.post(f"/api/v1/projects/{slug}/ai/describe-event", json={"event_id": event_id})
    assert held.calls == []

    for query in ("", f"?branch={main_id}"):
        patched = await client.patch(
            f"/api/v1/projects/{slug}/events/{event_id}{query}", json={"description": query}
        )
        assert patched.status_code == 200, patched.text
    project_id = await project_id_by_slug(slug)
    assert [args[1] for args, _ in held.calls] == [project_id, project_id]


@pytest.mark.asyncio
async def test_a_comment_holds_its_events_working_branch_before_reading_the_thread(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tripl-0zpq.290: the comment path takes the same lock, working branches only."""
    slug = "b18-lock-comments"
    await _seed_plan(client, slug)
    branch_id = await _create_branch(client, slug)
    branch_event = await _event_id(client, slug, "purchase:success", branch_id)
    main_event = await _event_id(client, slug, "purchase:success", None)
    order: list[str] = []
    held = _Calls(event_comment_service.hold_branch_for_plan_write)
    real_thread = event_comment_service.event_thread

    async def thread(*args: Any, **kwargs: Any) -> event_comment_service.EventThread:
        order.append("thread")
        return await real_thread(*args, **kwargs)

    async def hold(*args: Any, **kwargs: Any) -> PlanBranch | None:
        order.append("hold")
        return cast(PlanBranch | None, await held(*args, **kwargs))

    monkeypatch.setattr(event_comment_service, "hold_branch_for_plan_write", hold)
    monkeypatch.setattr(event_comment_service, "event_thread", thread)

    await _ask(client, slug, branch_event, "on the branch")
    assert order == ["hold", "thread"]
    assert held.calls[0][0][1] == uuid.UUID(branch_id)
    assert held.calls[0][1] == {"working_only": True}

    # On main the helper is asked with working_only, which locks nothing there.
    await _ask(client, slug, main_event, "on main")
    assert held.calls[1][1] == {"working_only": True}
    async with TestSessionLocal() as session:
        assert (
            await _plan_branch_locks.hold_branch_for_plan_write(
                session, await _main_branch_id(), working_only=True
            )
            is None
        )


@pytest.mark.asyncio
async def test_the_merge_locks_main_before_it_reads_main(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tripl-0zpq.294: the lock is taken before main_payload, not after."""
    slug = "b18-merge-lock-order"
    await _seed_plan(client, slug)
    branch_id = await _create_branch(client, slug)
    main_id = await _main_branch_id()
    order: list[str] = []
    real_lock = plan_branch_merge_service.lock_main_plan_for_merge
    real_snapshot = plan_branch_merge_service.build_plan_snapshot

    async def lock(session: AsyncSession, main_branch_id: uuid.UUID) -> None:
        order.append(f"lock:{main_branch_id}")
        await real_lock(session, main_branch_id)

    async def snapshot(*args: Any, **kwargs: Any) -> dict[str, Any]:
        order.append(f"snapshot:{kwargs.get('branch_id')}")
        return await real_snapshot(*args, **kwargs)

    monkeypatch.setattr(plan_branch_merge_service, "lock_main_plan_for_merge", lock)
    monkeypatch.setattr(plan_branch_merge_service, "build_plan_snapshot", snapshot)
    await _transition(client, slug, branch_id, "submit")
    await _transition(client, slug, branch_id, "approve")
    order.clear()
    merged = await client.post(f"/api/v1/projects/{slug}/branches/{branch_id}/merge")
    assert merged.status_code == 200, merged.text
    assert order.index(f"lock:{main_id}") < order.index(f"snapshot:{main_id}")
    assert order.count(f"lock:{main_id}") == 1


@pytest.mark.asyncio
async def test_the_helpers_lock_nothing_on_sqlite() -> None:
    """Off PostgreSQL no statement carries a lock clause, and main's two emit none."""
    statements: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        async with TestSessionLocal() as session:
            await _plan_branch_locks.hold_branch_for_plan_write(session, uuid.uuid4())
            assert len(statements) == 1
            await _plan_branch_locks.hold_main_plan_for_write(session, uuid.uuid4())
            await _plan_branch_locks.lock_main_plan_for_merge(session, uuid.uuid4())
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
    assert len(statements) == 1
    assert " FOR " not in statements[0].upper()


class _PostgresShapedSession:
    """Just enough session for the helpers to believe they talk to PostgreSQL."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    def get_bind(self) -> Any:
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    async def scalar(self, statement: Any) -> None:
        self.statements.append(statement)

    async def execute(self, statement: Any) -> None:
        self.statements.append(statement)


@pytest.mark.asyncio
async def test_the_helpers_emit_the_lock_modes_the_design_depends_on() -> None:
    """FOR SHARE for writes, FOR NO KEY UPDATE for the merge's hold on main.

    The modes are the design: SHARE lets writes to one branch run together and
    conflicts with both of the merge's locks; NO KEY UPDATE on main conflicts
    with SHARE but not with the KEY SHARE a foreign-key insert takes, so a scan
    adding events on main is not queued behind a merge.
    """
    fake = _PostgresShapedSession()
    session = cast(AsyncSession, fake)
    await _plan_branch_locks.hold_branch_for_plan_write(session, uuid.uuid4())
    await _plan_branch_locks.hold_main_plan_for_write(session, uuid.uuid4())
    await _plan_branch_locks.lock_main_plan_for_merge(session, uuid.uuid4())
    rendered = [str(s.compile(dialect=postgresql.dialect())) for s in fake.statements]
    assert rendered[0].rstrip().endswith("FOR SHARE")
    assert rendered[1].rstrip().endswith("FOR SHARE OF plan_branches")
    assert rendered[2].rstrip().endswith("FOR NO KEY UPDATE")


# --- PostgreSQL: the interleavings -------------------------------------------


@dataclass(frozen=True)
class _PgApp:
    client: AsyncClient
    sessions: async_sessionmaker[AsyncSession]


# The search index is written in the same transaction as the plan, through the
# text search configurations the migrations create. ``create_all`` does not run
# migrations, so a stand-in of each is made here; what they stem is beside the
# point of these tests. Left in place afterwards: they are idempotent here and
# no other gate on this database names them.
_SEARCH_CONFIGURATIONS = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgname = 'tripl_search') THEN
        CREATE TEXT SEARCH CONFIGURATION tripl_search (COPY = simple);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgname = 'tripl_search_surface') THEN
        CREATE TEXT SEARCH CONFIGURATION tripl_search_surface (COPY = simple);
    END IF;
END
$$
"""


# Every connection the app makes in these tests says so, and teardown ends
# exactly those; the rest of the gate's connections are none of its business.
_APPLICATION_NAME = "tripl_batch18_merge_races"
_END_OUR_SESSIONS = """
SELECT pg_terminate_backend(pid) FROM pg_stat_activity
WHERE datname = current_database() AND application_name = :name
"""


@pytest.fixture
async def pg_app() -> AsyncIterator[_PgApp]:
    """The real app on a clean PostgreSQL schema, signed in as its first owner."""
    sync_engine = _engine_or_skip()
    Base.metadata.drop_all(sync_engine)
    Base.metadata.create_all(sync_engine)
    with sync_engine.begin() as connection:
        connection.execute(text(_SEARCH_CONFIGURATIONS))
    async_engine = create_async_engine(
        asyncpg_url(), connect_args={"server_settings": {"application_name": _APPLICATION_NAME}}
    )
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)

    async def pg_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    previous = app.dependency_overrides.get(get_session)
    app.dependency_overrides[get_session] = pg_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            registered = await client.post(
                "/api/v1/auth/register",
                json={"email": "pg@example.com", "password": "Password123!", "name": "PG"},
            )
            assert registered.status_code == 201, registered.text
            yield _PgApp(client=client, sessions=sessions)
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_session, None)
        else:
            app.dependency_overrides[get_session] = previous
        # A failed assertion can leave a request in flight holding row locks,
        # and drop_all would wait on them for ever: end those sessions first,
        # so a failure fails instead of hanging the job.
        with sync_engine.begin() as connection:
            connection.execute(text(_END_OUR_SESSIONS), {"name": _APPLICATION_NAME})
        await async_engine.dispose()
        Base.metadata.drop_all(sync_engine)
        sync_engine.dispose()


class _Gate:
    """Holds a merge open at one point: ``reached`` once there, runs on ``release``."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    def after(self, wrapped: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        async def paused(*args: Any, **kwargs: Any) -> Any:
            result = await wrapped(*args, **kwargs)
            self.reached.set()
            await self.release.wait()
            return result

        return paused


def _write_request(slug: str) -> Request:
    """A PATCH as ``get_branch_id_override`` sees one from outside the router.

    With no matched route, the dependency decides "write" by the method, so a
    PATCH takes the plan lock (``deps._is_a_write``).
    """
    return Request(
        {
            "type": "http",
            "method": "PATCH",
            "path": f"/api/v1/projects/{slug}/events",
            "path_params": {"slug": slug},
            "query_string": b"",
            "headers": [],
        }
    )


def _any_user() -> User:
    # Only read to replay a route's gates, and a request with no route has none.
    return cast(User, SimpleNamespace(role="editor"))


async def _approved_branch(client: AsyncClient, slug: str, description: str) -> str:
    """A branch whose one change is purchase:success's description, approved."""
    await _seed_plan(client, slug)
    branch_id = await _create_branch(client, slug)
    event_id = await _event_id(client, slug, "purchase:success", branch_id)
    edited = await client.patch(
        f"/api/v1/projects/{slug}/events/{event_id}?branch={branch_id}",
        json={"description": description},
    )
    assert edited.status_code == 200, edited.text
    assert (await _transition(client, slug, branch_id, "submit"))["status"] == "ready_for_review"
    assert (await _transition(client, slug, branch_id, "approve"))["status"] == "approved"
    return branch_id


async def _description(sessions: async_sessionmaker[AsyncSession], event_id: str) -> str:
    async with sessions() as session:
        value = await session.scalar(
            select(Event.description).where(Event.id == uuid.UUID(event_id))
        )
    return str(value)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_a_branch_write_arriving_mid_merge_waits_then_is_refused(
    pg_app: _PgApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tripl-0zpq.288: the write waits for the merge, then reads ``merged``.

    Before the fix the dependency read ``approved`` with a plain SELECT, which a
    merge's FOR UPDATE does not block, and the edit committed onto the branch
    the merge had just recorded as landed.
    """
    client, slug = pg_app.client, "b18-pg-288-refused"
    branch_id = await _approved_branch(client, slug, "approved text")
    event_id = await _event_id(client, slug, "purchase:success", branch_id)
    gate = _Gate()
    monkeypatch.setattr(
        plan_branch_merge_service,
        "_check_min_approvals",
        gate.after(plan_branch_merge_service._check_min_approvals),
    )

    merge = asyncio.create_task(client.post(f"/api/v1/projects/{slug}/branches/{branch_id}/merge"))
    try:
        await asyncio.wait_for(gate.reached.wait(), _DEADLINE_SECONDS)
        write = asyncio.create_task(
            client.patch(
                f"/api/v1/projects/{slug}/events/{event_id}?branch={branch_id}",
                json={"description": "sneaked in"},
            )
        )
        await asyncio.sleep(_SETTLE_SECONDS)
        assert not write.done(), "the write did not wait for the merge in flight"
    finally:
        gate.release.set()
    merged = await asyncio.wait_for(merge, _DEADLINE_SECONDS)
    assert merged.status_code == 200, merged.text
    refused = await asyncio.wait_for(write, _DEADLINE_SECONDS)
    assert refused.status_code == 409, refused.text
    assert "merged" in refused.json()["detail"]
    assert await _description(pg_app.sessions, event_id) == "approved text"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_a_merge_arriving_mid_write_waits_and_then_sees_the_write(pg_app: _PgApp) -> None:
    """tripl-0zpq.288, the other order: the merge snapshots after the write.

    The write is held open inside the dependency's own transaction, the one the
    route writes through. The merge must wait for it rather than snapshot the
    branch without it; once it commits, the approval no longer matches the
    branch and the merge refuses with a stale approval instead of landing a
    plan nobody approved — or landing the approved one while the write goes
    onto a merged branch.
    """
    client, slug = pg_app.client, "b18-pg-288-merge-waits"
    branch_id = await _approved_branch(client, slug, "approved text")
    event_id = await _event_id(client, slug, "purchase:success", branch_id)

    async with pg_app.sessions() as writer:
        dependency = deps.get_branch_id_override(
            _write_request(slug), writer, _any_user(), branch=branch_id
        )
        assert await anext(dependency) == uuid.UUID(branch_id)
        await writer.execute(
            update(Event).where(Event.id == uuid.UUID(event_id)).values(description="late edit")
        )
        merge = asyncio.create_task(
            client.post(f"/api/v1/projects/{slug}/branches/{branch_id}/merge")
        )
        await asyncio.sleep(_SETTLE_SECONDS)
        assert not merge.done(), "the merge did not wait for the write in flight"
        await writer.commit()
        await dependency.aclose()

    refused = await asyncio.wait_for(merge, _DEADLINE_SECONDS)
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["insufficient_approvals"]["stale"] == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_a_reply_posted_mid_merge_follows_its_question_to_main(
    pg_app: _PgApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tripl-0zpq.290: the reply waits for the merge and lands on the moved parent.

    The merge is held open AFTER it moved the branch row's threads to main.
    Before the fix the reply still read its question on the branch row, was
    inserted there, and the thread was split across the branch row and main.
    """
    client, slug = pg_app.client, "b18-pg-290-reply"
    await _seed_plan(client, slug)
    branch_id = await _create_branch(client, slug)
    branch_et_id = await _branch_track_id(client, slug, branch_id, "track")
    branch_row = await _create_event(
        client, slug, branch_et_id, "checkout:new_tap", branch_id=branch_id
    )
    question = await _ask(client, slug, branch_row, "which screens?")
    await _transition(client, slug, branch_id, "submit")
    await _transition(client, slug, branch_id, "approve")
    gate = _Gate()
    monkeypatch.setattr(
        plan_branch_merge_service,
        "_move_event_threads_to_main",
        gate.after(plan_branch_merge_service._move_event_threads_to_main),
    )

    merge = asyncio.create_task(client.post(f"/api/v1/projects/{slug}/branches/{branch_id}/merge"))
    try:
        await asyncio.wait_for(gate.reached.wait(), _DEADLINE_SECONDS)
        reply = asyncio.create_task(
            client.post(
                f"/api/v1/projects/{slug}/events/{branch_row}/comments",
                json={"body": "all of them", "parent_id": question},
            )
        )
        await asyncio.sleep(_SETTLE_SECONDS)
        assert not reply.done(), "the reply did not wait for the merge in flight"
    finally:
        gate.release.set()
    merged = await asyncio.wait_for(merge, _DEADLINE_SECONDS)
    assert merged.status_code == 200, merged.text
    posted = await asyncio.wait_for(reply, _DEADLINE_SECONDS)
    assert posted.status_code == 201, posted.text

    async with pg_app.sessions() as session:
        anchors = dict(
            (
                await session.execute(
                    select(EventPhotoComment.id, EventPhotoComment.event_id).where(
                        EventPhotoComment.id.in_(
                            [uuid.UUID(question), uuid.UUID(posted.json()["id"])]
                        )
                    )
                )
            ).all()
        )
        main_row = await session.scalar(
            select(Event.id)
            .join(PlanBranch, PlanBranch.id == Event.branch_id)
            .where(PlanBranch.kind == BranchKind.main.value, Event.name == "checkout:new_tap")
        )
    assert main_row is not None
    assert set(anchors.values()) == {main_row}


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_a_main_edit_arriving_mid_merge_waits_and_is_not_overwritten(
    pg_app: _PgApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tripl-0zpq.294: the merge holds main, so the edit applies after it.

    The merge is held open after its conflict check. Before the fix the main
    edit committed right then, the check had already passed on the old value,
    and ``_apply_merge`` wrote the branch's description over it.
    """
    client, slug = pg_app.client, "b18-pg-294-edit-after"
    branch_id = await _approved_branch(client, slug, "from the branch")
    main_event = await _event_id(client, slug, "purchase:success", None)
    gate = _Gate()
    monkeypatch.setattr(
        plan_branch_merge_service,
        "_check_min_approvals",
        gate.after(plan_branch_merge_service._check_min_approvals),
    )

    merge = asyncio.create_task(client.post(f"/api/v1/projects/{slug}/branches/{branch_id}/merge"))
    try:
        await asyncio.wait_for(gate.reached.wait(), _DEADLINE_SECONDS)
        edit = asyncio.create_task(
            client.patch(
                f"/api/v1/projects/{slug}/events/{main_event}",
                json={"description": "edited on main"},
            )
        )
        await asyncio.sleep(_SETTLE_SECONDS)
        assert not edit.done(), "the main edit did not wait for the merge in flight"
    finally:
        gate.release.set()
    merged = await asyncio.wait_for(merge, _DEADLINE_SECONDS)
    assert merged.status_code == 200, merged.text
    edited = await asyncio.wait_for(edit, _DEADLINE_SECONDS)
    assert edited.status_code == 200, edited.text
    assert await _description(pg_app.sessions, main_event) == "edited on main"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_a_merge_arriving_mid_main_edit_waits_and_reports_the_conflict(
    pg_app: _PgApp,
) -> None:
    """tripl-0zpq.294, the other order: the merge's check sees the main edit.

    The main edit is held open inside the dependency's transaction. Before the
    fix the merge read main without it, found no conflict, and once the edit
    committed its UPDATE went through and overwrote it with the branch value.
    Now the merge waits for main, reads the edit, and refuses with a conflict.
    """
    client, slug = pg_app.client, "b18-pg-294-conflict"
    branch_id = await _approved_branch(client, slug, "from the branch")
    main_event = await _event_id(client, slug, "purchase:success", None)

    async with pg_app.sessions() as writer:
        dependency = deps.get_branch_id_override(
            _write_request(slug), writer, _any_user(), branch=None
        )
        assert await anext(dependency) is None
        await writer.execute(
            update(Event)
            .where(Event.id == uuid.UUID(main_event))
            .values(description="edited on main")
        )
        merge = asyncio.create_task(
            client.post(f"/api/v1/projects/{slug}/branches/{branch_id}/merge")
        )
        await asyncio.sleep(_SETTLE_SECONDS)
        assert not merge.done()
        await writer.commit()
        await dependency.aclose()

    refused = await asyncio.wait_for(merge, _DEADLINE_SECONDS)
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["conflicts"]
    assert await _description(pg_app.sessions, main_event) == "edited on main"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_a_write_waiting_on_a_deleted_branch_gets_404(pg_app: _PgApp) -> None:
    """The re-read under the lock can find the row gone; that is a 404, not a 500."""
    client, slug = pg_app.client, "b18-pg-288-deleted"
    await _seed_plan(client, slug)
    branch_id = await _create_branch(client, slug)

    async with pg_app.sessions() as deleter:
        await deleter.execute(
            select(PlanBranch.id).where(PlanBranch.id == uuid.UUID(branch_id)).with_for_update()
        )
        async with pg_app.sessions() as writer:
            dependency = deps.get_branch_id_override(
                _write_request(slug), writer, _any_user(), branch=branch_id
            )

            async def first_yield() -> uuid.UUID | None:
                return await anext(dependency)

            waiting = asyncio.create_task(first_yield())
            await asyncio.sleep(_SETTLE_SECONDS)
            assert not waiting.done()
            await deleter.delete(await deleter.get(PlanBranch, uuid.UUID(branch_id)))
            await deleter.commit()
            with pytest.raises(HTTPException) as refused:
                await asyncio.wait_for(waiting, _DEADLINE_SECONDS)
    assert refused.value.status_code == 404
