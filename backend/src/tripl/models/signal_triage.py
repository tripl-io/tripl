from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin, UtcDateTime, UUIDMixin
from tripl.models.domain_enums import MetricScopeType, SignalExpectedReason, SignalTriageAction
from tripl.models.enum_types import db_enum


class SignalTriage(UUIDMixin, TimestampMixin, Base):
    """A user's triage of an open signal (F01 #254).

    ``acknowledged`` / ``muted`` apply only to signals NOT routed to an
    incident. The verdicts (``expected``, ``tracking_bug``, ``false_positive``,
    ``real_issue``) apply to every signal; on a routed one the incident stays
    the source of truth and the row only refines what its status means.

    Keyed the way a signal keys itself — ``(scan_config_id, scope_type,
    scope_ref)`` like ``MetricAnomaly``, with a NULL ``scan_config_id`` for
    project-global ``metric`` scopes. ``acknowledged`` and the verdict rows also
    carry the ``bucket`` of the one signal they answer; a ``muted`` row carries
    no bucket because it hides every signal on the scope until ``muted_until``
    (NULL = until someone unmutes it). The check constraint pins that pairing.

    Deleting the row is the undo.
    """

    __tablename__ = "signal_triage"
    __table_args__ = (
        CheckConstraint(
            "(action = 'muted') = (bucket IS NULL)",
            name="ck_signal_triage_bucket_matches_action",
        ),
        # Per-signal rows on a scan scope. SQL treats NULLs as DISTINCT, so this
        # constraint never fires for a catalog metric (NULL scan_config_id) nor
        # for a mute (NULL bucket); the partial indexes below cover those spaces,
        # the same way ``uq_metric_anomaly_metric_scope`` does for MetricAnomaly.
        UniqueConstraint(
            "project_id",
            "scan_config_id",
            "scope_type",
            "scope_ref",
            "action",
            "bucket",
            name="uq_signal_triage_signal",
        ),
        Index(
            "uq_signal_triage_metric_signal",
            "project_id",
            "scope_type",
            "scope_ref",
            "action",
            "bucket",
            unique=True,
            postgresql_where=text("scan_config_id IS NULL"),
            sqlite_where=text("scan_config_id IS NULL"),
        ),
        Index(
            "uq_signal_triage_scope_mute",
            "project_id",
            "scan_config_id",
            "scope_type",
            "scope_ref",
            unique=True,
            postgresql_where=text("bucket IS NULL"),
            sqlite_where=text("bucket IS NULL"),
        ),
        Index(
            "uq_signal_triage_metric_mute",
            "project_id",
            "scope_type",
            "scope_ref",
            unique=True,
            postgresql_where=text("bucket IS NULL AND scan_config_id IS NULL"),
            sqlite_where=text("bucket IS NULL AND scan_config_id IS NULL"),
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
    )
    # NULL for ``metric`` scope (project-global catalog series).
    scan_config_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scan_configs.id", ondelete="CASCADE"),
        nullable=True,
    )
    scope_type: Mapped[str] = mapped_column(db_enum(MetricScopeType, "metric_scope_type"))
    scope_ref: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(db_enum(SignalTriageAction, "signal_triage_action"))
    # The signal's bucket for ``acknowledged`` / ``expected``; NULL for ``muted``.
    bucket: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    # ``muted`` only: when the mute lapses. NULL means until unmuted.
    muted_until: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    # Verdict rows only: the optional free-text note. On ``expected`` it is also
    # written to the chart annotation.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ``expected`` only: why (campaign / release / seasonality / other). NULL on
    # every other action and on an ``expected`` given without a reason.
    expected_reason: Mapped[str | None] = mapped_column(
        db_enum(SignalExpectedReason, "signal_expected_reason"), nullable=True
    )
    # ``expected`` only: the chart annotation the verdict wrote, so undoing the
    # verdict can take its marker off the chart too.
    annotation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chart_annotations.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
