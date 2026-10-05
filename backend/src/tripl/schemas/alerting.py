import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_serializer, field_validator, model_validator

from tripl.alert_templates import percent_delta_of, percent_delta_or_none
from tripl.alerting_matching import AlertMatchCandidate
from tripl.alerting_validation import (
    DEFAULT_PAGERDUTY_SEVERITY,
    _validate_https_url,
    normalize_optional_secret,
    normalize_required_text,
    validate_email_recipients,
    validate_email_subject_template,
    validate_jira_api_token,
    validate_jira_auth_email,
    validate_jira_issue_type,
    validate_jira_project_key,
    validate_linear_api_key,
    validate_linear_label_ids,
    validate_linear_state_id,
    validate_linear_team_id,
    validate_pagerduty_routing_key,
    validate_pagerduty_severity,
    validate_sender_address,
    validate_slack_webhook_url,
    validate_telegram_bot_token,
    validate_telegram_chat_id,
    validate_webhook_header_name,
    validate_webhook_header_value,
)
from tripl.core.alert_schedule import parse_cron
from tripl.models.alert_delivery import AlertDeliveryStatus
from tripl.models.alert_delivery_item import trim_scope_name
from tripl.models.alert_destination import AlertDestinationType
from tripl.models.alert_rule import DEFAULT_MIN_PERCENT_DELTA
from tripl.models.domain_enums import (
    AlertDriftType,
    AlertInboxStatus,
    AlertMessageFormat,
    AlertRuleFilterField,
    AlertRuleFilterOperator,
    AnomalyDirection,
    MetricScopeType,
)
from tripl.schemas.alert_owner import AlertOwnerNotificationResponse, AlertOwnerRef
from tripl.schemas.time_guards import require_future_instant

# ``note`` is the only member that does NOT change the incident's status: it
# documents one. Saving a note used to require taking an action, so writing down
# why something was a false positive meant first undoing the false positive.
AlertInboxAction = Literal["acknowledge", "resolve", "mute", "reopen", "false_positive", "note"]


class AlertRuleFilterPayload(BaseModel):
    field: AlertRuleFilterField
    operator: AlertRuleFilterOperator
    values: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_filter(self) -> AlertRuleFilterPayload:
        if not self.values:
            raise ValueError("Filter must have at least one value")
        if self.operator in ("eq", "ne") and len(self.values) != 1:
            raise ValueError("Operators '=' and '!=' require exactly one value")
        if self.field == "direction":
            allowed = {"up", "down"}
            invalid = [value for value in self.values if value not in allowed]
            if invalid:
                raise ValueError(
                    "Direction filter values must be 'up' or 'down'",
                )
        else:
            for value in self.values:
                try:
                    uuid.UUID(value)
                except ValueError as exc:
                    raise ValueError(
                        f"Filter value for {self.field} must be a UUID",
                    ) from exc
        return self


class AlertRuleFilterResponse(AlertRuleFilterPayload):
    id: uuid.UUID


class AlertRuleBase(BaseModel):
    # Bounded to the width of ``alert_rules.name`` (String(255)), and stripped by
    # ``normalize_name`` below the way destination names already are. Unbounded,
    # a 300-character name passed this layer and the INSERT failed underneath it
    # with a Postgres StringDataRightTruncation, which nothing catches but the
    # handler of last resort in ``main.py`` — a generic 500 for a body we had
    # already accepted. SQLite ignores VARCHAR widths, so this
    # bound is all the unit suite can see of that contract; the column is what it
    # stands in for, and ``tests/test_alerting_service_regressions.py`` asserts the two
    # numbers are still the same one.
    name: str | None = Field(None, max_length=255)
    enabled: bool | None = None
    # Narrow the rule to one scan config; null (the default) means every scan in
    # the project. On update, ``exclude_unset`` distinguishes "not mentioned"
    # from an explicit null, so a null in the body widens the rule back.
    scan_config_id: uuid.UUID | None = None
    include_project_total: bool | None = None
    include_event_types: bool | None = None
    include_events: bool | None = None
    include_schema_drifts: bool | None = None
    include_distribution_drifts: bool | None = None
    include_variable_value_drifts: bool | None = None
    include_release_regressions: bool | None = None
    include_metrics: bool | None = None
    include_source_freshness: bool | None = None
    include_lifecycle: bool | None = None
    include_property_drifts: bool | None = None
    notify_on_spike: bool | None = None
    notify_on_drop: bool | None = None
    ai_explanation_enabled: bool | None = None
    notify_owners: bool | None = None
    min_percent_delta: float | None = Field(None, ge=0)
    min_absolute_delta: float | None = Field(None, ge=0)
    min_expected_count: float | None = Field(None, ge=0)
    cooldown_minutes: int | None = Field(None, ge=1)
    message_template: str | None = None
    items_template: str | None = None
    message_format: AlertMessageFormat | None = None
    filters: list[AlertRuleFilterPayload] | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        """Strip the rule name and refuse one that is only whitespace.

        Declared on the base so create and PATCH cannot disagree about what a
        name is — the arrangement destination names have had all along (see
        ``AlertDestinationCreate.normalize_name``, which calls the same helper).
        Until this existed a rule could be created called ``""`` while a
        destination on the same screen could not, and ``create_rule`` copied the
        empty string onto the row unchanged.

        ``None`` is returned untouched rather than rejected, and nothing sends it
        today: pydantic never validates an unset default, and an explicit
        ``null`` for ``name`` is already refused by
        ``reject_null_for_required_fields``. The arm stays because this field is
        declared ``str | None`` while that guard is a frozenset a hundred lines
        below — covering the whole declared type here costs one line and cannot
        fall out of step with a set it does not own.
        """
        if value is None:
            return None
        return normalize_required_text(value, field_name="Rule name")

    @model_validator(mode="after")
    def validate_direction(self) -> AlertRuleBase:
        notify_on_spike = self.notify_on_spike
        notify_on_drop = self.notify_on_drop
        if notify_on_spike is False and notify_on_drop is False:
            raise ValueError("At least one alert direction must be enabled")
        return self


class AlertRuleCreate(AlertRuleBase):
    # Re-declared only to make it required. A re-declaration does not inherit the
    # base's ``Field``, so the bound has to be repeated here; ``normalize_name``
    # is registered per field NAME and keeps applying.
    name: str = Field(max_length=255)
    enabled: bool = True
    include_project_total: bool = True
    include_event_types: bool = True
    include_events: bool = True
    include_schema_drifts: bool = False
    include_distribution_drifts: bool = False
    include_variable_value_drifts: bool = False
    include_release_regressions: bool = False
    include_metrics: bool = False
    # Opt-in to "data is late" alerts: one per late/overdue scan config.
    include_source_freshness: bool = False
    # Opt-in to event lifecycle findings (GH #258): sunset overdue and
    # silent successor, one alert per open finding.
    include_lifecycle: bool = False
    # Opt-in to property drift (F23, #306): a new property, a missing required
    # one, or a type change, one alert per open drift.
    include_property_drifts: bool = False
    notify_on_spike: bool = True
    notify_on_drop: bool = True
    ai_explanation_enabled: bool = False
    # Also email the owners of each affected event type / catalog metric
    # (F07, #260). Off by default.
    notify_owners: bool = False
    # A new rule starts at the measured volume threshold rather than wide open;
    # see DEFAULT_MIN_PERCENT_DELTA. Zero is still accepted, for a caller that
    # deliberately wants every deviation.
    min_percent_delta: float = Field(DEFAULT_MIN_PERCENT_DELTA, ge=0)
    min_absolute_delta: float = Field(0, ge=0)
    min_expected_count: float = Field(0, ge=0)
    cooldown_minutes: int = Field(1440, ge=1)
    message_template: str | None = None
    items_template: str | None = None
    message_format: AlertMessageFormat = AlertMessageFormat.plain
    filters: list[AlertRuleFilterPayload] = Field(default_factory=list)


def _reject_explicit_nulls(data: Any, *, not_nullable: frozenset[str]) -> Any:
    """Refuse a PATCH body that spells a required field as ``null``.

    Every field on the update schemas below is Optional-with-None, because
    ``model_dump(exclude_unset=True)`` is the only thing that tells the service
    "not mentioned" from "mentioned as null" — a distinction ``scan_config_id``
    and the template fields genuinely need. The price is that a null and a real
    value are indistinguishable field by field, and the update services then
    assign whatever ``exclude_unset`` handed them.

    For a field backed by a NOT NULL column that assignment is a real NULL: the
    Python-side ``default=`` on the model applies on INSERT, not on UPDATE, so
    the write reaches the database and fails there. An IntegrityError at commit
    has no handler but the catch-all in ``main.py``, so the caller got
    ``Internal server error`` for a body this layer had already accepted.
    Refusing it here costs one dict walk and the answer names the field.
    """
    if not isinstance(data, dict):
        # Pydantic routes model instances and arbitrary objects through a
        # "before" validator too; only a mapping can carry an explicit null.
        return data
    nulled = sorted(
        str(key) for key, value in data.items() if value is None and str(key) in not_nullable
    )
    if nulled:
        raise ValueError(
            f"Cannot be set to null: {', '.join(nulled)}. Omit a field to leave it unchanged."
        )
    return data


# The ``AlertRuleBase`` fields a PATCH must not null out: every NOT NULL column
# on ``alert_rules``, plus ``filters``.
#
# The three fields deliberately ABSENT are the three nullable columns, where a
# null is the documented way to clear something and must keep working:
# ``scan_config_id`` (widens the rule back to the whole project),
# ``message_template`` and ``items_template`` (restore the built-in template).
# A blanket "reject every explicit null" breaks all three.
#
# ``filters`` is here for a different reason — it is a relationship, not a
# column, and ``update_rule`` treats a null as "not mentioned", so a caller who
# meant to clear the filters got a 200 and kept every one of them. The way to
# clear them is ``[]``, and the 422 says so.
#
# ``tests/test_alerting_service_regressions.py`` re-derives the column half of this set from
# ``AlertRule.__table__`` and fails if the two disagree, so a NOT NULL column
# added tomorrow cannot quietly fall out of it.
_RULE_NOT_NULLABLE_ON_UPDATE = frozenset(
    {
        "name",
        "enabled",
        "include_project_total",
        "include_event_types",
        "include_events",
        "include_schema_drifts",
        "include_distribution_drifts",
        "include_variable_value_drifts",
        "include_release_regressions",
        "include_metrics",
        "include_source_freshness",
        "include_lifecycle",
        "include_property_drifts",
        "notify_on_spike",
        "notify_on_drop",
        "ai_explanation_enabled",
        "notify_owners",
        "min_percent_delta",
        "min_absolute_delta",
        "min_expected_count",
        "cooldown_minutes",
        "message_format",
        "filters",
    }
)


