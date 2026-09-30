from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tripl.models.base import Base, UUIDMixin
from tripl.models.domain_enums import SchemaDriftStatus
from tripl.models.enum_types import db_enum
from tripl.models.schema_drift import SCHEMA_DRIFT_STATUS_OPEN

if TYPE_CHECKING:
    from tripl.models.event import Event
    from tripl.models.variable import Variable


class PropertyDriftKind(enum.StrEnum):
    # A key the event carried that its property list does not name. Only for
    # events whose list names at least one property: an empty list is "not
    # described yet", and reporting every key of it would drown the rest.
    new_property = "new_property"
    # A property the event's list marks required, carried less often than the
    # event's presence threshold.
    missing_required = "missing_required"
    # Sampled values of a kind the variable's type does not admit. Per
    # variable (``event_id`` NULL): the sampler reads the whole scan config,
    # so it cannot say which event carried the value.
    type_change = "type_change"


class PropertyDrift(UUIDMixin, Base):
    """A difference between an event's property list and what a scan saw (F23).

    One row per (variable, event, kind), ``event_id`` NULL for a type change.
    Triaged like value drift: the scan refreshes ``detail`` and
    ``detected_at`` and never touches ``status``; ``accepted`` rows freeze and
    reopen when the scan sees something the acceptance did not cover.
    """

    __tablename__ = "property_drifts"
    __table_args__ = (
        UniqueConstraint("variable_id", "event_id", "kind", name="uq_property_drift"),
        # NULLs are distinct in a unique constraint, so the per-variable kind
        # needs its own index to stay one row.
        Index(
            "uq_property_drift_variable",
            "variable_id",
            "kind",
            unique=True,
            postgresql_where=sa.text("event_id IS NULL"),
            sqlite_where=sa.text("event_id IS NULL"),
        ),
        Index("ix_property_drifts_project_detected", "project_id", "detected_at"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    variable_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("variables.id", ondelete="CASCADE"), index=True
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=True, index=True
    )
    scan_config_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scan_configs.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    # What the scan saw: ``{"presence_rate": 0.4, "threshold": 0.95}`` for a
    # missing property, ``{"presence_rate": 0.1}`` for a new one,
    # ``{"observed_type": "number", "expected_type": "string"}`` for a type.
    detail: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, server_default="{}")
    status: Mapped[str] = mapped_column(
        db_enum(SchemaDriftStatus, "schema_drift_status"),
        default=SCHEMA_DRIFT_STATUS_OPEN,
        server_default=SCHEMA_DRIFT_STATUS_OPEN,
    )
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    variable: Mapped[Variable] = relationship(lazy="selectin")
    event: Mapped[Event | None] = relationship(lazy="selectin")

    @property
    def variable_name(self) -> str:
        return self.variable.name

    @property
    def event_name(self) -> str | None:
        return self.event.name if self.event is not None else None
