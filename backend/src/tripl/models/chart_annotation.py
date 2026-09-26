from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin, UUIDMixin
from tripl.models.domain_enums import ChartAnnotationScopeType, ChartAnnotationSource
from tripl.models.enum_types import db_enum

CHART_ANNOTATION_SOURCE_MANUAL = ChartAnnotationSource.manual.value

# The predicate of the partial unique index below. Spelled once so the model, the
# worker's ``ON CONFLICT`` target and the migration cannot drift apart.
RELEASE_ANNOTATION_UNIQUE_WHERE = "source = 'release'"


class ChartAnnotation(UUIDMixin, TimestampMixin, Base):
    """Vertical marker overlaid on metric charts.

    Project-wide markers (scope_type IS NULL) appear on every chart inside
    the project; scoped markers only show on charts matching the
    (scope_type, scope_ref) pair — e.g. an outage on a single event_type.

    ``source`` says who drew the marker (see :class:`ChartAnnotationSource`).
    Release markers are unique per (project, label) for good: two scans of one
    project, or the same scan re-run, see the same activation and must not draw
    it twice. API markers are only de-duplicated inside a 24h window, which the
    service enforces; manual markers are never de-duplicated.
    """

    __tablename__ = "chart_annotations"
    __table_args__ = (
        Index("ix_chart_annotation_project_bucket", "project_id", "bucket"),
        Index("ix_chart_annotation_scope", "project_id", "scope_type", "scope_ref"),
        # Serves the create-time de-dup lookup (api within 24h, release for good).
        Index("ix_chart_annotation_project_source_label", "project_id", "source", "label"),
        Index(
            "uq_chart_annotation_release_label",
            "project_id",
            "label",
            unique=True,
            postgresql_where=text(RELEASE_ANNOTATION_UNIQUE_WHERE),
            sqlite_where=text(RELEASE_ANNOTATION_UNIQUE_WHERE),
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    scope_type: Mapped[str | None] = mapped_column(
        db_enum(ChartAnnotationScopeType, "chart_annotation_scope_type"), nullable=True
    )
    scope_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    label: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    color: Mapped[str] = mapped_column(String(20), default="#ef4444", server_default="#ef4444")
    source: Mapped[str] = mapped_column(
        db_enum(ChartAnnotationSource, "chart_annotation_source"),
        default=CHART_ANNOTATION_SOURCE_MANUAL,
        server_default=CHART_ANNOTATION_SOURCE_MANUAL,
        nullable=False,
    )
    # Where the marker links out to: the release notes, the PR, the deploy run.
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
