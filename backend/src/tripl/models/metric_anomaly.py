from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, UtcDateTime, UUIDMixin
from tripl.models.domain_enums import AnomalyDirection, MetricScopeType
from tripl.models.enum_types import db_enum


class MetricAnomaly(UUIDMixin, Base):
    __tablename__ = "metric_anomalies"
    __table_args__ = (
        UniqueConstraint(
            "scan_config_id",
            "scope_type",
            "scope_ref",
            "bucket",
            name="uq_metric_anomaly_scope_bucket",
        ),
        Index("ix_metric_anomaly_event_bucket", "event_id", "bucket"),
        Index("ix_metric_anomaly_type_bucket", "event_type_id", "bucket"),
        # ``metric``-scope rows (catalog MetricDefinition series) carry a NULL
        # ``scan_config_id`` and are keyed purely by ``scope_ref`` (the metric
        # definition id), so the series read/recompute paths look them up by
        # (scope_ref, bucket).
        Index("ix_metric_anomaly_scope_ref_bucket", "scope_ref", "bucket"),
        # Idempotency for project-global ``metric``-scope rows. The composite
        # ``uq_metric_anomaly_scope_bucket`` cannot dedupe them because it
        # includes ``scan_config_id`` and SQL treats NULLs as DISTINCT, so
        # ``(NULL, 'metric', ref, bucket)`` rows from concurrent recompute runs
        # never conflict. This partial unique index excludes ``scan_config_id``
        # and only covers the NULL space, giving the recompute upsert a real
        # conflict target.
        Index(
            "uq_metric_anomaly_metric_scope",
            "scope_type",
            "scope_ref",
            "bucket",
            unique=True,
            postgresql_where=text("scan_config_id IS NULL"),
            sqlite_where=text("scan_config_id IS NULL"),
        ),
    )

    # NULL for ``metric``-scope rows: catalog metric anomalies are project-global
    # and not tied to a single scan config. Event scopes always set it.
    scan_config_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scan_configs.id", ondelete="CASCADE"),
        nullable=True,
    )
    scope_type: Mapped[str] = mapped_column(db_enum(MetricScopeType, "metric_scope_type"))
    scope_ref: Mapped[str] = mapped_column(String(64))
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_type_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("event_types.id", ondelete="SET NULL"),
        nullable=True,
    )
    bucket: Mapped[datetime] = mapped_column(UtcDateTime())
    # Float: catalog metrics carry fractional actuals (ratios/averages); volume
    # scopes keep storing whole counts in the same column (tripl-68bc).
    actual_count: Mapped[float] = mapped_column(Float)
    expected_count: Mapped[float] = mapped_column(Float)
    stddev: Mapped[float] = mapped_column(Float)
    z_score: Mapped[float] = mapped_column(Float)
    # The floored stddev actually used in the z-score denominator. Serves the
    # chart band (expected ± sigma_threshold * effective_stddev) so "outside the
    # band" visually equals "flagged". Defaults to 0 for pre-migration rows.
    effective_stddev: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    # Which detector path produced the row: "phase" | "rolling" | "trend" |
    # "fractional". Backfills to "phase" for existing rows.
    detector_kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="phase")
    direction: Mapped[str] = mapped_column(db_enum(AnomalyDirection, "anomaly_direction"))
    # Set when the bucket falls inside a planned event that expected this move
    # (F18). Such a row is drawn but never alerted, notified or counted as open;
    # ``planned_event_service.retag_planned_anomalies`` keeps it in step with
    # the project's planned events.
    planned_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("planned_events.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
