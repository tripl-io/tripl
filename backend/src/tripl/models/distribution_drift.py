from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from tripl.models.base import Base, UUIDMixin
from tripl.models.domain_enums import DistributionDriftBand
from tripl.models.enum_types import db_enum


class DistributionDrift(UUIDMixin, Base):
    """Persisted PSI score for a (scan_config, event_type?, field, bucket).

    Written by the worker when distribution drift is detected on a configured
    field. Read by the API for the "Distribution" tab + by the alerting
    pipeline as a candidate (band == "significant").
    """

    __tablename__ = "distribution_drifts"
    __table_args__ = (
        Index(
            "ix_distribution_drift_scan_field_bucket",
            "scan_config_id",
            "field_name",
            "bucket",
        ),
        Index("ix_distribution_drift_event_type", "event_type_id", "bucket"),
    )

    scan_config_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scan_configs.id", ondelete="CASCADE"),
    )
    event_type_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("event_types.id", ondelete="SET NULL"),
        nullable=True,
    )
    field_name: Mapped[str] = mapped_column(String(255))
    bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    psi: Mapped[float] = mapped_column(Float)
    band: Mapped[str] = mapped_column(db_enum(DistributionDriftBand, "distribution_drift_band"))
    baseline_total: Mapped[int] = mapped_column(Integer)
    current_total: Mapped[int] = mapped_column(Integer)
    # Top contributing values: [{"value", "baseline_share", "current_share",
    # "contribution"}]. Stored as JSONB on Postgres, plain JSON on SQLite tests.
    top_movers: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"),
        default=list,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


def mover_float(value: object) -> float:
    """One numeric cell of a ``top_movers`` entry as a float; ``0.0`` when absent.

    The column is free-form JSON: a number or numeric text is read as written,
    and a missing cell (or a list, a dict, a null) reads as zero rather than
    failing the Distribution tab or the alert.
    """
    if isinstance(value, (int, float, str)):
        return float(value)
    return 0.0
