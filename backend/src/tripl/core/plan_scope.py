"""Main-branch scoping for synchronous plan queries (the worker and ``core``).

Scans and metrics collection always operate on a project's MAIN plan, but
working branches carry deep-copied EventType/Event/Variable rows under the same
names (e.g. the seeded demo branch). Any by-name lookup that ignores
``branch_id`` therefore returns multiple rows once a branch exists.

Lives in ``core`` so the event generator, which may not import the worker, uses
the same lookup the worker tasks do. The request path's async
``plan_branch_service.ensure_main_branch_id`` is a different function: it also
creates the branch when it is missing.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from tripl.models.plan_branch import BranchKind, PlanBranch


def main_branch_id(session: Session, project_id: uuid.UUID) -> uuid.UUID | None:
    """Return the id of the project's main plan branch, or None before one exists.

    A None result is safe to use in an equality filter: plan tables carry
    NOT NULL ``branch_id``, so rows can only exist once the main branch does.
    """
    return session.scalar(
        select(PlanBranch.id).where(
            PlanBranch.project_id == project_id,
            PlanBranch.kind == BranchKind.main.value,
        )
    )
