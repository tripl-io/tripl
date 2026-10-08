from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from tripl.models.data_source import DBType, TestStatus
from tripl.schemas.connection_settings_base import _ConnectionSettingsBase
from tripl.schemas.not_null_update import reject_explicit_nulls
from tripl.schemas.trino_settings import (
    AthenaSettings,
    TrinoHttpScheme,
    TrinoSettings,
)

# ClickHouse JSON path *discovery* (preview) mode. "dynamic" enumerates only the
# important typed subcolumn paths (JSONDynamicPaths, fast); "all" enumerates every
# path (JSONAllPaths). NULL means "use the connection default" (dynamic).
JsonPathDiscovery = Literal["all", "dynamic"]

# Bare hostname / IPv4 / bracketed IPv6 characters. Format-only guard: we do NOT
# block private/loopback addresses here on purpose — data sources legitimately
# point at private DBs (RFC1918 / VPC) and localhost. This only rejects values
# that are clearly not a host (a full URL, a path, or embedded whitespace).
_HOST_FORMAT_RE = re.compile(r"^[A-Za-z0-9._:\-\[\]]+$")


def _validate_host_format(value: str | None) -> str | None:
    if value is None:
        return value
    trimmed = value.strip()
    if "://" in trimmed or "/" in trimmed or any(ch.isspace() for ch in trimmed):
        raise ValueError(
            "host must be a bare hostname or IP address (no scheme, path, or whitespace)"
        )
    if not _HOST_FORMAT_RE.match(trimmed):
        raise ValueError("host contains invalid characters")
    return trimmed


# ---------------------------------------------------------------------------
# Typed, per-warehouse connection settings
# ---------------------------------------------------------------------------
#
# These used to ride in an untyped ``extra_params`` dict: unvalidated, invisible
# in the UI, and silently ignored by the adapter factory for everything except
# BigQuery's ``location``. They are now a strict Pydantic model per warehouse.
#
# Storage: still the existing ``data_sources.extra_params`` JSON column (no
# migration). What changes is that its contents are validated against the model
# for the row's ``db_type`` on every write, so a setting that does not apply to
# the warehouse is REJECTED (422) instead of being silently persisted and
# dropped. ``extra="forbid"`` on every model turns an unknown key into an error
# too.
#
# Secrets: ``PostgresSettings.sslkey`` is a private key. It is a ``SecretStr``
# (so it can never be echoed by a ``model_dump``/audit payload), is stored
# Fernet-encrypted under the ``sslkey_encrypted`` storage key exactly like
# ``password_encrypted``, and is never part of a response model — the response
# only carries ``sslkey_set: bool``.

# psql's sslmode ladder. Unset (None) is resolved host-aware by the adapter:
# "require" for remote hosts, "prefer" for localhost (see adapters/postgres.py
# _resolve_sslmode), while explicit modes are passed through as chosen.
PostgresSslMode = Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]

# Storage key for the Fernet-encrypted PostgreSQL client private key. Never the
# plaintext ``sslkey``: the ciphertext is what lands in the JSON column.
SSLKEY_STORAGE_KEY = "sslkey_encrypted"

# Server-side defaults. They deliberately differ per db_type — a BigQuery source
# needs a cost ceiling, a PostgreSQL source needs a TLS posture, and neither is
# meaningful for the other.
DEFAULT_TIMEOUT_SECONDS = 300
# ~100 GiB. A single scan/metric query that would bill more than this is refused
# by BigQuery before it runs, so a stray cross join cannot quietly burn a budget.
DEFAULT_BIGQUERY_MAXIMUM_BYTES_BILLED = 100 * 1024**3

# How many datasets ONE BigQuery schema browse may span. Each dataset costs its own
# billed catalog job (``INFORMATION_SCHEMA.COLUMNS`` is dataset-qualified), so the
# browse is bounded; ``core.adapters.bigquery._schema_datasets`` enforces it by
# truncating. The number is declared here, next to the write path that accepts the
# allowlist, because the two were separate literals (50 here, 20 there) and a save
# path that accepts a list the read path then silently drops is a defect
# once filed: the operator configured 50 datasets, 30 never appeared, and
# nothing said so.
MAX_SCHEMA_DATASETS = 20

# How many extra schemas one Databricks schema browse may span. A browse is a single
# ``information_schema.columns`` statement whatever the count, so this bounds the
# IN list and the autocomplete payload rather than a number of billed jobs.
MAX_DATABRICKS_SCHEMA_ALLOWLIST = 50

