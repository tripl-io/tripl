"""Demo project generator (orchestration only).

Creates a fully pre-populated project so users can explore the service without
connecting a warehouse. Every seeded row is synthetic — the DataSource never
receives a real query.

This module owns the two-phase, atomic provisioning transaction; the actual
seed shape lives in the versioned, declarative :mod:`tripl.services.demo`
scenario package (focused, independently-testable builders). ``DEMO_RECIPE_VERSION``
is re-exported here so existing callers keep importing it from ``demo_service``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Collection
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import ColumnElement, and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import cache, extensions
from tripl.middleware.org_context import OrgRef, bound_org, require_org_id
from tripl.middleware.request_id import current_request_id
from tripl.models.audit_log import AuditLog
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import ProjectGenerationStatus
from tripl.models.organization import Organization
from tripl.models.project import Project
from tripl.models.user import User
from tripl.schemas.project import DemoCancelResponse, ProjectResponse
from tripl.services import (
    audit_service,
    invitation_service,
    plan_branch_service,
    project_lookup,
    project_member_service,
    project_service,
)
from tripl.services._celery_dispatch import dispatch
from tripl.services.demo import (
    DEMO_RECIPE_VERSION,
    DEMO_SEED,
    DemoContext,
    seed_demo_content,
)

__all__ = [
    "DEMO_RECIPE_VERSION",
    "MAX_DEMOS_PER_CREATOR",
    "create_demo_project",
    "request_demo_cancel",
    "reset_demo_project",
]

logger = logging.getLogger(__name__)

# Stable log event names so demo provisioning/reset failures are greppable and
# alertable in aggregated logs regardless of the underlying exception type.
DEMO_PROVISION_FAILED_EVENT = "demo.provision.failed"
DEMO_PROVISION_CANCELLED_EVENT = "demo.provision.cancelled"
DEMO_RESET_FAILED_EVENT = "demo.reset.failed"
DEMO_SHELL_SWEPT_EVENT = "demo.shell.swept"

# Cancellation handshake. The create is one long blocking request, so a client
# abort only kills the browser's read of the response — the server would happily
# finish and materialise a workspace the user explicitly abandoned.
# `request_demo_cancel` instead flags the committed phase-1 shell with this stage
# from a SECOND request; phase 2 re-reads it before promoting and deletes itself
# if the flag is set. Writing a non-key column does not conflict with the FK
# row locks the in-flight seed holds, so the flag lands while seeding runs.
DEMO_CANCEL_REQUESTED_STAGE = "cancel_requested"

# How many live (seeding or ready) demos one creator may hold at once. Demos are
# synthetic workspaces that aggregate into the real workspace roll-ups, so an
# unbounded generator turns an exploratory click into permanent pollution.
# Reset/delete are the intended way to get a fresh one.
MAX_DEMOS_PER_CREATOR = 3

# A `failed` shell is diagnostic residue: hidden from every list, holding its
# slug forever. Keep it long enough to investigate a report, then reclaim it.
FAILED_SHELL_RETENTION_DAYS = 7

# A shell still `seeding` after this long was abandoned mid-provision (the worker
# process died between the phase-1 commit and either promotion or the failure
# marker). Comfortably above the ~11 s the seed takes and the 90 s client bound,
# so an actively-seeding shell can never fall inside it.
STALLED_SEEDING_HOURS = 1


def _demo_clock() -> datetime:
    """The demo's seed instant, floored to the hour so series land on buckets."""
    return datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)


def _demo_project_name(taken: Collection[str]) -> str:
    """Distinguishable name per demo, so N demos are not N identical cards.

    The first demo keeps the plain product name; later ones are numbered.
    The number is the lowest one not already used by the
    creator's live demos, NOT their count: after deleting ``Demo Project`` from
    a pair, a count of one would mint a second ``Demo Project 2`` next to the
    survivor. Reusing the freed low number is deliberate.
    """
    if "Demo Project" not in taken:
        return "Demo Project"
    number = 2
    while f"Demo Project {number}" in taken:
        number += 1
    return f"Demo Project {number}"


