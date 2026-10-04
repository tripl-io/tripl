from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class AuditEntryResponse(BaseModel):
    """One row of the audit list — everything the row itself renders.

    Deliberately WITHOUT ``payload``. The tab renders a payload only for the
    rows the reader expanded (AuditTab.tsx), so shipping one per row sent a page
    of JSON blobs across the wire to be displayed nowhere: on the only project
    with real audit history, ``/p/*/audit`` had the slowest first
    content of the 75 routes in the 2026-08-17 walk. The payload now travels one
    row at a time, as ``AuditEntryDetailResponse``.

    ``branch_id`` / ``branch_name`` are the plan branch the write was scoped to.
    Null and empty mean the write was not made through a branch-scoped request —
    main, or an action with no plan-branch dimension (alerting, scans, users) —
    so a reader must not render them as "main".
    """

    id: uuid.UUID
    created_at: datetime
    user_id: uuid.UUID | None
    user_email: str
    project_id: uuid.UUID | None
    project_slug: str
    branch_id: uuid.UUID | None
    branch_name: str
    action: str
    target_type: str
    target_id: uuid.UUID | None
    target_name: str

    model_config = {"from_attributes": True}


class AuditEntryDetailResponse(AuditEntryResponse):
    """One entry WITH the request payload that produced it.

    ``payload`` is the half of an audit entry that carries warehouse hosts and
    ``base_query`` SQL, which is why the whole router is owner-only — see the
    gate write-up in api/v1/audit.py. Secrets are already masked at write time
    by ``audit_service._redact``.
    """

    payload: dict[str, Any]


class AuditListResponse(BaseModel):
    items: list[AuditEntryResponse]
    total: int


class AuditActionGroup(BaseModel):
    """One labelled group of the audit action filter."""

    label: str
    actions: list[str]


class AuditActionCatalog(BaseModel):
    """Every action the audit log records, grouped for the filter.

    ``project`` holds the actions recorded WITH a project — the only ones a
    project-scoped query can match. ``workspace`` holds those recorded with no
    project (data sources, users, instance settings, a deleted project), which
    only the unfiltered workspace feed can match. An action is in exactly one.
    """

    project: list[AuditActionGroup]
    workspace: list[AuditActionGroup]
