"""The PostgreSQL-only advisory locks, run against PostgreSQL.

Every one of these guards is a no-op on the SQLite the suite runs on, by design
and said so at each site, so a wrong dialect string or a dropped lock call used
to ship green. These tests hold them on a real database and look from a SECOND
session — through ``pg_try_advisory_xact_lock`` and ``pg_locks`` — to prove the
lock is really taken and really released:

* ``auth_service.acquire_owner_set_xact_lock``, which serialises the first-owner
  registration (and the last-owner demotion) of one organization — with the
  race it exists for;
* ``metric_definition_service._try_acquire_metric_dispatch_transaction_lock``,
  the manual-collect side of the catalog scheduler's advisory lock — with the
  409 a manual collect answers while the scheduler holds it.

Branch-merge locking is covered by ``test_merge_races_pg``. Like every
PostgreSQL gate here these skip without ``TRIPL_TEST_PG_URL`` and fail when
``TRIPL_TEST_PG_REQUIRED=1`` finds no database (``test_alert_digest_concurrency_pg``).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tripl.models import Base
from tripl.models.domain_enums import OrganizationRole, ProjectMemberRole
from tripl.models.organization import DEFAULT_ORG_ID, Organization, OrganizationMember
from tripl.models.project import Project
from tripl.models.project_member import ProjectMember
from tripl.models.user import User
from tripl.schemas.auth import RegisterRequest
from tripl.services import auth_service, metric_definition_service, project_member_service
from tripl.tests._pg_url import asyncpg_url
from tripl.tests.test_alert_digest_concurrency_pg import _engine_or_skip
from tripl.tests.test_merge_races_pg import _SEARCH_CONFIGURATIONS
from tripl.worker.tasks.metrics.schedule import _METRIC_DEFINITION_DISPATCH_ADVISORY_LOCK_KEY

# Long enough that a call which is NOT blocked has finished by then on any CI
# runner; blocked ones are released explicitly, so this is no timeout.
_SETTLE_SECONDS = 1.0
_DEADLINE_SECONDS = 30.0
_APPLICATION_NAME = "tripl_batch18_locks"

# An advisory lock on a bigint key shows in ``pg_locks`` split in two: the high
# 32 bits in ``classid``, the low 32 in ``objid``, with ``objsubid = 1``.
_HELD_BY = """
SELECT count(*) FROM pg_locks
WHERE locktype = 'advisory' AND granted AND objsubid = 1
  AND classid = ((CAST(:key AS bigint) >> 32) & 4294967295)
  AND objid = (CAST(:key AS bigint) & 4294967295)
  AND pid = :pid
"""


@pytest.fixture
async def pg_sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A clean PostgreSQL schema — no users at all — and a session factory on it."""
    sync_engine = _engine_or_skip()
    Base.metadata.drop_all(sync_engine)
    Base.metadata.create_all(sync_engine)
    with sync_engine.begin() as connection:
        connection.execute(text(_SEARCH_CONFIGURATIONS))
    async_engine = create_async_engine(
        asyncpg_url(), connect_args={"server_settings": {"application_name": _APPLICATION_NAME}}
    )
    try:
        yield async_sessionmaker(async_engine, expire_on_commit=False)
    finally:
        await async_engine.dispose()
        # A failed assertion can leave a session holding a lock; end ours so
        # the next test's drop_all cannot wait on it.
        with sync_engine.begin() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND application_name = :name"
                ),
                {"name": _APPLICATION_NAME},
            )
        sync_engine.dispose()


async def _free_to_take(sessions: async_sessionmaker[AsyncSession], key: int) -> bool:
    """Whether ANOTHER session could take ``key`` right now (and gives it back)."""
    async with sessions() as probe:
        taken = bool(
            await probe.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key})
        )
        await probe.rollback()
    return bool(taken)


async def _held_by(session: AsyncSession, key: int) -> bool:
    """Whether ``session``'s own backend holds the advisory lock on ``key``."""
    pid = await session.scalar(text("SELECT pg_backend_pid()"))
    return bool(await session.scalar(text(_HELD_BY), {"key": key, "pid": pid}))


def _registration(email: str) -> RegisterRequest:
    return RegisterRequest(email=email, password="Password123!", name="Owner race")


