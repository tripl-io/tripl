from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Float, ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tripl.models.base import Base, UUIDMixin
from tripl.models.domain_enums import AlertDriftType, AnomalyDirection, MetricScopeType
from tripl.models.enum_types import db_enum

if TYPE_CHECKING:
    from tripl.models.alert_delivery import AlertDelivery


# ``scope_name`` is the human label an alert shows for the thing that fired.
# Three tables store one and all three declare it VARCHAR(255): this one,
# ``alert_pending_items`` and ``anomaly_scope_overrides``. Its sources are
# wider. ``Event.name`` is String(500) and ``schemas/event.py`` admits every
# character of it, while the drift scopes append ``.{field}`` to an event or
# event-type name and so overflow 255 even when the name alone does not.
#
# Untrimmed that is not cosmetic. Postgres rejects the INSERT with "value too
# long for type character varying(255)", and ``_prepare_alert_deliveries`` runs
# inside ``collect_metrics`` with no savepoint — so one long-named event rolled
# back the anomaly recalculation, the rule and cooldown state and every
# delivery for every scope of that scan config, and because the cooldown never
# committed the next tick failed identically for as long as the anomaly stayed
# active. SQLite ignores VARCHAR(n) entirely, which is why the
# suite never saw it.
#
# Widening the three columns was the alternative and was rejected for the
# reason ``services/audit_service.py`` already records for this same bug one
# table over: a migration whose downgrade has to truncate live
# rows, for a display-only field. Display-only is checkable here — nothing keys
# on the label. ``dispatch._correlation_group_id`` hashes the scope's PARTITION
# (its scan config, or a literal for a project-global metric scope) with
# rule:scope_type:scope_ref:direction, and the false-positive ratchet keys on
# ``(scan_config_id, scope_type, scope_ref)``.
#
# The guard lives on the model rather than beside a writer because the writers
# sit in two layers with no other way to share one: ``alert_payload`` under
# ``worker/``, and ``_event_generator_merge`` / ``_event_generator_merge_refs``
# under ``core/``, which may not import ``tripl.worker`` — a rule stated in
# ``_event_generator_merge_refs``'s module docstring, with
# ``MetricDefinition.mark_collection_error`` as the precedent for moving a
# helper down here to satisfy it. ``services/alerting_rendering.trim_alert_text``
# is the same three lines and deliberately stays separate: it is the generic
# alert-text trimmer, defaulted to 500, that the live send and the rule replay
# share, while this one is bound to the column's width.
SCOPE_NAME_MAX_LEN = 255


def trim_scope_name(value: str) -> str:
    """Fit a scope's display label into the ``scope_name`` column.

    An ellipsis rather than the hard slice ``audit_service`` uses for
    ``target_name``: that column is read back by machines, this one is read by
    a person in a Slack message or the Inbox, where a name that simply stops is
    indistinguishable from one that was authored that way.
    """
    if len(value) <= SCOPE_NAME_MAX_LEN:
        return value
    return value[: SCOPE_NAME_MAX_LEN - 3] + "..."


class AlertDeliveryItem(UUIDMixin, Base):
    __tablename__ = "alert_delivery_items"
    __table_args__ = (
        Index("ix_alert_delivery_item_delivery", "delivery_id"),
        # The alerting inbox and every per-incident view filter on the
        # correlation group; _alerting_deliveries.list_deliveries has no other
        # predicate to fall back on. The table has no retention, so an
        # unindexed scan here grows for the life of the deployment.
        Index("ix_alert_delivery_item_correlation_group", "correlation_group_id"),
    )

    delivery_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("alert_deliveries.id", ondelete="CASCADE"),
    )
    scope_type: Mapped[str] = mapped_column(db_enum(MetricScopeType, "metric_scope_type"))
    scope_ref: Mapped[str] = mapped_column(String(64))
    scope_name: Mapped[str] = mapped_column(String(SCOPE_NAME_MAX_LEN))
    event_type_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("event_types.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"),
        nullable=True,
    )
    bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    direction: Mapped[str] = mapped_column(db_enum(AnomalyDirection, "anomaly_direction"))
    # Floats: fractional catalog metrics deliver sub-unit actuals/deltas;
    # count scopes keep writing whole numbers.
    actual_count: Mapped[float] = mapped_column(Float)
    expected_count: Mapped[float] = mapped_column(Float)
    absolute_delta: Mapped[float] = mapped_column(Float)
    percent_delta: Mapped[float] = mapped_column()
    details_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    monitoring_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    drift_field: Mapped[str | None] = mapped_column(String(255), nullable=True)
    drift_type: Mapped[str | None] = mapped_column(
        db_enum(AlertDriftType, "alert_drift_type"), nullable=True
    )
    sample_value: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Start of the window this item's comparison was measured over; ``bucket``
    # is its end. NULL for every scope whose window IS the bucket — only
    # release regressions, measured over the activation-anchored rollout
    # overlap, set it. Snapshotted here rather than read back from
    # ReleaseRegression at render time because those rows are deleted and
    # rewritten on every scan, so a delivery retried from the Inbox would find
    # nothing and silently render an unqualified line.
    window_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The handle for one ongoing INCIDENT, and the id the alert inbox acts on
    # (``POST /alert-inbox/{correlation_group_id}/actions``).
    #
    # ``dispatch._correlation_group_id`` owns the key and is the only place it
    # is derived. It is NAMED here rather than restated because restating it is
    # how this comment went stale: it described a key with the bucket still in
    # it, and a column that was NULL for singletons, long after both had stopped
    # being true. Go and read that function; do not re-copy it into this file.
    #
    # Two consequences a reader keeps guessing wrong:
    #
    # * It is set on EVERY item, not only on co-fired ones. The inbox lists an
    #   item only if it has one (``_alerting_deliveries._INBOX_GROUP_SELECT``
    #   filters ``is_not(None)``), so while a group meant "2+ peers" a solitary
    #   alert never reached the inbox and could not be acknowledged, muted or
    #   resolved at all — and solitary is the common case.
    #   "Did this co-fire?" is therefore a peer COUNT within one delivery, which
    #   is what ``AlertDeliveryRow.buildCorrelationLabels`` counts before it
    #   letters a row, and never a test for this column being non-NULL.
    # * The bucket is deliberately absent from the key, so peers inside one
    #   group are the same scope over time rather than the scopes that fired
    #   together, and a group outlives the hour that opened it.
    #
    # Nullable only for history. Older rows carry none,
    # and ``_alerting_deliveries.list_deliveries(ungrouped=True)`` exists to
    # give them a section of their own rather than drop them silently. Nothing
    # writes NULL today: the buffered twin ``AlertPendingItem`` declares the
    # same column NOT NULL, and ``alert_flush`` copies it straight across onto
    # the item it mints.
    correlation_group_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    delivery: Mapped[AlertDelivery] = relationship(back_populates="items")
