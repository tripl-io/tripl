"""Feature-branch journey builder.

Seeds an API-visible collaboration story: a base ``PlanRevision`` snapshot, one
working ``PlanBranch`` (a feature branch — the kind enum is main/working, so a
working branch with a feature-style name), a top-level ``PlanBranchComment``,
and — new in recipe 4 — a REAL pending change: the main plan is deep-copied
onto the branch and exactly one branch-side edit is applied, so
``/branches/{id}/diff`` shows one modified event and the merge preview is
non-empty. Reachable via ``/branches``, ``/branches/{id}/comments``,
``/branches/{id}/diff``, and ``/revisions``.

The ORM writes mirror ``plan_branch_service.create_branch`` inline because that
service commits internally, which would break the seeder's single end-of-function
commit; the plan copy itself reuses the service's non-committing
``deep_copy_plan_to_branch`` helper so the two paths cannot drift.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.event import Event
from tripl.models.plan_branch import BranchKind, BranchStatus, PlanBranch
from tripl.models.plan_branch_comment import PlanBranchComment
from tripl.models.plan_revision import PlanRevision, PlanRevisionKind
from tripl.services.demo.scenario import DemoContext
from tripl.services.plan_branch_service import deep_copy_plan_to_branch
from tripl.services.plan_revision_service import build_plan_snapshot

_BRANCH_NAME = "feature/checkout-funnel"
_BRANCH_DESCRIPTION = "Redesign the checkout funnel: paywall copy and Buy CTA placement."
_COMMENT_BODY = (
    "Kicking off the checkout funnel redesign. First pass tightens the paywall "
    "copy and moves the Buy CTA above the fold — see the linked Figma spec."
)

# The single branch-side edit (the pending change). Keyed by event name so the
# diff reports exactly one changed event; the copy matches the branch's story —
# the paywall CTA copy moves from "Buy now" to "Start free trial".
CHANGED_EVENT_NAME = "Buy Button Click"
CHANGED_EVENT_DESCRIPTION = (
    "User taps the primary CTA on the paywall. Checkout-funnel redesign: CTA "
    "copy changes from 'Buy now' to 'Start free trial', moved above the fold."
)

# The branch's story on the seed clock, in the order it happened: opened (the
# base snapshot and the branch share the instant), the branch-side edit, then
# the kick-off comment. Dated back from ``ctx.now`` like the rest of the recipe,
# never ahead of it, instead of left to the database's transaction time: the
# audit builder dates its branch rows off these columns, and the audit log, Plan
# history and the branch list then name the same moments.
_BRANCH_OPENED_BEFORE_SEED = timedelta(minutes=21)
_BRANCH_EDITED_BEFORE_SEED = timedelta(minutes=14)
_BRANCH_COMMENTED_BEFORE_SEED = timedelta(minutes=7)


async def build_branches(session: AsyncSession, ctx: DemoContext) -> None:
    opened_at = ctx.now - _BRANCH_OPENED_BEFORE_SEED
    # Merge base: a snapshot of the (now fully-seeded) main plan.
    base_payload = await build_plan_snapshot(session, ctx.project_id, branch_id=ctx.branch_id)
    base_revision = PlanRevision(
        project_id=ctx.project_id,
        created_by=ctx.created_by,
        summary=f"Base snapshot for branch '{_BRANCH_NAME}'",
        kind=PlanRevisionKind.branch_base.value,
        payload=base_payload,
        created_at=opened_at,
    )
    session.add(base_revision)
    await session.flush()

    branch = PlanBranch(
        project_id=ctx.project_id,
        name=_BRANCH_NAME,
        kind=BranchKind.working.value,
        status=BranchStatus.draft.value,
        description=_BRANCH_DESCRIPTION,
        base_revision_id=base_revision.id,
        created_by=ctx.created_by,
        created_at=opened_at,
        updated_at=opened_at,
    )
    session.add(branch)
    await session.flush()
    base_revision.branch_id = branch.id

    # Isolated branch copy of the whole plan, then ONE modification on the copy,
    # so the branch diff is exactly one changed event and nothing else.
    await deep_copy_plan_to_branch(
        session,
        project_id=ctx.project_id,
        source_branch_id=ctx.branch_id,
        target_branch_id=branch.id,
    )
    await session.flush()
    branch_event = (
        await session.execute(
            select(Event).where(
                Event.project_id == ctx.project_id,
                Event.branch_id == branch.id,
                Event.name == CHANGED_EVENT_NAME,
            )
        )
    ).scalar_one()
    branch_event.description = CHANGED_EVENT_DESCRIPTION
    # Set explicitly, so the UPDATE carries it instead of ``onupdate``'s now().
    branch_event.updated_at = ctx.now - _BRANCH_EDITED_BEFORE_SEED

    commented_at = ctx.now - _BRANCH_COMMENTED_BEFORE_SEED
    session.add(
        PlanBranchComment(
            branch_id=branch.id,
            parent_id=None,
            user_id=ctx.created_by,
            body=_COMMENT_BODY,
            created_at=commented_at,
            updated_at=commented_at,
        )
    )
    await session.flush()
