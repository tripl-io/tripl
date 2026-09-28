"""Which projects the scheduled worker jobs may touch (F20 PR14, tripl-oam4.9).

A project belongs to exactly one organization, and only an ``active`` one is
worked on by the beat schedule: scan and metric collection, the freshness
sweep, alert digests and redeliveries, the weekly plan digest, the sunset
alert, lifecycle findings, health snapshots, tracker syncs, notification
emails, demo ticks and the search/embedding sweeps. A ``suspended``
organization is paused, not purged — everything resumes on unsuspend — and a
``deleting`` one is on its way out, so neither gets new work.

One helper for every scheduler query, so "active" cannot mean two things.
Models only: the worker imports it without the request stack.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, select
from sqlalchemy.orm import QueryableAttribute
from sqlalchemy.sql.elements import ColumnElement

from tripl.models.domain_enums import OrganizationStatus
from tripl.models.organization import Organization
from tripl.models.project import Project


def active_org_ids() -> Select[tuple[uuid.UUID]]:
    """``SELECT id FROM organizations WHERE status = 'active'``, for ``IN``."""
    return select(Organization.id).where(Organization.status == OrganizationStatus.active.value)


def active_org_project_ids() -> Select[tuple[uuid.UUID]]:
    """The ids of every project of an ``active`` organization, for ``IN``."""
    return select(Project.id).where(Project.organization_id.in_(active_org_ids()))


def project_in_active_org() -> ColumnElement[bool]:
    """For a statement that already has ``projects`` in its FROM clause."""
    return Project.organization_id.in_(active_org_ids())


def in_active_org(
    project_id: ColumnElement[uuid.UUID] | QueryableAttribute[uuid.UUID],
) -> ColumnElement[bool]:
    """``project_id`` (a column of the enclosing query) is a project of an active org."""
    return project_id.in_(active_org_project_ids())
