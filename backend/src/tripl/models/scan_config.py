from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tripl.models.base import Base, TimestampMixin, UtcDateTime, UUIDMixin
from tripl.models.domain_enums import ScanInterval
from tripl.models.enum_types import db_enum
from tripl.models.project_anomaly_settings import (
    DEFAULT_MIN_EXPECTED_COUNT,
    DEFAULT_SIGMA_THRESHOLD,
)

if TYPE_CHECKING:
    from tripl.models.data_source import DataSource
    from tripl.models.event_type import EventType
    from tripl.models.scan_job import ScanJob


class ScanConfig(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "scan_configs"
    __table_args__ = (
        UniqueConstraint("data_source_id", "name", name="uq_scan_config_ds_name"),
        Index("ix_scan_config_project", "project_id"),
    )

    data_source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("data_sources.id", ondelete="CASCADE")
    )
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    event_type_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("event_types.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255))
    base_query: Mapped[str] = mapped_column(Text)
    event_type_column: Mapped[str | None] = mapped_column(String(255), nullable=True)
    time_column: Mapped[str | None] = mapped_column(String(255), nullable=True)
    event_name_format: Mapped[str | None] = mapped_column(String(500), nullable=True)
    json_value_paths: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    # How the scan was set up (F23.4c): ``custom``, or ``event_properties`` —
    # names from ``event_name_column``, every key of ``properties_column`` a
    # property. The preset DERIVES ``event_name_format``, ``json_value_paths``,
    # ``event_type_column`` and ``event_group_rules``
    # (``core.scan_setup_preset``); the two columns are stored so an edit
    # reopens the preset. Both are NULL on a custom config.
    setup_preset: Mapped[str] = mapped_column(String(32), default="custom", server_default="custom")
    event_name_column: Mapped[str | None] = mapped_column(String(255), nullable=True)
    properties_column: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # String columns every read of the source parses as JSON (F23.9,
    # ``core.json_string_columns``); ClickHouse and BigQuery only.
    json_string_columns: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    event_group_rules: Mapped[list[dict[str, object]]] = mapped_column(
        JSON,
        default=list,
        server_default="[]",
    )
    metric_breakdown_columns: Mapped[list[str]] = mapped_column(
        JSON,
        default=list,
        server_default="[]",
    )
    metric_breakdown_values_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    distribution_drift_fields: Mapped[list[str]] = mapped_column(
        JSON,
        default=list,
        server_default="[]",
    )
    cardinality_threshold: Mapped[int] = mapped_column(Integer, default=100)
    interval: Mapped[str | None] = mapped_column(
        db_enum(ScanInterval, "scan_interval"), nullable=True
    )
    # Splits the metrics collection window into interval-aligned chunks so a long
    # replay runs several bounded warehouse queries instead of one that times out.
    # Must be >= ``interval``; NULL means "scan the whole window in one query".
    replay_chunk_interval: Mapped[str | None] = mapped_column(
        db_enum(ScanInterval, "scan_interval"), nullable=True
    )
    # Optional lookback for manual scan/preview catalog sync. Scheduled metrics
    # catalog sync uses the collection window when this is unset.
    scan_lookback_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Optional override for GROUP BY ALL scan query row cap.
    scan_row_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Optional override for time-bucketed replay/metrics query row caps.
    metrics_row_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Optional column holding the app/build version (e.g. "2.10.0"). When set,
    # per-version metric series are collected and release regressions detected.
    # NULL means version observation is fully disabled (e.g. web scans with no
    # app version) and the whole version pipeline is inert.
    app_version_column: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # How many latest releases (by semver order) to retain/observe; older
    # releases collapse into an "other" bucket. NULL falls back to a system
    # default at collection time.
    app_version_keep_releases: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Optional per-scan regex; version strings matching it are treated as
    # pre-release/dev builds — excluded from "latest" and given a retention slot
    # only after released versions, exactly like a SemVer pre-release tag (which
    # is always excluded regardless of this column). NULL means only the SemVer
    # default applies. An invalid regex is ignored at read time so it can never
    # crash a scan.
    app_version_prerelease_pattern: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Optional per-scan override for the activation traffic-share floor: the
    # fraction of total traffic a release must hold (over consecutive buckets) to
    # count as "active". NULL falls back to the system default (0.05).
    app_version_active_share_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Optional column holding the client platform (e.g. "ios"/"android"/"web").
    # When set, it is collected as a scan-level breakdown so platform values land
    # in EventMetricBreakdown — powering the per-event platform presence matrix
    # and per-platform volume anomalies. NULL means no platform dimension.
    platform_column: Mapped[str | None] = mapped_column(String(255), nullable=True)
    anomaly_detection_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    detect_project_total: Mapped[bool] = mapped_column(Boolean, default=True)
    detect_event_types: Mapped[bool] = mapped_column(Boolean, default=True)
    detect_events: Mapped[bool] = mapped_column(Boolean, default=True)
    baseline_window_buckets: Mapped[int] = mapped_column(Integer, default=14)
    min_history_buckets: Mapped[int] = mapped_column(Integer, default=7)
    sigma_threshold: Mapped[float] = mapped_column(Float, default=DEFAULT_SIGMA_THRESHOLD)
    min_expected_count: Mapped[int] = mapped_column(Integer, default=DEFAULT_MIN_EXPECTED_COUNT)
    # Source freshness (#269). ``last_event_at`` is the start of the newest
    # non-empty bucket stored for this config after the latest successful
    # scheduled collection — the newest event time observed, at bucket
    # resolution — and never moves backwards. ``last_collection_at`` is when
    # that collection finished reading the warehouse. Freshness itself is
    # computed from these two (``services.source_freshness``), never stored.
    last_event_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    last_collection_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)

    data_source: Mapped[DataSource] = relationship(back_populates="scan_configs")
    event_type: Mapped[EventType | None] = relationship()

    # This collection exists for ONE reason: the ORM delete cascade in
    # ``scan_service.delete_scan_config``. Nothing reads the ATTRIBUTE. Every
    # read of the job history is a direct query against the table instead — the
    # Scans tab's ``scan_service.list_scan_jobs``, the dispatcher's
    # ``_get_active_scan_jobs`` and ``coverage.covered_buckets_from_scan_jobs``.
    #
    # It must therefore stay lazily loaded. It used to be ``lazy="selectin"``,
    # so EVERY ScanConfig ENTITY load hydrated that config's entire scan
    # history — JSON ``result_summary`` and ``error_message`` included — for
    # callers that only wanted ``name`` or ``interval``: the alert inbox's
    # ``_alerting_deliveries._INBOX_GROUP_SELECT`` (four call sites, once per
    # request), ``alert_flush``, the metrics scheduler's beat tick over every
    # scheduled config, the Scans tab, the metrics/insights services and the
    # search indexer (tripl-0zpq.157).
    #
    # Unlike the small parent-child fan-outs elsewhere in this package, that one
    # had no ceiling. ``scan_jobs`` gains a row per collection and is pruned only
    # for demo projects (``demo_runtime._prune_retention``), so the bill grew
    # with deployment age x cadence — on the order of 8.7K rows per config per
    # year at a 1h interval, 35K at 15m — with nothing to bound it.
    #
    # ``await session.delete(config)`` still removes the jobs: AsyncSession.delete
    # is a coroutine precisely so the cascade can lazy-load. That cascade rides on
    # ``cascade="all, delete-orphan"`` with ``passive_deletes`` off — change either
    # and the unit of work stops emitting the child DELETEs, leaving only the
    # DB-level ``ondelete="CASCADE"`` on ``ScanJob.scan_config_id``, which not every
    # test engine turns on (tests/_sqlite.py). The LOADER strategy is no part of
    # that, contrary to the obvious guess: ``lazy="raise"`` was measured on this
    # relationship and the unit of work still loads the collection and deletes the
    # rows, emitting SQL identical to the above. Same decision and same reasoning as
    # the four plan collections on ``Project`` (tripl-jfm3.54).
    scan_jobs: Mapped[list[ScanJob]] = relationship(
        back_populates="scan_config", cascade="all, delete-orphan"
    )
