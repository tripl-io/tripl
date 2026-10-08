from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tripl.models.base import Base, TimestampMixin, UUIDMixin
from tripl.models.enum_types import db_enum

if TYPE_CHECKING:
    from tripl.models.scan_config import ScanConfig


class DBType(enum.StrEnum):
    clickhouse = "clickhouse"
    postgres = "postgres"
    bigquery = "bigquery"
    databricks = "databricks"
    snowflake = "snowflake"
    # PostgreSQL-protocol warehouses; see core/adapters/greenplum.py and redshift.py.
    greenplum = "greenplum"
    redshift = "redshift"
    # Trino SQL engines; see core/adapters/trino.py and athena.py.
    trino = "trino"
    athena = "athena"
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
    __table_args__ = (
        # F20 PR5: a name is unique inside an organization, not instance-wide.
        UniqueConstraint("organization_id", "name", name="uq_data_sources_organization_name"),
        # A project-bound source lives in its project's organization. MATCH
        # SIMPLE: a NULL project_id (a workspace-wide source) is not checked.
        # Two FKs now point at ``projects`` (this and ``project_id``'s own), so
        # ``join(Project)`` without an ON clause raises AmbiguousForeignKeysError:
        # join with ``DataSource.project_id == Project.id``, and give any future
        # ``relationship()`` to Project ``foreign_keys=[project_id]``.
        ForeignKeyConstraint(
            ["project_id", "organization_id"],
            ["projects.id", "projects.organization_id"],
            name="fk_data_sources_project_organization",
            ondelete="CASCADE",
        ),
    )

    # Ownership. NULL = workspace-global source (the normal case, shared across
    # projects). A non-NULL owner scopes this source to one project — used by
    # generated demo workspaces so their synthetic warehouse is cleaned up with
    # the project (ON DELETE CASCADE) instead of leaking a workspace-wide orphan.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, default=None, index=True
    )
    # The owning organization. No ORM or server default (F20 PR5): a write that
    # forgets it fails instead of landing in the default organization.
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"),
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
    # every other engine. Does not affect scan-time value extraction.
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
