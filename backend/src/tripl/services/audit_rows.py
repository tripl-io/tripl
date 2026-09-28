"""One audit row as an outside system reads it: the export and the webhook (F20).

The same eleven fields, in the same order, for the CSV/NDJSON export
(``audit_export_service``) and the webhook body (``audit_webhook_delivery``),
so a SIEM that ingests both sees one shape. Models only: the worker imports it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from tripl.models.audit_log import AuditLog

#: The columns, in order: the CSV header and the JSON keys.
COLUMNS: Final = (
    "id",
    "created_at",
    "org_slug",
    "project_slug",
    "branch_name",
    "user_email",
    "action",
    "target_type",
    "target_id",
    "target_name",
    "payload",
)


def iso_utc(value: datetime) -> str:
    """ISO 8601 in UTC with a ``Z``; a naive value (SQLite) is taken as UTC."""
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat().replace("+00:00", "Z")


def row_record(row: AuditLog, org_slug: str) -> dict[str, Any]:
    """``row`` as a JSON-ready dict with :data:`COLUMNS` as keys, in order."""
    return {
        "id": str(row.id),
        "created_at": iso_utc(row.created_at),
        "org_slug": org_slug,
        "project_slug": row.project_slug or "",
        "branch_name": row.branch_name or "",
        "user_email": row.user_email or "",
        "action": row.action,
        "target_type": row.target_type,
        "target_id": str(row.target_id) if row.target_id is not None else None,
        "target_name": row.target_name or "",
        "payload": row.payload if isinstance(row.payload, dict) else {},
    }