class AlertRuleUpdate(AlertRuleBase):
    # No docstring: a model's docstring is emitted as the schema ``description``
    # in backend/openapi.json, and this class carries none today. The reasoning
    # lives on ``_reject_explicit_nulls`` and on the set above instead.
    @model_validator(mode="before")
    @classmethod
    def reject_null_for_required_fields(cls, data: Any) -> Any:
        return _reject_explicit_nulls(data, not_nullable=_RULE_NOT_NULLABLE_ON_UPDATE)


class AlertRuleResponse(BaseModel):
    id: uuid.UUID
    destination_id: uuid.UUID
    scan_config_id: uuid.UUID | None
    name: str
    enabled: bool
    include_project_total: bool
    include_event_types: bool
    include_events: bool
    include_schema_drifts: bool
    include_distribution_drifts: bool
    include_variable_value_drifts: bool
    include_release_regressions: bool
    include_metrics: bool
    include_source_freshness: bool
    include_lifecycle: bool
    include_property_drifts: bool
    notify_on_spike: bool
    notify_on_drop: bool
    ai_explanation_enabled: bool
    # Defaulted so a response built by an older construction site stays valid;
    # ``rule_to_response`` always sends it.
    notify_owners: bool = False
    min_percent_delta: float
    min_absolute_delta: float
    min_expected_count: float
    cooldown_minutes: int
    message_template: str | None
    items_template: str | None
    message_format: AlertMessageFormat
    filters: list[AlertRuleFilterResponse]
    # Mute state of the RULE — the same AlertRule row, and the same computation,
    # that GET /monitors/{rule_id} already reports. The destination card and the
    # monitors screen render the same object and used to disagree about whether
    # it was muted, because this response carried no mute state at all and the
    # card therefore could neither show nor set one. A lapsed
    # ``muted_until`` is NOT muted; see ``_alerting_monitors.is_rule_muted``,
    # which both paths call.
    muted: bool
    muted_until: datetime | None
    # Delivery health, so the card can say whether this rule's channel has
    # actually carried anything — "bot token set" means a value is stored, not
    # that it reaches Telegram. All three are the values
    # ``MonitorDetailResponse`` already reports for the SAME AlertRule, under the
    # same names: a monitor IS an alert rule seen from the other side, so the two
    # payloads must not describe one number twice.
    #
    # ``total_deliveries`` shipped here as ``delivery_count`` while the monitor
    # detail called the identical all-time count ``total_deliveries``, and the
    # borrowed name was already taken elsewhere in this very file:
    # ``AlertInboxGroupResponse.delivery_count`` is the number of deliveries
    # inside ONE incident, not a rule's lifetime total. One name for two
    # quantities in one API is how a client renders an incident's three sends as
    # a rule's whole history.
    total_deliveries: int
    last_delivery_at: datetime | None
    last_delivery_status: AlertDeliveryStatus | None
    # What deleting this rule would destroy. AlertDelivery.rule_id is
    # ondelete=CASCADE and the Inbox INNER JOINs through it, so the confirm has
    # to be quantitative rather than a bare "Delete?".
    # ``incident_count`` counts DISTINCT non-null correlation groups.
    incident_count: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- destination URL guards: shape here, reachability in the service ---------
#
# The two free-form destination URLs — a webhook's ``target_url`` and a Jira
# ``jira_base_url`` — are checked in TWO places, and the split is deliberate.
# Their SHAPE (present, https, no whitespace or control characters, trailing
# slash normalized) is a property of the string and is settled here, at parse
# time, so a malformed URL comes back as an ordinary field-level 422 naming the
# field. WHERE THEY POINT is settled in ``_alerting_destinations``, before the
# row is written.
#
# The public ``validate_webhook_target_url`` / ``validate_jira_base_url`` are
# these same two calls with ``block_private_hosts=True``, and that flag ends in
# ``reject_private_host`` -> ``socket.getaddrinfo``: a BLOCKING resolver call
# with no timeout of ours. A pydantic validator runs inside FastAPI's body
# parsing, which for an ``async def`` route happens ON THE EVENT LOOP — so
# saving a destination whose host resolved slowly stalled the entire uvicorn
# worker, and every unrelated request already in flight on it, for however long
# the resolver took. That is why these wrappers exist and why
# neither of them may grow a ``block_private_hosts=True``.
#
# The SSRF guard is NOT weakened by the move, only relocated: it still runs on
# every create and every update before anything is stored, off the loop via
# ``asyncio.to_thread`` — the same offload ``_alerting_test_send`` already uses
# for the same guard. Send time keeps its own independent re-check
# (``alerts_channels._reject_private_target``); that one is the DNS-rebinding
# defence and belongs exactly where it is.
#
# Calling the private ``_validate_https_url`` is the point rather than an
# accident. The alternative is restating the https / emptiness / whitespace
# rules and their exact error sentences in this file, and two spellings of one
# rule drift apart. The field labels below are the SAME strings the public
# wrappers pass, so which half refused a URL cannot change the sentence the
# operator reads — ``tests/test_alerting_service_regressions.py`` compares the two messages
# and fails if they ever diverge.


def _validate_webhook_target_url_format(value: str | None) -> str:
    return _validate_https_url(value, field="Webhook target_url", block_private_hosts=False)


def _validate_teams_webhook_url_format(value: str | None) -> str:
    return _validate_https_url(value, field="Teams webhook_url", block_private_hosts=False)


def _validate_jira_base_url_format(value: str | None) -> str:
    return _validate_https_url(
        value, field="Jira base_url", strip_trailing_slash=True, block_private_hosts=False
    )


def _validate_email_from_override(value: str | None) -> str | None:
    """The destination's optional From: override — None falls back to the global.

    Checked with ``validate_sender_address``, the SEND PATH's own helper, and
    NOT with the strict ``validate_email_address`` it used to reach through
    ``validate_email_from_address`` — a helper this change DELETED, because this
    was its last caller. Both places that resolve
    a From: at send time read this exact column —
    ``alerts._resolve_email_context`` for the immediate alert and the combined
    digest, ``alerts_channels._send_digest_to_destination`` for the weekly plan
    digest and the sunset alert — and both of them run the result through
    ``validate_sender_address`` and hand the ORIGINAL string to ``msg["From"]``.
    The destination's own Test button does the same (``_alerting_test_send``
    reads ``destination.email_from_address`` straight into ``_TestTarget``).

    So ``Tripl Alerts <no-reply@example.com>`` on a destination delivers, and
    delivers with the display name intact — the strict helper here was refusing
    a value every consumer of the column already accepts. That is
    the inverse of an earlier bug at the other end of the same pipe: there the
    diagnostics were more permissive than delivery, here the SAVE was stricter
    than delivery. Both mislead the operator about a configuration they cannot
    otherwise inspect, and both are fixed by the columns' readers and writers
    answering the question the same way.

    The global ``EmailSettingsUpdate.smtp_from_address`` — the value this one
    overrides — has been checked this way for some time. A value whose
    SHAPE was accepted globally and then refused on the destination that
    overrides it was the last asymmetry of that kind left in the pair.

    An asymmetry of a different kind remains, and it is about storage rather
    than validation: the global lives in ``app_settings.value``, a JSON
    document, so it genuinely has no width, while this override is a
    ``String(255)`` column. That matters here because dropping the strict helper
    dropped a length bound with it — ``validate_email_address`` normalises
    through ``email_validator``, which refuses an address over 254 octets, while
    ``validate_sender_address`` parses the address out of the value and hands
    back the WHOLE original string, display name and surrounding whitespace
    included. Neither of those is an address, so neither was ever capped. The
    two fields that call this helper therefore carry an explicit
    ``max_length=255`` of their own; without it a long organisation display name
    is accepted here and then fails in the INSERT, the 500 naming no field that
    ``AlertRuleBase.name`` describes. The bound belongs on the
    fields and not in this function: the send paths that share
    ``validate_sender_address`` read a column this wide or wider, or none at all.

    What is still refused is a string with no @-sign in it. ``validate_email_address``
    behind ``parseaddr`` sees to that, and it is the one thing worth catching:
    it serialises into the header happily and comes back from the relay hours
    later as an error naming nothing.

    None and whitespace stay None, exactly as before — that is how the override
    is CLEARED, and an empty override is the supported state that falls back to
    ``settings.smtp_from_address``.
    """
    if value is None or not value.strip():
        return None
    return validate_sender_address(value)


