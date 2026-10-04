from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tripl.models.base import Base, TimestampMixin, UtcDateTime, UUIDMixin
from tripl.models.enum_types import db_enum

if TYPE_CHECKING:
    from tripl.models.alert_delivery import AlertDelivery
    from tripl.models.alert_rule import AlertRule


class AlertDestinationType(enum.StrEnum):
    slack = "slack"
    telegram = "telegram"
    webhook = "webhook"
    email = "email"
    jira = "jira"
    linear = "linear"
    # Local, non-sendable sink for generated demo projects.
    # A ``demo_sink`` destination carries NO credentials and never performs an
    # outbound send: the dispatch worker renders the message and records it
    # locally so Monitors / inbox / delivery history / retry / simulate are all
    # explorable with zero network side effects. It is demo-only (creatable only
    # on ``Project.is_demo`` projects) and is kept out of the user-selectable
    # "create a real destination" options. Added via ALTER TYPE migration
    # f1a2b3c4d5e8.
    demo_sink = "demo_sink"


class AlertDestination(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "alert_destinations"
    __table_args__ = (Index("ix_alert_destination_project", "project_id"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
    )
    type: Mapped[str] = mapped_column(db_enum(AlertDestinationType, "alert_destination_type"))
    name: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # Hold this destination's alerts back and deliver them on a cadence instead
    # of after every metrics collection. NULL — the default, and what every
    # destination created before this column carries — means IMMEDIATE, i.e.
    # exactly today's behaviour: `_prepare_alert_deliveries` mints deliveries
    # and `collect_metrics` dispatches them at the end of the run.
    #
    # A 5-field cron expression otherwise, evaluated in the owning project's
    # timezone (`Project.timezone`). The UI's friendly presets ("daily at
    # 09:00") are cron strings the frontend generates, so there is exactly ONE
    # encoding of a cadence in the database and one parser that reads it
    # (`core/alert_schedule.parse_cron`).
    delivery_schedule_cron: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Watermark for the cadence: the fire instant of the last window this
    # destination flushed — the scheduled instant, not the clock the flush ran
    # at. That keeps the due test a clean total order and makes the window claim
    # single-flight: a second tick, or a second worker, recomputes the same
    # instant for the same window and loses the compare-and-set
    # (`worker/tasks/alert_flush`). A watermark advanced to `now()` on each
    # flush would be a new value every time and could never reject anything.
    #
    # `now()` is written here only as the SEED: attaching or changing a cadence
    # stamps the current clock (`services/_alerting_destinations`, plus the
    # defensive first-tick adoption in the flusher) so the first digest is the
    # next real fire rather than a backlog dump. NULL means no cadence — or a
    # destination that was disabled, which forgets its alerting state.
    #
    # The claim is per INSTANT, not per wall-clock time. The autumn fold's two
    # 02:30s are two instants an hour apart, so both are claimed — two windows
    # that day, each carrying only what was buffered since the previous one.
    #
    # UtcDateTime rather than DateTime(timezone=True): SQLite hands tz-aware
    # columns back naive, and every read of this value is compared against an
    # aware `datetime.now(UTC)` (see models/base.py:11-18).
    last_flushed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    webhook_url_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    bot_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    chat_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Generic webhook channel: arbitrary https endpoint + optional secret header.
    target_url_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    webhook_header_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    webhook_header_value_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Email channel: per-destination recipient list (comma-separated string), plus
    # optional from-address and subject-template overrides. SMTP credentials live
    # at the instance level (settings.smtp_*) so destinations stay credential-free.
    email_recipients: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_from_address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email_subject_template: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Jira channel: REST API v3 — Basic auth with auth_email + api_token (encrypted),
    # creates an issue per delivery on jira_project_key with jira_issue_type.
    jira_base_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    jira_auth_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    jira_api_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    jira_project_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    jira_issue_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Linear channel: GraphQL — Authorization header with api_key (encrypted),
    # creates an issue on linear_team_id with optional state + label ids (CSV).
    linear_api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    linear_team_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    linear_state_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    linear_label_ids: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    rules: Mapped[list[AlertRule]] = relationship(
        back_populates="destination",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    deliveries: Mapped[list[AlertDelivery]] = relationship(
        back_populates="destination",
        cascade="all, delete-orphan",
    )
