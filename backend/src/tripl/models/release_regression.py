from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, UUIDMixin
from tripl.models.domain_enums import (
    MetricScopeType,
    ReleaseComparabilityReason,
    ReleaseRegressionKind,
)
from tripl.models.enum_types import db_enum


class ReleaseRegression(UUIDMixin, Base):
    """A detected per-release regression for an event (or event type).

    Captures that, relative to the previous active release, an event either
    disappeared (``kind = "missing"``) or fired materially less than its
    previous-release composition would predict (``kind = "volume_drop"``). One
    row per (scan, scope, new release); recomputed in full on every scan.

    The model itself — maturity gate, comparison window, composition-share
    normalization, and the missing/volume_drop split — is stated in
    ``tripl.core.analyzers.release_regression``, which implements it.
    """

    __tablename__ = "release_regressions"
    __table_args__ = (
        UniqueConstraint(
            "scan_config_id",
            "scope_type",
            "scope_ref",
            "version",
            name="uq_release_regression_scope_version",
        ),
        Index("ix_release_regression_event", "event_id"),
        Index("ix_release_regression_event_type", "event_type_id"),
    )

    scan_config_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scan_configs.id", ondelete="CASCADE"),
    )
    scope_type: Mapped[str] = mapped_column(db_enum(MetricScopeType, "metric_scope_type"))
    scope_ref: Mapped[str] = mapped_column(String(64))
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_type_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("event_types.id", ondelete="SET NULL"),
        nullable=True,
    )
    app_version_column: Mapped[str] = mapped_column(String(255))
    version: Mapped[str] = mapped_column(String(500))  # the release under test (v_new)
    previous_version: Mapped[str] = mapped_column(String(500))  # baseline release (v_prev)
    kind: Mapped[str] = mapped_column(db_enum(ReleaseRegressionKind, "release_regression_kind"))
    observed_count: Mapped[int] = mapped_column(BigInteger)
    expected_count: Mapped[float] = mapped_column(Float)
    ratio: Mapped[float] = mapped_column(Float)
    share_prev: Mapped[float] = mapped_column(Float)
    share_new: Mapped[float] = mapped_column(Float)
    release_share: Mapped[float] = mapped_column(Float)
    window_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_to: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class ReleaseComparability(UUIDMixin, Base):
    """Whether the latest release could be judged at all, per scan and scope.

    Zero ``ReleaseRegression`` rows is two different facts — "this release is
    fine" and "this release cannot be judged yet" — and until this table existed
    the two payloads were byte-identical: the verdict was computed, logged at
    INFO, and dropped. One row per (scan, scope type), rewritten by every
    recalculation alongside the regression rows, so it always describes the same
    pass that produced (or withheld) them.

    ``version``/``previous_version`` are nullable because the release pair is
    only known once two eligible releases were found; ``reason ==
    no_baseline`` leaves both NULL.

    The pair is the comparison the stored regression rows came from, which is
    not always the latest release's: while the newest release ramps with
    nothing found, the pass hands over to its predecessor's comparison
    (``handed_over``). ``latest_active_version`` always names the newest active
    release, so "latest" is read from it and never from ``version``, and
    ``latest_release_share`` is that release's share of traffic over its own
    comparison window, what the hand-over label quotes.
    """

    __tablename__ = "release_comparabilities"
    __table_args__ = (
        UniqueConstraint(
            "scan_config_id",
            "scope_type",
            name="uq_release_comparability_scan_scope",
        ),
    )

    scan_config_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scan_configs.id", ondelete="CASCADE"),
    )
    scope_type: Mapped[str] = mapped_column(db_enum(MetricScopeType, "metric_scope_type"))
    app_version_column: Mapped[str] = mapped_column(String(255))
    version: Mapped[str | None] = mapped_column(String(500), nullable=True)
    previous_version: Mapped[str | None] = mapped_column(String(500), nullable=True)
    comparable: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(
        db_enum(ReleaseComparabilityReason, "release_comparability_reason")
    )
    latest_active_version: Mapped[str | None] = mapped_column(String(500), nullable=True)
    latest_release_share: Mapped[float | None] = mapped_column(Float, nullable=True)
    handed_over: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    emerging_share: Mapped[float] = mapped_column(Float)
    # The bound ``emerging_share`` was tested against, stored rather than read
    # from config at render time so the number an operator sees is the one the
    # verdict was actually made with.
    max_emerging_share: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