# Databricks authentication: a personal access token, or a service principal's
# OAuth client ID (``username``) and secret (``password``) exchanged for a token.
DatabricksAuthType = Literal["pat", "oauth_m2m"]

# Snowflake authentication: the user's password, or key-pair sign-in with the
# user's PEM private key in the ``password`` slot.
SnowflakeAuthType = Literal["password", "key_pair"]

# How many extra schemas one Snowflake schema browse may span: one
# ``INFORMATION_SCHEMA.COLUMNS`` statement whatever the count, like Databricks.
MAX_SNOWFLAKE_SCHEMA_ALLOWLIST = 50

_BQ_LOCATION_RE = re.compile(r"^[A-Za-z0-9-]{2,40}$")
# A SQL warehouse's HTTP path (``/sql/1.0/warehouses/<id>``), or a cluster's
# (``/sql/protocolv1/o/<org>/<cluster>``). Path characters only: no scheme, host,
# whitespace or quote can ride in it.
_DBX_HTTP_PATH_RE = re.compile(r"^/[A-Za-z0-9_\-./?=&]{1,499}$")
# A Unity Catalog schema name as the browse and the session default accept it.
_DBX_SCHEMA_RE = re.compile(r"^[A-Za-z0-9_-]{1,255}$")
_BQ_DATASET_RE = re.compile(r"^[A-Za-z0-9_]{1,1024}$")
# A Snowflake warehouse, role or schema name. Letters, digits, ``_``, ``$`` and
# ``-``: what an unquoted identifier allows, plus the hyphen an account's own
# object names sometimes carry (sent as data, never spliced into SQL unquoted).
_SF_OBJECT_RE = re.compile(r"^[A-Za-z0-9_$-]{1,255}$")
# A comma-separated list of plain SQL identifiers. Interpolated into
# ``SET search_path`` by the adapter, so anything that is not an identifier list
# (quotes, semicolons, whitespace tricks) must not get through.
_SEARCH_PATH_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(\s*,\s*[A-Za-z_][A-Za-z0-9_$]*)*$")
# One slot short of the browse budget, because the connection's own default dataset
# always takes the first one (``bigquery._schema_datasets`` puts it there and never
# lets the allowlist squeeze it out). Flat rather than conditional on "is a default
# dataset configured": this model is validated standalone by
# ``parse_connection_settings(db_type, raw)``, which never sees the DataSource row
# and therefore cannot know whether ``database_name`` is set. The cost of the flat
# rule is one wasted slot for a source with no default dataset; the benefit is a
# bound that is one sentence long and true wherever it is quoted.
_MAX_DATASET_ALLOWLIST = MAX_SCHEMA_DATASETS - 1


def _require_pem(value: str | None, *, label: str) -> str | None:
    """Certificates/keys are passed as PEM *content*, never as a server path.

    A filesystem path would be both useless (the worker container does not have
    the operator's files) and a directory-traversal foot-gun, so reject anything
    that is not a PEM block outright.
    """
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    if not trimmed.startswith("-----BEGIN"):
        raise ValueError(
            f"{label} must be the PEM content itself (starting with '-----BEGIN'), not a file path"
        )
    return trimmed


class ClickHouseSettings(_ConnectionSettingsBase):
    """ClickHouse has no extra connection settings.

    Its two knobs — ``timeout_seconds`` and ``json_path_discovery`` — are
    first-class columns on the data source, not connection settings.
    """


class SyntheticSettings(_ConnectionSettingsBase):
    """The synthetic (demo) warehouse is in-memory: it has nothing to configure."""


class PostgresSettings(_ConnectionSettingsBase):
    """Typed allowlist of safe PostgreSQL connection options."""

    sslmode: PostgresSslMode | None = None
    sslrootcert: str | None = Field(default=None, max_length=64_000)
    sslcert: str | None = Field(default=None, max_length=64_000)
    # Write-only: a private key. Never returned by the API (see
    # ``ConnectionSettingsResponse.sslkey_set``) and never logged (SecretStr).
    sslkey: SecretStr | None = Field(default=None, max_length=64_000)
    search_path: str | None = Field(default=None, max_length=255)

    @field_validator("sslrootcert")
    @classmethod
    def _check_root_cert(cls, value: str | None) -> str | None:
        return _require_pem(value, label="sslrootcert")

    @field_validator("sslcert")
    @classmethod
    def _check_client_cert(cls, value: str | None) -> str | None:
        return _require_pem(value, label="sslcert")

    @field_validator("sslkey")
    @classmethod
    def _check_client_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        secret = value.get_secret_value()
        # An empty key means "clear the stored key" — see the service.
        if not secret.strip():
            return SecretStr("")
        _require_pem(secret, label="sslkey")
        return value

    @field_validator("search_path")
    @classmethod
    def _check_search_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            return None
        if not _SEARCH_PATH_RE.match(trimmed):
            raise ValueError(
                "search_path must be a comma-separated list of plain schema identifiers"
            )
        return trimmed


