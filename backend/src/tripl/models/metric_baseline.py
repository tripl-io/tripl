from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, UtcDateTime, UUIDMixin
from tripl.models.domain_enums import MetricScopeType
from tripl.models.enum_types import db_enum


class MetricBaseline(UUIDMixin, Base):
    """The band the detector judged one bucket of one scope against.

    ``metric_anomalies`` only holds FLAGGED buckets, so a chart could draw the
    expected value and band on those alone. The metrics worker
    writes one row here for every bucket it scored — flagged or not — keyed like
    an anomaly row. Scan-scoped series only (``project_total``, ``event_type``,
    ``event``): catalog ``metric`` scopes chart through their own series and
    store nothing here yet.

    Not ``event_metrics`` columns: the project-total series is a SUM over the
    event-type rows and has no row of its own to carry a baseline.

    ``effective_stddev`` is the floored stddev the z-score divides by, so the
    band ``expected_count ± sigma_threshold * effective_stddev`` is exactly the
    detector's boundary: a point outside it is a point it would flag.
    """

    __tablename__ = "metric_baselines"
    __table_args__ = (
        # Also the read index: the chart asks for one scope over a bucket range.
        UniqueConstraint(
            "scan_config_id",
            "scope_type",
            "scope_ref",
            "bucket",
            name="uq_metric_baseline_scope_bucket",
        ),
    )

    scan_config_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scan_configs.id", ondelete="CASCADE"),
    )
    scope_type: Mapped[str] = mapped_column(db_enum(MetricScopeType, "metric_scope_type"))
    scope_ref: Mapped[str] = mapped_column(String(64))
    bucket: Mapped[datetime] = mapped_column(UtcDateTime())
    expected_count: Mapped[float] = mapped_column(Float)
    effective_stddev: Mapped[float] = mapped_column(Float)
