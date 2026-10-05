"""A pool of demos seeded ahead of time, handed out the moment a visitor asks.

Seeding a demo costs seconds of worker CPU. On a public demo a burst of
sign-ups queues those seeds, and the last visitor in the queue waits for all of
them. With ``DEMO_POOL_SIZE`` above 0 the ``refill-demo-pool`` beat task keeps
that many demos ready in a service organization (``DEMO_POOL_ORG_ID``), and
:func:`claim_pooled_demo` moves one into the visitor's organization at once.
The pool only fills up to its size: an idle instance seeds nothing more.

Every pooled demo is seeded by the real recipe for a throwaway user of its own
(``pool-<hex>@demo-pool.invalid``), a member of the service organization and
nothing else. That makes the claim a plain rename: every column that points at
that user now points at the visitor, every row of the demo that points at the
service organization now points at the visitor's, and the throwaway user is
deleted. The columns are found by walking the schema's foreign keys, not listed
by hand, so a table added later is moved too.

Entries older than ``DEMO_POOL_MAX_AGE_HOURS`` are discarded and seeded again:
the recipe's incident is anchored at seed time and must still be open, and
inside its 24-hour window, when a visitor first sees it.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import Table, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import cache
from tripl.auth_utils import hash_password
from tripl.config import settings
from tripl.models import Base
from tripl.models.domain_enums import OrganizationRole, ProjectGenerationStatus
from tripl.models.organization import (
    DEMO_POOL_ORG_ID,
    DEMO_POOL_ORG_SLUG,
    Organization,
    OrganizationMember,
)
from tripl.models.project import Project
from tripl.models.user import User
from tripl.services import plan_branch_service, project_member_service, project_service

logger = logging.getLogger(__name__)

DEMO_POOL_CLAIMED_EVENT = "demo.pool.claimed"
DEMO_POOL_REFILLED_EVENT = "demo.pool.refilled"

POOL_USER_EMAIL_DOMAIN = "demo-pool.invalid"

# Rows that describe a person or an organization itself rather than the demo:
# sign-in, membership and identity records. A claim never moves them; the
# throwaway user's go when it is deleted. The SSO and SCIM ones exist only where
# the Enterprise extension's models are loaded.
_IDENTITY_TABLES = frozenset(
    {
        "api_keys",
        "email_verification_tokens",
        "invitations",
        "organization_group_members",
        "organization_members",
        "password_reset_tokens",
        "platform_step_ins",
        "scim_user_links",
        "sso_link_tickets",
        "sso_membership_blocks",
        "user_notification_prefs",
        "user_sessions",
        "user_sso_identities",
    }
)


def _columns_referencing(target: str) -> list[tuple[Table, str]]:
    """Every (table, column) with a foreign key to ``target``, identity tables aside."""
    found: list[tuple[Table, str]] = []
    for table in Base.metadata.sorted_tables:
        if table.name in _IDENTITY_TABLES:
            continue
        for column in table.columns:
            if any(fk.column.table.name == target for fk in column.foreign_keys):
                found.append((table, column.name))
    return found


def pool_enabled() -> bool:
    return settings.demo_enabled and settings.demo_pool_size > 0


async def _ensure_pool_org(session: AsyncSession) -> None:
    if await session.get(Organization, DEMO_POOL_ORG_ID) is None:
        session.add(Organization(id=DEMO_POOL_ORG_ID, slug=DEMO_POOL_ORG_SLUG, name="Demo pool"))
        await session.flush()


def _fresh_cutoff(now: datetime) -> datetime:
    return now - timedelta(hours=settings.demo_pool_max_age_hours)


async def claim_pooled_demo(
    session: AsyncSession,
    *,
    visitor_id: uuid.UUID,
    organization_id: uuid.UUID,
    name: str,
) -> Project | None:
    """Move one ready pooled demo to ``visitor_id`` in ``organization_id``. Commits.

    Returns None when the pool is off or has nothing fresh to hand out; the
    caller then seeds a demo the ordinary way.
    """
    if not pool_enabled():
        return None
    now = datetime.now(tz=UTC)
    candidate = await session.scalar(
        select(Project)
        .where(
            Project.organization_id == DEMO_POOL_ORG_ID,
            Project.is_demo.is_(True),
            Project.generation_status == ProjectGenerationStatus.ready.value,
            Project.demo_seeded_at >= _fresh_cutoff(now),
        )
        .order_by(Project.demo_seeded_at.desc())
        .limit(1)
        # Two visitors claiming at once take two different demos.
        .with_for_update(skip_locked=True)
    )
    if candidate is None or candidate.created_by_user_id is None:
        return None
    project_id = candidate.id
    pool_user_id = candidate.created_by_user_id
    await _reassign(
        session,
        project_id=project_id,
        pool_user_id=pool_user_id,
        visitor_id=visitor_id,
        organization_id=organization_id,
    )
    await session.execute(
        update(Project)
        .where(Project.id == project_id)
        .values(name=name, created_at=now, demo_last_accessed_at=now)
        .execution_options(synchronize_session=False)
    )
    await session.execute(delete(User).where(User.id == pool_user_id))
    await session.commit()
    # Only the project: expiring everything would also expire the caller's
    # user, which the request reads again after this returns.
    session.expire(candidate)
    await cache.delete_prefix(cache.prefix_projects())
    logger.info("%s project_id=%s", DEMO_POOL_CLAIMED_EVENT, project_id)
    return await session.get(Project, project_id)


async def _reassign(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    pool_user_id: uuid.UUID,
    visitor_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> None:
    """Point the demo's rows at the visitor and their organization, without committing.

    Organization columns move first: a row of the demo is recognised by its
    project, or — for the project-less rows the recipe files, such as its
    instance-scoped audit entries — by the throwaway user, which is still in
    place at that point.
    """
    user_columns: dict[str, list[str]] = {}
    for table, name in _columns_referencing("users"):
        user_columns.setdefault(table.name, []).append(name)

    # A row that names its project AND that project's organization in one
    # foreign key (a demo's synthetic warehouse) cannot follow the project
    # one column at a time: whichever moves first breaks the key. Detach it,
    # move both sides, and attach it again.
    detached: list[tuple[Table, list[uuid.UUID]]] = []
    for table in _tables_keyed_on_project_org():
        ids = list(
            (
                await session.scalars(select(table.c.id).where(table.c.project_id == project_id))
            ).all()
        )
        if ids:
            await session.execute(update(table).where(table.c.id.in_(ids)).values(project_id=None))
            detached.append((table, ids))

    for table, column in _columns_referencing("organizations"):
        owned_by_demo = []
        if table.name == Project.__tablename__:
            owned_by_demo.append(table.c.id == project_id)
        if "project_id" in table.c:
            owned_by_demo.append(table.c.project_id == project_id)
        owned_by_demo.extend(
            table.c[name] == pool_user_id for name in user_columns.get(table.name, [])
        )
        if not owned_by_demo:
            continue
        await session.execute(
            update(table)
            .where(table.c[column] == DEMO_POOL_ORG_ID, or_(*owned_by_demo))
            .values({column: organization_id})
        )
    for table, ids in detached:
        await session.execute(
            update(table)
            .where(table.c.id.in_(ids))
            .values(organization_id=organization_id, project_id=project_id)
        )
    for table, column in _columns_referencing("users"):
        await session.execute(
            update(table).where(table.c[column] == pool_user_id).values({column: visitor_id})
        )


def _tables_keyed_on_project_org() -> list[Table]:
    """Tables with one foreign key to ``projects`` over (id, organization_id)."""
    found: list[Table] = []
    for table in Base.metadata.sorted_tables:
        for constraint in table.foreign_key_constraints:
            referred = {element.column.name for element in constraint.elements}
            if constraint.referred_table.name == "projects" and "organization_id" in referred:
                found.append(table)
    return found


async def refill_demo_pool(session: AsyncSession) -> dict[str, int]:
    """Discard stale entries and start seeds until the pool holds its size. Commits.

    Seeds already running count toward the size, so a beat tick that fires
    while the last refill is still seeding does not overfill the pool.
    """
    if not pool_enabled():
        return {"discarded": 0, "started": 0}
    from tripl.services import demo_service

    await _ensure_pool_org(session)
    await session.commit()
    now = datetime.now(tz=UTC)

    stale = (
        await session.scalars(
            select(Project).where(
                Project.organization_id == DEMO_POOL_ORG_ID,
                Project.is_demo.is_(True),
                or_(
                    Project.generation_status == ProjectGenerationStatus.failed.value,
                    (Project.generation_status == ProjectGenerationStatus.ready.value)
                    & (Project.demo_seeded_at < _fresh_cutoff(now)),
                    (Project.generation_status == ProjectGenerationStatus.seeding.value)
                    & (
                        Project.created_at
                        < now - timedelta(hours=demo_service.STALLED_SEEDING_HOURS)
                    ),
                ),
            )
        )
    ).all()
    for project in stale:
        await discard_pool_entry(session, project)

    live = await session.scalar(
        select(func.count())
        .select_from(Project)
        .where(
            Project.organization_id == DEMO_POOL_ORG_ID,
            Project.is_demo.is_(True),
            Project.generation_status.in_(
                [ProjectGenerationStatus.ready.value, ProjectGenerationStatus.seeding.value]
            ),
        )
    )
    started = 0
    for _ in range(max(settings.demo_pool_size - (live or 0), 0)):
        project_id = await _new_pool_entry(session)
        try:
            await demo_service.enqueue_demo_seed(session, project_id, None)
        except Exception:
            # The shell stays ``seeding`` and ages out as stalled; the next
            # tick tries again.
            logger.exception("demo.pool.enqueue_failed project_id=%s", project_id)
            break
        started += 1
    if stale or started:
        logger.info("%s discarded=%d started=%d", DEMO_POOL_REFILLED_EVENT, len(stale), started)
    return {"discarded": len(stale), "started": started}


async def _new_pool_entry(session: AsyncSession) -> uuid.UUID:
    """Commit a seeding shell owned by a fresh throwaway user of the pool."""
    from tripl.services import demo_service

    token = secrets.token_hex(8)
    user = User(
        email=f"pool-{token}@{POOL_USER_EMAIL_DOMAIN}",
        name="Demo visitor",
        # Nobody knows this password; the address cannot receive a reset.
        password_hash=hash_password(secrets.token_urlsafe(32)),
    )
    session.add(user)
    await session.flush()
    session.add(
        OrganizationMember(
            organization_id=DEMO_POOL_ORG_ID,
            user_id=user.id,
            role=OrganizationRole.member.value,
        )
    )
    project = demo_service._new_demo_project(
        slug=f"demo-{token[:6]}",
        created_by=user.id,
        organization_id=DEMO_POOL_ORG_ID,
    )
    session.add(project)
    await session.flush()
    await plan_branch_service.ensure_main_branch_id(session, project.id)
    await project_member_service.grant_membership(
        session, project_id=project.id, user_id=user.id, added_by_user_id=user.id
    )
    await session.commit()
    return project.id


async def discard_pool_entry(session: AsyncSession, project: Project) -> None:
    """Delete a pooled demo and its throwaway user. Commits."""
    from tripl.services import demo_service

    pool_user_id = project.created_by_user_id
    await demo_service._purge_audit_trail(session, project)
    await project_service.purge_project_rows(session, project)
    if pool_user_id is not None:
        await session.execute(
            delete(User).where(
                User.id == pool_user_id,
                User.email.like(f"%@{POOL_USER_EMAIL_DOMAIN}"),
            )
        )
    await session.commit()
