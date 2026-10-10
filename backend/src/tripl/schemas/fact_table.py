from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tripl.core.adapters.measure_validator import (
    validate_identifier,
    validate_select_sql_safety,
    validate_sql_fragment,
)
from tripl.schemas.integers import INT32_MAX
from tripl.schemas.not_null_update import reject_explicit_nulls


def _validate_optional_identifier(value: str | None) -> str | None:
    """Run ``validate_identifier`` on a non-None identifier-class field.

    Identifier-class fields flow into warehouse SQL with no bound parameters, so
    the schema boundary is the only gate. ``None`` (the field is absent) is left
    untouched; any provided value must pass the allowlist regex or raise.
    """
    return value if value is None else validate_identifier(value)


def _validate_identifier_list(value: list[str]) -> list[str]:
    """Validate every member of an identifier-column list against the allowlist."""
    return [validate_identifier(item) for item in value]


# A warehouse type NAME has no length contract: a labelled ClickHouse
# ``Enum8('checkout_started' = 1, ...)``, a named ``Tuple``/``Map``, or a BigQuery
# ``STRUCT<...>`` renders well past this bound, and introspection passes the
# adapter's string through verbatim. The value is descriptive and every consumer
# reads its HEAD (``core.warehouse_types.classify_time`` / ``classify_complex``
# are ``startswith``-based), so it is bounded on the way in rather than rejected —
# a rejection here is a ``ValidationError`` raised inside the preview handler,
# which is neither ``FactTableIntrospectionError`` nor ``HTTPException`` and so
# becomes a blanket 500 over one irrelevant column. Same rule, same shape and same
# ellipsis as ``worker/tasks/metrics/schema_drift._truncate_observed_type``.
NATIVE_TYPE_MAX_LEN = 255


def _bound_native_type(value: object) -> object:
    """Fit a warehouse type name into ``NATIVE_TYPE_MAX_LEN``, truncating the tail.

    Truncate from the tail because the head carries the outer constructor
    (``Nullable(``, ``Map(``, ``Tuple(``, ``Enum8(``, ``STRUCT<``) — the part every
    classifier matches on. Anything that is not an over-long ``str`` is returned
    untouched so pydantic still owns type errors, ``None`` and ``min_length``.
    """
    if isinstance(value, str) and len(value) > NATIVE_TYPE_MAX_LEN:
        return value[: NATIVE_TYPE_MAX_LEN - 1] + "…"
    return value


# ── Nested value objects ─────────────────────────────────────────────────────


class FactTableColumnSchema(BaseModel):
    """An introspected column with UI type plus optional native warehouse type."""

    name: str = Field(min_length=1, max_length=255)
    type: str = Field(min_length=1, max_length=255)
    native_type: str | None = Field(
        default=None,
        min_length=1,
        # Kept even though the before-validator makes it unreachable for strings:
        # it is the declared contract NATIVE_TYPE_MAX_LEN is pinned against, and
        # the two are asserted equal by the fact-table tests.
        max_length=NATIVE_TYPE_MAX_LEN,
        exclude_if=lambda value: value is None,
    )

    @field_validator("native_type", mode="before")
    @classmethod
    def _truncate_native_type(cls, value: object) -> object:
        return _bound_native_type(value)


class FactTableRowFilter(BaseModel):
    """A reusable named row filter: a label plus a boolean WHERE fragment.

    The ``sql`` fragment flows into warehouse SQL with no bound parameters, so it
    is validated at this boundary via the shared SQL-fragment validator (rejects
    comment markers, ``;`` separators, and DDL/DML/``UNION`` keywords).
    """

    name: str = Field(min_length=1, max_length=255)
    sql: str = Field(min_length=1, max_length=32768)

    @field_validator("sql")
    @classmethod
    def _check_sql(cls, value: str) -> str:
        return validate_sql_fragment(value)


