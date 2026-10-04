from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tripl.models.base import Base, TimestampMixin, UtcDateTime, UUIDMixin
from tripl.models.domain_enums import AlertMessageFormat
from tripl.models.enum_types import db_enum

if TYPE_CHECKING:
    from tripl.models.alert_destination import AlertDestination
    from tripl.models.alert_rule_filter import AlertRuleFilter

# Percent gap between observed and expected below which a VOLUME anomaly is not
# worth a message. Drift and release-regression scopes return before the numeric
# thresholds in ``alerting_matching.rule_matches_anomaly`` and are unaffected.
#
# The gate is |actual - expected| / expected * 100, and it is asymmetric: a
# SPIKE reaches 100 at double the expectation, but a DROP reaches 100 only when
# volume falls to zero. The earlier default of 100 was measured on volume
# (replaying 24 hours of live iOS collections gave 436 items in 54 deliveries at
# 0, 267/32 at 50 and 37/7 at 100), but it also meant the obvious "tell me when
# X drops" rule ignored a 50 % or a 90 % fall. 30 is a move worth hearing
# about in either direction, and the rule editor starts new rules there too
# (``DEFAULT_RULE_MIN_PERCENT_DELTA`` in the frontend). Migration a4c8e2f61b93
# moved the server default; stored rows keep the value they were saved with.
#
# A scope going dark still passes: actual 0 against any positive expectation is
# exactly 100%, and the comparison is strict.
DEFAULT_MIN_PERCENT_DELTA = 30.0


