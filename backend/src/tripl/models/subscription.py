"""Who watches what: one row per (user, entity) for the notification center (#259).

``entity_id`` is deliberately not a foreign key: it points at one of four
tables (``events``, ``event_types``, ``metric_definitions``, ``plan_branches``)
depending on ``entity_type``. A row that outlives its entity is inert — nothing
ever notifies about an entity that no longer exists — and the ``project_id``
cascade removes it with the project.

For an event the id is the discussion's HOME row (the main twin of a branch
copy, ``event_comment_service.event_thread``), so watching an event on a branch
and on main is one subscription, the same way it is one thread.

``reasons`` records why the row exists (``author``, ``owner``, ``commenter``,
``reviewer``, ``manual``); auto-subscribe adds a reason to an existing row and
never flips ``muted``. ``muted`` keeps the row (and the Watch state) but stops
every notification about the entity except a direct @mention.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import JSON, Boolean, CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin, UUIDMixin


class SubscriptionEntityType(enum.StrEnum):
    event = "event"
    event_type = "event_type"
    metric = "metric"
    branch = "branch"


class SubscriptionReason(enum.StrEnum):
    author = "author"
    owner = "owner"
    commenter = "commenter"
    reviewer = "reviewer"
    manual = "manual"


class Subscription(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint("user_id", "entity_type", "entity_id", name="uq_subscription_user_entity"),
        CheckConstraint(
            "entity_type IN ('event', 'event_type', 'metric', 'branch')",
            name="ck_subscription_entity_type",
        ),
        Index("ix_subscription_entity", "entity_type", "entity_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    entity_type: Mapped[str] = mapped_column(String(16))
    entity_id: Mapped[uuid.UUID] = mapped_column()
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    muted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