def _reject_duplicate_filter_names(
    value: list[FactTableRowFilter] | None,
) -> list[FactTableRowFilter] | None:
    """Row-filter names are the key metrics reference them by, so they must be unique.

    A fact metric stores the NAME and the collector resolves it to the FIRST match
    (``worker/tasks/metrics/_fact_conditions._resolve_named_filter_fragment``
    returns on the first row whose ``name`` matches). Two filters sharing a name
    therefore make the metric form offer the same label twice while only one of the
    two fragments can ever run, and the save-time membership check cannot see it:
    ``metric_definition_service._verify_fact_metric`` builds a SET of names, into
    which two identical names collapse. Save time is the last point where the
    ambiguity is still visible to the person who created it.

    Raised as a ``ValueError`` so it surfaces as a 422 — a malformed payload, like
    every other rejection this module produces. Deliberately not the 409
    ``create_fact_table`` uses: that one reports a collision with an EXISTING row,
    this one is a collision inside a single payload.

    Iterating and raising on the first repeat rather than comparing
    ``len(set(...))`` (the idiom in ``schemas/variable.py``) is what lets the
    message name the offending filter.

    Stored rows that already hold duplicates are untouched: this is an input
    validator, first-match stays their behaviour, and a backfill or a DB
    constraint would block saving any legacy table until the user fixed it.
    """
    if value is None:
        return None
    seen: set[str] = set()
    for row_filter in value:
        if row_filter.name in seen:
            msg = f"Duplicate row filter name {row_filter.name!r}"
            raise ValueError(msg)
        seen.add(row_filter.name)
    return value


# ── Create ───────────────────────────────────────────────────────────────────


