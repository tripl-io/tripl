from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin, UUIDMixin
from tripl.models.domain_enums import MetricScopeType
from tripl.models.enum_types import db_enum


class AlertRuleState(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "alert_rule_states"
    __table_args__ = (
        UniqueConstraint(
            "rule_id",
            "scan_config_id",
            "scope_type",
            "scope_ref",
            name="uq_alert_rule_state_scope",
        ),
        # SQL treats NULLs as DISTINCT, so the composite constraint above never
        # fires for ``metric``-scope rows (NULL scan_config_id): each scan
        # config's dispatch run would insert its own state and the project-wide
        # series would get one cooldown clock per config — the duplicate sends
        # this key exists to prevent. This partial unique index excludes the
        # NULL column and covers only the NULL space, exactly as
        # ``uq_metric_anomaly_metric_scope`` does for MetricAnomaly and
        # ``uq_anomaly_scope_override_metric_scope`` for AnomalyScopeOverride.
        #
        # No project_id column is needed for it to be project-scoped:
        # rule_id -> alert_rules.destination_id -> alert_destinations.project_id
        # is a functional dependency, so (rule_id, scope_type, scope_ref) is
        # already unique within one project.
        #
        # ``sqlite_where`` is LOAD-BEARING, not decoration. The unit suite builds
        # its schema from ``Base.metadata.create_all`` and never runs the
        # migration chain (which is Postgres-gated), so this is the only place
        # SQLite learns the index exists — and without it the dedupe would be
        # untested everywhere it is testable.
        Index(
            "uq_alert_rule_state_metric_scope",
            "rule_id",
            "scope_type",
            "scope_ref",
            unique=True,
            postgresql_where=text("scan_config_id IS NULL"),
            sqlite_where=text("scan_config_id IS NULL"),
        ),
        Index("ix_alert_rule_state_rule", "rule_id"),
        Index("ix_alert_rule_state_scan", "scan_config_id"),
    )

    rule_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("alert_rules.id", ondelete="CASCADE"),
    )
    # NULL for ``metric`` scope: a catalog metric series is project-global, and
    # the anomaly row that produces it already says so with a NULL of its own
    # (``MetricAnomaly.scan_config_id``). Every other scope is partitioned by
    # config and always sets this.
    #
    # CASCADE is kept and is still correct for those config-scoped rows: a
    # deleted scan takes its own states with it. A metric row stores NULL, so
    # the cascade cannot reach it — which is the whole point. Dispatch used to
    # anchor metric states on the project's LOWEST config id instead, and uuid4
    # has no order, so creating a config that sorted below the anchor (or
    # deleting the anchor) moved it: the shared row became unreachable, the
    # cooldown reset, a duplicate notification shipped, and the abandoned row
    # stayed is_active=True forever, inflating the Monitors screen's
    # ``active_scope_count``.
    scan_config_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scan_configs.id", ondelete="CASCADE"),
        nullable=True,
    )
    scope_type: Mapped[str] = mapped_column(db_enum(MetricScopeType, "metric_scope_type"))
    scope_ref: Mapped[str] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_anomaly_bucket: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_notified_delivery_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("alert_deliveries.id", ondelete="SET NULL"),
        nullable=True,
    )