def _new_demo_project(
    *,
    slug: str,
    created_by: uuid.UUID | None,
    organization_id: uuid.UUID,
    name: str = "Demo Project",
) -> Project:
    """The demo's Project row, shared by create and reset so they cannot drift.

    ``organization_id`` is explicit (F20 PR5): the column has no default, and a
    demo must land in the organization it was created or reset in.
    """
    return Project(
        name=name,
        slug=slug,
        description=(
            "A pre-populated demo workspace. "
            "Explore events, metrics, signals, and distribution drift "
            "without connecting a warehouse."
        ),
        is_demo=True,
        demo_recipe_version=DEMO_RECIPE_VERSION,
        generation_status=ProjectGenerationStatus.seeding.value,
        generation_stage="init",
        created_by_user_id=created_by,
        organization_id=organization_id,
    )


async def create_demo_project(
    session: AsyncSession,
    *,
    created_by: uuid.UUID | None = None,
    slug: str | None = None,
) -> ProjectResponse:
    """Start provisioning a demo workspace; the worker seeds it.

    Phase 1, here: commit a hidden ``seeding`` project shell (the provisioning
    marker) with its creator's membership, then hand the seed to the Celery
    worker. The response is that shell, ``generation_status="seeding"``; the
    client polls the project until it reads ``ready`` or ``failed``.

    Seeding takes seconds of CPU, and it used to run inside this request. On a
    public demo a burst of sign-ups then held every API worker at once and the
    whole app stalled for everyone; on the worker the burst is a queue instead.
    Phase 2 — :func:`finish_demo_provision` — keeps the atomicity it had: a
    failure or a cancel leaves no partial demo behind.
    """
    explicit_slug = slug is not None
    if slug is None:
        # Unique slug so repeated create calls never collide.
        slug = f"demo-{uuid.uuid4().hex[:6]}"

    # Reclaim long-dead failed shells before minting another one. Failed shells
    # only ever appear on this path, so this is also the only path that needs to
    # sweep them — no extra scheduled job to keep alive.
    await _sweep_failed_demo_shells(session)

    # The organization the demo is created in; nothing is written without one.
    organization_id = require_org_id()
    live_names = await _live_demo_names(session, created_by, organization_id)
    live = len(live_names)
    if created_by is not None and live >= MAX_DEMOS_PER_CREATOR:
        raise HTTPException(
            status_code=409,
            detail=(
                f"You already have {live} demo workspaces (the limit is "
                f"{MAX_DEMOS_PER_CREATOR}). Reset or delete one before generating another."
            ),
        )

    if created_by is not None and not explicit_slug:
        # A demo an extension seeded ahead of time (the Enterprise demo pool).
        ready = await extensions.claim_ready_demo(
            session,
            visitor_id=created_by,
            organization_id=organization_id,
            name=_demo_project_name(live_names),
        )
        if ready is not None:
            extension, claimed = ready
            # Committed by ``_record_claim`` with the claim's audit restamp.
            await invitation_service.share_demo_with_colleagues(session, claimed)
            await _record_claim(session, claimed, created_by)
            await extension.on_ready_demo_claimed()
            return await project_service.get_project(session, claimed.slug)

    project = _new_demo_project(
        slug=slug,
        created_by=created_by,
        organization_id=organization_id,
        name=_demo_project_name(live_names),
    )
    session.add(project)
    await session.flush()
    project_id = project.id
    await plan_branch_service.ensure_main_branch_id(session, project_id)
    # The creator is the demo's one member (a non-member cannot see a project),
    # so they can poll the shell, cancel it, and see a failure marker.
    if created_by is not None:
        await project_member_service.grant_membership(
            session, project_id=project_id, user_id=created_by, added_by_user_id=created_by
        )
    await session.commit()
    await cache.delete_prefix(cache.prefix_projects())

    try:
        await enqueue_demo_seed(session, project_id, current_request_id())
    except Exception as exc:
        # No broker, no seed: say so on the shell rather than leaving it seeding
        # forever. Phase 2 never ran, so there is nothing to roll back.
        logger.warning(
            "%s slug=%s request_id=%s error=broker_unavailable",
            DEMO_PROVISION_FAILED_EVENT,
            slug,
            current_request_id() or "-",
            exc_info=exc,
        )
        await _mark_failed(session, project_id, "The worker could not be reached.")
        raise HTTPException(status_code=503, detail="Demo provisioning is unavailable") from exc
    # The seed may already have moved the shell on (an eager worker): read its
    # columns fresh rather than from this session's identity map.
    session.expire(project)
    try:
        return await project_service.get_project(session, slug)
    except HTTPException as exc:
        # Only when the seed ran before this read (an eager worker) and a
        # cancel discarded the shell: there is nothing left to return.
        if exc.status_code == 404:
            raise HTTPException(status_code=409, detail="Demo provisioning was cancelled") from exc
        raise