class FactTableCreate(BaseModel):
    """Create a fact table from a full read-only query plus its column metadata.

    ``sql`` must be a single read-only ``SELECT`` or top-level ``WITH ... SELECT``
    (validated via the shared SELECT-safety path that rejects stacked statements,
    comments, DDL/DML and ``UNION``). ``timestamp_column`` and
    ``identifier_columns`` are validated as bare identifiers since they reach
    warehouse SQL unparameterised.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=255)
    description: str = ""
    color: str = Field(default="#6366f1", pattern=r"^#[0-9a-fA-F]{6}$")
    data_source_id: uuid.UUID | None = None
    sql: str = Field(min_length=1)
    timestamp_column: str = Field(min_length=1, max_length=255)
    columns: list[FactTableColumnSchema] = Field(default_factory=list)
    identifier_columns: list[str] = Field(default_factory=list)
    row_filters: list[FactTableRowFilter] = Field(default_factory=list, max_length=100)

    @field_validator("sql")
    @classmethod
    def _check_sql(cls, value: str) -> str:
        return validate_select_sql_safety(value)

    @field_validator("timestamp_column")
    @classmethod
    def _check_timestamp_column(cls, value: str) -> str:
        return validate_identifier(value)

    @field_validator("identifier_columns")
    @classmethod
    def _check_identifier_columns(cls, value: list[str]) -> list[str]:
        return _validate_identifier_list(value)

    @field_validator("row_filters")
    @classmethod
    def _check_row_filter_names(cls, value: list[FactTableRowFilter]) -> list[FactTableRowFilter]:
        _reject_duplicate_filter_names(value)
        return value

    def to_create_values(self) -> dict[str, object]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "color": self.color,
            "data_source_id": self.data_source_id,
            "sql": self.sql,
            "timestamp_column": self.timestamp_column,
            "columns": [column.model_dump() for column in self.columns],
            "identifier_columns": list(self.identifier_columns),
            "row_filters": [row_filter.model_dump() for row_filter in self.row_filters],
        }


# ── Update ───────────────────────────────────────────────────────────────────


# The update fields whose FactTable column is NOT NULL, so an explicit ``null``
# from a client is a 422 and not a DB-level 500 — see
# ``schemas/not_null_update``. ``data_source_id`` is deliberately absent: it is
# nullable, and a null on it unbinds the data source.
_FACT_TABLE_NOT_NULL_UPDATE_FIELDS = frozenset(
    {
        "display_name",
        "description",
        "color",
        "order",
        "sql",
        "timestamp_column",
        "columns",
        "identifier_columns",
        "row_filters",
    }
)


class FactTableUpdate(BaseModel):
    """Partial update of a fact table.

    ``name`` is immutable (it is the per-project identity) — recreate the fact
    table to rename it. Every other field is optional; ``exclude_unset`` at the
    service layer keeps only the fields the client actually sent.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    order: int | None = Field(default=None, ge=0, le=INT32_MAX)
    data_source_id: uuid.UUID | None = None
    sql: str | None = Field(default=None, min_length=1)
    timestamp_column: str | None = Field(default=None, min_length=1, max_length=255)
    columns: list[FactTableColumnSchema] | None = None
    identifier_columns: list[str] | None = None
    row_filters: list[FactTableRowFilter] | None = Field(default=None, max_length=100)

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_nulls(cls, data: object) -> object:
        # ``order`` used to be the only field guarded here, and its comment named
        # exactly the 500 the other NOT NULL fields still reached: an explicit
        # ``"sql": null`` or ``"row_filters": null`` set the column to NULL and
        # the commit raised. ``data_source_id`` stays out of the
        # set — a null there is how the form UNBINDS the warehouse.
        return reject_explicit_nulls(data, _FACT_TABLE_NOT_NULL_UPDATE_FIELDS)

    @field_validator("sql")
    @classmethod
    def _check_sql(cls, value: str | None) -> str | None:
        return value if value is None else validate_select_sql_safety(value)

    @field_validator("timestamp_column")
    @classmethod
    def _check_timestamp_column(cls, value: str | None) -> str | None:
        return _validate_optional_identifier(value)

    @field_validator("identifier_columns")
    @classmethod
    def _check_identifier_columns(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _validate_identifier_list(value)

    @field_validator("row_filters")
    @classmethod
    def _check_row_filter_names(
        cls, value: list[FactTableRowFilter] | None
    ) -> list[FactTableRowFilter] | None:
        # ``None`` means the client did not send ``row_filters`` at all, which
        # ``exclude_unset`` drops at the service layer — leave it alone.
        return _reject_duplicate_filter_names(value)


# ── Read models ──────────────────────────────────────────────────────────────


class FactTableResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    display_name: str
    description: str
    color: str
    order: int
    data_source_id: uuid.UUID | None
    sql: str
    timestamp_column: str
    columns: list[FactTableColumnSchema]
    identifier_columns: list[str]
    row_filters: list[FactTableRowFilter]
    created_at: datetime
    updated_at: datetime


class FactTableListItem(BaseModel):
    """Slim catalog row: presentation + source binding, without the heavy SQL."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    display_name: str
    description: str
    color: str
    order: int
    data_source_id: uuid.UUID | None
    timestamp_column: str
    created_at: datetime
    updated_at: datetime
    # Catalog rollups, so the list can fill its "Used by" and "Columns"
    # cells without fetching every fact table's detail. Defaulted: they are
    # merged in after ``model_validate`` reads the ORM row.
    metric_count: int = 0
    column_count: int = 0
    identifier_count: int = 0


class FactTableListResponse(BaseModel):
    items: list[FactTableListItem]
    total: int


# ── Preview / introspect ─────────────────────────────────────────────────────


class FactTablePreviewRequest(BaseModel):
    """Introspect a candidate read-only SELECT/CTE before saving a fact table."""

    model_config = ConfigDict(extra="forbid")

    data_source_id: uuid.UUID | None = None
    sql: str = Field(min_length=1)
    timestamp_column: str | None = None

    @field_validator("sql")
    @classmethod
    def _check_sql(cls, value: str) -> str:
        return validate_select_sql_safety(value)

    @field_validator("timestamp_column")
    @classmethod
    def _check_timestamp_column(cls, value: str | None) -> str | None:
        return _validate_optional_identifier(value)


class FactTablePreviewResponse(BaseModel):
    columns: list[FactTableColumnSchema]
    identifier_candidates: list[str]
