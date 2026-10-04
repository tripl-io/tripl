from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tripl.models.base import Base, UUIDMixin
from tripl.models.plan_branch import default_branch_id

if TYPE_CHECKING:
    from tripl.models.event_type import EventType
    from tripl.models.field_definition import FieldDefinition
    from tripl.models.project import Project


class EventTypeRelation(UUIDMixin, Base):
    __tablename__ = "event_type_relations"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    branch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plan_branches.id", ondelete="CASCADE"), index=True, default=default_branch_id
    )
    source_event_type_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("event_types.id", ondelete="CASCADE")
    )
    target_event_type_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("event_types.id", ondelete="CASCADE")
    )
    source_field_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("field_definitions.id", ondelete="CASCADE")
    )
    target_field_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("field_definitions.id", ondelete="CASCADE")
    )
    # The MAIN relation this branch copy was made from, as ``Event.origin_id``
    # records it for events — and, for the reason given there, not a foreign
    # key: nothing makes the four-part natural key unique, so two relations can
    # link the same two fields. NULL on main and on relations
    # created on a branch.
    origin_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    relation_type: Mapped[str] = mapped_column(String(50), default="belongs_to")
    description: Mapped[str] = mapped_column(Text, default="")

    project: Mapped[Project] = relationship(back_populates="relations")
    source_event_type: Mapped[EventType] = relationship(foreign_keys=[source_event_type_id])
    target_event_type: Mapped[EventType] = relationship(foreign_keys=[target_event_type_id])
    source_field: Mapped[FieldDefinition] = relationship(foreign_keys=[source_field_id])
    target_field: Mapped[FieldDefinition] = relationship(foreign_keys=[target_field_id])
