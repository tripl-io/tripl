"""The one place a project slug becomes a project (F20 PR2, GH #273).

A slug names a project only inside an organization, so every lookup here is
filtered by the organization bound for this request
(:func:`tripl.middleware.org_context.require_org_id`), and raises
:class:`~tripl.middleware.org_context.OrgContextMissing` when none is bound —
never a silent default. Code elsewhere must not compare ``Project.slug`` itself;
``tests/test_project_slug_guard.py`` enforces that. Inside a single query that
joins ``Project``, filter with :func:`project_slug_clause`.

This module imports models and the org context only, so any service (the
model-only :mod:`tripl.services.project_access` included) can import it at
module level.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import ColumnElement, and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.middleware.org_context import require_org_id
from tripl.models.project import Project

PROJECT_NOT_FOUND = "Project not found"


def project_slug_clause(slug: str) -> ColumnElement[bool]:
    """``Project`` is the project named ``slug`` in the bound organization."""
    return and_(Project.organization_id == require_org_id(), Project.slug == slug)


async def resolve_project(
    session: AsyncSession,
    slug: str,
    *,
    detail: str = PROJECT_NOT_FOUND,
) -> Project:
    """The bound organization's project ``slug``; 404 ``detail`` when there is none."""
    project: Project | None = await session.scalar(select(Project).where(project_slug_clause(slug)))
    if project is None:
        raise HTTPException(status_code=404, detail=detail)
    return project


async def resolve_project_id(
    session: AsyncSession,
    slug: str,
    *,
    detail: str = PROJECT_NOT_FOUND,
) -> uuid.UUID:
    """:func:`resolve_project` for callers that only need the id (one narrow select)."""
    project_id: uuid.UUID | None = await session.scalar(
        select(Project.id).where(project_slug_clause(slug))
    )
    if project_id is None:
        raise HTTPException(status_code=404, detail=detail)
    return project_id


# Transition aliases, org-filtered like the functions they name, so a call site
# not yet converted is still correct. Deleted once every caller has moved.
get_project_by_slug = resolve_project
get_project_id_by_slug = resolve_project_id
