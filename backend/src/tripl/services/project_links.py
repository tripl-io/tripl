"""Links to a project's pages that name its organization (F20 PR8, GH #273).

Since F20 PR5 a project slug is unique only inside its organization, so a link
of the form ``/p/{slug}/...`` names a project only relative to whichever
organization the reader happens to be in. Every link the server emits therefore
carries the organization too: ``/o/{org_slug}/p/{project_slug}/...``.

* :func:`project_url` is the one builder. Relative (``/o/...``) by default,
  absolute when the caller hands it ``base_url`` (``app_base_url``: alert deep
  links, emails).
* :func:`qualify_project_path` upgrades an org-less ``/p/{slug}/...`` path to the
  org-qualified form. It exists for stored text that must NOT depend on the org
  (critique #21): search documents keep their org-less ``route_path`` because it
  is part of ``content_hash`` and a change would re-embed the whole corpus, and
  stored incident-summary facts and notification rows written before this
  release still hold ``/p/`` links. Such paths are qualified when they are read.
* The lookups resolve the organization slug of a project by id, for callers
  (workers, notification producers) that have no request organization.

Organization slugs are immutable (owner decision 6, enforced by
``OrganizationUpdate``'s ``extra="forbid"``), so a link built here never needs
rewriting after it is stored or sent. Invitation links (``/invite/{token}``)
are not project pages and do not go through this module.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from tripl.models.organization import Organization
from tripl.models.project import Project

__all__ = [
    "legacy_project_path",
    "project_link",
    "project_link_slugs",
    "project_link_slugs_sync",
    "project_org_slugs",
    "project_org_slugs_sync",
    "project_url",
    "qualify_project_path",
]

_LEGACY_PREFIX = "/p/"


def project_url(org_slug: str, project_slug: str, path: str = "", *, base_url: str = "") -> str:
    """``{base_url}/o/{org_slug}/p/{project_slug}{path}``.

    ``path`` is the part after the project (``/alerting``, ``/events/detail/<id>``,
    optionally with a query string) or empty for the project's root. Without
    ``base_url`` the link is relative, which is what the API and stored
    notifications carry; with it the link is absolute, for messages read outside
    the app.
    """
    if path and not path.startswith(("/", "?")):
        path = f"/{path}"
    return f"{base_url.rstrip('/')}/o/{org_slug}/p/{project_slug}{path}"


def legacy_project_path(project_slug: str, path: str = "") -> str:
    """The org-less ``/p/{project_slug}{path}`` form.

    Only for text whose hash must not depend on the organization (search
    documents' ``route_path``); qualify it with :func:`qualify_project_path`
    before it leaves the server.
    """
    if path and not path.startswith(("/", "?")):
        path = f"/{path}"
    return f"{_LEGACY_PREFIX}{project_slug}{path}"


def qualify_project_path(org_slug: str, path: str) -> str:
    """``/p/{slug}/...`` → ``/o/{org_slug}/p/{slug}/...``; anything else unchanged.

    Idempotent: an already org-qualified path, an absolute URL and an empty
    string come back as they went in.
    """
    if path.startswith(_LEGACY_PREFIX):
        return f"/o/{org_slug}{path}"
    return path


def _slugs_stmt(project_ids: Iterable[uuid.UUID]) -> Select[uuid.UUID, str, str]:
    return (
        select(Project.id, Organization.slug, Project.slug)
        .join(Organization, Organization.id == Project.organization_id)
        .where(Project.id.in_(set(project_ids)))
    )


async def project_link_slugs(
    session: AsyncSession, project_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, str]]:
    """``project_id → (org_slug, project_slug)`` for the given projects."""
    ids = set(project_ids)
    if not ids:
        return {}
    rows = (await session.execute(_slugs_stmt(ids))).all()
    return {project_id: (org_slug, slug) for project_id, org_slug, slug in rows}


def project_link_slugs_sync(
    session: Session, project_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, str]]:
    """The sync twin of :func:`project_link_slugs`, for Celery tasks."""
    ids = set(project_ids)
    if not ids:
        return {}
    rows = session.execute(_slugs_stmt(ids)).all()
    return {project_id: (org_slug, slug) for project_id, org_slug, slug in rows}


async def project_org_slugs(
    session: AsyncSession, project_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """``project_id → org_slug`` for the given projects."""
    return {
        project_id: org_slug
        for project_id, (org_slug, _slug) in (
            await project_link_slugs(session, project_ids)
        ).items()
    }


def project_org_slugs_sync(
    session: Session, project_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """The sync twin of :func:`project_org_slugs`."""
    return {
        project_id: org_slug
        for project_id, (org_slug, _slug) in project_link_slugs_sync(session, project_ids).items()
    }


async def project_link(session: AsyncSession, project_id: uuid.UUID, path: str = "") -> str:
    """The relative org-qualified link to ``path`` in the project with this id.

    Raises ``LookupError`` for a project that does not exist.
    """
    slugs = (await project_link_slugs(session, [project_id])).get(project_id)
    if slugs is None:
        raise LookupError(f"Project {project_id} not found")
    return project_url(slugs[0], slugs[1], path)
