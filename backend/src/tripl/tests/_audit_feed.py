"""The organization's audit rows as the API would list them, read from the table.

The organization-wide feed (``GET /audit``) is part of the Enterprise edition;
Community serves a project's history only. Tests that check a row written
outside any project, or the whole trail at once, read it here instead.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import desc, select

from tripl.models.audit_log import AuditLog
from tripl.schemas.audit import AuditEntryDetailResponse, AuditEntryResponse
from tripl.tests.conftest import TestSessionLocal


async def org_audit(action: str | None = None, *, payload: bool = False) -> list[dict[str, Any]]:
    """Every row (of an action), newest first, in the list response's shape, or
    the detail response's with ``payload``."""
    query = select(AuditLog).order_by(desc(AuditLog.created_at), desc(AuditLog.id))
    if action is not None:
        query = query.where(AuditLog.action == action)
    async with TestSessionLocal() as session:
        rows = (await session.scalars(query)).all()
    shape = AuditEntryDetailResponse if payload else AuditEntryResponse
    return [shape.model_validate(row).model_dump(mode="json") for row in rows]
