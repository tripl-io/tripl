from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin, UUIDMixin
from tripl.models.enum_types import db_enum

# Reuse the shared lifecycle states (pending/running/completed/failed).
from tripl.models.scan_job import ScanJobStatus  # noqa: F401  (re-exported for callers)


class ScanDryRunJob(UUIDMixin, TimestampMixin, Base):
    """Async "what would this scan create?" pass over a draft or a saved config.

    Deliberately NOT a second mode on ``ScanPreviewJob``. That row already
    carries two modes (fast preview / JSON path discovery) and none of the
    inputs a dry-run needs — grouping rules, name format, cardinality threshold.
    A dry-run is also strictly slower than a preview: it runs the same
    ``GROUP BY ALL`` a real scan runs, which is exactly why the preview already
    needed a worker job.

    Every draft input lives on the row and the computed answer lives in
    ``result_summary`` (a ``ScanDryRunResponse`` payload), same shape as
    ``ScanPreviewJob``. When ``scan_config_id`` is set the worker reads the saved
    config instead and every draft column below is ignored — the shape
    ``integrate/agent-api-guide.md`` documents for API and agent callers that
    already hold a stored config. The scan form always sends a draft, so no
    frontend code takes the saved-config path.
    """

    __tablename__ = "scan_dry_run_jobs"
    __table_args__ = (Index("ix_scan_dry_run_job_project", "project_id"),)

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    # Saved-config path (API/agent callers). NULL for a draft from the form.
    scan_config_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scan_configs.id", ondelete="CASCADE"), nullable=True
    )
    # Draft path. NULL only when ``scan_config_id`` is set.
    data_source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=True
    )
    base_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_type_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("event_types.id", ondelete="SET NULL"), nullable=True
    )
    event_type_column: Mapped[str | None] = mapped_column(String(255), nullable=True)
    time_column: Mapped[str | None] = mapped_column(String(255), nullable=True)
    event_name_format: Mapped[str | None] = mapped_column(String(500), nullable=True)
    event_group_rules: Mapped[list[dict[str, object]]] = mapped_column(sa.JSON, default=list)
    json_value_paths: Mapped[list[str]] = mapped_column(sa.JSON, default=list)
    # The draft's setup preset (F23.4c) and its two columns; see ScanConfig.
    setup_preset: Mapped[str] = mapped_column(String(32), default="custom", server_default="custom")
    event_name_column: Mapped[str | None] = mapped_column(String(255), nullable=True)
    properties_column: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The draft's String columns to parse as JSON (F23.9); see ScanConfig.
    json_string_columns: Mapped[list[str]] = mapped_column(
        sa.JSON, default=list, server_default="[]"
    )
    cardinality_threshold: Mapped[int] = mapped_column(Integer, default=100)
    app_version_column: Mapped[str | None] = mapped_column(String(255), nullable=True)
    platform_column: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scan_lookback_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sample_row_limit: Mapped[int] = mapped_column(Integer, default=5000)

    status: Mapped[str] = mapped_column(
        db_enum(ScanJobStatus, "scan_job_status"), default=ScanJobStatus.pending.value
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_summary: Mapped[dict[str, object] | None] = mapped_column(sa.JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