# --- auth_service.acquire_owner_set_xact_lock -----------------------------------


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_the_owner_set_lock_is_held_until_the_transaction_ends(
    pg_sessions: async_sessionmaker[AsyncSession],
) -> None:
    key = auth_service.owner_set_lock_key(DEFAULT_ORG_ID)
    other_org_key = auth_service.owner_set_lock_key(uuid.uuid4())
    assert key != other_org_key
    assert await _free_to_take(pg_sessions, key)
    async with pg_sessions() as holder:
        await auth_service.acquire_owner_set_xact_lock(holder, DEFAULT_ORG_ID)
        assert await _held_by(holder, key)
        assert not await _free_to_take(pg_sessions, key)
        # Per organization (F20 PR4): another org's owner set is not blocked.
        assert await _free_to_take(pg_sessions, other_org_key)
        await holder.commit()
    # Transaction-scoped: the commit released it without an unlock call.
    assert await _free_to_take(pg_sessions, key)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_a_second_first_registration_waits_and_then_is_not_owner(
    pg_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The TOCTOU the lock closes: two registrations on an empty instance.

    The first holds the owner-set lock with no user written yet — the moment
    the race lives in. The second registration must wait on the lock rather than
    read the empty users table, and once the first commits its owner it must
    see that owner and come out a plain member. Without the lock both read "no
    users" and both become owner.
    """
    async with pg_sessions() as first:
        await auth_service.acquire_owner_set_xact_lock(first, DEFAULT_ORG_ID)
        assert not await auth_service.has_any_users(first)

        async def register_second() -> User:
            async with pg_sessions() as second:
                user, _token = await auth_service.register_user(
                    second, _registration("second@example.com")
                )
                return user

        racing = asyncio.create_task(register_second())
        await asyncio.sleep(_SETTLE_SECONDS)
        assert not racing.done(), "the second registration did not wait for the owner-set lock"

        first_user = User(email="first@example.com", name="First", password_hash="x")
        first.add(first_user)
        await first.flush()
        first.add(
            OrganizationMember(
                organization_id=DEFAULT_ORG_ID,
                user_id=first_user.id,
                role=OrganizationRole.owner.value,
            )
        )
        await first.commit()

    second_user = await asyncio.wait_for(racing, _DEADLINE_SECONDS)
    assert second_user.is_platform_admin is False
    async with pg_sessions() as session:
        owners = (
            await session.execute(
                select(User.email)
                .join(OrganizationMember, OrganizationMember.user_id == User.id)
                .where(
                    OrganizationMember.organization_id == DEFAULT_ORG_ID,
                    OrganizationMember.role == OrganizationRole.owner.value,
                )
            )
        ).all()
    assert [row.email for row in owners] == ["first@example.com"]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_adding_a_project_member_waits_for_a_removal_from_the_organization(
    pg_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """``project_member_service.add_member`` takes the owner-set lock first.

    A removal from the organization holds that lock while it deletes the
    membership. An add racing it must wait, then see the removal and refuse:
    without the lock its membership check could pass just before the removal
    and its insert land just after, leaving a row for someone outside the
    organization.
    """
    org_id = uuid.uuid4()
    async with pg_sessions() as setup:
        setup.add(Organization(id=org_id, slug=f"race-{org_id.hex[:8]}", name="Race"))
        owner = User(email="owner@example.com", name="Owner", password_hash="x")
        leaving = User(email="leaving@example.com", name="Leaving", password_hash="x")
        setup.add_all([owner, leaving])
        await setup.flush()
        setup.add_all(
            [
                OrganizationMember(
                    organization_id=org_id, user_id=owner.id, role=OrganizationRole.owner.value
                ),
                OrganizationMember(
                    organization_id=org_id, user_id=leaving.id, role=OrganizationRole.member.value
                ),
            ]
        )
        project = Project(name="Shop", slug="shop", organization_id=org_id)
        setup.add(project)
        await setup.commit()

    async with pg_sessions() as removal:
        await auth_service.acquire_owner_set_xact_lock(removal, org_id)

        async def add() -> None:
            async with pg_sessions() as adding:
                target = await adding.get(Project, project.id)
                assert target is not None
                await project_member_service.add_member(
                    adding,
                    target,
                    user_id=leaving.id,
                    role=ProjectMemberRole.editor,
                    added_by=owner.id,
                )

        racing = asyncio.create_task(add())
        await asyncio.sleep(_SETTLE_SECONDS)
        assert not racing.done(), "add_member did not wait for the owner-set lock"

        await removal.execute(
            delete(OrganizationMember).where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.user_id == leaving.id,
            )
        )
        await removal.commit()

    with pytest.raises(HTTPException) as refused:
        await asyncio.wait_for(racing, _DEADLINE_SECONDS)
    assert refused.value.status_code == 404
    async with pg_sessions() as session:
        rows = await session.scalars(
            select(ProjectMember.id).where(ProjectMember.user_id == leaving.id)
        )
        assert list(rows) == []


# --- metric_definition_service: the dispatcher's advisory lock ------------------


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_the_manual_dispatch_lock_is_the_schedulers_key_and_is_transaction_scoped(
    pg_sessions: async_sessionmaker[AsyncSession],
) -> None:
    key = _METRIC_DEFINITION_DISPATCH_ADVISORY_LOCK_KEY
    async with pg_sessions() as dispatch:
        assert await metric_definition_service._try_acquire_metric_dispatch_transaction_lock(
            dispatch
        )
        assert await _held_by(dispatch, key)
        assert not await _free_to_take(pg_sessions, key)
        await dispatch.commit()
    assert await _free_to_take(pg_sessions, key)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_a_manual_collect_is_refused_while_the_scheduler_holds_the_lock(
    pg_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The beat tick holds the SESSION-level lock on the same key; a manual
    collect in that window must not read, decide and dispatch beside it."""
    key = _METRIC_DEFINITION_DISPATCH_ADVISORY_LOCK_KEY
    async with pg_sessions() as scheduler:
        assert await scheduler.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        try:
            async with pg_sessions() as manual:
                assert not (
                    await metric_definition_service._try_acquire_metric_dispatch_transaction_lock(
                        manual
                    )
                )
            async with pg_sessions() as manual:
                # Refused before any read, so the metric need not exist.
                with pytest.raises(HTTPException) as refused:
                    await metric_definition_service.trigger_metric_collection(
                        manual, "no-such-project", uuid.uuid4()
                    )
            assert refused.value.status_code == 409
            assert refused.value.detail == "Metric collection dispatcher is busy"
        finally:
            await scheduler.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
            await scheduler.commit()
    assert await _free_to_take(pg_sessions, key)
