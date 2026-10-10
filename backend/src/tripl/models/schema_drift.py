from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, UUIDMixin
from tripl.models.domain_enums import SchemaDriftStatus, SchemaDriftType
from tripl.models.enum_types import db_enum

SCHEMA_DRIFT_STATUS_OPEN = SchemaDriftStatus.open.value
SCHEMA_DRIFT_STATUS_ACCEPTED = SchemaDriftStatus.accepted.value
SCHEMA_DRIFT_STATUS_SNOOZED = SchemaDriftStatus.snoozed.value


class SchemaDrift(UUIDMixin, Base):
    __tablename__ = "schema_drifts"
    __table_args__ = (
        UniqueConstraint(
            "event_type_id",
            "field_name",
            "drift_type",
            name="uq_schema_drift_event_type_field_kind",
        ),
        Index(
            "ix_schema_drift_event_type_detected",
            "event_type_id",
            "detected_at",
        ),
    )

    event_type_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("event_types.id", ondelete="CASCADE"),
    )
    scan_config_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scan_configs.id", ondelete="SET NULL"),
        nullable=True,
    )
    field_name: Mapped[str] = mapped_column(String(255))
    drift_type: Mapped[str] = mapped_column(db_enum(SchemaDriftType, "schema_drift_type"))
    observed_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    declared_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sample_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        db_enum(SchemaDriftStatus, "schema_drift_status"),
        default=SCHEMA_DRIFT_STATUS_OPEN,
        server_default=SCHEMA_DRIFT_STATUS_OPEN,
    )
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
