from __future__ import annotations

import enum
import uuid
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tripl.models.base import Base, TimestampMixin, UUIDMixin
from tripl.models.enum_types import db_enum
from tripl.models.plan_branch import default_branch_id

if TYPE_CHECKING:
    from tripl.models.event import Event
    from tripl.models.field_definition import FieldDefinition
    from tripl.models.project import Project
    from tripl.models.variable import Variable


class VariableValueKind(enum.StrEnum):
    low = "low"
    high = "high"


class VariableValue(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "variable_values"
    __table_args__ = (
        UniqueConstraint(
            "variable_id",
            "event_id",
            "field_definition_id",
            name="uq_variable_value_context",
        ),
        Index("ix_variable_values_event", "event_id"),
        Index("ix_variable_values_project_branch", "project_id", "branch_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    branch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plan_branches.id", ondelete="CASCADE"), index=True, default=default_branch_id
    )
    variable_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("variables.id", ondelete="CASCADE"))
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    field_definition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("field_definitions.id", ondelete="CASCADE")
    )
    source_column: Mapped[str] = mapped_column(String(255))
    value_kind: Mapped[str] = mapped_column(
        db_enum(VariableValueKind, "variable_value_kind"),
        default=VariableValueKind.high.value,
    )
    observed_count: Mapped[int] = mapped_column(Integer, default=0)
    values: Mapped[list[str]] = mapped_column(sa.JSON, default=list, server_default="[]")
    # Share of the event's breakdown rows (weighted by their counts) that carried
    # this JSON path, from the last scan that could tell (F23). NULL: unknown —
    # a regular column, a replay, or rows that came back without counts.
    presence_rate: Mapped[float | None] = mapped_column(sa.Float, nullable=True)

    project: Mapped[Project] = relationship()
    variable: Mapped[Variable] = relationship(back_populates="value_contexts")
    event: Mapped[Event] = relationship()
    field_definition: Mapped[FieldDefinition] = relationship(lazy="selectin")

    @property
    def variable_name(self) -> str:
        return self.variable.name

    @property
    def event_name(self) -> str:
        return self.event.name

    @property
    def field_name(self) -> str:
        return self.field_definition.name

    @property
    def field_display_name(self) -> str:
        return self.field_definition.display_name
