from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import BigInteger, ForeignKey, Index, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin, UtcDateTime, UUIDMixin


class EventFieldObservation(UUIDMixin, TimestampMixin, Base):
    """The values a scan saw for one field of one event, when it saw more than one.

    An event keeps ONE value per field (``event_field_values``), so when the
    breakdown rows of one scan identity disagree on a field — a structured event
    fired on several screens — the busiest row's value is stored and the rest
    used to be thrown away. This row keeps them, with their row counts, so the
    event page can say "Seen with 2 values: map/main (62%), spot/main (38%)".

    Its own table, not a column on ``event_field_values``, for the reason
    ``VariableValue`` has one: saving the event form and merging a branch both
    delete and re-insert every ``EventFieldValue``, and an observation riding on
    them would be wiped by every save.

    Written only by ``core.analyzers._event_field_observations``, and only for
    MAIN-branch events: scans see main alone. A branch copy reads its main
    twin's row (``services.event_field_observation_service``) instead of holding
    one, so nothing here is deep-copied, snapshotted, diffed or merged — it is
    scan-observed data, excluded from the plan the way ``VariableValue`` is.

    The last run that observed the event replaces the row; counts are never
    summed across runs, because scan windows overlap. A run that saw exactly one
    value deletes it. ``values`` holds at most the top 20 values;
    ``distinct_count`` and ``total_count`` describe the full set, so the reader
    can still say "+N more" and give true shares.
    """

    __tablename__ = "event_field_observations"
    __table_args__ = (
        UniqueConstraint("event_id", "field_definition_id", name="uq_event_field_observation"),
        Index("ix_event_field_observations_project", "project_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    field_definition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("field_definitions.id", ondelete="CASCADE")
    )
    scan_config_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scan_configs.id", ondelete="SET NULL"), nullable=True
    )
    #: ``[{"value": str, "count": int | None}, ...]``, busiest first; a ``None``
    #: count means the adapter returned no row counts for that value.
    values: Mapped[list[dict[str, Any]]] = mapped_column(sa.JSON, default=list, server_default="[]")
    #: Distinct values seen, including those past the 20 kept in ``values``.
    distinct_count: Mapped[int] = mapped_column(Integer)
    #: Sum of every value's count; NULL when any count was unknown.
    total_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(UtcDateTime())