class AlertDestinationCreate(BaseModel):
    type: AlertDestinationType
    # Same width as ``alert_destinations.name`` (String(255)). ``normalize_name``
    # below has always refused a blank destination name, but nothing capped its
    # length, so an overlong one reached the INSERT exactly the way a rule name
    # did — see ``AlertRuleBase.name`` for the 500 that produced.
    name: str = Field(max_length=255)
    enabled: bool = True
    # Hold this destination's alerts and deliver them on a cadence instead of
    # after every metrics collection. NULL/omitted means IMMEDIATE — today's
    # behaviour, and what every existing destination keeps. A 5-field cron
    # expression otherwise, read in the project's timezone. The UI's presets
    # ("daily at 09:00") are cron strings it generates, so the wire format has
    # exactly one shape.
    delivery_schedule_cron: str | None = Field(None, max_length=120)
    webhook_url: str | None = None
    bot_token: str | None = None
    # ``name`` above was not the only field here written into a bounded VARCHAR
    # with nothing measuring it. ``chat_id``, ``webhook_header_name``,
    # ``jira_base_url`` and ``linear_label_ids`` were the rest, and each now
    # carries its own column's width verbatim, for the reason
    # ``AlertRuleBase.name`` gives. Every pair is asserted
    # equal to that column in ``tests/test_alerting_service_regressions.py``.
    #
    # Bounding the INPUT is enough even though ``validate_channel_config``
    # rewrites all four afterwards: the validators it calls only strip, dedupe
    # or drop a trailing slash, so what reaches the column is never longer than
    # the string pydantic measured here.
    #
    # Declare every bounded destination column on the wire too. This keeps the
    # OpenAPI contract aligned with PostgreSQL even when another validator also
    # checks an address, template, cron expression, or identifier.
    chat_id: str | None = Field(None, max_length=255)
    target_url: str | None = None
    webhook_header_name: str | None = Field(None, max_length=255)
    webhook_header_value: str | None = None
    email_recipients: str | None = None
    # Same width as ``alert_destinations.email_from_address`` (String(255)).
    # ``_validate_email_from_override`` checks the SHAPE of this value and
    # returns the original string, so nothing in it caps a length: only the
    # address part goes through ``email_validator``, and the display name an
    # override exists to carry is not the address part. See that helper for why
    # the bound sits on the field rather than inside it.
    email_from_address: str | None = Field(None, max_length=255)
    email_subject_template: str | None = Field(None, max_length=500)
    jira_base_url: str | None = Field(None, max_length=255)
    jira_auth_email: str | None = Field(None, max_length=255)
    jira_api_token: str | None = None
    jira_project_key: str | None = Field(None, max_length=64)
    jira_issue_type: str | None = Field(None, max_length=64)
    linear_api_key: str | None = None
    linear_team_id: str | None = Field(None, max_length=64)
    linear_state_id: str | None = Field(None, max_length=64)
    # Same width as ``alert_destinations.linear_label_ids`` (String(1024)), and
    # the entry-count limit does not imply it: ``_LINEAR_LABEL_LIMIT`` is 20 and
    # ``_LINEAR_ID_RE`` allows 64 characters each, so the longest list
    # ``validate_linear_label_ids`` documents as legal joins to 1299 characters,
    # 275 wider than the column it feeds. Twenty ids of the shape Linear
    # actually issues (UUIDs) come to 739 and are untouched by this bound; a
    # list that does overflow is now a 422 naming this field instead of an
    # INSERT the database refuses.
    linear_label_ids: str | None = Field(None, max_length=1024)
    # PagerDuty: the Events API v2 integration key (a secret, stored encrypted)
    # and the severity every event carries. The key's own validator bounds it at
    # 64; the severity is one of four words and its column is String(16).
    pagerduty_routing_key: str | None = None
    pagerduty_severity: str | None = Field(None, max_length=16)
    # Microsoft Teams: an incoming-webhook / Workflows URL. Like the generic
    # webhook's ``target_url`` only its SHAPE is checked here; where it points
    # is settled in the service (see the long comment above).
    teams_webhook_url: str | None = None

    @field_validator("delivery_schedule_cron")
    @classmethod
    def validate_delivery_schedule_cron(cls, value: str | None) -> str | None:
        """Reject an unusable cadence at write time, naming the bad field.

        The worker degrades on a bad expression (logs and skips that
        destination) so one typo cannot stop every other digest — which is
        exactly why it must not be possible to store one from the API.
        """
        if value is None:
            return None
        cleaned = " ".join(value.split())
        if not cleaned:
            return None
        try:
            parse_cron(cleaned)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return cleaned

    @field_validator("type", mode="before")
    @classmethod
    def normalize_type(cls, value: object) -> str:
        return normalize_required_text(str(value), field_name="Destination type").lower()

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return normalize_required_text(value, field_name="Destination name")

    @field_validator(
        "webhook_url",
        "bot_token",
        "target_url",
        "webhook_header_name",
        "webhook_header_value",
        "jira_api_token",
        "linear_api_key",
        "pagerduty_routing_key",
        "teams_webhook_url",
        mode="before",
    )
    @classmethod
    def normalize_optional_secret_fields(cls, value: str | None) -> str | None:
        return normalize_optional_secret(value)

    @field_validator("chat_id")
    @classmethod
    def normalize_chat_id(cls, value: str | None) -> str | None:
        # Blank is "not given", like the secret fields above: a form that holds
        # every channel's inputs sends ``chat_id: ""`` for a Slack or webhook
        # destination, and refusing that as a missing Telegram chat id failed a
        # create that never involved Telegram. The telegram arm of
        # ``validate_channel_config`` still requires one.
        if value is None or not value.strip():
            return None
        return normalize_required_text(value, field_name="Telegram chat_id")

    @model_validator(mode="after")
    def validate_channel_config(self) -> AlertDestinationCreate:
        if self.type == "slack":
            self.webhook_url = validate_slack_webhook_url(self.webhook_url)
        elif self.type == "telegram":
            self.bot_token = validate_telegram_bot_token(self.bot_token)
            self.chat_id = validate_telegram_chat_id(self.chat_id)
        elif self.type == "webhook":
            self.target_url = _validate_webhook_target_url_format(self.target_url)
            self.webhook_header_name = validate_webhook_header_name(self.webhook_header_name)
            self.webhook_header_value = validate_webhook_header_value(self.webhook_header_value)
            if (self.webhook_header_name is None) != (self.webhook_header_value is None):
                raise ValueError("Webhook header name and value must be provided together")
        elif self.type == "email":
            self.email_recipients = validate_email_recipients(self.email_recipients)
            self.email_from_address = _validate_email_from_override(self.email_from_address)
            self.email_subject_template = validate_email_subject_template(
                self.email_subject_template
            )
        elif self.type == "jira":
            self.jira_base_url = _validate_jira_base_url_format(self.jira_base_url)
            self.jira_auth_email = validate_jira_auth_email(self.jira_auth_email)
            self.jira_api_token = validate_jira_api_token(self.jira_api_token)
            self.jira_project_key = validate_jira_project_key(self.jira_project_key)
            self.jira_issue_type = validate_jira_issue_type(self.jira_issue_type or "Task")
        elif self.type == "linear":
            self.linear_api_key = validate_linear_api_key(self.linear_api_key)
            self.linear_team_id = validate_linear_team_id(self.linear_team_id)
            self.linear_state_id = validate_linear_state_id(self.linear_state_id)
            self.linear_label_ids = validate_linear_label_ids(self.linear_label_ids)
        elif self.type == "pagerduty":
            self.pagerduty_routing_key = validate_pagerduty_routing_key(self.pagerduty_routing_key)
            self.pagerduty_severity = (
                validate_pagerduty_severity(self.pagerduty_severity) or DEFAULT_PAGERDUTY_SEVERITY
            )
        elif self.type == "teams":
            self.teams_webhook_url = _validate_teams_webhook_url_format(self.teams_webhook_url)
        elif self.type == "demo_sink":
            # A demo_sink is a local, non-sendable sink: it carries NO
            # credentials or channel configuration and never stores a secret or
            # fake token. Reject any attempt to supply them.
            provided = [
                name
                for name in (
                    "webhook_url",
                    "bot_token",
                    "chat_id",
                    "target_url",
                    "webhook_header_name",
                    "webhook_header_value",
                    "email_recipients",
                    "email_from_address",
                    "email_subject_template",
                    "jira_base_url",
                    "jira_auth_email",
                    "jira_api_token",
                    "jira_project_key",
                    "jira_issue_type",
                    "linear_api_key",
                    "linear_team_id",
                    "linear_state_id",
                    "linear_label_ids",
                    "pagerduty_routing_key",
                    "pagerduty_severity",
                    "teams_webhook_url",
                )
                if getattr(self, name) is not None
            ]
            if provided:
                raise ValueError(
                    "A demo_sink destination is a local sink and must not carry "
                    "any credentials or channel configuration"
                )
        else:
            raise ValueError("Unsupported destination type")
        return self


# The ``AlertDestinationUpdate`` fields a PATCH must not null out. Much smaller
# than the rule's set because almost everything on a destination IS a nullable
# column or an ignore-if-None secret: only ``name`` and ``enabled`` are NOT NULL
# on ``alert_destinations``. In particular ``delivery_schedule_cron`` stays out
# — null there means IMMEDIATE and is how a cadence is removed — as do
# ``linear_label_ids``, ``linear_state_id`` and the rest of the optional channel
# config, where null clears the stored value. Same guard test as the rule set.
_DESTINATION_NOT_NULLABLE_ON_UPDATE = frozenset({"name", "enabled"})


