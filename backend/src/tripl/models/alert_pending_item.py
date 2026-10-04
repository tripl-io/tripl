from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.alert_delivery_item import SCOPE_NAME_MAX_LEN
from tripl.models.base import Base, TimestampMixin, UUIDMixin
from tripl.models.domain_enums import AlertDriftType, AnomalyDirection, MetricScopeType
from tripl.models.enum_types import db_enum


class AlertPendingItem(UUIDMixin, TimestampMixin, Base):
    """One alert held back, waiting for its destination's next digest window.

    A destination with ``delivery_schedule_cron`` set does not deliver after
    every metrics collection. Instead ``_prepare_alert_deliveries`` upserts the
    matched signals here, and ``flush_due_alert_digests`` turns the accumulated
    rows into ordinary ``AlertDelivery`` + ``AlertDeliveryItem`` rows when a
    cron boundary passes.

    Deliberately NOT a held ``AlertDelivery``. A delivery parked in ``pending``
    is swept and sent by ``requeue_stranded_alert_deliveries`` within 15
    minutes (worker/tasks/maintenance.py), so the feature would fail OPEN on a
    15-minute fuse; and a delivery is only ever born at flush time here, which
    keeps ``AlertDeliveryStatus``, the reaper's predicate, the Inbox, and the
    four ``created_at``-ordered read paths untouched.

    A row lives exactly as long as its destination's hold does, and leaves this
    table one of four ways: the flush CLAIMS it by DELETE in the transaction
    that mints its delivery; the 14-day sweep drops one whose cadence has
    stopped firing; disabling the destination discards the whole buffer; and so
    does clearing the destination's cadence, because "Immediately" has no window
    left to wait for and the immediate path delivers those scopes itself on the
    next collection (tripl-0zpq.38 — both paths delivering them is a double
    send, since a held scope's ``AlertRuleState.last_notified_at`` is still
    NULL). Nothing prunes a row merely because its scope fell quiet.

    Every value is SNAPSHOTTED rather than FK-referenced back to the anomaly
    that produced it, for the reason ``AlertDeliveryItem.window_from`` already
    documents: the source rows are deleted and rewritten on every scan, so a
    digest assembled a day later would find nothing.
    """

    __tablename__ = "alert_pending_items"
    __table_args__ = (
        # One row per scope per DIRECTION per rule per destination: a scope that
        # re-fires the SAME WAY on every 5-minute collection collapses into ONE
        # digest line carrying its latest numbers, which is what "aggregate up
        # to the moment it is sent" means. The key is `_correlation_group_id`'s
        # five components plus the destination partition, so the buffer's
        # identity and the inbox's incident identity are the same thing — and
        # that is where `direction` comes from, not from a wish for two lines.
        #
        # So a scope that dropped and later spiked inside one window holds TWO
        # rows and ships two lines, one in each of the digest's groups. They are
        # two incidents and two Inbox cards; a single `correlation_group_id`
        # column cannot name both, so folding them together would leave one of
        # the operator's decisions unhonourable. What this
        # aggregates is each INCIDENT up to the moment of sending, not the
        # scope — `dispatch._buffer_pending_items` argues it in full.
        UniqueConstraint(
            "destination_id",
            "rule_id",
            "scan_config_id",
            "scope_type",
            "scope_ref",
            "direction",
            name="uq_alert_pending_item_scope",
        ),
        # The same key minus the config, for the ``metric`` scope that stores
        # NULL there. SQL treats NULLs as DISTINCT, so the constraint above can
        # never fire for one of those rows: without this index a three-config
        # project would buffer three rows for one project-wide metric anomaly
        # and the digest would carry it three times — silently, with no
        # duplicate-key error anywhere to announce it. Same shape and same
        # reason as ``uq_metric_anomaly_metric_scope`` on MetricAnomaly.
        #
        # ``sqlite_where`` is LOAD-BEARING: the unit suite builds its schema from
        # ``Base.metadata.create_all`` and never runs the Postgres-gated
        # migration, so this is the only place SQLite learns the index exists —
        # and it is the index the buffer's upsert names as its conflict target
        # (``dispatch._PENDING_ITEM_METRIC_CONFLICT_KEYS``).
        Index(
            "uq_alert_pending_item_metric_scope",
            "destination_id",
            "rule_id",
            "scope_type",
            "scope_ref",
            "direction",
            unique=True,
            postgresql_where=text("scan_config_id IS NULL"),
            sqlite_where=text("scan_config_id IS NULL"),
        ),
        # The flusher's hot read is "everything buffered for this destination".
        # The age sweep scans by recency; without this it degrades into a full
        # scan of a table that has no other retention.
        Index("ix_alert_pending_item_updated", "updated_at"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
    )
    destination_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("alert_destinations.id", ondelete="CASCADE"),
    )
    rule_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("alert_rules.id", ondelete="CASCADE"),
    )
    # For every scope but ``metric`` this is the firing scan config. A metric
    # scope is project-global and stores NULL, mirroring ``AlertRuleState`` and
    # ``MetricAnomaly`` — one buffered row for one project-wide anomaly,
    # however many configs collect it. ``uq_alert_pending_item_metric_scope``
    # is what enforces that, because the composite constraint cannot.
    #
    # The NULL also puts metric rows out of reach of the CASCADE below, which
    # is a fix rather than a side effect: while they anchored on a real config,
    # deleting that config destroyed the metric alerts held for the next digest
    # — never delivered, never recoverable. The cascade stays
    # right for config-scoped rows, whose alerts are about that scan.
    #
    # ``project_id`` above is what the flush uses to resolve a config to RENDER
    # a project-global digest against (``alert_flush._build_digest``); this
    # column is identity, that one is presentation.
    scan_config_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scan_configs.id", ondelete="CASCADE"),
        nullable=True,
    )
    scan_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scan_jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    # The id of the anomaly row this was snapshotted from. Debug/provenance
    # only — never dereferenced, because the row is gone by flush time.
    source_anomaly_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    # ── The AlertMatchCandidate snapshot ──────────────────────────────────
    scope_type: Mapped[str] = mapped_column(db_enum(MetricScopeType, "metric_scope_type"))
    scope_ref: Mapped[str] = mapped_column(String(64))
    # Same width, and deliberately the same constant, as
    # ``AlertDeliveryItem.scope_name`` — the flush copies this label straight
    # onto the delivery item it mints (``alert_flush._build_digest``), so the
    # two columns cannot be sized independently. See that constant for why the
    # label is trimmed rather than the columns widened.
    scope_name: Mapped[str] = mapped_column(String(SCOPE_NAME_MAX_LEN))
    # CASCADE, where AlertDeliveryItem uses SET NULL. That table is HISTORY —
    # a message already sent, which must keep its record even after the event
    # it named is gone. This one is pre-delivery state: an alert about an event
    # that has since been deleted (or merged away) must not be delivered at
    # all, and the next collection re-buffers the survivor's own anomaly under
    # the correct scope_ref anyway. See the DELIBERATELY_CASCADES entry in
    # tests/test_event_fk_classification.py.
    event_type_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("event_types.id", ondelete="CASCADE"),
        nullable=True,
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"),
        nullable=True,
    )
    bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    direction: Mapped[str] = mapped_column(db_enum(AnomalyDirection, "anomaly_direction"))
    actual_count: Mapped[float] = mapped_column(Float)
    expected_count: Mapped[float] = mapped_column(Float)
    drift_field: Mapped[str | None] = mapped_column(String(255), nullable=True)
    drift_type: Mapped[str | None] = mapped_column(
        db_enum(AlertDriftType, "alert_drift_type"), nullable=True
    )
    sample_value: Mapped[str | None] = mapped_column(String(500), nullable=True)
    window_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Stamped at buffer time from the value dispatch already computed, NOT
    # recomputed at flush. The inbox holds acknowledgements against the id this
    # row carries, so a second derivation is a second chance to mint a different
    # one — which is exactly what happened while ``_correlation_group_id`` hashed
    # the FIRING scan config and this row was keyed project-wide.
    #
    # Both now hash the partition the row STORES — NULL for a project-global
    # metric scope (``dispatch._scope_partition_id``) — so the buffer's key and
    # the incident's key agree by construction. That is a reason
    # to keep copying the value across, not a licence to recompute it: agreement
    # today is not a promise that a flush-time derivation would see the same
    # inputs the collection did.
    correlation_group_id: Mapped[uuid.UUID] = mapped_column(Uuid)

    # How many collections have re-offered this scope while it waited. Renders
    # as "seen N times" context and makes a stuck buffer obvious in support.
    observation_count: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
