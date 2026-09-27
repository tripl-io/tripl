from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, UUIDMixin


class MetricAnomalyAttribution(UUIDMixin, Base):
    """Why a volume anomaly happened, computed when it was detected (F02, #255).

    One row per ``MetricAnomaly``. Stored rather than derived per request so the
    alert that fires, the AI explanation and the drilldown's "Why" panel all
    quote the same numbers. The anomaly row is replaced on every re-score, and
    the attribution goes with it (ON DELETE CASCADE); the worker recomputes it
    for the new row in the same run.

    ``delta`` is ``actual_count - expected_count`` of the anomaly. ``columns`` is
    at most three ``{column, explained_share, values: [{value, delta, expected,
    actual, share}]}`` entries (see ``tripl.core.analyzers.attribution``);
    ``release`` is ``{version, previous_version, share, reached_at}`` or NULL.
    """

    __tablename__ = "metric_anomaly_attributions"
    __table_args__ = (UniqueConstraint("anomaly_id", name="uq_metric_anomaly_attribution_anomaly"),)

    anomaly_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("metric_anomalies.id", ondelete="CASCADE"),
    )
    delta: Mapped[float] = mapped_column(Float)
    columns: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    release: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