class AlertDestinationUpdate(BaseModel):
    # Same bound as on create: renaming a destination writes the same column.
    name: str | None = Field(None, max_length=255)
    enabled: bool | None = None
    # Hold this destination's alerts and deliver them on a cadence instead of
    # after every metrics collection. NULL/omitted means IMMEDIATE — today's
    # behaviour, and what every existing destination keeps. A 5-field cron
    # expression otherwise, read in the project's timezone. The UI's presets
    # ("daily at 09:00") are cron strings it generates, so the wire format has
    # exactly one shape.
    delivery_schedule_cron: str | None = None
    webhook_url: str | None = None
    bot_token: str | None = None
    # Same bounds as on create: a PATCH writes these same four columns. See the
    # create schema for why the input is what gets measured, and for the bounded
    # columns that are deliberately left undeclared on both.
    chat_id: str | None = Field(None, max_length=255)
    target_url: str | None = None
    webhook_header_name: str | None = Field(None, max_length=255)
    webhook_header_value: str | None = None
    email_recipients: str | None = None
    # Same bound as on create: a From: override writes the same column, and the
    # field validator below shares the same shape-only helper.
    email_from_address: str | None = Field(None, max_length=255)
    email_subject_template: str | None = None
    # Bounded as on create. The ``mode="before"`` validator below runs ahead of
    # this bound, so a value that is over-long AND not an https URL is refused
    # for its shape; the length is what refuses one that is a URL.
    jira_base_url: str | None = Field(None, max_length=255)
    jira_auth_email: str | None = None
    jira_api_token: str | None = None
    jira_project_key: str | None = None
    jira_issue_type: str | None = None
    linear_api_key: str | None = None
    linear_team_id: str | None = None
    linear_state_id: str | None = None
    linear_label_ids: str | None = Field(None, max_length=1024)
    # Secrets are ignore-if-None on update, like every other: blank keeps the
    # stored key / URL. Severity null is "back to the default" (error).
    pagerduty_routing_key: str | None = None
    pagerduty_severity: str | None = Field(None, max_length=16)
    teams_webhook_url: str | None = None

    @field_validator("delivery_schedule_cron")
    @classmethod
    def validate_delivery_schedule_cron(cls, value: str | None) -> str | None:
        """Reject an unusable cadence at write time, naming the bad field.

        The worker degrades on a bad expression (logs and skips that
        destination) so one typo cannot stop every other digest — which is
        exactly why it must not be possible to store one from the API.
        """
        if value is None:
            return None
        cleaned = " ".join(value.split())
        if not cleaned:
            return None
        try:
            parse_cron(cleaned)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return cleaned

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        # The None arm is kept on purpose, for the reason spelled out on
        # ``AlertRuleBase.normalize_name``: nothing reaches it today (an unset
        # default is never validated, and an explicit null is refused by
        # ``reject_null_for_required_fields``), but the field is declared
        # ``str | None`` and this covers that declaration instead of trusting a
        # frozenset defined elsewhere in the file.
        if value is None:
            return None
        return normalize_required_text(value, field_name="Destination name")

    @field_validator("webhook_url", mode="before")
    @classmethod
    def validate_webhook_url(cls, value: str | None) -> str | None:
        normalized = normalize_optional_secret(value)
        if normalized is None:
            return None
        return validate_slack_webhook_url(normalized)

    @field_validator("bot_token", mode="before")
    @classmethod
    def validate_bot_token(cls, value: str | None) -> str | None:
        normalized = normalize_optional_secret(value)
        if normalized is None:
            return None
        return validate_telegram_bot_token(normalized)

    @field_validator("chat_id")
    @classmethod
    def validate_chat_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_telegram_chat_id(value)

    @field_validator("target_url", mode="before")
    @classmethod
    def validate_target_url(cls, value: str | None) -> str | None:
        normalized = normalize_optional_secret(value)
        if normalized is None:
            return None
        return _validate_webhook_target_url_format(normalized)

    @field_validator("webhook_header_value", mode="before")
    @classmethod
    def validate_header_value(cls, value: str | None) -> str | None:
        return validate_webhook_header_value(value)

    @field_validator("webhook_header_name")
    @classmethod
    def validate_header_name(cls, value: str | None) -> str | None:
        return validate_webhook_header_name(value)

    @field_validator("email_recipients")
    @classmethod
    def validate_recipients(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_email_recipients(value)

    @field_validator("email_from_address")
    @classmethod
    def validate_from(cls, value: str | None) -> str | None:
        return _validate_email_from_override(value)

    @field_validator("email_subject_template")
    @classmethod
    def validate_subject(cls, value: str | None) -> str | None:
        return validate_email_subject_template(value)

    @field_validator("jira_base_url", mode="before")
    @classmethod
    def validate_jira_base_url_update(cls, value: str | None) -> str | None:
        normalized = normalize_optional_secret(value)
        if normalized is None:
            return None
        return _validate_jira_base_url_format(normalized)

    @field_validator("jira_auth_email")
    @classmethod
    def validate_jira_auth_email_update(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_jira_auth_email(value)

    @field_validator("jira_api_token", mode="before")
    @classmethod
    def validate_jira_api_token_update(cls, value: str | None) -> str | None:
        normalized = normalize_optional_secret(value)
        if normalized is None:
            return None
        return validate_jira_api_token(normalized)

    @field_validator("jira_project_key")
    @classmethod
    def validate_jira_project_key_update(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_jira_project_key(value)

    @field_validator("jira_issue_type")
    @classmethod
    def validate_jira_issue_type_update(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_jira_issue_type(value)

    @field_validator("linear_api_key", mode="before")
    @classmethod
    def validate_linear_api_key_update(cls, value: str | None) -> str | None:
        normalized = normalize_optional_secret(value)
        if normalized is None:
            return None
        return validate_linear_api_key(normalized)

    @field_validator("linear_team_id")
    @classmethod
    def validate_linear_team_id_update(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_linear_team_id(value)

    @field_validator("linear_state_id")
    @classmethod
    def validate_linear_state_id_update(cls, value: str | None) -> str | None:
        return validate_linear_state_id(value)

    @field_validator("linear_label_ids")
    @classmethod
    def validate_linear_label_ids_update(cls, value: str | None) -> str | None:
        return validate_linear_label_ids(value)

    @field_validator("pagerduty_routing_key", mode="before")
    @classmethod
    def validate_pagerduty_routing_key_update(cls, value: str | None) -> str | None:
        normalized = normalize_optional_secret(value)
        if normalized is None:
            return None
        return validate_pagerduty_routing_key(normalized)

    @field_validator("pagerduty_severity")
    @classmethod
    def validate_pagerduty_severity_update(cls, value: str | None) -> str | None:
        return validate_pagerduty_severity(value)

    @field_validator("teams_webhook_url", mode="before")
    @classmethod
    def validate_teams_webhook_url_update(cls, value: str | None) -> str | None:
        normalized = normalize_optional_secret(value)
        if normalized is None:
            return None
        return _validate_teams_webhook_url_format(normalized)

    # Runs before every field validator above, so a null is named and refused
    # before ``update_destination`` can assign it. See ``_reject_explicit_nulls``
    # and ``_DESTINATION_NOT_NULLABLE_ON_UPDATE`` for which fields and why.
    @model_validator(mode="before")
    @classmethod
    def reject_null_for_required_fields(cls, data: Any) -> Any:
        return _reject_explicit_nulls(data, not_nullable=_DESTINATION_NOT_NULLABLE_ON_UPDATE)


class AlertDestinationResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    type: AlertDestinationType
    name: str
    enabled: bool
    webhook_set: bool
    bot_token_set: bool
    chat_id: str | None
    target_url_set: bool
    webhook_header_name: str | None
    email_recipients: str | None
    email_from_address: str | None
    email_subject_template: str | None
    jira_base_url: str | None
    jira_auth_email: str | None
    jira_api_token_set: bool
    jira_project_key: str | None
    jira_issue_type: str | None
    linear_api_key_set: bool
    linear_team_id: str | None
    linear_state_id: str | None
    linear_label_ids: str | None
    # The routing key and the Teams URL are write-only secrets, reported only
    # as "is one on file" — the same contract as ``webhook_set``.
    pagerduty_routing_key_set: bool
    pagerduty_severity: str | None
    teams_webhook_set: bool
    # The delivery cadence, and what the operator needs to trust it: NULL means
    # alerts go out after every collection (immediate). ``last_digest_at`` is
    # the fire instant of the last window flushed, ``next_digest_at`` the next
    # one due — both computed in ``project_timezone`` so the UI can say "09:00
    # Europe/Moscow" without re-deriving the zone.
    delivery_schedule_cron: str | None = None
    project_timezone: str = "UTC"
    last_digest_at: datetime | None = None
    next_digest_at: datetime | None = None
    # Alerts matched and HELD for the next digest. A cadence puts a destination
    # into "on and quiet" for a whole window by design, and nothing else on the
    # screen can tell that apart from "on and structurally dead".
    held_count: int = 0
    # True for a ``demo_sink`` destination: a local, non-sendable sink that
    # renders and records deliveries locally with no outbound network. The UI
    # uses it to badge the destination as LOCAL SIMULATED.
    is_local: bool = False
    # Destination-wide totals of what a delete would destroy, so the destination
    # confirm can state it too — deleting a destination CASCADEs every rule under
    # it and every delivery under those. NOT the sum of the
    # per-rule numbers below: one correlation group can be carried by two rules
    # of the same destination, so summing per-rule DISTINCT counts would count
    # that incident twice.
    delivery_count: int
    incident_count: int
    rules: list[AlertRuleResponse]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ``config``: our own validators refused a stored value; ``policy``: this
# project may not send outside (a demo); ``other``: nothing more specific fits.
DestinationTestErrorKind = Literal[
    "config", "policy", "dns", "timeout", "tls", "network", "http_status", "smtp", "other"
]


class AlertDestinationDraftTestRequest(BaseModel):
    """A destination's settings as the dialog holds them, to test before saving.

    Setting up Slack used to take Create, close, find the card, then Test — and
    a wrong webhook was a stored destination by the time anyone learned it.

    ``destination_id`` names the saved destination an EDIT dialog is open on.
    The dialog never holds that row's secrets (they are write-only), so a
    secret left blank here means "the one on file", exactly as the PATCH reads
    it; every other field is taken as sent. Without it this is an unsaved
    destination and nothing is borrowed from anywhere.

    Deliberately lenient about what is MISSING: a half-filled form is the
    normal state of a draft, and the answer to testing one is the same
    ``ok: false`` / ``config`` result a stored destination with a bad value gets
    — not a 422 the dialog would render as "the request was wrong". What is
    checked here is only what a send must never carry at all: header names and
    values that could inject, and ids that could reach another endpoint's path.
    Fields the form sends that a test does not use (``enabled``, the cadence,
    the subject template) are ignored.
    """

    destination_id: uuid.UUID | None = None
    type: AlertDestinationType
    name: str | None = Field(None, max_length=255)
    webhook_url: str | None = None
    bot_token: str | None = None
    chat_id: str | None = Field(None, max_length=255)
    target_url: str | None = None
    webhook_header_name: str | None = Field(None, max_length=255)
    webhook_header_value: str | None = None
    email_recipients: str | None = None
    email_from_address: str | None = Field(None, max_length=255)
    jira_base_url: str | None = Field(None, max_length=255)
    jira_auth_email: str | None = Field(None, max_length=255)
    jira_api_token: str | None = None
    jira_project_key: str | None = Field(None, max_length=64)
    jira_issue_type: str | None = Field(None, max_length=64)
    linear_api_key: str | None = None
    linear_team_id: str | None = Field(None, max_length=64)
    linear_state_id: str | None = Field(None, max_length=64)
    linear_label_ids: str | None = Field(None, max_length=1024)
    pagerduty_routing_key: str | None = None
    pagerduty_severity: str | None = Field(None, max_length=16)
    teams_webhook_url: str | None = None

    @field_validator("type", mode="before")
    @classmethod
    def normalize_type(cls, value: object) -> str:
        return normalize_required_text(str(value), field_name="Destination type").lower()

    @field_validator(
        "name",
        "webhook_url",
        "bot_token",
        "chat_id",
        "target_url",
        "email_recipients",
        "jira_base_url",
        "jira_auth_email",
        "jira_api_token",
        "jira_project_key",
        "jira_issue_type",
        "linear_api_key",
        "linear_team_id",
        "pagerduty_routing_key",
        "teams_webhook_url",
        mode="before",
    )
    @classmethod
    def blank_is_absent(cls, value: str | None) -> str | None:
        # Blank is "not given", as on the create schema: the form sends every
        # input it holds, and an empty one is not a value to test with.
        return normalize_optional_secret(value)

    @field_validator("webhook_header_name")
    @classmethod
    def validate_header_name(cls, value: str | None) -> str | None:
        return validate_webhook_header_name(value)

    @field_validator("webhook_header_value", mode="before")
    @classmethod
    def validate_header_value(cls, value: str | None) -> str | None:
        return validate_webhook_header_value(value)

    @field_validator("email_from_address")
    @classmethod
    def validate_from(cls, value: str | None) -> str | None:
        return _validate_email_from_override(value)

    @field_validator("linear_state_id")
    @classmethod
    def validate_state_id(cls, value: str | None) -> str | None:
        return validate_linear_state_id(normalize_optional_secret(value))

    @field_validator("linear_label_ids")
    @classmethod
    def validate_label_ids(cls, value: str | None) -> str | None:
        return validate_linear_label_ids(value)

    @field_validator("pagerduty_severity")
    @classmethod
    def validate_severity(cls, value: str | None) -> str | None:
        # Settled here rather than left to the test: it is a closed set, and a
        # value outside it is a request no send could ever honour.
        return validate_pagerduty_severity(value)


class AlertDestinationTestResponse(BaseModel):
    """Result of a manual test send — did this destination reach its channel?

    A channel refusal is an ANSWER, not a server fault: a revoked Telegram token
    and a healthy one look identical in the destination form, and
    the whole point of the probe is to tell them apart. So the route returns 200
    with ``ok=False`` and the channel's own message rather than a 5xx the UI would
    render as "something went wrong on our side".
    """

    ok: bool
    # Both nullable but ALWAYS SENT — FastAPI serializes every declared field, so
    # a default here only ever told the generated client the key may be absent
    # while the server never omits it. Same contract rule as ``event_id`` on the
    # inbox group: the four construction sites in ``_alerting_test_send`` name
    # both values explicitly, including the ``None`` half of each pair.
    error: str | None
    sent_at: datetime | None
    # What kind of failure ``error`` describes, so the dialog can pick its
    # advice without pattern-matching exception text; ``http_status`` carries the
    # code when the kind is ``http_status``. Null on success. Defaulted, unlike
    # the two above, because older clients and fixtures predate them.
    error_kind: DestinationTestErrorKind | None = None
    http_status: int | None = None


class AlertDeliveryItemResponse(BaseModel):
    id: uuid.UUID
    delivery_id: uuid.UUID
    scope_type: MetricScopeType
    scope_ref: str
    scope_name: str
    event_type_id: uuid.UUID | None
    event_id: uuid.UUID | None
    bucket: datetime
    direction: AnomalyDirection
    actual_count: float
    expected_count: float
    absolute_delta: float
    # ``None``, not the stored 0.0, when there was no baseline — the same
    # encoding ``payload_snapshot`` and the webhook already use. This response
    # carries BOTH: ``AlertDeliveryDetailResponse`` inherits ``payload_snapshot``
    # and adds ``items``, so leaving this one a bare float made a single JSON
    # body answer the same question two ways, and the typed array is the half an
    # external consumer reads off the OpenAPI spec.
    #
    # ``AlertDeliveryItem.percent_delta`` stays NOT NULL: the row is frozen
    # history and is not rewritten. Only the outbound encoding changes.
    percent_delta: float | None
    details_path: str | None
    monitoring_path: str | None
    drift_field: str | None
    drift_type: AlertDriftType | None
    sample_value: str | None
    # The incident this row belongs to: one
    # (scan config, rule, scope type, scope ref, direction).
    # It is also the handle the alert inbox acts on, so EVERY item written
    # now carries one — a solitary alert had none before and was
    # therefore invisible to the inbox and impossible to acknowledge. Co-firing
    # is the peer COUNT within a delivery, not the presence of this id. NULL
    # only on rows written by older releases.
    correlation_group_id: uuid.UUID | None = None

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def blank_percent_delta_without_a_baseline(self) -> AlertDeliveryItemResponse:
        """Enforce the encoding here rather than at the call site.

        The one construction site reads ORM rows straight through
        (``_alerting_deliveries.get_delivery``), so a rule applied there would be
        one edit away from being lost. Applying it on the model means any future
        caller gets it too, and the invariant this response advertises —
        ``percent_delta`` is null exactly when ``expected_count`` is 0 — cannot
        be broken by a new code path.
        """
        self.percent_delta = percent_delta_or_none(
            self.percent_delta if self.percent_delta is not None else 0.0,
            self.expected_count,
        )
        return self


class AlertDeliveryResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    scan_config_id: uuid.UUID
    scan_job_id: uuid.UUID | None
    destination_id: uuid.UUID
    rule_id: uuid.UUID
    destination_name: str
    rule_name: str
    scan_name: str
    status: AlertDeliveryStatus
    channel: AlertDestinationType
    matched_count: int
    payload_snapshot: dict[str, object] | None
    error_message: str | None
    # True when the delivery was rendered + recorded locally by a ``demo_sink``
    # destination with no outbound network. ``is_local`` and ``is_simulated``
    # both track ``channel == demo_sink`` so the UI can badge the delivery as a
    # LOCAL SIMULATED send that never claims a real external success.
    is_local: bool = False
    is_simulated: bool = False
    created_at: datetime
    updated_at: datetime
    sent_at: datetime | None


class AlertDeliveryDetailResponse(AlertDeliveryResponse):
    items: list[AlertDeliveryItemResponse]
    # The owners this delivery's rule emailed (F07, #260), oldest first. Empty
    # when the rule does not notify owners or none of its scopes is owned.
    owner_notifications: list[AlertOwnerNotificationResponse] = Field(default_factory=list)


class AlertDeliveryListResponse(BaseModel):
    items: list[AlertDeliveryResponse]
    total: int
    # Opaque keyset cursor for the page after this one, `None` on the last
    # page. Passing it back as `?cursor=` continues strictly after the last row
    # served, so a list that changes between requests cannot skip a row the
    # way an offset can. Always sent, no default, for the reason
    # `window_truncated_at` below gives.
    next_cursor: str | None


class AlertInboxRuleRef(BaseModel):
    """One rule that carried this incident: the id AND the name, together.

    Replaces the parallel ``rule_ids`` / ``rule_names`` arrays, which could not
    be zipped: ``rule_ids`` was sorted by UUID and ``rule_names`` by name, so
    index *i* of one had nothing to do with index *i* of the other and the card
    linked "Volume rule" to whichever monitor happened to sort first. Two rules
    of one group can even share a name, so no client-side join could repair it
    either.
    """

    id: uuid.UUID
    name: str


class AlertInboxGroupResponse(BaseModel):
    correlation_group_id: uuid.UUID
    status: AlertInboxStatus
    # Effective flag, not a second opinion: ``muted`` is true exactly when
    # ``status`` is ``muted``, i.e. while the mute is IN FORCE. Present because
    # ``AlertRuleResponse`` and ``MonitorSummaryItem`` already pair a ``muted``
    # bool with a raw ``muted_until``, and a client reading both payload families
    # had to know that "muted" is asked one way here and another way there.
    # ``muted_until`` is nulled once the mute lapses, so the
    # two fields can never contradict each other the way the rule payload's can.
    # Always sent, so no default — see ``event_id`` below.
    muted: bool
    muted_until: datetime | None = None
    note: str | None = None
    false_positive_count: int = 0
    item_count: int
    delivery_count: int
    latest_bucket: datetime
    latest_delivery_at: datetime
    # When this incident first spoke WITHIN THE WINDOW THIS READING COVERS.
    # `latest_delivery_at` alone says when it last fired and nothing about how
    # long it has been going, which is the difference between a blip and a
    # week-old regression. On the list — and on the action reply,
    # which mirrors it — that window is `INBOX_LOOKBACK_DAYS`, so an incident
    # older than the window reports its first delivery INSIDE the window, not its
    # true birth. `GET /alert-inbox/{correlation_group_id}` reads the whole
    # history and does report the true first, which is one reason it exists.
    # Same qualification applies to `item_count`, `delivery_count` and
    # `max_abs_percent_delta`.
    first_delivery_at: datetime
    direction: AnomalyDirection
    # Magnitude of the newest item. Two firings of one scope differing only in
    # scope_type rendered as near-identical cards, with nothing on screen saying
    # what fired or how big it was. The columns are already on
    # AlertDeliveryItem, so the builder fills these from rows it already holds.
    actual_count: float
    expected_count: float
    # ``None``, not the stored 0.0, when the newest item had no baseline — the
    # same encoding ``AlertDeliveryItemResponse`` uses, and enforced the same way
    # (see the validator below). Copying the column straight off the row made the
    # card render "0%" for a scope firing from nothing, which is the LOUDEST
    # class there is, while the delivery the card expands to correctly said null
    # — one payload family answering the same question two ways.
    percent_delta: float | None
    # Largest deviation anywhere in the group, so "worst first" is orderable
    # without fetching the group's items. Computed over the rows that HAVE a
    # baseline only; ``None`` when no row in the group does, because a group of
    # zero-baseline firings has no measured deviation to be largest — reporting
    # the placeholder 0.0 sorted the loudest incidents last.
    max_abs_percent_delta: float | None
    # The scope of the most recent item, so the card can link straight to the
    # thing that fired. `scope_names` is display text and cannot be routed;
    # without these the reader could see WHAT alerted and had no way to go look
    # at it.
    scope_type: MetricScopeType
    scope_ref: str
    # Nullable but ALWAYS SENT — `_build_inbox_group_response` fills it from the
    # latest item unconditionally. No default, so the contract says required and
    # matches its two siblings above; with one, the generated type made the key
    # optional while the hand-written one called it required, and the two
    # disagreed about a field the server never omits.
    event_id: uuid.UUID | None
    # DISTINCT scope types present in the group, sorted. `scope_type` above is
    # the newest item's alone, and legacy groups can mix types, so a single
    # value cannot label the card — nor can the client derive what a
    # false-positive click will actually tune from it.
    scope_types: list[MetricScopeType]
    scope_names: list[str]
    destination_names: list[str]
    # Distinct rule names, sorted. Kept alongside `rules` because the inbox card
    # already renders it as a plain label line (frontend AlertingInbox.tsx), and
    # `rules` is the routable form for linking each name to its monitor.
    rule_names: list[str]
    # Every rule behind this incident as an (id, name) PAIR, sorted by name. See
    # `AlertInboxRuleRef` for why the two parallel arrays could not survive.
    rules: list[AlertInboxRuleRef]
    scan_names: list[str]
    acted_at: datetime | None = None
    acted_by: uuid.UUID | None = None
    # Display name of `acted_by`, or `None` when the user has no name on file.
    # The API shipped a bare UUID, so "already handled by <uuid>" was the best a
    # card could say. It deliberately does NOT fall back to the
    # operator's EMAIL: this endpoint is readable by every project member, and a
    # fallback would turn an incident card into a roster of colleagues' email
    # addresses on a surface that previously exposed only an opaque id. The card
    # already handles a missing name. Nullable but ALWAYS SENT, like `event_id`
    # above — no default, so the generated client cannot call it optional while
    # the hand-written type calls it required.
    acted_by_name: str | None
    # Owners of the newest item's event type / catalog metric (F07, #260): the
    # current project members with an email that "Notify owners" would reach.
    # Names only, never addresses — this payload is readable by every member.
    owners: list[AlertOwnerRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def blank_percent_delta_without_a_baseline(self) -> AlertInboxGroupResponse:
        """Enforce the no-baseline encoding on the model, as the item does.

        Mirrors ``AlertDeliveryItemResponse.blank_percent_delta_without_a_baseline``
        so a future builder cannot reintroduce the placeholder: this response's
        ``percent_delta`` describes the same newest item its ``expected_count``
        comes from, so the invariant — null exactly when ``expected_count`` is 0
        — is checkable right here.

        ``max_abs_percent_delta`` spans the WHOLE group and has no companion
        expected_count on this model, so nothing here can verify it; the builder
        computes it over baselined rows only and the tests pin both branches.
        """
        self.percent_delta = percent_delta_or_none(
            self.percent_delta if self.percent_delta is not None else 0.0,
            self.expected_count,
        )
        return self


class AlertInboxStatusCounts(BaseModel):
    """Incidents per effective status over the whole window.

    Counted after the non-status filters and before the status one, so a chip
    reads what choosing it would list, without loading every page.
    """

    open: int = 0
    acknowledged: int = 0
    muted: int = 0
    resolved: int = 0
    false_positive: int = 0


class AlertInboxListResponse(BaseModel):
    items: list[AlertInboxGroupResponse]
    total: int
    status_counts: AlertInboxStatusCounts = Field(default_factory=AlertInboxStatusCounts)
    # Where the list's window ACTUALLY starts when `INBOX_MAX_SOURCE_ITEMS` cut
    # it shorter than `INBOX_LOOKBACK_DAYS`, and `None` when the documented
    # window held — which is every deployment measured so far.
    #
    # It exists because the page states its own coverage ("last 30 days + still
    # silenced") and that sentence was unconditional while the bound behind it
    # was not: the cap is applied on DELIVERY recency before grouping, so a
    # loud-enough project would silently get a shorter window with nothing
    # saying so, and a missing incident would be indistinguishable from a
    # handled one.
    #
    # ONE nullable instant rather than a `window_truncated` bool beside a
    # `window_start` datetime: two fields can disagree — truncated with no
    # start, or a start that is really just the cutoff — and the client would
    # then have to decide which to believe. Null IS "not truncated", and the
    # only thing a client does with the value is name it.
    #
    # Nullable but ALWAYS SENT, no default, like `event_id` and `acted_by_name`
    # above: with a default the generated client calls the key optional while
    # the hand-written type calls it required, and the two disagree about a
    # field the server never omits.
    window_truncated_at: datetime | None
    # Opaque keyset cursor for "Load more", `None` on the last page; see
    # AlertDeliveryListResponse.next_cursor.
    next_cursor: str | None


class AlertInboxActionRequest(BaseModel):
    action: AlertInboxAction
    note: str | None = Field(None, max_length=2000)
    # ``None`` on a ``mute`` is the INDEFINITE mute — "muted until I unmute" —
    # not a missing field; see ``validate_action``. A value sent with any other
    # action is refused rather than discarded.
    muted_until: datetime | None = None

    @model_validator(mode="after")
    def validate_action(self) -> AlertInboxActionRequest:
        """Reject the action bodies that would be accepted and then do nothing.

        A ``mute`` with NO ``muted_until`` is no longer one of them. On the INBOX
        a null means "muted until I unmute": the operator watching a scope they
        already know is broken had to invent an expiry date, and got paged again
        the moment they guessed too short. The column is nullable
        and every reader already agrees on that reading —
        ``_effective_inbox_status`` leaves a null-muted row at ``muted``,
        ``_suppressed_correlation_group_ids`` never lapses it, and
        ``_build_inbox_group_response`` emits ``muted: true`` with a null
        ``muted_until``. ``reopen`` is the only exit, which is the point.

        This is TRUE OF THE INBOX ONLY. On an ``AlertRule`` a null
        ``muted_until`` means NOT MUTED — ``_alerting_monitors.is_rule_muted``
        returns False for it — because the rule carries no status column to tell
        "never muted" from "muted forever", and NULL is what every rule ever
        created already has. Relaxing ``MonitorMuteRequest.muted_until`` to match
        this model would therefore mute every monitor in the fleet at once. The
        rule's permanent lever is its ``enabled`` switch; the two payloads read
        the same column name in opposite directions and must not be unified.
        """
        # ``note`` is the one action whose entire effect is the note, and the
        # write below is conditional on ``note is not None``. Without this guard
        # a ``{"action": "note"}`` body was a silent 200 that changed nothing
        # while still inserting a correlation-state row — the request looked
        # accepted and the note was never saved. The mute guard this one used to
        # mirror — "a mute needs an end" — is still gone, because a null
        # ``muted_until`` became the indefinite mute; the mute guard
        # below is a different one, about the value rather than its presence.
        # An EMPTY STRING stays valid: it is the documented way to clear a note
        # (``apply_alert_inbox_action`` stores ``strip() or None``).
        if self.action == "note" and self.note is None:
            raise ValueError("note is required when action is note")
        # A mute that ends BEFORE IT BEGINS is the other body this docstring
        # promises to refuse and never did. Nothing sweeps an
        # expired mute or writes it back: ``_effective_inbox_status`` decides
        # whether a mute is in force when the row is READ, so storing one that
        # has already lapsed leaves the card open and hands the operator a 200
        # that moved nothing on screen. ``mute_monitor`` has answered 422 to the
        # same input since it shipped, and that is the other Mute button on this
        # same screen — the asymmetry, not the lapse logic, is the defect.
        #
        # ``is not None`` is load-bearing and is NOT a null check to be tidied
        # away: a null ``muted_until`` is the INDEFINITE mute described above,
        # the one silence that can never lapse, so it is the last body that
        # should be refused for having lapsed.
        if self.action == "mute" and self.muted_until is not None:
            # Assigned back so that the instant the service stores, and the one
            # the route audits through ``model_dump``, is the UTC value this
            # check passed on rather than a floating wall time.
            self.muted_until = require_future_instant(self.muted_until, field_name="muted_until")
        # An end date on an acknowledge, a resolve or a reopen is a client that
        # meant to mute; every action but ``mute`` nulls the column, so taking
        # it would answer 200 and do something else. Refused, as
        # ``EventCommentActionRequest`` refuses a stray ``snoozed_until`` —
        # one rule across the four action bodies.
        if self.action != "mute" and self.muted_until is not None:
            raise ValueError("muted_until is only meaningful when action is mute")
        return self


class AlertInboxActionResponse(BaseModel):
    """What the action DID, not only what the group looks like afterwards.

    ``false_positive`` writes no scope override for scope types the ratchet does
    not tune — release regressions among them — so the button promised a
    detection change it never made, on 10 of 57 production groups.
    The count is reported so the UI can say "tightened 2 scopes" or "no scopes
    tightened"; it must NOT be guessed client-side from ``scope_type``, which is
    only the newest item's.
    """

    group: AlertInboxGroupResponse
    # ``None`` for every action that cannot ratchet anything — acknowledge,
    # resolve, mute, reopen, note — and an actual count only for
    # ``false_positive``. It was hard-set to 0 for the other five, so a client
    # rendering "no scopes tightened" off ``=== 0`` announced a detection
    # decision after an Acknowledge, which never touches detection at all.
    # ``None`` means "not applicable", ``0`` means "tried and tightened nothing".
    # Nullable but ALWAYS SENT, so no default — see ``event_id`` on the group.
    overrides_written: int | None


# The largest selection this route accepts in one call, pinned to the largest
# page ``list_alert_inbox`` can hand the operator: its ``limit`` is
# ``Query(50, ge=1, le=200)`` (api/v1/alerting.py). The selection this route
# serves is made by ticking rows on ONE page, so a lower cap would 422 the
# "select all" the feature exists for, and a higher one would admit a list no
# page of the UI can produce.
#
# Nothing else in this repo caps its bulk id list — ``EventBulkUpdate``,
# ``EventBulkDelete``, ``VariableBulkUpdate``, ``VariableBulkDelete`` and
# ``MetricBulkUpdate`` all take an unbounded one — and those five are
# deliberately NOT retrofitted here. They each mutate every named
# row in a single UPDATE statement, so list length costs them almost nothing;
# this route does per-group work — a correlation-state row, a rebuilt card and an
# audit row EACH — so length is a real cost here and only a theoretical one
# there. Retrofitting them would be a behavior change to four shipped endpoints
# bought with this feature's budget.
MAX_BULK_INBOX_ACTION_GROUPS = 200


class AlertInboxBulkActionRequest(BaseModel):
    """One triage decision applied to several incidents at once.

    A TRIAGE SHORTCUT, not an incident record. There is no group-of-groups
    object, no new table and no migration behind this body: whatever it says is
    COPIED into each selected incident's own correlation state, so afterwards
    every selected row carries the same note, the same ``acted_at`` and the same
    ``acted_by`` and is indistinguishable from N single-incident clicks.

    A persistent supergroup was costed and rejected:
    ``_reopen_closed_incidents`` runs inside the per-rule loop and resets member
    incidents individually, so a parent row would either never release — because
    no single member's release can speak for it — or leak the moment one member
    went quiet. Copying the decision into the members has neither failure.

    ``correlation_group_ids`` comes FIRST to match the ``<entity>_ids``-first
    convention every other bulk body in the repo already uses
    (``EventBulkUpdate``, ``EventBulkDelete``, ``VariableBulkUpdate``,
    ``MetricBulkUpdate``, ``DeadEventArchiveRequest``); the action fields that
    follow are exactly ``AlertInboxActionRequest``'s, and mean exactly the same
    things, so the two bodies stay readable side by side.
    """

    correlation_group_ids: list[uuid.UUID] = Field(
        min_length=1, max_length=MAX_BULK_INBOX_ACTION_GROUPS
    )
    action: AlertInboxAction
    note: str | None = Field(None, max_length=2000)
    # ``None`` on a ``mute`` is the INDEFINITE mute here too — "muted until I
    # unmute" — exactly as on ``AlertInboxActionRequest``, and for the same
    # reason. Bulk-muting a screenful of incidents an operator
    # already knows are broken is the case that most needs it, so this route
    # must not be the one place that demands an invented expiry date.
    muted_until: datetime | None = None

    @model_validator(mode="after")
    def validate_action(self) -> AlertInboxBulkActionRequest:
        """Reject the three bodies this route cannot honour.

        Mirrors ``AlertInboxActionRequest.validate_action`` twice — a ``note``
        action whose entire effect is the note, sent without one, and a ``mute``
        whose end has already passed, both of which are 200s that change
        nothing — and adds the refusal that is specific to acting on MANY
        incidents at once.
        """
        # Same guard, same reason as the single-incident body: the note write is
        # conditional on ``note is not None``, so a ``{"action": "note"}`` with
        # no note is a request that looks accepted and saves nothing.
        # An EMPTY STRING stays valid: it is the documented way to clear a note.
        if self.action == "note" and self.note is None:
            raise ValueError("note is required when action is note")
        # Also the same guard and the same reasons as the single-incident body —
        # read ``AlertInboxActionRequest.validate_action`` for why an expired
        # instant is a silence no reader can honour, and why the null arm must
        # stay. It matters MORE here: this route exists to silence a screenful at
        # once, so one mistyped instant is up to 200 incidents reported as muted
        # and not one of them actually silenced.
        if self.action == "mute" and self.muted_until is not None:
            self.muted_until = require_future_instant(self.muted_until, field_name="muted_until")
        # Same refusal as the single-incident body.
        if self.action != "mute" and self.muted_until is not None:
            raise ValueError("muted_until is only meaningful when action is mute")
        # ``false_positive`` is refused in bulk, and this is the ONLY action that
        # is. Direction is part of the correlation key (see
        # worker/tasks/metrics/dispatch.py), so ONE scope's spike and ONE scope's
        # drop are two separate incidents sitting side by side in this list —
        # which is precisely what an operator sweeping a noisy scope selects
        # together. ``_tune_false_positive_thresholds`` dedupes only WITHIN a
        # single call (its ``seen`` set) and each step compounds off the scope's
        # own current value, so selecting both would take two ratchet steps on
        # one scope for one human decision, permanently desensitising detection
        # there with nothing in the record to say it was a single click. The
        # per-scope override carries a ``false_positive_count``, not a batch id.
        #
        # This is a REFUSAL rather than a silent dedupe on purpose: deduping
        # would have to guess which of the two incidents the operator meant, and
        # the ratchet is not undoable from the inbox — the undo is deleting the
        # override in Detection settings.
        if self.action == "false_positive":
            raise ValueError(
                "false_positive cannot be applied in bulk. Alert direction is part of the "
                "correlation key, so a single scope's spike and drop are two separate "
                "incidents here, and marking both would tighten that scope's detection "
                "thresholds twice for one decision. Mark false positives one incident at a "
                "time from the incident's own actions."
            )
        return self


class AlertInboxBulkActionResponse(BaseModel):
    """The rebuilt cards for every incident the batch touched.

    DELIBERATELY NOT the house 204 that ``/bulk-update`` and ``/bulk-delete``
    return on events, variables and metrics. Those routes mutate rows the caller
    is about to re-fetch anyway; this one replaces cards the operator is LOOKING
    AT, and its single-incident sibling ``AlertInboxActionResponse`` already
    answers with the rebuilt group for exactly that reason. A 204 would force the
    client to re-list the whole inbox to redraw N rows it had just changed, and
    would throw away ``overrides_written``, which that sibling documents as
    nullable and ALWAYS SENT. ``DeadEventArchiveResponse``
    (services/reconciliation_service.py) is the standing precedent in this repo
    for a bulk route answering with a body instead of 204.
    """

    # In the order the request listed them, with duplicates dropped. It can be
    # SHORTER than the request in one case only: an incident whose deliveries
    # were deleted between this call's commit and its rebuild has no rows left to
    # render a card from, and is omitted rather than 404ing a change that already
    # landed (the failure already fixed on the single route). The state
    # change still happened and the audit row still names it, so a client that
    # wants certainty should match on ``correlation_group_id`` rather than
    # position.
    groups: list[AlertInboxGroupResponse]
    # The id shared by every audit row this call wrote. One row is recorded PER
    # GROUP — ``audit_log.target_id`` holds a single UUID, and a trail that
    # cannot name WHICH incident was muted is worse than none — so this is the
    # only thing tying those N rows back to the one click that made them.
    batch_id: uuid.UUID
    # ALWAYS ``None`` here, and sent anyway. ``false_positive`` is the only
    # action that can ratchet anything and this route refuses it, so there is
    # never a count to report. The key is still present so a client sharing one
    # handler with the single-incident response cannot read a MISSING key as 0
    # and announce "no scopes tightened" after a bulk acknowledge — the exact
    # defect already fixed on the single route.
    overrides_written: int | None


class SimulatedRuleFiring(BaseModel):
    """One virtual delivery the rule would have triggered during the window."""

    anomaly_id: uuid.UUID
    scan_config_id: uuid.UUID | None = None
    scope_type: MetricScopeType
    scope_ref: str
    scope_name: str
    event_type_id: uuid.UUID | None
    event_id: uuid.UUID | None
    drift_field: str | None = None
    drift_type: AlertDriftType | None = None
    sample_value: str | None = None
    bucket: datetime
    # The START of the window this comparison was measured over; ``bucket`` is
    # its end. Only release regressions have one — their window is the
    # activation-anchored rollout overlap rather than a scan bucket — and it is
    # what lets the PREVIEW print the same "over the 51h rollout overlap" clause
    # the delivered message prints, out of the one shared
    # ``alert_templates.build_drift_line``. Previously the replay
    # never loaded a release regression, so the field would have had nothing to
    # hold; the delivered twin (``AlertDeliveryItem.window_from``) has carried
    # it since the scope shipped.
    #
    # Defaulted, like the four drift fields above it and unlike the always-sent
    # nullables on the response models in this module: ``SimulatedRuleFiring``
    # is also hand-built where there is usually no window to give —
    # ``demo.builders.alerts._build_firings``, and the rendering tests for every
    # family except the release regression — and requiring an explicit ``None``
    # at all of those buys nothing. FastAPI still emits the key on every firing.
    window_from: datetime | None = None
    direction: AnomalyDirection
    actual_count: float
    expected_count: float
    absolute_delta: float
    # The stored SIZE of the move, held as a plain float on the object and nulled
    # on the way out — see ``encode_percent_delta`` for why the two differ here
    # and nowhere else.
    percent_delta: float
    rendered_item: str | None = None

    @classmethod
    def from_candidate(
        cls,
        candidate: AlertMatchCandidate,
        *,
        scope_name: str,
        bucket: datetime | None = None,
    ) -> SimulatedRuleFiring:
        """The one place a firing's fields are read off a matched candidate.

        Both builders of this DTO go through here — the rule simulator
        (``alerting_service.simulate_rule``) and the demo seeder
        (``demo.builders.alerts._build_firings``). They used to be two
        hand-maintained constructor calls, and they had already drifted: the
        seeder never passed the drift fields, so a field added here reached the
        live replay and silently not the demo.

        ``scope_name`` is resolved by the caller (each has its own name source)
        and trimmed here the way the live send path trims it. The delta goes
        through the shared ``percent_delta_of``. The drift fields and
        ``window_from`` are read with ``getattr`` because ``AlertMatchCandidate``
        is a Protocol whose ``MetricAnomaly`` members carry none of them.
        ``bucket`` overrides the candidate's when the caller has to normalise it
        (the demo seeder re-attaches the UTC zone SQLite drops).
        """
        return cls(
            anomaly_id=candidate.id,
            scan_config_id=candidate.scan_config_id,
            scope_type=candidate.scope_type,
            scope_ref=candidate.scope_ref,
            scope_name=trim_scope_name(scope_name),
            event_type_id=candidate.event_type_id,
            event_id=candidate.event_id,
            drift_field=getattr(candidate, "drift_field", None),
            drift_type=getattr(candidate, "drift_type", None),
            sample_value=getattr(candidate, "sample_value", None),
            bucket=candidate.bucket if bucket is None else bucket,
            # ``bucket`` is the window's END; this carries the START for the
            # release-regression family, so the preview prints the same
            # "over the 51h rollout overlap" clause the delivered item does.
            window_from=getattr(candidate, "window_from", None),
            direction=candidate.direction,
            actual_count=candidate.actual_count,
            expected_count=candidate.expected_count,
            absolute_delta=abs(candidate.actual_count - candidate.expected_count),
            percent_delta=percent_delta_of(candidate.actual_count, candidate.expected_count),
        )

    @field_serializer("percent_delta")
    def encode_percent_delta(self, value: float) -> float | None:
        """``null``, not the stored 0.0, when there was no baseline to divide by.

        This was the last surface emitting the raw placeholder.
        ``alert_templates.percent_delta_of`` answers 0.0 where
        ``expected_count`` is 0 because the ratio is undefined and the column it
        normally lands in is NOT NULL, and every other outbound encoding already
        NAMES that rather than printing it: the delivery's typed ``items[]``
        (``AlertDeliveryItemResponse``), the inbox card
        (``AlertInboxGroupResponse``), ``payload_snapshot`` and the webhook body
        all route through ``percent_delta_or_none``. The replay did not, so one
        incident answered "how big was this move" with ``null`` on the delivery
        it produced and with ``0.0`` on the simulate response that predicted it,
        and a consumer testing ``percent_delta > threshold`` read "no change"
        for the loudest class of firing there is — a scope firing from nothing,
        or resuming after an outage.

        A SERIALIZER, not the ``@model_validator(mode="after")`` idiom the two
        sibling responses use, and the difference is not stylistic. Those two are
        response-only models. This one is ALSO the DTO two non-API consumers
        read, and both need a real float:
        ``alerting_rendering.render_firing_item`` formats it with
        ``f"{firing.percent_delta:.1f}"`` to fill the documented bare-number
        ``${percent_delta}`` template variable, and
        ``demo.builders.alerts._delivery_item`` copies it into
        ``AlertDeliveryItem.percent_delta``, which is NOT NULL. A validator fires
        at CONSTRUCTION, so it would hand ``None`` to both — and the renderer
        runs inside ``simulate_rule`` itself, meaning the preview would crash for
        precisely the zero-baseline firings this fix exists for. Nulling the
        ENCODING leaves the attribute alone: only the JSON changes.

        The ``float | None`` return annotation is load-bearing, not decoration.
        FastAPI builds a response model's OpenAPI from pydantic's SERIALIZATION
        schema, which takes the field's type from here; drop it and the body goes
        ``null`` while the published contract still promises a number.
        """
        return percent_delta_or_none(value, self.expected_count)


class AlertRuleSimulateResponse(BaseModel):
    rule_id: uuid.UUID
    rule_name: str
    days: int
    window_from: datetime
    window_to: datetime
    anomalies_considered: int
    matched_before_cooldown: int
    firings: list[SimulatedRuleFiring]
    noisy: bool
    # Every knob this replay can answer a what-if about comes back as a
    # ``*_used`` / ``*_saved`` pair: what THIS run applied, and what is stored on
    # the rule. Replay existed to answer "would a stricter rule have cut these
    # incidents", but only the cooldown could be varied, so testing a threshold
    # meant saving it onto a rule that is live-routing to a real channel and
    # waiting to see what it did to production.
    #
    # ``*_used`` equals ``*_saved`` when no override was passed, and mirrors the
    # override otherwise. ``*_saved`` is sent so the UI can show "current vs
    # override" without a second round-trip.
    cooldown_minutes_used: int
    cooldown_minutes_saved: int
    min_percent_delta_used: float
    min_percent_delta_saved: float
    min_expected_count_used: float
    min_expected_count_saved: float
    # The detector's sensitivity, not a rule field. It gates whether an anomaly
    # was RECORDED at all, so the replay can only apply it as a stricter re-read
    # of the rows the detector already wrote: raising it drops recorded anomalies
    # whose |z| no longer clears the bar, while lowering it cannot resurrect
    # anomalies that were never scored. Drift and release-regression signals
    # carry no z-score and are untouched by it, exactly as they bypass the rule's
    # numeric thresholds.
    #
    # ``sigma_threshold_saved`` is the PROJECT's configured threshold — the
    # ``sigma_threshold`` of its Detection settings, and ``DEFAULT_SIGMA_THRESHOLD``
    # for a project that has never opened that screen and so has no settings row.
    # It was quoted off ``ScanConfig.sigma_threshold`` until it was fixed: a
    # per-scan copy of the same number that the detector never reads and no API
    # writes, so a tuned project was told its replay was measured against a value
    # nothing detects with. Per-scope ratchet overrides (the false-positive
    # button) can raise the effective threshold above this for individual scopes,
    # so it is the project-wide base rather than a promise about every scope.
    # ``sigma_threshold_used`` is the override, or the saved value when none was
    # passed.
    #
    # BOTH STAY NULLABLE THOUGH THE SERVER NO LONGER SENDS NULL. The null meant
    # "the scans this rule spans disagree, so there is no one saved value to
    # quote", which cannot arise against a single project-wide setting. Narrowing
    # the pair to ``float`` would re-publish a shipped response contract —
    # ``backend/openapi.json``, ``frontend/src/types/api.gen.ts`` and every
    # generated client holding a nullable field — to describe behaviour no caller
    # can observe, so the wire shape is left alone and the replay dialog keeps
    # its null arm as a defensive one.
    sigma_threshold_used: float | None
    sigma_threshold_saved: float | None
    rendered_message: str | None = None


class AlertScopeReadiness(BaseModel):
    """Whether each drift-style scope has any source data at all, PROJECT-wide.

    Not a per-rule verdict and not a prediction: it answers "could this scope
    ever produce a candidate here", so a client can tell an enabled-but-inert
    toggle from a quiet one. Both fields are always sent and
    neither carries a default — see the no-defaults note on
    ``MonitorSummaryItem`` for why a default here would be a lie to the
    generated client rather than a server behaviour.
    """

    # False when nothing on the MAIN branch documents an allowed-values list —
    # neither a ``Variable.allowed_values`` nor a per-event override — for a
    # variable scans still observe, AND no open/snoozed VariableValueDrift row
    # from the last 30 days is linked to one of this project's scans.
    # Documentation on a working branch does not count until it merges: the
    # detector runs against main (core/analyzers/event_generator.py). Rows alone
    # are enough — the candidate builder reads rows, not documentation
    # (worker/tasks/metrics/signals.py).
    variable_value_drift: bool
    # False when no scan config lists ``distribution_drift_fields`` AND no
    # DistributionDrift row exists yet. Rows alone are enough — the candidate
    # builder reads rows, not config (worker/tasks/metrics/signals.py).
    distribution_drift: bool


class MonitorSummaryItem(BaseModel):
    """One alert rule rolled up into a single monitor status."""

    rule_id: uuid.UUID
    rule_name: str
    destination_id: uuid.UUID
    destination_name: str
    destination_type: str
    enabled: bool
    status: Literal["firing", "warning", "healthy"]
    active_scope_count: int
    firing_scope_count: int
    # Nullable but ALWAYS SENT, so no defaults anywhere on this model — the two
    # builders in ``_alerting_monitors`` name every field. A default here is not
    # a server behaviour, it is a claim to the generated client that the key can
    # be missing, and it made the SAME AlertRule carry two TypeScript shapes:
    # ``muted``/``muted_until``/``last_delivery_at``/``last_delivery_status``
    # were required on ``AlertRuleResponse`` (the destination card) and optional
    # here (the monitors screen), for one object the server always describes in
    # full. Same rule as ``event_id`` on the inbox group.
    last_anomaly_at: datetime | None
    last_notified_at: datetime | None
    # Condition summary (so the UI can show the monitor's trigger at a glance).
    notify_on_spike: bool
    notify_on_drop: bool
    min_percent_delta: float
    min_expected_count: float
    cooldown_minutes: int
    # Manual snooze state. ``muted`` is the effective flag (``muted_until`` in
    # the future); ``muted_until`` is the raw timestamp the mute lifts at.
    muted: bool
    muted_until: datetime | None


class MonitorsSummaryResponse(BaseModel):
    monitors: list[MonitorSummaryItem]
    firing_count: int
    warning_count: int
    healthy_count: int
    total: int
    # On the envelope, never on ``MonitorSummaryItem``: this is one fact about
    # the PROJECT, and repeating it per rule would be N copies of a value that
    # cannot differ between them.
    scope_readiness: AlertScopeReadiness


class MonitorFiringScope(BaseModel):
    """One scope of a monitor that is firing now.

    Chosen by the same horizon test as ``firing_scope_count``, so the list and
    the count cannot disagree. ``scope_name``, ``event_id`` and ``direction``
    come from the item of the delivery that last notified this scope; all three
    are null for a scope the rule has not notified (a cooldown or a mute can
    hold the first message back while the state is already open).
    """

    scan_config_id: uuid.UUID | None
    scope_type: MetricScopeType
    scope_ref: str
    scope_name: str | None
    event_id: uuid.UUID | None
    direction: AnomalyDirection | None
    last_anomaly_bucket: datetime
    last_notified_at: datetime | None


class MonitorDetailResponse(MonitorSummaryItem):
    """A single monitor with the extra context a drill-in detail view needs."""

    # Raw enable flags, so the UI can explain why a monitor is disabled
    # (``enabled`` on the summary is the AND of these two).
    rule_enabled: bool
    destination_enabled: bool
    # WHICH scan's anomalies this rule can see. SAME name and SAME meaning as
    # ``AlertRuleResponse.scan_config_id`` and the column behind both: null means
    # every scan in the project. ``scan_name`` is that scan's display name, null
    # exactly when the id is — shipped beside the id for the reason
    # ``AlertDeliveryResponse`` ships ``scan_config_id`` next to ``scan_name``:
    # the consuming screen holds no scans list to resolve an id against. Named
    # rather than pointed at by line number: the ``(:NNN)`` refs this tree uses
    # elsewhere rot silently, and this one was already two lines off when written.
    #
    # These two and ``scope_readiness`` below answer DIFFERENT questions and must
    # never be read as one:
    #   scan_config_id / scan_name — which scan this rule is narrowed to.
    #   scope_readiness            — whether the PROJECT has any source data for
    #                                a drift scope. It is NOT narrowed by the
    #                                binding above, so a rule bound to a scan
    #                                that feeds nothing can still read ready
    #                                because a sibling scan does.
    # Naming them separately is the whole point: one name carrying two meanings
    # on two responses is a failure that was reported.
    scan_config_id: uuid.UUID | None
    scan_name: str | None
    # Scope coverage — which signal kinds this monitor subscribes to.
    include_project_total: bool
    include_event_types: bool
    include_events: bool
    include_schema_drifts: bool
    include_distribution_drifts: bool
    include_variable_value_drifts: bool
    include_release_regressions: bool
    include_metrics: bool
    include_source_freshness: bool
    include_lifecycle: bool
    include_property_drifts: bool
    # Quick fired-history stats for the detail header (full history comes from
    # GET /alert-deliveries?rule_id=...). The same three numbers
    # ``AlertRuleResponse`` carries for this rule, under the same names and — see
    # the no-defaults note on ``MonitorSummaryItem`` — now with the same
    # required-but-nullable shape.
    total_deliveries: int
    last_delivery_at: datetime | None
    last_delivery_status: AlertDeliveryStatus | None
    # The scopes behind ``firing_scope_count``, newest anomaly first.
    firing_scopes: list[MonitorFiringScope]
    # The SAME block, under the same name and with the same meaning, as the one
    # on ``MonitorsSummaryResponse``. The monitors list and the monitor detail
    # describe one project, and a field name that meant two things on two
    # responses is a disagreement that was reported.
    #
    # Still PROJECT-level even on a rule that carries a ``scan_config_id`` above:
    # narrowing it here and not on the summary would give one name two meanings,
    # and narrowing it on both would need a per-draft query the rule editor does
    # not have.
    scope_readiness: AlertScopeReadiness


class MonitorMuteRequest(BaseModel):
    # Timed mute ONLY: the monitor stays muted until this instant, which must be
    # in the future. REQUIRED and non-null, unlike the inbox mute action this
    # used to mirror — there a null ``muted_until`` now means "muted until I
    # unmute", but here it means NOT MUTED: ``is_rule_muted``
    # answers False for a null, and null is the default on every AlertRule ever
    # created. Making this optional to match the inbox would mute the whole
    # fleet at once. A rule's permanent lever is ``enabled``.
    muted_until: datetime

    @field_validator("muted_until")
    @classmethod
    def normalize_datetime(cls, value: datetime) -> datetime:
        """Read an instant sent without an offset as UTC instead of 500ing on it.

        ``{"muted_until": "2026-09-11T10:00:00"}`` is a contract-legal body —
        the published schema says ``format: date-time`` and has never demanded
        an offset — and pydantic keeps such a value NAIVE. ``mute_monitor``
        then compares it against ``datetime.now(UTC)``, which raises "can't
        compare offset-naive and offset-aware datetimes"; the only handler for
        that is the catch-all in ``main.py``, so a client that omitted the
        offset got a 500 instead of a mute or the 422 about a past instant.
        The browser is not affected — it sends aware ISO
        strings — so this is an API/agent-client defect only.

        Coercion rather than ``AwareDatetime``: rejecting the naive value would
        turn today's 500 into a 422 and break every client that omits the
        offset, while this keeps every currently-accepted body accepted and
        leaves the past-instant 422 as the single refusal on this route. It
        also normalizes what the route audits (``data.model_dump(mode="json")``
        on ``mute_monitor``), which used to record a floating wall time that no
        reader could place. Same rule and same name as
        ``ScanMetricsReplayRequest.normalize_datetime`` — one idiom for this,
        not a second one.
        """
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
