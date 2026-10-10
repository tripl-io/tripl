from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, UUIDMixin

# Default detector sensitivity, shared by project monitoring settings and the
# per-scan copies on ScanConfig. 4.0 / 50 (raised from 3.0 / 10) keep the
# marginal band — |z| in 3..4 on series expecting under ~50 events per bucket —
# from flooding a mature project's Anomalies page with hundreds of open
# signals; per-project overrides remain available in Detection settings.
DEFAULT_SIGMA_THRESHOLD = 4.0
DEFAULT_MIN_EXPECTED_COUNT = 50

# How long (wall clock) an anomaly keeps counting as an OPEN signal on the
# Anomalies page and in the sidebar badge. 24h is the historical hard-coded
# value in services/monitoring_utils.RECENT_SIGNAL_WINDOW, kept as the default
# so behaviour is unchanged until a project opts into a different window.
DEFAULT_RECENT_SIGNAL_WINDOW_HOURS = 24

# Wall-clock allowance for the warehouse to finish delivering a bucket before
# its value is scored. 120 minutes is the historical module constant
# ``worker.tasks.metrics.tasks.ANOMALY_INGESTION_SETTLING``, kept as the default
# so behaviour is unchanged until a project opts into a different allowance.
# 0 disables the hold-back entirely (score every collected bucket immediately).
DEFAULT_ANOMALY_INGESTION_SETTLING_MINUTES = 120

# The remaining dials. Detection itself is opt-in; once on, every scope is
# scored. The columns take their defaults from here, and so does the settings
# read of a project that never saved a row of its own.
DEFAULT_ANOMALY_DETECTION_ENABLED = False
DEFAULT_DETECT_SCOPE = True
DEFAULT_BASELINE_WINDOW_BUCKETS = 14
DEFAULT_MIN_HISTORY_BUCKETS = 7


class ProjectAnomalySettings(UUIDMixin, Base):
    __tablename__ = "project_anomaly_settings"
    __table_args__ = (UniqueConstraint("project_id", name="uq_project_anomaly_settings_project"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
    )
    anomaly_detection_enabled: Mapped[bool] = mapped_column(
        Boolean, default=DEFAULT_ANOMALY_DETECTION_ENABLED
    )
    detect_project_total: Mapped[bool] = mapped_column(Boolean, default=DEFAULT_DETECT_SCOPE)
    detect_event_types: Mapped[bool] = mapped_column(Boolean, default=DEFAULT_DETECT_SCOPE)
    detect_events: Mapped[bool] = mapped_column(Boolean, default=DEFAULT_DETECT_SCOPE)
    # Run anomaly detection over user-defined catalog metric series. On by
    # default (like the other detect_* scopes); delivery is still opt-in via the
    # alert rule's ``include_metrics`` flag.
    detect_metrics: Mapped[bool] = mapped_column(
        Boolean, default=DEFAULT_DETECT_SCOPE, server_default="true"
    )
    baseline_window_buckets: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_BASELINE_WINDOW_BUCKETS
    )
    min_history_buckets: Mapped[int] = mapped_column(Integer, default=DEFAULT_MIN_HISTORY_BUCKETS)
    sigma_threshold: Mapped[float] = mapped_column(Float, default=DEFAULT_SIGMA_THRESHOLD)
    min_expected_count: Mapped[int] = mapped_column(Integer, default=DEFAULT_MIN_EXPECTED_COUNT)
    # Server default keeps pre-existing rows reading 24 rather than NULL, so the
    # signal-freshness window is unchanged for every project that never opts in.
    recent_signal_window_hours: Mapped[int] = mapped_column(
        Integer,
        default=DEFAULT_RECENT_SIGNAL_WINDOW_HOURS,
        server_default="24",
    )
    # Ingestion-settling allowance: the newest buckets of a
    # freshly collected series are held back from anomaly EMISSION for this many
    # wall-clock minutes, because a warehouse keeps delivering rows for a bucket
    # after its clock interval closes. Server default 120 reproduces the module
    # constant this replaced, so existing rows read 120 rather than NULL.
    anomaly_ingestion_settling_minutes: Mapped[int] = mapped_column(
        Integer,
        default=DEFAULT_ANOMALY_INGESTION_SETTLING_MINUTES,
        server_default="120",
    )
    # A country's public holidays as project-wide planned events (F18): an
    # ISO 3166-1 alpha-2 code, NULL for none. ``holiday_calendar`` keeps the
    # rows in step with it.
    holiday_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(),
        onupdate=func.now(),
    )
