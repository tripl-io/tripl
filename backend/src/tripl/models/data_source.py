from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tripl.models.base import Base, TimestampMixin, UUIDMixin
from tripl.models.enum_types import db_enum
from tripl.models.organization import DEFAULT_ORG_ID, default_org_server_default

if TYPE_CHECKING:
    from tripl.models.scan_config import ScanConfig


class DBType(enum.StrEnum):
    clickhouse = "clickhouse"
    postgres = "postgres"
    bigquery = "bigquery"
    # Local, in-memory synthetic warehouse. Created ONLY by the demo seeder (never
    # by the user-facing create path) and always scoped to a demo project. Its
    # adapter serves a bounded, deterministic dataset with no network/filesystem
    # access, so the normal warehouse-facing paths run against a fake source.
    synthetic = "synthetic"


class TestStatus(enum.StrEnum):
    success = "success"
    failed = "failed"


class DataSource(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "data_sources"
    __table_args__ = (UniqueConstraint("name", name="uq_data_source_name"),)

    # Ownership. NULL = workspace-global source (the normal case, shared across
    # projects). A non-NULL owner scopes this source to one project — used by
    # generated demo workspaces so their synthetic warehouse is cleaned up with
    # the project (ON DELETE CASCADE) instead of leaking a workspace-wide orphan.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, default=None, index=True
    )
    # F20 PR1: the owning organization. Always the default one for now — the
    # ORM default and the server default both name it (see models/organization).
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        default=DEFAULT_ORG_ID,
        server_default=default_org_server_default(),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255))
    db_type: Mapped[str] = mapped_column(db_enum(DBType, "data_source_db_type"))
    host: Mapped[str] = mapped_column(String(500))
    port: Mapped[int] = mapped_column(Integer, default=8123)
    database_name: Mapped[str] = mapped_column(String(255))
    username: Mapped[str] = mapped_column(String(255), default="")
    password_encrypted: Mapped[str] = mapped_column(Text, default="")
    timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    # ClickHouse-only: which path-enumeration function the JSON path *discovery*
    # (preview) query uses — "all" (JSONAllPaths, every path incl. shared data) or
    # "dynamic" (JSONDynamicPaths, only the important typed subcolumn paths, much
    # faster). NULL falls back to the adapter default ("dynamic"). Ignored by
    # Postgres/BigQuery. Does not affect scan-time value extraction.
    json_path_discovery: Mapped[str | None] = mapped_column(String(16), nullable=True, default=None)
    extra_params: Mapped[dict[str, object] | None] = mapped_column(sa.JSON, nullable=True)

    last_test_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    last_test_status: Mapped[str | None] = mapped_column(
        db_enum(TestStatus, "data_source_test_status"), nullable=True, default=None
    )
    last_test_message: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)

    scan_configs: Mapped[list[ScanConfig]] = relationship(
        back_populates="data_source", cascade="all, delete-orphan"
    )
