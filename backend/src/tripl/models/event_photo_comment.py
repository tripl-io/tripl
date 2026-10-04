from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin, UUIDMixin
from tripl.models.domain_enums import EventCommentStatus
from tripl.models.enum_types import db_enum

EVENT_COMMENT_STATUS_OPEN = EventCommentStatus.open.value
EVENT_COMMENT_STATUS_RESOLVED = EventCommentStatus.resolved.value
EVENT_COMMENT_STATUS_SNOOZED = EventCommentStatus.snoozed.value


class EventPhotoComment(UUIDMixin, TimestampMixin, Base):
    """A threaded comment, anchored either on one attachment or on the event.

    One table for both because they are the same object: threaded, authored,
    plain text, and deliberately NOT plan content — no snapshot, no approval
    hash, no search index, nothing that reaches whoever implements the event.
    An event-anchored row is the discussion the analyst asked for ("not a
    Title and not a Description — it raises something"); a photo-anchored row
    is the same conversation pinned to one frame.

    Exactly one anchor is set. ``photo_id`` had to become nullable for that,
    which is also why every query meaning "the attachment threads" filters on
    ``photo_id`` rather than reading the whole table: an event thread must stay
    out of ``build_plan_snapshot``, out of the branch deep copy, and out of the
    merge's photo-thread carry-back. The merge moves a thread hanging on a
    branch row onto main separately: onto the main twin that row already reads
    its discussion through, else onto the row the merge gives the event
    (``plan_branch_merge_service._move_event_threads_to_main``).
    """

    __tablename__ = "event_photo_comments"
    __table_args__ = (
        CheckConstraint(
            "(photo_id IS NULL) <> (event_id IS NULL)",
            name="ck_event_photo_comment_one_anchor",
        ),
        Index("ix_event_photo_comment_photo_created", "photo_id", "created_at"),
        # The catalog filter asks "which events have an unanswered question",
        # which is this index's exact shape: anchored on an event, top-level,
        # not yet resolved.
        Index("ix_event_photo_comment_event_status", "event_id", "status"),
    )

    photo_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("event_photos.id", ondelete="CASCADE"), nullable=True
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=True
    )
    # Self-FK for threaded replies. NULL = top-level comment.
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("event_photo_comments.id", ondelete="CASCADE"), nullable=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    body: Mapped[str] = mapped_column(Text)

    # Resolution state, the five columns SchemaDrift and VariableValueDrift both
    # carry. It lives on every row because this is one table, but it only MEANS
    # anything on a top-level comment: the thread is the unit that gets answered,
    # and the service refuses an action on a reply. `snoozed_until` is the escape
    # valve that keeps "open question" from becoming a permanent nag — a snooze
    # that has lapsed counts as open again, computed at read time rather than
    # stored, so no job has to sweep it.
    status: Mapped[str] = mapped_column(
        db_enum(EventCommentStatus, "event_comment_status"),
        default=EVENT_COMMENT_STATUS_OPEN,
        server_default=EVENT_COMMENT_STATUS_OPEN,
        nullable=False,
    )
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
