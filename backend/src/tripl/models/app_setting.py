from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin, UUIDMixin

# Well-known setting keys. New service-wide settings live under one document so
# the API can return/edit every global section together. ``ai`` is kept as a
# legacy read path for partially applied local migrations from the first cut of
# this feature.
SERVICE_SETTINGS_KEY = "service"
AI_SETTINGS_KEY = "ai"

_OPERATOR_SCOPE = text("organization_id IS NULL")
_ORGANIZATION_SCOPE = text("organization_id IS NOT NULL")


class AppSetting(UUIDMixin, TimestampMixin, Base):
    """Service-wide runtime settings, one JSON document per key.

    Each row stores only the explicitly overridden fields for its domain
    (e.g. key="ai"); anything absent falls back to the env-based
    ``tripl.config.Settings`` value. Secret fields inside ``value`` are
    encrypted with :mod:`tripl.crypto` before storage.

    ``organization_id`` NULL is the operator (instance) scope. A non-NULL value
    is an organization's own override of the ``ORG_FIELDS`` (F20 PR9), resolved
    org -> operator -> env by ``app_settings_service``. Uniqueness is per
    scope, so each query MUST say which scope it means: a lookup by ``key`` alone
    would pick an arbitrary row once organization rows exist.
    """

    __tablename__ = "app_settings"
    __table_args__ = (
        Index(
            "uq_app_settings_operator_key",
            "key",
            unique=True,
            postgresql_where=_OPERATOR_SCOPE,
            sqlite_where=_OPERATOR_SCOPE,
        ),
        Index(
            "uq_app_settings_organization_key",
            "organization_id",
            "key",
            unique=True,
            postgresql_where=_ORGANIZATION_SCOPE,
            sqlite_where=_ORGANIZATION_SCOPE,
        ),
    )

    key: Mapped[str] = mapped_column(String(100))
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, default=None
    )
