from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy import ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tripl.models.base import Base, TimestampMixin, UUIDMixin
from tripl.models.plan_branch import default_branch_id

if TYPE_CHECKING:
    from tripl.models.event import Event
    from tripl.models.variable import Variable


class VariableEventValueOverride(UUIDMixin, TimestampMixin, Base):
    """One variable as a property of one event (F23, #306).

    The row IS the event's property list entry: a variable with a row here is a
    property the event carries, ``required`` says whether every occurrence must
    carry it, and ``values`` optionally documents the allowed values for this
    event. A ``values`` list REPLACES (does not extend) the variable's global
    ``allowed_values`` for that event; NULL means no override — the global list
    applies. Rows from before F23 are all overrides, so their ``values`` is a
    list. User-owned: the scan pipeline never writes these rows.
    """

    __tablename__ = "variable_event_value_overrides"
    __table_args__ = (
        UniqueConstraint("variable_id", "event_id", name="uq_variable_event_value_override"),
        Index("ix_variable_event_value_overrides_project_branch", "project_id", "branch_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    branch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plan_branches.id", ondelete="CASCADE"), index=True, default=default_branch_id
    )
    variable_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("variables.id", ondelete="CASCADE"), index=True
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), index=True
    )
    values: Mapped[list[str] | None] = mapped_column(sa.JSON(none_as_null=True), nullable=True)
    required: Mapped[bool] = mapped_column(sa.Boolean, default=False, server_default=sa.false())

    variable: Mapped[Variable] = relationship(back_populates="event_overrides")
    event: Mapped[Event] = relationship(lazy="selectin")

    @property
    def event_name(self) -> str:
        return self.event.name


def copy_override_values(values: list[str] | None) -> list[str] | None:
    """A fresh copy of an entry's ``values``, keeping NULL ("no override") NULL.

    Every copy of a row goes through here: ``list(values or [])`` turned "no
    override" into "no values allowed".
    """
    return None if values is None else list(values)