class BigQuerySettings(_ConnectionSettingsBase):
    """Typed BigQuery execution controls (location, cost guard, dataset scope)."""

    location: str | None = Field(default=None, max_length=40)
    # Cost guard: BigQuery refuses a job whose dry-run estimate exceeds this.
    maximum_bytes_billed: int | None = Field(default=None, ge=1)
    # Datasets the schema browser may enumerate. Empty/unset = the default
    # dataset only (today's behavior).
    dataset_allowlist: list[str] | None = None

    @field_validator("location")
    @classmethod
    def _check_location(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            return None
        if not _BQ_LOCATION_RE.match(trimmed):
            raise ValueError(
                "location must be a BigQuery region or multi-region, e.g. EU, us-east1"
            )
        return trimmed

    @field_validator("dataset_allowlist")
    @classmethod
    def _check_datasets(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned: list[str] = []
        for raw in value:
            dataset = raw.strip()
            if not dataset:
                continue
            if not _BQ_DATASET_RE.match(dataset):
                raise ValueError(
                    f"dataset_allowlist entry {dataset!r} is not a valid BigQuery dataset id"
                )
            if dataset not in cleaned:
                cleaned.append(dataset)
        if len(cleaned) > _MAX_DATASET_ALLOWLIST:
            # The reason travels with the limit: the previous message named a number
            # (50) that the schema browser would never honour, so an operator who
            # trimmed to exactly 50 still lost 30 datasets with no further word.
            raise ValueError(
                f"dataset_allowlist accepts at most {_MAX_DATASET_ALLOWLIST} datasets — "
                f"a schema browse covers {MAX_SCHEMA_DATASETS} and the connection's "
                "default dataset takes one of them"
            )
        return cleaned or None


class DatabricksSettings(_ConnectionSettingsBase):
    """Databricks SQL warehouse connection: where the warehouse is, and what to browse.

    ``host`` is the workspace hostname and ``database_name`` the default catalog;
    these are the settings that have no column of their own.
    """

    # Required: without it there is no warehouse to send a statement to.
    http_path: str = Field(min_length=1, max_length=500)
    # Unset reads as "pat".
    auth_type: DatabricksAuthType | None = None
    # The default schema unqualified names resolve in. Unset = "default".
    schema_name: str | None = Field(default=None, max_length=255)
    # Further schemas of the same catalog the schema browser lists.
    schema_allowlist: list[str] | None = None

    @field_validator("http_path")
    @classmethod
    def _check_http_path(cls, value: str) -> str:
        trimmed = value.strip()
        if not _DBX_HTTP_PATH_RE.match(trimmed):
            raise ValueError(
                "http_path must be the warehouse's HTTP path, e.g. /sql/1.0/warehouses/1234abcd"
            )
        return trimmed

    @field_validator("schema_name")
    @classmethod
    def _check_schema_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            return None
        if not _DBX_SCHEMA_RE.match(trimmed):
            raise ValueError(f"schema_name {trimmed!r} is not a valid schema name")
        return trimmed

    @field_validator("schema_allowlist")
    @classmethod
    def _check_schemas(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned: list[str] = []
        for raw in value:
            schema = raw.strip()
            if not schema:
                continue
            if not _DBX_SCHEMA_RE.match(schema):
                raise ValueError(f"schema_allowlist entry {schema!r} is not a valid schema name")
            if schema not in cleaned:
                cleaned.append(schema)
        if len(cleaned) > MAX_DATABRICKS_SCHEMA_ALLOWLIST:
            raise ValueError(
                f"schema_allowlist accepts at most {MAX_DATABRICKS_SCHEMA_ALLOWLIST} schemas"
            )
        return cleaned or None


def _snowflake_object(value: str | None, *, label: str) -> str | None:
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    if not _SF_OBJECT_RE.match(trimmed):
        raise ValueError(f"{label} {trimmed!r} is not a valid Snowflake object name")
    return trimmed


class SnowflakeSettings(_ConnectionSettingsBase):
    """Snowflake connection: the virtual warehouse to run on, and what to browse.

    ``host`` is the account identifier, ``database_name`` the database and
    ``username`` the user; these are the settings that have no column of their own.
    """

    # Required: without a warehouse no statement can run.
    warehouse: str = Field(min_length=1, max_length=255)
    # Unset reads as "password".
    auth_type: SnowflakeAuthType | None = None
    # Unset uses the user's default role.
    role: str | None = Field(default=None, max_length=255)
    # The default schema unqualified names resolve in. Unset = PUBLIC.
    schema_name: str | None = Field(default=None, max_length=255)
    # Further schemas of the same database the schema browser lists.
    schema_allowlist: list[str] | None = None

    @field_validator("warehouse")
    @classmethod
    def _check_warehouse(cls, value: str) -> str:
        checked = _snowflake_object(value, label="warehouse")
        if checked is None:
            raise ValueError("warehouse is required")
        return checked

    @field_validator("role")
    @classmethod
    def _check_role(cls, value: str | None) -> str | None:
        return _snowflake_object(value, label="role")

    @field_validator("schema_name")
    @classmethod
    def _check_schema_name(cls, value: str | None) -> str | None:
        return _snowflake_object(value, label="schema_name")

    @field_validator("schema_allowlist")
    @classmethod
    def _check_schemas(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned: list[str] = []
        for raw in value:
            schema = _snowflake_object(raw, label="schema_allowlist entry")
            if schema is not None and schema not in cleaned:
                cleaned.append(schema)
        if len(cleaned) > MAX_SNOWFLAKE_SCHEMA_ALLOWLIST:
            raise ValueError(
                f"schema_allowlist accepts at most {MAX_SNOWFLAKE_SCHEMA_ALLOWLIST} schemas"
            )
        return cleaned or None


# The write-side union. Every member forbids extras, so a key that belongs to no
# warehouse at all is rejected by FastAPI before the service is reached; the
# service then checks the parsed settings against the row's db_type (a BigQuery
# key on a PostgreSQL source is a 422, not a silent drop).
ConnectionSettings = (
    ClickHouseSettings
    | PostgresSettings
    | BigQuerySettings
    | DatabricksSettings
    | SnowflakeSettings
    | TrinoSettings
    | AthenaSettings
    | SyntheticSettings
)

CONNECTION_SETTINGS_MODELS: dict[str, type[_ConnectionSettingsBase]] = {
    DBType.clickhouse.value: ClickHouseSettings,
    DBType.postgres.value: PostgresSettings,
    # Same libpq connection (TLS material, search_path) as PostgreSQL.
    DBType.greenplum.value: PostgresSettings,
    DBType.redshift.value: PostgresSettings,
    DBType.bigquery.value: BigQuerySettings,
    DBType.databricks.value: DatabricksSettings,
    DBType.snowflake.value: SnowflakeSettings,
    DBType.trino.value: TrinoSettings,
    DBType.athena.value: AthenaSettings,
    DBType.synthetic.value: SyntheticSettings,
}

# Every setting name known to *some* warehouse. Used to tell "you sent a setting
# that does not apply to this warehouse" apart from "you sent a typo".
_ALL_SETTING_NAMES: frozenset[str] = frozenset(
    name for model in CONNECTION_SETTINGS_MODELS.values() for name in model.model_fields
)


class ConnectionSettingsError(ValueError):
    """Raised when connection settings are unknown, inapplicable, or malformed."""


def settings_model_for(db_type: str) -> type[_ConnectionSettingsBase]:
    model = CONNECTION_SETTINGS_MODELS.get(str(db_type))
    if model is None:
        raise ConnectionSettingsError(f"Unsupported db_type: {db_type}")
    return model


def parse_connection_settings(
    db_type: str, raw: Mapping[str, object] | None
) -> _ConnectionSettingsBase | None:
    """Validate ``raw`` against the settings model for ``db_type``.

    Raises :class:`ConnectionSettingsError` with an operator-readable message —
    settings that do not apply to this warehouse are rejected, never ignored.
    """
    if raw is None:
        return None
    model = settings_model_for(db_type)
    try:
        return model.model_validate(dict(raw))
    except ValidationError as exc:
        raise ConnectionSettingsError(_explain(db_type, model, exc)) from exc


def _explain(db_type: str, model: type[_ConnectionSettingsBase], exc: ValidationError) -> str:
    """Turn a pydantic ValidationError into one clear sentence for the operator."""
    inapplicable: list[str] = []
    unknown: list[str] = []
    other: list[str] = []
    for error in exc.errors():
        field = ".".join(str(part) for part in error["loc"]) or "connection_settings"
        if error["type"] == "extra_forbidden":
            (inapplicable if field in _ALL_SETTING_NAMES else unknown).append(field)
        else:
            other.append(f"{field}: {error['msg']}")

    parts: list[str] = []
    if inapplicable:
        applicable = ", ".join(model.model_fields) or "none"
        parts.append(
            f"{', '.join(sorted(inapplicable))} "
            f"{'is' if len(inapplicable) == 1 else 'are'} not a connection setting for a "
            f"{db_type} data source (applicable settings: {applicable})"
        )
    if unknown:
        parts.append(f"unknown connection settings: {', '.join(sorted(unknown))}")
    parts.extend(other)
    return "Invalid connection settings — " + "; ".join(parts)


class ConnectionSettingsResponse(BaseModel):
    """Read side of the connection settings: the union, flattened, secrets removed.

    Applicability is enforced on write, so the response can be one flat object;
    only the fields that apply to the source's ``db_type`` are ever populated.
    """

    # BigQuery
    location: str | None = None
    maximum_bytes_billed: int | None = None
    dataset_allowlist: list[str] | None = None
    # PostgreSQL
    sslmode: PostgresSslMode | None = None
    sslrootcert: str | None = None
    sslcert: str | None = None
    search_path: str | None = None
    # Databricks (``auth_type``, ``schema_name`` and ``schema_allowlist`` are
    # Snowflake's too; ``schema_name`` and ``schema_allowlist`` Trino's, and
    # ``schema_allowlist`` Athena's)
    http_path: str | None = None
    auth_type: DatabricksAuthType | SnowflakeAuthType | None = None
    schema_name: str | None = None
    schema_allowlist: list[str] | None = None
    # Snowflake
    warehouse: str | None = None
    role: str | None = None
    # Trino
    http_scheme: TrinoHttpScheme | None = None
    # Athena
    work_group: str | None = None
    s3_output_location: str | None = None
    catalog_name: str | None = None
    # The private key itself is never returned — only whether one is stored.
    sslkey_set: bool = False


_RESPONSE_FIELDS = frozenset(ConnectionSettingsResponse.model_fields) - {"sslkey_set"}


def connection_settings_response(stored: Mapping[str, object] | None) -> ConnectionSettingsResponse:
    """Build the public view of a stored settings blob.

    Lenient by design: a GET must never 500 on a legacy or hand-edited row, so
    unrecognized storage keys are dropped here (writes are what validate).
    """
    if not stored:
        return ConnectionSettingsResponse()
    known = {key: value for key, value in stored.items() if key in _RESPONSE_FIELDS}
    return ConnectionSettingsResponse.model_validate(
        {**known, "sslkey_set": bool(stored.get(SSLKEY_STORAGE_KEY))}
    )


class DataSourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    db_type: DBType
    host: str = Field(min_length=1, max_length=500)
    port: int = Field(default=8123, ge=1, le=65535)
    database_name: str = Field(min_length=1, max_length=255)
    # Bounded like its neighbours, to the width of ``data_sources.username``
    # (String(255)). It was the one connection field left unbounded, so an
    # overlong username was a 500 out of the INSERT instead of a 422 naming the
    # field. ``password`` needs no bound: it is stored
    # encrypted, in a Text column.
    username: str = Field("", max_length=255)
    password: str = ""
    timeout_seconds: int | None = Field(None, ge=1)
    json_path_discovery: JsonPathDiscovery | None = None
    connection_settings: ConnectionSettings | None = None

    @field_validator("host")
    @classmethod
    def _check_host(cls, value: str) -> str:
        validated = _validate_host_format(value)
        assert validated is not None  # host is required on create
        return validated


# The update fields whose DataSource column is NOT NULL, so an explicit ``null``
# is a 422 naming the field and not a DB-level 500 out of
# ``update_data_source``'s generic ``setattr`` loop — see
# ``schemas/not_null_update``. ``password`` and
# ``connection_settings`` are handled before that loop and each reads a null as
# "leave the stored secret / clear the blob"; ``timeout_seconds`` and
# ``json_path_discovery`` are nullable columns where a null means "the
# connection default".
_DATA_SOURCE_NOT_NULL_UPDATE_FIELDS = frozenset(
    {"name", "db_type", "host", "port", "database_name", "username"}
)


class DataSourceUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    db_type: DBType | None = None
    host: str | None = Field(None, min_length=1, max_length=500)
    port: int | None = Field(None, ge=1, le=65535)
    database_name: str | None = Field(None, min_length=1, max_length=255)
    # Same bound as on create: the PATCH writes the same column.
    username: str | None = Field(None, max_length=255)
    password: str | None = None
    timeout_seconds: int | None = Field(None, ge=1)
    json_path_discovery: JsonPathDiscovery | None = None
    # Replaces the stored settings wholesale (a field left out is cleared), with
    # one exception: an omitted ``sslkey`` keeps the stored key, exactly like an
    # omitted password. Send ``"sslkey": ""`` to clear it.
    connection_settings: ConnectionSettings | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_nulls(cls, data: object) -> object:
        return reject_explicit_nulls(data, _DATA_SOURCE_NOT_NULL_UPDATE_FIELDS)

    @field_validator("host")
    @classmethod
    def _check_host(cls, value: str | None) -> str | None:
        return _validate_host_format(value)


# "Used by" links listed per source; past this the card says "and N more".
DATA_SOURCE_SCAN_REFS_LIMIT = 20


class DataSourceScanRef(BaseModel):
    """One scan reading a data source, enough to link to it."""

    id: uuid.UUID
    name: str
    project_slug: str
    project_name: str


class DataSourceResponse(BaseModel):
    id: uuid.UUID
    # Ownership scope: None = workspace-global source (shared across projects); a
    # non-None owner scopes it to one project (e.g. a demo's synthetic warehouse).
    # Exposed so project surfaces can filter out sources owned by other projects.
    # A source bound to a project the caller is not a member of is never
    # returned: it is left out of the list and 404s on direct read.
    project_id: uuid.UUID | None = None
    name: str
    db_type: DBType
    # True when this is a local, in-memory synthetic warehouse (demo data). The
    # UI uses it to badge the source as synthetic rather than a real connection.
    is_synthetic: bool
    host: str
    port: int
    database_name: str
    username: str
    password_set: bool
    timeout_seconds: int | None = None
    json_path_discovery: JsonPathDiscovery | None = None
    connection_settings: ConnectionSettingsResponse
    last_test_at: datetime | None
    last_test_status: TestStatus | None
    last_test_message: str | None
    created_at: datetime
    updated_at: datetime

    # What depends on this source: the scans reading it and the runs
    # they logged — both deleted with the source. Counted over the projects the
    # caller is a member of (every project for an instance owner), so a shared
    # source never reveals a project the caller cannot see. Merged in per
    # request, never served from the list cache, so a scan created a moment ago
    # is counted.
    scan_count: int = 0
    scan_run_count: int = 0
    # The scans behind ``scan_count``, for the card's "Used by" links: ids and
    # names only, capped at ``DATA_SOURCE_SCAN_REFS_LIMIT`` (``scan_count``
    # keeps the total). Only scans in projects the caller is a member of; a
    # project-bound API key never reaches this route.
    scans: list[DataSourceScanRef] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class DataSourceTestResponse(BaseModel):
    success: bool
    message: str
    tested_at: datetime
    data_source: DataSourceResponse


class DataSourceConnectionTest(DataSourceCreate):
    """An unsaved data-source config to test a connection with.

    The create body, validated the same way (host format included), so a form
    can test exactly what Create would store. ``name`` is optional because the
    test does not store anything. Every secret the probe needs travels in this
    request; nothing is read from or written to a stored source.
    """

    name: str = Field("", max_length=255)


class DataSourceConnectionTestResponse(BaseModel):
    success: bool
    message: str
    tested_at: datetime


class DataSourceThroughputPoint(BaseModel):
    bucket: datetime
    count: int


class DataSourceStatsResponse(BaseModel):
    """Runtime activity for a data source, aggregated from EventMetric rollups."""

    events_tracked: int
    volume_window: int
    window_hours: int
    throughput: list[DataSourceThroughputPoint]
