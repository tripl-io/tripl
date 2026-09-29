import re
import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator

from tripl.core.adapters.measure_validator import validate_select_sql_safety
from tripl.core.intervals import get_interval
from tripl.core.json_string_columns import (
    check_json_string_columns_roles,
    normalize_json_string_columns,
)
from tripl.core.scan_setup_preset import ScanSetupPreset, apply_setup_preset
from tripl.json_paths import (
    MAX_PROPERTY_FIELDS,
    normalize_json_value_paths,
    split_property_field,
)
from tripl.models.domain_enums import ScanInterval
from tripl.models.scan_job import ScanJobStatus
from tripl.schemas.not_null_update import reject_explicit_nulls


class EventGroupCondition(BaseModel):
    field: str = Field(min_length=1, max_length=255)
    pattern: str = Field(min_length=1, max_length=500)

    @field_validator("field", "pattern")
    @classmethod
    def strip_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value cannot be blank")
        return stripped

    @field_validator("pattern")
    @classmethod
    def validate_regex(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid regex pattern: {exc}") from exc
        return value


class EventGroupRule(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    condition_logic: Literal["all", "any"] = "all"
    conditions: list[EventGroupCondition] = Field(min_length=1, max_length=20)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name cannot be blank")
        return stripped


def check_scalar_columns_unreserved(
    *,
    metric_breakdown_columns: list[str],
    distribution_drift_fields: list[str],
    event_type_column: str | None,
    time_column: str | None,
    app_version_column: str | None = None,
    platform_column: str | None = None,
) -> None:
    """Reject selections that overlap reserved time/grouping/version/platform columns.

    Used by both ScanConfigCreate (full payload) and the scan service
    update path (merged payload). Raises ValueError so pydantic surfaces it
    as 422 directly; the service-layer catches it to convert to HTTPException.

    ``app_version_column`` and ``platform_column`` are reserved because, when set,
    each is collected on its own path; letting either double as a generic
    breakdown or drift column would collect it twice. The platform column is also
    barred from coinciding with the other reserved roles.
    """
    reserved = {
        column
        for column in (event_type_column, time_column, app_version_column, platform_column)
        if column
    }
    if set(metric_breakdown_columns) & reserved:
        raise ValueError(
            "metric_breakdown_columns cannot include event_type_column, time_column, "
            "app_version_column or platform_column"
        )
    if set(distribution_drift_fields) & reserved:
        raise ValueError(
            "distribution_drift_fields cannot include event_type_column, time_column, "
            "app_version_column or platform_column"
        )
    other_roles = {
        column for column in (event_type_column, time_column, app_version_column) if column
    }
    if platform_column and platform_column in other_roles:
        raise ValueError(
            "platform_column cannot also be event_type_column, time_column or app_version_column"
        )


def check_replay_chunk_against_interval(
    *,
    interval: str | None,
    replay_chunk_interval: str | None,
) -> None:
    """A replay chunk cannot be finer than the collection interval (you can't
    split a window below one bucket). NULL chunk means "no split" and is allowed
    regardless of interval."""
    if replay_chunk_interval is None:
        return
    if interval is None:
        raise ValueError("replay_chunk_interval requires a collection interval")
    if get_interval(replay_chunk_interval).delta < get_interval(interval).delta:
        raise ValueError("replay_chunk_interval must be greater than or equal to interval")


def validate_prerelease_pattern(value: str | None) -> str | None:
    if value is not None:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid prerelease pattern: {exc}") from exc
    return value


def _apply_preset_to_model(model: BaseModel) -> None:
    """Run ``apply_setup_preset`` on a create/dry-run payload, in place.

    ``model_fields_set`` is what the caller sent: a default is never a conflict.
    """
    derived = apply_setup_preset(model.model_dump(), explicit=model.model_fields_set)
    for key, value in derived.items():
        setattr(model, key, value)


class ScanConfigCreate(BaseModel):
    data_source_id: uuid.UUID
    event_type_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=255)
    base_query: str = Field(min_length=1)
    event_type_column: str | None = None
    time_column: str | None = None
    # Bounded to the width of ``scan_configs.event_name_format`` (String(500)).
    # A format is a short template like ``${action}_${object}``, but nothing was
    # enforcing that, so a long one passed validation here and failed in the
    # INSERT as a Postgres StringDataRightTruncation — a generic 500 for a body
    # this layer had already accepted (tripl-0zpq.275).
    event_name_format: str | None = Field(None, max_length=500)
    json_value_paths: list[str] = Field(default_factory=list)
    event_group_rules: list[EventGroupRule] = Field(default_factory=list)
    # "event_properties": names come from ``event_name_column`` and every key of
    # ``properties_column`` is catalogued as a property; the name format, JSON
    # value paths, Event type column and group rules are derived
    # (``core.scan_setup_preset``). "custom": everything as sent.
    setup_preset: ScanSetupPreset = "custom"
    event_name_column: str | None = Field(default=None, min_length=1, max_length=255)
    properties_column: str | None = Field(default=None, min_length=1, max_length=255)
    # String (ClickHouse) / STRING (BigQuery) columns to parse as JSON, so their
    # keys become properties like a JSON column's (``core.json_string_columns``).
    json_string_columns: list[str] = Field(default_factory=list)
    metric_breakdown_columns: list[str] = Field(default_factory=list)
    metric_breakdown_values_limit: int | None = Field(default=None, ge=1)
    distribution_drift_fields: list[str] = Field(default_factory=list)
    cardinality_threshold: int = Field(default=100, ge=1)
    interval: ScanInterval | None = None
    replay_chunk_interval: ScanInterval | None = None
    scan_lookback_hours: int | None = Field(default=None, ge=1)
    scan_row_limit: int | None = Field(default=None, ge=1)
    metrics_row_limit: int | None = Field(default=None, ge=1)
    app_version_column: str | None = Field(default=None, min_length=1, max_length=255)
    app_version_keep_releases: int | None = Field(
        default=None,
        ge=1,
        deprecated=True,
        description=(
            "Deprecated compatibility mirror of Project.app_version_keep_releases; "
            "caller values are ignored."
        ),
    )
    app_version_prerelease_pattern: str | None = Field(default=None, min_length=1, max_length=255)
    app_version_active_share_min: float | None = Field(default=None, gt=0.0, lt=1.0)
    platform_column: str | None = Field(default=None, min_length=1, max_length=255)

    _validate_prerelease_pattern = field_validator("app_version_prerelease_pattern")(
        validate_prerelease_pattern
    )

    @field_validator("base_query")
    @classmethod
    def validate_base_query(cls, value: str) -> str:
        return validate_select_sql_safety(value)

    @field_validator("json_value_paths")
    @classmethod
    def validate_json_value_paths(cls, value: list[str]) -> list[str]:
        normalized = normalize_json_value_paths(value)
        invalid = sorted(set(value) - set(normalized))
        if invalid:
            raise ValueError("json_value_paths must use <json_column>.<nested.path> format")
        return normalized

    @field_validator("json_string_columns")
    @classmethod
    def validate_json_string_columns(cls, value: list[str]) -> list[str]:
        return normalize_json_string_columns(value)

    @field_validator("metric_breakdown_columns")
    @classmethod
    def validate_metric_breakdown_columns(cls, value: list[str]) -> list[str]:
        return _normalize_scalar_columns(
            value,
            field_name="metric_breakdown_columns",
        )

    @field_validator("distribution_drift_fields")
    @classmethod
    def validate_distribution_drift_fields(cls, value: list[str]) -> list[str]:
        return _normalize_scalar_columns(
            value,
            field_name="distribution_drift_fields",
        )

    @model_validator(mode="after")
    def validate_monitoring_selection(self) -> ScanConfigCreate:
        # First: the preset decides the event type column checked below.
        _apply_preset_to_model(self)
        check_scalar_columns_unreserved(
            metric_breakdown_columns=self.metric_breakdown_columns,
            distribution_drift_fields=self.distribution_drift_fields,
            event_type_column=self.event_type_column,
            time_column=self.time_column,
            app_version_column=self.app_version_column,
            platform_column=self.platform_column,
        )
        check_json_string_columns_roles(
            self.json_string_columns,
            event_type_column=self.event_type_column,
            time_column=self.time_column,
            app_version_column=self.app_version_column,
            platform_column=self.platform_column,
            event_name_column=self.event_name_column,
            metric_breakdown_columns=self.metric_breakdown_columns,
            distribution_drift_fields=self.distribution_drift_fields,
        )
        check_replay_chunk_against_interval(
            interval=self.interval,
            replay_chunk_interval=self.replay_chunk_interval,
        )
        return self


def _normalize_scalar_columns(value: list[str], *, field_name: str) -> list[str]:
    """Trim and dedupe a breakdown / distribution-drift selection.

    An entry is a scalar column or a property: ``<json_column>.<path>``, the
    ``json_value_paths`` format (F23, #306). A property must follow the path
    grammar every adapter extracts safely, and at most ``MAX_PROPERTY_FIELDS``
    of them may be listed, since each one parses the JSON document per row.
    """
    normalized: list[str] = []
    seen: set[str] = set()
    properties = 0
    for item in value:
        column = item.strip()
        if not column:
            continue
        try:
            is_property = split_property_field(column) is not None
        except ValueError as exc:
            raise ValueError(f"{field_name}: {exc}") from exc
        if column in seen:
            continue
        if is_property:
            properties += 1
            if properties > MAX_PROPERTY_FIELDS:
                raise ValueError(f"{field_name} can list at most {MAX_PROPERTY_FIELDS} properties")
        normalized.append(column)
        seen.add(column)
    return normalized


# The update fields whose ScanConfig column is NOT NULL, so an explicit ``null``
# is a 422 naming the field and not a DB-level 500 out of
# ``update_scan_config``'s generic ``setattr`` loop — see
# ``schemas/not_null_update`` (tripl-0zpq.267). The four JSON list columns are in
# the set because they are NOT NULL with a ``[]`` server default: "no columns" is
# spelled ``[]``, never ``null``. ``event_type_id`` and every other field here is
# nullable, where a null unbinds or clears the setting.
_SCAN_CONFIG_NOT_NULL_UPDATE_FIELDS = frozenset(
    {
        "name",
        "base_query",
        "json_value_paths",
        "event_group_rules",
        "setup_preset",
        "json_string_columns",
        "metric_breakdown_columns",
        "distribution_drift_fields",
        "cardinality_threshold",
    }
)


class ScanConfigUpdate(BaseModel):
    event_type_id: uuid.UUID | None = None
    name: str | None = Field(None, min_length=1, max_length=255)
    base_query: str | None = Field(None, min_length=1)
    event_type_column: str | None = None
    time_column: str | None = None
    # Same bound as on create: the PATCH writes the same column.
    event_name_format: str | None = Field(None, max_length=500)
    json_value_paths: list[str] | None = None
    event_group_rules: list[EventGroupRule] | None = None
    # Checked by ``scan_service.update_scan_config`` on the merged config.
    setup_preset: ScanSetupPreset | None = None
    event_name_column: str | None = Field(default=None, max_length=255)
    properties_column: str | None = Field(default=None, max_length=255)
    json_string_columns: list[str] | None = None
    metric_breakdown_columns: list[str] | None = None
    metric_breakdown_values_limit: int | None = Field(default=None, ge=1)
    distribution_drift_fields: list[str] | None = None
    cardinality_threshold: int | None = Field(None, ge=1)
    interval: ScanInterval | None = None
    replay_chunk_interval: ScanInterval | None = None
    scan_lookback_hours: int | None = Field(default=None, ge=1)
    scan_row_limit: int | None = Field(default=None, ge=1)
    metrics_row_limit: int | None = Field(default=None, ge=1)
    app_version_column: str | None = Field(default=None, max_length=255)
    app_version_keep_releases: int | None = Field(
        default=None,
        ge=1,
        deprecated=True,
        description=(
            "Deprecated compatibility mirror of Project.app_version_keep_releases; "
            "caller values are ignored."
        ),
    )
    app_version_prerelease_pattern: str | None = Field(default=None, max_length=255)
    app_version_active_share_min: float | None = Field(default=None, gt=0.0, lt=1.0)
    platform_column: str | None = Field(default=None, max_length=255)

    _validate_prerelease_pattern = field_validator("app_version_prerelease_pattern")(
        validate_prerelease_pattern
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_nulls(cls, data: object) -> object:
        return reject_explicit_nulls(data, _SCAN_CONFIG_NOT_NULL_UPDATE_FIELDS)

    @field_validator("base_query")
    @classmethod
    def validate_base_query(cls, value: str | None) -> str | None:
        return value if value is None else validate_select_sql_safety(value)

    @field_validator("json_value_paths")
    @classmethod
    def validate_json_value_paths(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = normalize_json_value_paths(value)
        invalid = sorted(set(value) - set(normalized))
        if invalid:
            raise ValueError("json_value_paths must use <json_column>.<nested.path> format")
        return normalized

    @field_validator("json_string_columns")
    @classmethod
    def validate_json_string_columns(cls, value: list[str] | None) -> list[str] | None:
        return value if value is None else normalize_json_string_columns(value)

    @field_validator("metric_breakdown_columns")
    @classmethod
    def validate_metric_breakdown_columns(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return _normalize_scalar_columns(value, field_name="metric_breakdown_columns")

    @field_validator("distribution_drift_fields")
    @classmethod
    def validate_distribution_drift_fields(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return _normalize_scalar_columns(value, field_name="distribution_drift_fields")


SourceFreshnessStatus = Literal["fresh", "late", "overdue", "unknown"]


class SourceFreshness(BaseModel):
    """How current a scan's source data is (#269). Computed, never stored.

    ``fresh`` — events are arriving and the scan is running on schedule.
    ``late`` — the newest event is older than the scan's lateness allowance
    (``max(3 x interval, settling window + interval)``).
    ``overdue`` — the scan itself has not completed a collection within
    ``2 x interval``; wins over ``late``.
    ``unknown`` — the scan has no schedule (manual only) or nothing recorded yet.

    ``lag_seconds`` is ``now - last_event_at``; ``expected_by`` the moment the
    source counts (or counted) as late: ``last_event_at`` plus the allowance.
    """

    status: SourceFreshnessStatus
    lag_seconds: int | None = None
    last_event_at: datetime | None = None
    last_collection_at: datetime | None = None
    expected_by: datetime | None = None


class SourceFreshnessItem(BaseModel):
    """One scan config's freshness, for Overview and data-source cards."""

    id: uuid.UUID
    name: str
    data_source_id: uuid.UUID
    freshness: SourceFreshness


class ScanConfigResponse(BaseModel):
    id: uuid.UUID
    data_source_id: uuid.UUID
    project_id: uuid.UUID
    event_type_id: uuid.UUID | None
    name: str
    base_query: str
    event_type_column: str | None
    time_column: str | None
    event_name_format: str | None
    json_value_paths: list[str]
    event_group_rules: list[EventGroupRule]
    setup_preset: ScanSetupPreset = "custom"
    event_name_column: str | None = None
    properties_column: str | None = None
    json_string_columns: list[str] = Field(default_factory=list)
    metric_breakdown_columns: list[str]
    metric_breakdown_values_limit: int | None
    distribution_drift_fields: list[str]
    cardinality_threshold: int
    interval: ScanInterval | None
    replay_chunk_interval: ScanInterval | None
    scan_lookback_hours: int | None
    scan_row_limit: int | None
    metrics_row_limit: int | None
    app_version_column: str | None
    app_version_keep_releases: int | None = Field(
        deprecated=True,
        description="Deprecated compatibility mirror of Project.app_version_keep_releases.",
    )
    app_version_prerelease_pattern: str | None
    app_version_active_share_min: float | None
    platform_column: str | None
    last_event_at: datetime | None = None
    last_collection_at: datetime | None = None
    # Filled by ``scan_config_service`` (it needs the project's settling window),
    # so build responses through ``scan_config_service.scan_config_response(s)``
    # rather than ``model_validate(config)`` on its own.
    freshness: SourceFreshness
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def monitoring_enabled(self) -> bool:
        """Whether the scheduler collects metrics for this scan (MO-23).

        Derived, never stored: the beat schedule and every monitoring read path
        select on ``interval IS NOT NULL``, so this is that test said out loud
        rather than a second switch that could disagree with it.
        """
        return self.interval is not None


class ScanConfigDetailResponse(ScanConfigResponse):
    """One scan config with its metrics schedule (DA-5).

    Only ``GET /scans/{id}`` computes these, so they are not on the list rows.
    ``last_metrics_run_at`` is when the newest scheduled metrics collection
    finished; ``next_metrics_run_at`` the earliest moment the scheduler
    considers the config due again (``now`` when it is due already). Both are
    null for a scan the scheduler never collects (no interval or no time
    column), and ``last_metrics_run_at`` also before its first collection.
    """

    last_metrics_run_at: datetime | None = None
    next_metrics_run_at: datetime | None = None


class ScanPreviewColumnResponse(BaseModel):
    name: str
    type_name: str
    is_nullable: bool


class ScanPreviewJsonPathResponse(BaseModel):
    full_path: str
    path: str
    sample_values: list[str]


class ScanPreviewJsonColumnResponse(BaseModel):
    column: str
    paths: list[ScanPreviewJsonPathResponse]


class ScanConfigPreviewRequest(BaseModel):
    data_source_id: uuid.UUID
    base_query: str = Field(min_length=1)
    limit: int = Field(default=10, ge=1, le=50)
    json_value_paths: list[str] = Field(default_factory=list)
    time_column: str | None = Field(default=None, min_length=1, max_length=255)
    scan_lookback_hours: int | None = Field(default=None, ge=1)
    # When true, run the slow JSON path discovery instead of the fast preview.
    include_json_paths: bool = False
    # The "event + properties" preset's columns. With both, the fast preview
    # also returns ``event_properties``: what the sample rows would yield.
    event_name_column: str | None = Field(default=None, min_length=1, max_length=255)
    properties_column: str | None = Field(default=None, min_length=1, max_length=255)
    # The draft's String columns to parse as JSON: the preview reads the source
    # the way the saved scan will, so their keys are discovered too.
    json_string_columns: list[str] = Field(default_factory=list)

    @field_validator("base_query")
    @classmethod
    def validate_base_query(cls, value: str) -> str:
        return validate_select_sql_safety(value)

    @field_validator("json_string_columns")
    @classmethod
    def validate_json_string_columns(cls, value: list[str]) -> list[str]:
        return normalize_json_string_columns(value)

    @field_validator("json_value_paths")
    @classmethod
    def validate_json_value_paths(cls, value: list[str]) -> list[str]:
        normalized = normalize_json_value_paths(value)
        invalid = sorted(set(value) - set(normalized))
        if invalid:
            raise ValueError("json_value_paths must use <json_column>.<nested.path> format")
        return normalized


class ScanPreviewEventProperty(BaseModel):
    """One key of the properties column, as the preview's sample rows carry it."""

    path: str
    # Share of this event's sample rows that carried the key.
    presence: float
    # ``core.property_schema.infer_property_type`` over the sampled values; None
    # when the kinds disagree or nothing typed was seen.
    type: str | None
    sample_values: list[str]


class ScanPreviewEvent(BaseModel):
    name: str
    sample_rows: int
    properties: list[ScanPreviewEventProperty]


class ScanPreviewEventProperties(BaseModel):
    """What the "event + properties" preset yields from the preview's sample.

    A sample, never a census: an event or key absent here may still exist. The
    dry run answers for the lookback window.
    """

    event_name_column: str
    properties_column: str
    sample_rows: int
    events: list[ScanPreviewEvent]
    # Why nothing could be summarised (a missing or non-JSON column), or None.
    error: str | None = None


class ScanConfigPreviewResponse(BaseModel):
    columns: list[ScanPreviewColumnResponse]
    rows: list[dict[str, object]]
    json_columns: list[ScanPreviewJsonColumnResponse]
    event_properties: ScanPreviewEventProperties | None = None


class ScanDryRunRequest(BaseModel):
    """Inputs for "what would this scan create?".

    Two shapes, and exactly one of them must be supplied. The scan form has no
    saved config, so it sends the draft — that is the only shape the UI sends.
    ``scan_config_id`` is the API/agent shape (``integrate/agent-api-guide.md``):
    a caller that already has a stored config asks about it by id instead of
    re-serialising twenty fields it did not author, and every draft field below
    is then ignored. Nothing in the frontend uses it today; it is a documented
    capability of the HTTP API, not unfinished UI.
    """

    scan_config_id: uuid.UUID | None = None
    data_source_id: uuid.UUID | None = None
    base_query: str | None = Field(default=None, min_length=1)
    event_type_id: uuid.UUID | None = None
    event_type_column: str | None = None
    time_column: str | None = None
    # Same bound again: a dry run stores the draft it was asked about, and
    # ``scan_dry_run_jobs.event_name_format`` is String(500) like the real one.
    event_name_format: str | None = Field(None, max_length=500)
    event_group_rules: list[EventGroupRule] = Field(default_factory=list)
    json_value_paths: list[str] = Field(default_factory=list)
    setup_preset: ScanSetupPreset = "custom"
    event_name_column: str | None = Field(default=None, min_length=1, max_length=255)
    properties_column: str | None = Field(default=None, min_length=1, max_length=255)
    json_string_columns: list[str] = Field(default_factory=list)
    cardinality_threshold: int = Field(default=100, ge=1)
    app_version_column: str | None = Field(default=None, min_length=1, max_length=255)
    platform_column: str | None = Field(default=None, min_length=1, max_length=255)
    scan_lookback_hours: int | None = Field(default=None, ge=1)
    # How many breakdown combinations the dry-run may examine. Bounded at both
    # ends: below 100 the answer is noise, above 20000 it is a production scan.
    sample_row_limit: int = Field(default=5000, ge=100, le=20000)

    @field_validator("base_query")
    @classmethod
    def validate_base_query(cls, value: str | None) -> str | None:
        # The same gate ScanConfigCreate applies. Reused, never re-implemented:
        # this endpoint executes free-text SQL against a stored credential.
        return value if value is None else validate_select_sql_safety(value)

    @field_validator("json_string_columns")
    @classmethod
    def validate_json_string_columns(cls, value: list[str]) -> list[str]:
        return normalize_json_string_columns(value)

    @field_validator("json_value_paths")
    @classmethod
    def validate_json_value_paths(cls, value: list[str]) -> list[str]:
        normalized = normalize_json_value_paths(value)
        invalid = sorted(set(value) - set(normalized))
        if invalid:
            raise ValueError("json_value_paths must use <json_column>.<nested.path> format")
        return normalized

    @model_validator(mode="after")
    def validate_target(self) -> ScanDryRunRequest:
        if self.scan_config_id is not None:
            return self
        if self.data_source_id is None or not self.base_query:
            raise ValueError(
                "either scan_config_id, or both data_source_id and base_query, must be provided"
            )
        _apply_preset_to_model(self)
        check_json_string_columns_roles(
            self.json_string_columns,
            event_type_column=self.event_type_column,
            time_column=self.time_column,
            app_version_column=self.app_version_column,
            platform_column=self.platform_column,
            event_name_column=self.event_name_column,
        )
        # The preset names its events itself, and files them under the chosen
        # event type or the one it creates.
        if self.setup_preset == "event_properties":
            return self
        # A draft that names neither is unanswerable, not merely empty: the
        # planner resolves event types exactly the way a real run does, and both
        # abort on this. Rejecting it here turns what was a dispatched job that
        # failed with the worker's internal precondition into a 422 the caller
        # can act on before any warehouse query is issued.
        if self.event_type_id is None and not self.event_type_column:
            # Named after the CONTROLS, not the columns behind them. The worker's
            # sibling message (``_errors.NO_EVENT_NAMING_MSG``) was rewritten the
            # same way in this change, and a 422 body is read by a person — the
            # agent API and the CLI surface it verbatim. "event_type_id" is the
            # exact vocabulary this epic exists to keep off a user's screen.
            raise ValueError(
                "set either Event type or Event type column, so the preview "
                "knows how this scan names its events"
            )
        return self


class ScanDryRunEvent(BaseModel):
    """One event the config would produce, and how much of the sample it is.

    An event is identified by ``(event_type, source_name)``, not by the name
    alone: a run writes one Event per event type, so a grouped scan whose name
    format collapses to the same string under two event types creates two
    Events. Listing them as one would undercount the answer this panel exists to
    give.
    """

    name: str
    source_name: str
    # The event type this event would be written under. On the grouped
    # (``event_type_column``) path this is the group value; with an explicit
    # ``event_type_id`` every row shares the one event type's name.
    event_type: str
    approx_row_count: int
    share_of_sample: float
    # Only two values are emitted. "merged" is carried by ``grouped_by_rule``
    # instead: a grouped event is ALSO either new or already in the plan, and a
    # three-way status would make "N new · M already in your plan" fail to
    # account for every event in the list.
    status: Literal["new", "existing"]
    grouped_by_rule: str | None = None
    count_confidence: Literal["exact", "sampled"]


class ScanDryRunField(BaseModel):
    """A field the config would add to the plan.

    ``type`` is "json" or "string" and nothing else — that is the ENTIRE
    inference the scan performs (``worker.tasks.metrics.generation``). Promising
    integer/timestamp here would be a lie about what the scan creates.
    """

    name: str
    type: Literal["json", "string"]
    status: Literal["new", "exists"]
    event_type: str


class ScanDryRunTemplatedColumn(BaseModel):
    """A column collapsed into a ``${column}`` template by the cardinality rule.

    Surfaced so the user can see WHY they got 3 events instead of 3000 — it is a
    step function of a threshold they are editing in the same form, not a
    property of their data.
    """

    column: str
    distinct_values: int
    threshold: int


class ScanDryRunDuplicateOf(BaseModel):
    event_id: uuid.UUID
    name: str
    score: float
    status: str


class ScanDryRunNameWarning(BaseModel):
    """A problem with the NAMES a run would create (GH #265).

    ``combinatorial_explosion``: more than 50 new names under one event type
    differ only in one slot (``slot`` / ``slot_label``; ``pattern`` shows the
    fixed parts with ``*``), i.e. a high-cardinality value is part of the name.
    ``duplicate``: the new event ``name`` looks like ``duplicate_of`` already in
    the plan. Best-effort; an empty list never means "checked and clean" when
    the check could not run.
    """

    code: Literal["combinatorial_explosion", "duplicate"]
    event_type: str
    message: str
    count: int | None = None
    slot: int | None = None
    slot_label: str | None = None
    pattern: str | None = None
    samples: list[str] = Field(default_factory=list)
    name: str | None = None
    duplicate_of: ScanDryRunDuplicateOf | None = None


class ScanDryRunResponse(BaseModel):
    """What a scan would create, bounded by three separate partialities.

    1. the lookback window (``window_from`` / ``window_to``) — an event absent
       from 24h is not an event that will not be created;
    2. the sample (``sample_is_complete``) — when false the caller must say
       "at least N", never "N";
    3. the event cap (``max_events_reached``) — the real scan stops there too.

    Never extrapolate any of these to a table-wide total. ``share_of_sample``
    exists so the caller does not have to invent one.
    """

    window_from: datetime | None = None
    window_to: datetime | None = None
    # Warehouse rows the examined combinations cover (the sum of each row's
    # ``_cnt``), NOT the number of combinations — that is
    # ``breakdown_combinations``.
    sampled_rows: int = 0
    sample_row_limit: int = 0
    sample_is_complete: bool = True
    breakdown_combinations: int = 0
    events: list[ScanDryRunEvent] = Field(default_factory=list)
    events_truncated: bool = False
    max_events_reached: bool = False
    fields: list[ScanDryRunField] = Field(default_factory=list)
    templated_columns: list[ScanDryRunTemplatedColumn] = Field(default_factory=list)
    reserved_columns: list[str] = Field(default_factory=list)
    unmapped_columns: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # Name-format failures land here rather than failing the job: catching an
    # unknown-key format in a dry-run, instead of after 200 failed production
    # runs, is the single highest-value thing this endpoint does (tripl-lpin).
    errors: list[str] = Field(default_factory=list)
    name_warnings: list[ScanDryRunNameWarning] = Field(default_factory=list)


class ScanDryRunJobResponse(BaseModel):
    id: uuid.UUID
    status: ScanJobStatus
    started_at: datetime | None
    completed_at: datetime | None
    # Typed, unlike ``ScanPreviewJobResponse``'s bare dict: the payload is the
    # whole point of the endpoint, and typing it here is what puts
    # ``ScanDryRunResponse`` in the OpenAPI components so the frontend codegens
    # the shape instead of hand-copying it. Null while pending/running or on
    # failure.
    result_summary: ScanDryRunResponse | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ScanMetricsReplayRequest(BaseModel):
    time_from: datetime
    time_to: datetime

    @field_validator("time_from", "time_to")
    @classmethod
    def normalize_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_window(self) -> ScanMetricsReplayRequest:
        if self.time_from >= self.time_to:
            raise ValueError("time_from must be earlier than time_to")
        return self