class AlertRule(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "alert_rules"
    __table_args__ = (
        Index("ix_alert_rule_destination", "destination_id"),
        Index("ix_alert_rule_scan_config", "scan_config_id"),
    )

    destination_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("alert_destinations.id", ondelete="CASCADE"),
    )
    # Narrow the rule to ONE scan. NULL — the default, and what every rule
    # created before this column carries — means the whole project, which is the
    # behaviour rules have always had: a rule hangs off a destination, a
    # destination off a project, and ``_prepare_alert_deliveries`` runs once per
    # scan config, so one rule fired for every scan there is. On the live
    # instance a single low-value legacy scan contributed 82% of the alert
    # stream and there was no way to exclude it — ``AlertRuleFilterField`` has
    # exactly three values (event_type, event, direction) and
    # ``filter_matches_anomaly`` passes anything else through, so no filter could
    # name a scan.
    #
    # ondelete is SET NULL and NOT the obvious CASCADE: deleting a scan would
    # otherwise delete the rule outright — name, thresholds, templates, filters
    # — and take its delivery history with it through ``AlertDelivery.rule_id``,
    # including deliveries the rule produced for OTHER scans while it was still
    # project-wide. A bare SET NULL is not enough on its own either, because a
    # rule narrowed to the noisiest scan would silently re-widen to the whole
    # project the moment that scan is deleted and start paging on every other
    # one. ``scan_service.delete_scan_config`` therefore also DISABLES the rules
    # it unbinds, so the orphan is visible in the UI and inert until re-aimed.
    scan_config_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scan_configs.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    include_project_total: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
    )
    include_event_types: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
    )
    include_events: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    include_schema_drifts: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
    )
    include_distribution_drifts: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
    )
    include_variable_value_drifts: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
    )
    # Opt-in to app-version release-regression signals (events that disappeared
    # or dropped in the latest release). Off by default, like the drift toggles,
    # so regressions are a deliberate subscription rather than riding on the
    # generic event/event-type anomaly rules.
    include_release_regressions: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
    )
    # Opt-in to anomalies on user-defined catalog metric series (scope_type
    # ``metric``). Off by default — SAFE OFF — so metric anomalies only deliver
    # when a rule deliberately subscribes to them.
    include_metrics: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
    )
    # Opt-in to "data is late" alerts: one ``source_freshness`` candidate per
    # late or overdue scan (#269). Off by default like the other families.
    include_source_freshness: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
    )
    # Opt-in to lifecycle findings (#258): one ``lifecycle`` candidate per open
    # ``LifecycleFinding`` — a deprecated event past its sunset that still
    # receives data, or a successor nobody sends. Off by default like the other
    # families.
    include_lifecycle: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
    )
    # Opt-in to property drift (F23, #306): one ``property_drift`` candidate per
    # open ``PropertyDrift`` — a new property, a missing required one, or a
    # type change. Off by default like the other drift families.
    include_property_drifts: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
    )
    # Append an LLM-generated explanation paragraph to delivered alert
    # messages. Off by default; a no-op unless AI features are enabled in
    # instance settings.
    # Also email the owners of every event type / catalog metric a delivery of
    # this rule touches (F07, #260), after the rule's own destination has the
    # message. Off by default; unowned scopes behave exactly as without it. The
    # follow-up is ``worker.tasks.alert_owner_notify.notify_owners``, and a
    # failure there never fails the rule's own delivery.
    notify_owners: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
    )
    ai_explanation_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
    )
    notify_on_spike: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    notify_on_drop: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    min_percent_delta: Mapped[float] = mapped_column(
        Float,
        default=DEFAULT_MIN_PERCENT_DELTA,
        server_default=str(DEFAULT_MIN_PERCENT_DELTA),
    )
    min_absolute_delta: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    min_expected_count: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    cooldown_minutes: Mapped[int] = mapped_column(Integer, default=1440, server_default="1440")
    message_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    items_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_format: Mapped[str] = mapped_column(
        db_enum(AlertMessageFormat, "alert_message_format"),
        default=AlertMessageFormat.plain.value,
        server_default=AlertMessageFormat.plain.value,
    )
    # Manual snooze for a monitor: while ``muted_until`` is in the future the
    # monitor is muted and delivers nothing. NULL means NOT MUTED — it is what
    # every rule ever created carries, and a rule has no status column to tell
    # "never muted" from "muted forever". Same shape as
    # ``AlertCorrelationState.muted_until`` but read the OPPOSITE way: on an
    # inbox incident a NULL is the INDEFINITE mute. The two checks
    # look alike and must not be unified — see
    # ``AlertInboxActionRequest.validate_action``.
    #
    # Timed-only, and deliberately NOT like the inbox mute action this was
    # modelled on: ``MonitorMuteRequest.muted_until`` is required, non-null and
    # must be in the future, because accepting a null HERE would mute the whole
    # fleet at once. A rule's permanent lever is ``enabled``.
    #
    # The worker suppresses deliveries for a muted rule on BOTH delivery paths.
    # An older version of this comment called that "a separate follow-up", and
    # on the strength of it the Monitors UI shipped a Mute button that wrote
    # this column and changed nothing. Today:
    # ``metrics.dispatch._prepare_alert_deliveries`` skips a muted rule BEFORE
    # it splits immediate destinations from scheduled ones, so a muted monitor
    # neither mints an ``AlertDelivery`` nor buffers an ``AlertPendingItem``;
    # ``alert_flush._build_digest`` re-checks when the digest is built, so a
    # mute set during a hold window drops the items already buffered along with
    # the claim instead of releasing them once the mute lapses.
    # ``alerts._assert_rule_still_active`` re-checks queued deliveries just
    # before outbound send, after rendering may have taken time.
    # ``metrics.freshness_sweep`` (overdue source-freshness alerts, #269) skips a
    # muted rule the same way before it mints anything.
    # ``alert_owner_notify._rule_is_muted`` (owner email routing, #260) skips
    # emailing the owners of a muted rule's event types. The rule's
    # open/close state is updated before the dispatch check, deliberately, so a
    # mute does not leave the monitor stuck "firing" on a stale scope. The
    # API-side predicate is ``_alerting_monitors.is_rule_muted``.
    muted_until: Mapped[datetime | None] = mapped_column(
        UtcDateTime(),
        nullable=True,
    )

    destination: Mapped[AlertDestination] = relationship(back_populates="rules")
    filters: Mapped[list[AlertRuleFilter]] = relationship(
        back_populates="rule",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="AlertRuleFilter.position",
    )