async def _record_claim(session: AsyncSession, project: Project, user_id: uuid.UUID) -> None:
    """File the visitor's ``project.create`` for a ready demo handed out (an extension's pool).

    The seed filed one for the pool's throwaway user, which the claim moved to
    the visitor; it is restamped to now, because the visitor generated the demo
    now, not when the pool seeded it.
    """
    await session.execute(
        update(AuditLog)
        .where(
            AuditLog.project_id == project.id,
            AuditLog.action == "project.create",
            AuditLog.user_id == user_id,
        )
        .values(created_at=datetime.now(tz=UTC))
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def enqueue_demo_seed(
    session: AsyncSession, project_id: uuid.UUID, request_id: str | None
) -> None:
    """Hand phase 2 to the worker. A module attribute, so tests can run it inline.

    ``session`` is unused here; the inline test double seeds on its engine. The
    request id travels with the task, so a seed failure logged on the worker
    still correlates with the request that started it.
    """
    del session
    from tripl.worker.tasks.demo_provision import seed_demo_project

    await dispatch(seed_demo_project.delay, str(project_id), request_id)


async def _mark_failed(session: AsyncSession, project_id: uuid.UUID, error: str) -> None:
    failed = await session.get(Project, project_id)
    if failed is not None:
        failed.generation_status = ProjectGenerationStatus.failed.value
        failed.generation_stage = None
        failed.generation_error = error
        await session.commit()
    await cache.delete_prefix(cache.prefix_projects())


async def finish_demo_provision(session: AsyncSession, project_id: uuid.UUID) -> str:
    """Phase 2: seed the shell's content and promote it to ``ready``.

    Runs on the worker. Returns what became of the shell: ``ready``,
    ``failed``, ``cancelled``, or ``skipped`` when there is no seeding shell
    to finish — a redelivered task, or a shell a cancel already removed.

    Everything is seeded in ONE transaction and committed only at the end, so a
    failure rolls the partial seed back and marks the shell ``failed`` (still
    hidden from the project list), and a cancel discards it.
    """
    shell = await session.get(Project, project_id)
    if shell is None or shell.generation_status != ProjectGenerationStatus.seeding.value:
        return "skipped"
    slug = shell.slug
    created_by = shell.created_by_user_id
    organization_id = shell.organization_id
    name = shell.name
    branch_id = await plan_branch_service.ensure_main_branch_id(session, project_id)
    now = _demo_clock()
    org_slug = await session.scalar(
        select(Organization.slug).where(Organization.id == organization_id)
    )
    # The worker has no request, so nothing bound the demo's organization. Bind
    # it the way the auth dependency would, for any lookup the seed makes.
    with bound_org(OrgRef(id=organization_id, slug=org_slug or "")):
        return await _seed_and_promote(
            session,
            project_id=project_id,
            branch_id=branch_id,
            slug=slug,
            name=name,
            now=now,
            created_by=created_by,
        )


async def _seed_and_promote(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    slug: str,
    name: str,
    now: datetime,
    created_by: uuid.UUID | None,
) -> str:
    try:
        await _seed_demo_content(
            session,
            project_id=project_id,
            branch_id=branch_id,
            slug=slug,
            now=now,
            created_by=created_by,
        )
    except Exception as exc:
        # Diagnosable failure: a stable event name plus the full traceback and
        # the failing DB statement/constraint. The client sees only the
        # ``failed`` status and a generic message.
        logger.warning(
            "%s slug=%s request_id=%s error=%s detail=%s",
            DEMO_PROVISION_FAILED_EVENT,
            slug,
            current_request_id() or "-",
            type(exc).__name__,
            _db_failure_detail(exc),
            exc_info=exc,
        )
        await session.rollback()
        await _mark_failed(session, project_id, _safe_generation_error(exc))
        return "failed"

    # Cancellation is decided here, at the one atomic decision point: everything
    # seeded above is still uncommitted (the search builder reindexes with
    # ``commit=False`` for exactly this), so abandoning it costs a rollback and
    # the shell delete. The trail purge is belt and braces: a cancelled demo
    # never existed, so nothing it wrote may outlive it in the workspace-wide
    # audit view.
    if await _cancel_requested(session, project_id):
        await session.rollback()
        cancelled = await session.get(Project, project_id)
        if cancelled is not None:
            await _purge_audit_trail(session, cancelled)
            await project_service.purge_project_rows(session, cancelled)
            await session.commit()
        await cache.delete_prefix(cache.prefix_projects())
        await cache.delete_prefix(cache.prefix_data_sources())
        logger.info("%s slug=%s", DEMO_PROVISION_CANCELLED_EVENT, slug)
        return "cancelled"

    ready = await session.get(Project, project_id)
    if ready is None:
        return "cancelled"
    ready.generation_status = ProjectGenerationStatus.ready.value
    ready.generation_stage = None
    ready.generation_error = None
    ready.demo_seeded_at = now
    # Colleagues invited into this organization (public demo) see it too.
    await invitation_service.share_demo_with_colleagues(session, ready)
    # A demo is a project, and generating one is a person's decision — so it
    # files the same action a hand-made project does. The recipe's own
    # backfilled rows are the ones marked ``demo_seed``; this one is not.
    creator = await session.get(User, created_by) if created_by is not None else None
    await audit_service.record(
        session,
        user=creator,
        action="project.create",
        target_type="project",
        target_id=project_id,
        target_name=name,
        project=ready,
        payload={"slug": slug, "name": name, "is_demo": True},
        commit=False,
    )
    await session.commit()
    await cache.delete_prefix(cache.prefix_projects())
    await cache.delete_prefix(cache.prefix_data_sources())
    return "ready"


# How recent a ready demo must be for a cancel that found nothing in flight to
# report it as the create it raced (``state="finished"``). Generous next to the
# client's provisioning timeout, so a slow cancel request still says "it
# finished" rather than "nothing happened"; an older demo is some earlier one.
DEMO_CANCEL_FINISHED_WINDOW = timedelta(minutes=10)


async def request_demo_cancel(
    session: AsyncSession, *, created_by: uuid.UUID | None = None
) -> DemoCancelResponse:
    """Ask this user's in-flight demo provision to abandon itself.

    Runs in its own (short) request while the create's long phase-2 transaction
    is still open: it flags the already-committed shell, and the create checks
    that flag before promoting. Returns ``cancelled=False`` when there is no
    seeding shell to flag — the create either already finished or never got far
    enough — so the caller can say so instead of implying a rollback that did
    not happen.

    ``state`` tells those two apart: ``finished`` when a demo of this
    user's became ready within :data:`DEMO_CANCEL_FINISHED_WINDOW`, so the UI can
    promise it is in the list, and ``none`` otherwise.
    """
    if created_by is None:
        return DemoCancelResponse(cancelled=False, slug=None, state="none")
    # Only the bound organization's demos (F20 PR5): a user in two organizations
    # cancelling under B must neither abort nor be handed back a demo in A.
    organization_id = require_org_id()

    in_flight = (
        (
            await session.execute(
                select(Project).where(
                    Project.is_demo.is_(True),
                    Project.generation_status == ProjectGenerationStatus.seeding.value,
                    Project.created_by_user_id == created_by,
                    Project.organization_id == organization_id,
                )
            )
        )
        .scalars()
        .all()
    )
    if not in_flight:
        finished = (
            await session.execute(
                select(Project.slug)
                .where(
                    Project.is_demo.is_(True),
                    Project.generation_status == ProjectGenerationStatus.ready.value,
                    Project.created_by_user_id == created_by,
                    Project.organization_id == organization_id,
                    Project.created_at >= datetime.now(UTC) - DEMO_CANCEL_FINISHED_WINDOW,
                )
                .order_by(Project.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if finished is not None:
            return DemoCancelResponse(cancelled=False, slug=finished, state="finished")
        return DemoCancelResponse(cancelled=False, slug=None, state="none")

    for shell in in_flight:
        shell.generation_stage = DEMO_CANCEL_REQUESTED_STAGE
    await session.commit()
    return DemoCancelResponse(cancelled=True, slug=in_flight[0].slug, state="stopped")


async def _cancel_requested(session: AsyncSession, project_id: uuid.UUID) -> bool:
    """Re-read the shell's stage from the database, bypassing the identity map.

    A column-only SELECT always emits SQL, so this sees the cancel committed by
    the other request; ``session.get`` could hand back a cached instance loaded
    before it.
    """
    stage = await session.scalar(select(Project.generation_stage).where(Project.id == project_id))
    return stage == DEMO_CANCEL_REQUESTED_STAGE


async def _live_demo_names(
    session: AsyncSession, created_by: uuid.UUID | None, organization_id: uuid.UUID
) -> list[str]:
    """Names of the demos this creator currently holds against their cap in one organization.

    The cap is counted per organization (F20 PR5): a user who belongs to two
    organizations holds up to ``MAX_DEMOS_PER_CREATOR`` demos in each, and the
    demos of one organization never block or number the other's.

    One row per demo, so ``len`` is the cap count and the names feed
    :func:`_demo_project_name`.

    Failed shells are residue, not demos. A shell abandoned mid-seed (the process
    died between the phase-1 commit and either promotion or the failure marker)
    is residue too — counting it would let a crash permanently consume a slot the
    user cannot see, let alone free.
    """
    if created_by is None:
        return []
    stall_cutoff = datetime.now(tz=UTC) - timedelta(hours=STALLED_SEEDING_HOURS)
    rows = await session.scalars(
        select(Project.name).where(
            Project.is_demo.is_(True),
            Project.organization_id == organization_id,
            Project.created_by_user_id == created_by,
            Project.generation_status != ProjectGenerationStatus.failed.value,
            (Project.generation_status != ProjectGenerationStatus.seeding.value)
            | (Project.created_at >= stall_cutoff),
        )
    )
    return list(rows.all())


async def _sweep_failed_demo_shells(session: AsyncSession) -> int:
    """Delete provisioning shells that will never become a workspace.

    Two kinds: ``failed`` shells past the retention window, and ``seeding`` shells
    long past any plausible in-flight provision (~11 s of work, 90 s client
    bound). The stall horizon is hours, so an actively-seeding shell is never in
    range. Committed separately from the create that triggered it, so a later
    seed failure can never resurrect the rows this reclaimed.
    """
    failed_cutoff = datetime.now(tz=UTC) - timedelta(days=FAILED_SHELL_RETENTION_DAYS)
    stall_cutoff = datetime.now(tz=UTC) - timedelta(hours=STALLED_SEEDING_HOURS)
    stale = (
        (
            await session.execute(
                select(Project).where(
                    Project.is_demo.is_(True),
                    (
                        (Project.generation_status == ProjectGenerationStatus.failed.value)
                        & (Project.created_at < failed_cutoff)
                    )
                    | (
                        (Project.generation_status == ProjectGenerationStatus.seeding.value)
                        & (Project.created_at < stall_cutoff)
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    if not stale:
        return 0
    for shell in stale:
        # A shell that never became a workspace keeps no trail either: a seed
        # that got as far as writing audit rows must not leave them orphaned.
        await _purge_audit_trail(session, shell)
        await project_service.purge_project_rows(session, shell)
    await session.commit()
    logger.info(
        "%s count=%d slugs=%s failed_cutoff=%s stalled_cutoff=%s",
        DEMO_SHELL_SWEPT_EVENT,
        len(stale),
        ",".join(shell.slug for shell in stale),
        failed_cutoff.isoformat(),
        stall_cutoff.isoformat(),
    )
    await cache.delete_prefix(cache.prefix_projects())
    return len(stale)


async def reset_demo_project(
    session: AsyncSession, slug: str, *, created_by: uuid.UUID | None = None
) -> ProjectResponse:
    """Restore a demo to its freshly-seeded state under the same slug, atomically.

    The old demo and its replacement live in ONE transaction: the existing rows
    are dropped, a fresh demo is seeded under the same slug, and only then is
    anything committed. If seeding fails the rollback puts the original demo back
    exactly as it was, so a transient error can never trade a working demo for a
    hidden failed shell.

    Re-seeding in place — rather than seeding a replacement elsewhere and swapping
    it in — is deliberate: the seed derives the synthetic warehouse's name and the
    search documents from the slug, so content seeded under a temporary slug would
    carry that temporary slug. The original creator is preserved so ownership-based
    management still applies afterwards.
    """
    project = await project_lookup.resolve_project(session, slug)
    if not project.is_demo:
        raise HTTPException(status_code=400, detail="Only demo projects can be reset")
    creator = project.created_by_user_id or created_by
    # Captured before the purge: a reset refreshes the CONTENT, so the workspace
    # keeps the name it was listed under (demos are numbered per creator now).
    name = project.name
    now = _demo_clock()
    # The purge cascades the memberships away with the old row; whoever could see
    # the demo before a reset can see it after.
    grants = await project_member_service.snapshot_grants(session, project.id)
    # The purged row's id keys its cache entries (F20 PR3).
    old_project_id = project.id
    # The replacement stays in the organization the demo lived in.
    organization_id = project.organization_id

    try:
        await _purge_audit_trail(session, project)
        await project_service.purge_project_rows(session, project)
        replacement = _new_demo_project(
            slug=slug, created_by=creator, organization_id=organization_id, name=name
        )
        session.add(replacement)
        await session.flush()
        branch_id = await plan_branch_service.ensure_main_branch_id(session, replacement.id)
        await project_member_service.restore_grants(session, replacement.id, grants)
        if creator is not None:
            await project_member_service.grant_membership(
                session, project_id=replacement.id, user_id=creator, added_by_user_id=creator
            )
        await _seed_demo_content(
            session,
            project_id=replacement.id,
            branch_id=branch_id,
            slug=slug,
            now=now,
            created_by=creator,
        )
        replacement.generation_status = ProjectGenerationStatus.ready.value
        replacement.generation_stage = None
        replacement.generation_error = None
        replacement.demo_seeded_at = now
        await session.commit()
    except Exception as exc:
        logger.warning(
            "%s slug=%s request_id=%s error=%s detail=%s",
            DEMO_RESET_FAILED_EVENT,
            slug,
            current_request_id() or "-",
            type(exc).__name__,
            _db_failure_detail(exc),
            exc_info=exc,
        )
        await session.rollback()
        raise HTTPException(
            status_code=500,
            detail="Demo reset failed. Your existing demo workspace was left unchanged.",
        ) from exc

    await cache.delete_prefix(cache.prefix_projects())
    await cache.delete_prefix(cache.prefix_data_sources())
    await project_service._invalidate_project_caches(old_project_id)
    await project_service._forget_purged_project(old_project_id)
    return await project_service.get_project(session, slug)


async def _purge_audit_trail(session: AsyncSession, project: Project) -> None:
    """Drop the audit rows belonging to the demo a reset is about to replace.

    Deliberately HERE and not in ``project_service.purge_project_rows``: for a
    real project the trail is meant to OUTLIVE the delete. ``audit_log.project_id``
    is ``ON DELETE SET NULL`` and ``project_slug`` is denormalized precisely so an
    entry stays readable once its project is gone — "who deleted what" is the
    thing an audit log exists to keep.

    A demo reset is the one delete that RE-USES the slug, so those surviving rows
    do not stay readable — they pile up behind the replacement's name. Two resets
    and three generations of one project's authoring sit in the log, two of them
    describing a project that no longer exists. The rows are not being preserved,
    they are being orphaned in place.

    Note what this does NOT rely on: ``list_entries`` resolves a slug to a project
    and filters on its id now, so the project tab would hide the
    old rows either way. Hiding is not the same as not having — the workspace-wide
    view shows every row on the instance, and that is where three
    generations of a demo would otherwise be on display.

    The demo's own ``data_source.create`` row goes too, and it needs finding by a
    second rule: it deliberately carries no project (its real route records the
    action instance-wide), so an id-scoped delete cannot see it.
    Left behind it would outlive the warehouse it names — ``purge_project_rows``
    drops that DataSource in this same transaction and writes no ``delete``
    counterpart — so every reset would add another creation of a warehouse that
    no longer exists. Only sources this project OWNS are matched, so a
    workspace-global source a reader connected themselves keeps its trail.

    Runs INSIDE the reset transaction, before the purge, so a seeding failure
    rolls the whole thing back and the old demo returns with its trail intact.
    """
    owned_sources = (
        (await session.execute(select(DataSource.id).where(DataSource.project_id == project.id)))
        .scalars()
        .all()
    )
    scopes: list[ColumnElement[bool]] = [AuditLog.project_id == project.id]
    if owned_sources:
        scopes.append(
            and_(AuditLog.target_type == "data_source", AuditLog.target_id.in_(owned_sources))
        )
    await session.execute(delete(AuditLog).where(or_(*scopes)))


def _safe_generation_error(exc: Exception) -> str:
    """Short, user-safe failure summary — never internals (SQL, secrets, trace)."""
    return f"Demo provisioning failed during seeding ({type(exc).__name__})."


def _db_failure_detail(exc: Exception) -> str:
    """Server-only diagnostics for a seed failure: the failing DB statement and,
    when present, the violated constraint.

    SQLAlchemy wraps the driver error on ``.orig``; asyncpg exposes the offending
    constraint via ``.orig.diag.constraint_name``. Falls back to the exception's
    own text for non-DB failures. Logged only — never returned to a client.
    """
    orig = getattr(exc, "orig", None)
    if orig is None:
        return str(exc)
    constraint = getattr(getattr(orig, "diag", None), "constraint_name", None)
    if constraint:
        return f"{orig} [constraint={constraint}]"
    return str(orig)


async def _seed_demo_content(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    slug: str,
    now: datetime,
    created_by: uuid.UUID | None = None,
) -> None:
    """Thin wrapper: build the scenario context and run the ordered builders.

    Kept as a module-level attribute (not inlined) so provisioning tests can
    monkeypatch it. Does not commit — it runs inside the caller's phase-2
    transaction so a failure rolls back cleanly.
    """
    ctx = DemoContext(
        project_id=project_id,
        branch_id=branch_id,
        slug=slug,
        now=now,
        seed=DEMO_SEED,
        created_by=created_by,
    )
    await seed_demo_content(session, ctx)
