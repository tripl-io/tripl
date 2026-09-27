"""Row locks on ``plan_branches`` that serialise plan writes against a merge.

One mechanism for three merge-time races (tripl-0zpq.288, .290 and .294): the
``plan_branches`` row of the branch a write lands on is the lock for that
branch's plan.

* A plan write takes its branch's row ``FOR SHARE`` in the request's own
  transaction and holds it until that transaction ends. Writes to one branch
  share the lock, so they never wait for each other.
* A merge already takes the merged branch's row ``FOR UPDATE``
  (``plan_branch_merge_service._lock_branch_for_merge``) and now also takes
  MAIN's row ``FOR NO KEY UPDATE`` (:func:`lock_main_plan_for_merge`) before it
  reads main for the conflict check, holding both to its commit.

``FOR SHARE`` conflicts with both of the merge's locks, so every plan write is
ordered wholly before or wholly after a merge of its branch or into main: a
write that arrives mid-merge waits for the merge to commit and then re-reads the
row (PostgreSQL returns the newest committed version to a locking read at READ
COMMITTED), sees ``merged`` and is refused — or, on main, applies on top of the
merged plan; a merge that arrives mid-write waits for the write to commit and
then snapshots a plan that includes it.

Why main's lock is ``FOR NO KEY UPDATE`` and not ``FOR UPDATE``: every insert of
a main-scoped row (an event, a type, a variable) takes ``FOR KEY SHARE`` on
main's row for its foreign key, and that is compatible with ``NO KEY UPDATE``
but not with ``UPDATE``. So a scan inserting events on main in a worker is not
queued behind a merge it has nothing to do with; only the writes that took the
explicit ``FOR SHARE`` are.

DEADLOCK AUDIT (tripl-0zpq.288). Two ``FOR SHARE`` holders that both go on to
UPDATE the same row deadlock. No write path that takes this lock updates the
``plan_branches`` row: the rows it is written from are the transition route
(status), the merge (status, ``merged_*``) and branch deletion, and none of
them resolves its branch through ``?branch=``, the comment path or the photo
path. The merge, which does update the row, takes ``FOR UPDATE`` up front
rather than ``FOR SHARE`` and upgrading. Anything new that wants to change a
branch row inside a plan write must take ``FOR UPDATE`` there instead.

Lock order: a merge locks its branch, then main. A plan write takes exactly one
of these rows. A write that held main's row and then needed a WORKING branch's
row for a foreign key while a merge of that branch waited on main would
deadlock. No plan write found in the audit inserts rows pointing at a branch
other than its own, and PostgreSQL would abort one side with 40P01 rather than
hang if one ever did.

PostgreSQL only, like ``_lock_branch_for_merge`` and
``demo_runtime._acquire_project_xact_lock``: SQLite (the unit suite) has one
writer at a time and no row locks, so the lock clause is left off there. The
row is still read afresh, so callers see the same row either way.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.plan_branch import BranchKind, PlanBranch

__all__ = [
    "hold_branch_for_plan_write",
    "hold_main_plan_for_write",
    "lock_main_plan_for_merge",
    "locks_rows",
]


def locks_rows(session: AsyncSession) -> bool:
    """Whether ``session`` talks to a database these row locks mean anything on."""
    return session.get_bind().dialect.name == "postgresql"


async def hold_branch_for_plan_write(
    session: AsyncSession, branch_id: uuid.UUID, *, working_only: bool = False
) -> PlanBranch | None:
    """Re-read the branch row, holding it ``FOR SHARE`` to the end of the transaction.

    Returns the row as it stands once the lock is granted — after any merge that
    held it has committed — so the caller decides on the current status, not on
    one read before the wait. ``populate_existing`` for the same reason
    ``_lock_branch_for_merge`` uses it: an instance already in the identity map
    would otherwise hand back the pre-wait status.

    ``working_only`` skips main (returns ``None`` for it, locking nothing): the
    comment path locks only a working branch, since only a working branch's
    threads move in a merge (tripl-0zpq.290).
    """
    stmt = (
        select(PlanBranch)
        .where(PlanBranch.id == branch_id)
        .execution_options(populate_existing=True)
    )
    if working_only:
        stmt = stmt.where(PlanBranch.kind == BranchKind.working.value)
    if locks_rows(session):
        stmt = stmt.with_for_update(read=True)
    branch: PlanBranch | None = await session.scalar(stmt)
    return branch


async def hold_main_plan_for_write(session: AsyncSession, project_id: uuid.UUID) -> None:
    """Hold the project's main branch row ``FOR SHARE`` for a write to main.

    The main-side half of tripl-0zpq.294: a main edit either commits before a
    merge reads main for its conflict check, or waits until the merge has
    committed and then applies on top of it. It never lands in between, where the
    merge's apply step would overwrite it without reporting a conflict.

    A no-op off PostgreSQL, where it would only cost a query per write. A
    project whose main row does not exist yet has no branch that could be
    merging into it, so there is nothing to wait for.

    Keyed by the project's id, which the caller resolved within its
    organization (F20 PR3): a slug is unique only inside an organization.
    """
    if not locks_rows(session):
        return
    await session.execute(
        select(PlanBranch.id)
        .where(PlanBranch.project_id == project_id, PlanBranch.kind == BranchKind.main.value)
        .with_for_update(read=True, of=PlanBranch)
    )


async def lock_main_plan_for_merge(session: AsyncSession, main_branch_id: uuid.UUID) -> None:
    """Hold main's branch row against plan writes until the merge commits.

    Taken after the merged branch's own row, before main is read for the
    conflict check, and held through ``_apply_merge`` (tripl-0zpq.294). See the
    module docstring for why ``NO KEY UPDATE``.
    """
    if not locks_rows(session):
        return
    await session.execute(
        select(PlanBranch.id).where(PlanBranch.id == main_branch_id).with_for_update(key_share=True)
    )
