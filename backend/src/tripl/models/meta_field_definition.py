from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tripl.models.base import Base, UUIDMixin
from tripl.models.domain_enums import MetaFieldType, Sensitivity
from tripl.models.enum_types import db_enum
from tripl.models.plan_branch import default_branch_id

if TYPE_CHECKING:
    from tripl.models.project import Project


class MetaFieldDefinition(UUIDMixin, Base):
    __tablename__ = "meta_field_definitions"
    __table_args__ = (
        UniqueConstraint("project_id", "branch_id", "name", name="uq_meta_field_def_project_name"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    branch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plan_branches.id", ondelete="CASCADE"), index=True, default=default_branch_id
    )
    name: Mapped[str] = mapped_column(String(100))
    display_name: Mapped[str] = mapped_column(String(255))
    field_type: Mapped[str] = mapped_column(db_enum(MetaFieldType, "meta_field_type"))
    is_required: Mapped[bool] = mapped_column(Boolean, default=False)
    # An event may carry several values for this field, the way it carries
    # several tags. "One Jira key per event" was never a decision anyone made
    # about tickets — it fell out of ``uq_event_meta_value_event_meta``, and an
    # event updated in a second task had nowhere to put the second key.
    # Orthogonal to ``field_type`` so a url or enum field can
    # be multi-valued too; ``default_value`` stays one value, the first entry.
    allow_multiple: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa.false(), nullable=False
    )
    enum_options: Mapped[list[str] | None] = mapped_column(sa.JSON, nullable=True)
    default_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    link_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    order: Mapped[int] = mapped_column(Integer, default=0)
    sensitivity: Mapped[str] = mapped_column(
        db_enum(Sensitivity, "sensitivity_level"),
        default=Sensitivity.none.value,
        server_default=Sensitivity.none.value,
    )

    project: Mapped[Project] = relationship(back_populates="meta_field_definitions")
