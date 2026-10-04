from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin, UUIDMixin
from tripl.models.domain_enums import (
    MetricAggregation,
    MetricComposition,
    MetricKind,
    MetricStatus,
    ScanInterval,
)
from tripl.models.enum_types import db_enum

# Persisted ``last_collection_status`` markers (String(32) below).
#
# Here rather than in the collector because ``mark_collection_error`` is here,
# and a status constant that lived one import away from the only method that
# writes it would be the same split this relocation exists to close.
COLLECTION_STATUS_RUNNING = "running"
COLLECTION_STATUS_SUCCESS = "success"
COLLECTION_STATUS_ERROR = "error"


class MetricDefinition(UUIDMixin, TimestampMixin, Base):
    """A user-defined metric in the catalog, monitored alongside events.

    Metrics are a GLOBAL, project-scoped layer (NOT plan-branched): they carry
    ``project_id`` only and are excluded from branch deep-copy/merge. An
    ``event_composition`` metric references canonical (main-branch) events via the
    numerator/denominator FKs; those ids are taken from main and may drift if the
    referenced rows are replaced by a branch merge (an accepted trade-off of the
    global model).

    A metric is defined in one of three ``kind``s:
    - ``fact``: an aggregation over a column of a separately-defined ``FactTable``
      (referenced by ``fact_table_id``); the column/aggregation/filter (and any
      ratio numerator/denominator) live in ``config``.
    - ``sql``: a user-authored SELECT returning a per-bucket measure (config:
      ``metric_sql``, ``time_column``).
    - ``event_composition``: derived from already-collected ``event_metrics`` —
      ``single`` count, ``ratio`` A/B, or ``per_distinct_user`` (``composition``
      + numerator/denominator refs).
    """

    __tablename__ = "metric_definitions"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_metric_def_project_name"),
        Index("ix_metric_def_project_order", "project_id", "order"),
        Index("ix_metric_def_project_status", "project_id", "status"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))

    # --- Catalog presentation (mirrors EventType) ---
    name: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    color: Mapped[str] = mapped_column(String(7), default="#6366f1")
    order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Display unit hint, e.g. "count", "%", "ms", "users". NULL = unitless.
    unit: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # --- Lifecycle (SIMPLE: no 7-state machine, no merge-rank) ---
    status: Mapped[str] = mapped_column(
        db_enum(MetricStatus, "metric_status"),
        default=MetricStatus.draft,
        server_default=MetricStatus.draft.value,
        nullable=False,
    )
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    reviewed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )

    # --- Definition kind + kind-specific config ---
    kind: Mapped[str] = mapped_column(db_enum(MetricKind, "metric_kind"), nullable=False)
    # Aggregation for a ``fact`` single operand (count/sum/avg/min/max/count_distinct).
    aggregation: Mapped[str | None] = mapped_column(
        db_enum(MetricAggregation, "metric_aggregation"), nullable=True
    )
    # Composition operator for ``event_composition`` (single/ratio/per_distinct_user)
    # and ``fact`` (single/ratio).
    composition: Mapped[str | None] = mapped_column(
        db_enum(MetricComposition, "metric_composition"), nullable=True
    )
    # Free-form, kind-specific scalar config validated by a Pydantic discriminated
    # union at the schema boundary. fact single: measure_column, distinct_column,
    # row_filters (named filters, ANDed), filter_sql (free-text WHERE). fact ratio:
    # numerator/denominator operands. sql: metric_sql, time_column.
    config: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, server_default="{}")

    # --- Fact-table model binding ---
    # A ``fact`` metric references a separately-defined FactTable (the single
    # operand, or the ratio numerator); the detailed fact config
    # (measure/distinct/filter, and the ratio operands) lives in ``config``. NULL
    # for non-``fact`` kinds; the fact table is kept when this metric is unbound
    # (SET NULL).
    fact_table_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("fact_tables.id", ondelete="SET NULL"), nullable=True
    )

    # --- Dimensions (mirror ScanConfig breakdown/version/platform) ---
    breakdown_columns: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    breakdown_values_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    app_version_column: Mapped[str | None] = mapped_column(String(255), nullable=True)
    platform_column: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- Collection binding (HYBRID) ---
    # ``sql`` collects from its own data source on its own interval. ``fact``
    # leaves ``data_source_id`` NULL (the data source comes from the referenced
    # FactTable) but carries its own ``interval``. ``event_composition`` leaves
    # both NULL and reads event_metrics on the source grid.
    data_source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("data_sources.id", ondelete="SET NULL"), nullable=True
    )
    interval: Mapped[str | None] = mapped_column(
        db_enum(ScanInterval, "scan_interval"), nullable=True
    )
    # Splits a long collection window into interval-aligned chunks (see ScanConfig).
    replay_chunk_interval: Mapped[str | None] = mapped_column(
        db_enum(ScanInterval, "scan_interval"), nullable=True
    )

    # --- event_composition references (canonical main-branch events) ---
    numerator_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"), nullable=True
    )
    numerator_event_type_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("event_types.id", ondelete="SET NULL"), nullable=True
    )
    denominator_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"), nullable=True
    )
    denominator_event_type_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("event_types.id", ondelete="SET NULL"), nullable=True
    )

    # --- Monitoring ---
    anomaly_detection_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )

    # --- Collection status (inline for v1; a job table can come later) ---
    last_collected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Exclusive upper bound of the latest successfully processed collection
    # window. Unlike ``last_collected_at`` this is source-grid progress, so a
    # successful empty warehouse response still advances the scheduler.
    last_collection_window_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_collection_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_collection_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # When the newest failure was stamped. Symmetric with ``last_collected_at``,
    # and written ONLY by the collection cycle — which is the whole point. The
    # dispatcher's post-error cooldown used to measure from ``updated_at``, and
    # ``TimestampMixin`` sets ``onupdate=func.now()``, so an operator editing a
    # broken metric in order to fix it restarted the very cooldown they were
    # waiting out.
    last_collection_failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def mark_collection_error(self, message: str) -> None:
        """Put this metric into the error state — the only way it is entered.

        Stamps the timestamp alongside the status because the dispatcher's
        post-error cooldown measures from it: a site that set the status alone
        would leave the metric cooling down from whenever it last failed, or —
        on a first failure — not cooling down at all, which is the retry storm
        the backoff exists to stop. That exact shape has taken this repository's
        production down twice.

        It lives on the MODEL rather than in ``worker.tasks.metrics.metric_collect``
        where it was written, because the seventh caller is not a worker: the
        group-rule merge in ``core.analyzers`` has to fail a metric whose two
        ratio operands have just collapsed onto one event, and ``core`` must
        never import ``worker``. The old home's own docstring named the hazard —
        "a rule six call sites must remember is one the seventh forgets" — so
        the rule moved somewhere every caller can reach rather than being
        copied. ``metric_collect.mark_collection_error`` delegates here, so no
        existing import site changed.

        Does NOT commit. The callers differ on that — one rolls a partial write
        back first, another must preserve an earlier commit — and folding a
        commit in here would take the decision away from them.
        """
        self.last_collection_status = COLLECTION_STATUS_ERROR
        self.last_collection_error = message
        self.last_collection_failed_at = datetime.now(UTC)
