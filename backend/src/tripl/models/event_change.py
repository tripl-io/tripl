from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin, UUIDMixin


class EventChange(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "event_changes"
    __table_args__ = (Index("ix_event_changes_event_id", "event_id"),)

    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: Either a tracked attribute name (``status``, ``sunset_at``, ``tags``) or a
    #: KEYED entry, ``field:<field name>`` / ``meta:<meta field name>``, written by
    #: ``event_service._record_keyed_changes`` and split back apart by
    #: ``frontend/src/lib/eventHistory.ts``. Both name columns are ``String(100)``
    #: and both create schemas allow the full 100, so a keyed entry can be 106
    #: characters — six more than the 100 this column held until tripl-0zpq.256,
    #: which made editing such a field a rolled-back 500 on PostgreSQL
    #: (StringDataRightTruncation at flush) while SQLite, which does not enforce
    #: VARCHAR widths, stored it happily in the tests. 255 rather than a tight 106
    #: so a longer prefix or a wider name column does not reopen the same hole.
    field: Mapped[str] = mapped_column(String(255))
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Who made the change when it was not a person (#258): ``"scan"``
    #: (``EVENT_CHANGE_SOURCE_SCAN``) for a transition the metrics worker made
    #: from data, NULL for a person. Readers decide "made by the scan" on THIS,
    #: never on ``user_id IS NULL`` — ``user_id`` is ``ON DELETE SET NULL``, so
    #: a deleted user's edits are NULL there too and are still a person's.
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)


#: ``EventChange.source`` of a data-driven transition written by the metrics
#: worker (auto-live and the other lifecycle transitions that are a DATA FACT
#: rather than a plan edit).
EVENT_CHANGE_SOURCE_SCAN = "scan"

# How the history and the activity rail name the author of a change whose
# ``source`` is ``EVENT_CHANGE_SOURCE_SCAN``: what a reader sees instead of an
# email.
SCAN_AUTHOR_LABEL = "tripl (scan)"


def create_event_change(
    *,
    event_id: uuid.UUID,
    user_id: uuid.UUID | None,
    field: str,
    old_value: str | None,
    new_value: str | None,
    source: str | None = None,
) -> EventChange:
    now = datetime.now(UTC)
    return EventChange(
        event_id=event_id,
        user_id=user_id,
        field=field,
        old_value=old_value,
        new_value=new_value,
        source=source,
        created_at=now,
        updated_at=now,
    )
