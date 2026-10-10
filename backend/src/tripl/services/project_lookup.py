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

from tripl.middleware.org_context import current_org_id, require_org_id
from tripl.models.organization import DEFAULT_ORG_ID
from tripl.models.project import Project

PROJECT_NOT_FOUND = "Project not found"


def owning_org_id() -> uuid.UUID:
    """The bound organization, else the default one: for reads and the audit log.

    Not for creating projects, data sources, API keys or invitations: since F20
    PR5 those columns have no default and their create paths call
    :func:`~tripl.middleware.org_context.require_org_id`, so a write with no
    organization bound fails instead of landing in the default organization.
    """
    return current_org_id() or DEFAULT_ORG_ID


async def project_slug_taken(
    session: AsyncSession,
    organization_id: uuid.UUID,
    slug: str,
    *,
    exclude_project_id: uuid.UUID | None = None,
) -> bool:
    """Whether ``slug`` already names a project of ``organization_id``.

    The availability check behind create and rename (a 409 before the write).
    Per organization since F20 PR5, like ``uq_projects_organization_slug``: the
    same slug in another organization is no conflict. ``exclude_project_id``
    leaves the renamed project itself out.
    """
    statement = select(Project.id).where(
        Project.organization_id == organization_id, Project.slug == slug
    )
    if exclude_project_id is not None:
        statement = statement.where(Project.id != exclude_project_id)
    return await session.scalar(statement.limit(1)) is not None


def project_slug_clause(slug: str) -> ColumnElement[bool]:
    """``Project`` is the project named ``slug`` in the bound organization."""
    return and_(Project.organization_id == require_org_id(), Project.slug == slug)


async def resolve_project(session: AsyncSession, slug: str) -> Project:
    """The bound organization's project ``slug``; 404 :data:`PROJECT_NOT_FOUND` when there is none.

    The detail never echoes the slug: an unknown project and one the caller may
    not see get the same answer from every route, so the 404 reveals nothing.
    """
    project: Project | None = await session.scalar(select(Project).where(project_slug_clause(slug)))
    if project is None:
        raise HTTPException(status_code=404, detail=PROJECT_NOT_FOUND)
    return project


async def resolve_project_id(session: AsyncSession, slug: str) -> uuid.UUID:
    """:func:`resolve_project` for callers that only need the id (one narrow select)."""
    project_id: uuid.UUID | None = await session.scalar(
        select(Project.id).where(project_slug_clause(slug))
    )
    if project_id is None:
        raise HTTPException(status_code=404, detail=PROJECT_NOT_FOUND)
    return project_id
