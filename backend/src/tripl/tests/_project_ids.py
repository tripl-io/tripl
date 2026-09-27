"""Resolve a test project's id from its slug (F20 PR3).

Not a test module. Cache keys, realtime channels and plan locks are keyed by
project id, so a test that seeds a project over HTTP by slug needs its id to
name the key it expects.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from tripl.models.organization import DEFAULT_ORG_ID
from tripl.models.project import Project
from tripl.tests.conftest import TestSessionLocal


async def project_id_by_slug(slug: str, *, org_id: uuid.UUID = DEFAULT_ORG_ID) -> uuid.UUID:
    """The id of the project ``slug`` in ``org_id`` (the default organization)."""
    async with TestSessionLocal() as session:
        project_id = await session.scalar(
            select(Project.id).where(Project.organization_id == org_id, Project.slug == slug)
        )
    assert project_id is not None, f"no project {slug!r} in organization {org_id}"
    return project_id
