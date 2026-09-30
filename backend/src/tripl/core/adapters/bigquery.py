from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC, date, datetime
from typing import cast, override

from google.cloud import bigquery
from google.oauth2 import service_account

from tripl.core.adapters.base import (
    AggregateSpec,
    BaseAdapter,
    ColumnInfo,
    FieldContractExpectation,
    FieldContractViolation,
    SchemaColumn,
    SchemaTable,
    clamp_field_contract_threshold,
    contract_bound_literal,
    is_breakdown_field_of,
)
from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.core.adapters.measure_validator import (
    build_aggregate_sql,
    coerce_aggregation,
    validate_measure_column,
)
from tripl.core.bucketing import EPOCH, format_utc_literal, to_utc
from tripl.core.intervals import IntervalUnit, get_interval
from tripl.core.warehouse_types import ComplexKind, TimeKind, classify_complex, classify_time
from tripl.json_paths import split_property_field
from tripl.models.domain_enums import MetricAggregation
from tripl.schemas.data_source import MAX_SCHEMA_DATASETS

logger = logging.getLogger(__name__)

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]*$")
_IDENTIFIER_PART_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
# GCP project ids allow letters/digits/hyphens; dataset ids allow
# letters/digits/underscores. Validate the model-derived identifiers before
# interpolating them into the catalog query as defense-in-depth.
_BQ_PROJECT_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9-]*$")
_BQ_DATASET_RE = re.compile(r"^[a-zA-Z0-9_]+$")
# Sane upper bound so a pathologically long identifier can't reach logs/SQL.
# This does not tighten the character class above; currently-valid ids still pass.
_BQ_IDENTIFIER_MAX_LEN = 1024

# Hard cap on catalog rows pulled for SQL-editor autocomplete. Kept generous and
# in line with the ClickHouse/Postgres adapters so a dataset with thousands of
# wide tables can't blow up the response. This is the budget for the WHOLE browse,
# shared across every dataset it spans — not a per-dataset allowance.
_SCHEMA_ROW_LIMIT = 50000

# How many datasets one schema browse may span. ClickHouse and Postgres cover every
# non-system database/schema in a SINGLE catalog query; BigQuery's
# INFORMATION_SCHEMA.COLUMNS view is dataset-qualified, so covering N datasets costs
# N jobs. A UNION ALL across them would be one job but would make a single
# permission-denied dataset fail the whole browse, which is exactly the failure mode
# the contract forbids. So: one job per dataset, hard-capped, so an autocomplete
# keystroke can never fan out into an unbounded number of billed jobs.
#
# The number itself is declared in ``schemas.data_source`` and only aliased here: it
# is simultaneously the bound this module truncates to and the bound the
# ``dataset_allowlist`` write path validates against, and while it was two literals
# the write path accepted 50 datasets that this one silently dropped to 20. The
# dependency runs schemas -> adapters, the direction ``adapters.registry`` already
# imports ``DEFAULT_BIGQUERY_MAXIMUM_BYTES_BILLED`` in; the reverse is not available,
# because this module imports ``google.cloud.bigquery`` at module scope and the
# schema layer is imported by every API request.
_MAX_SCHEMA_DATASETS = MAX_SCHEMA_DATASETS

# Wall-clock cap on the catalog introspection job so a hung BQ job can't block
# the worker thread forever. Scoped to schema introspection: this is a CAP, not a
# default — a data source configuring a *shorter* timeout_seconds still wins.
_SCHEMA_QUERY_TIMEOUT_SECONDS = 30

# GoogleSQL keeps TIMESTAMP/DATETIME/DATE in separate type families, each with its
# own bucket + trunc functions. Applying the wrong family's function to a column is
# a hard query error, so the family is selected from the column's declared type.
# The same prefixes name the literal types (`TIMESTAMP '...'`, `DATETIME '...'`,
# `DATE '...'`), which is likewise a per-family choice: a TIMESTAMP literal cannot
# be compared against a DATETIME or DATE column.
_BUCKET_FN_PREFIX = {
    TimeKind.timestamp: "TIMESTAMP",
    TimeKind.datetime: "DATETIME",
    TimeKind.date: "DATE",
}

# Intervals finer than a day cannot be expressed against a DATE column.
_SUB_DAY_UNITS = (IntervalUnit.minute, IntervalUnit.hour)

# Literal renderings. TIMESTAMP carries the explicit `+00:00` offset that
# `bucketing.format_utc_literal` emits; DATETIME is a *zone-less* wall clock, so its
# literal must NOT carry an offset (BigQuery rejects one) and instead spells the UTC
# wall clock; DATE keeps the date part only.
_DATETIME_LITERAL_FMT = "%Y-%m-%d %H:%M:%S.%f"
_DATE_LITERAL_FMT = "%Y-%m-%d"

# How deep `JSON_KEYS` walks a JSON value when enumerating paths. ClickHouse's
# JSONAllPaths returns every nested leaf path, so a top-level-only enumeration would
# make the same document look different on each warehouse. There is no "unlimited"
# depth argument, so this is a bound: leaves below it are not discovered.
_JSON_PATH_MAX_DEPTH = 20

# BigQuery spells an array-valued field as mode=REPEATED rather than a distinct type.
_REPEATED_MODE = "REPEATED"


def _decode_grouped_array(value: object) -> object:
    """Turn a ``TO_JSON_STRING(...)`` grouped value back into the list it stands for.

    GoogleSQL flatly refuses to ``GROUP BY`` an ARRAY ("Grouping by expressions of type
    ARRAY is not allowed"), and refuses a constant array just as hard ("Cannot GROUP BY
    literal values"). Every array-valued grouped column — a JSON/STRUCT column's
    leaf-path array, and a REPEATED scalar column — is therefore grouped by its JSON
    *string* rendering, which is a scalar STRING and groups fine.

    That is a SQL-level trick, and it must not leak into the row contract. ``BaseAdapter``
    documents the json-paths column as an ARRAY of paths, and
    ``core.analyzers.cardinality._process_breakdown`` branches on
    ``isinstance(paths, (list, tuple))``: handed the raw ``'["a","b"]'`` string it would
    read the whole blob as ONE path and silently corrupt every cardinality count. So the
    string is decoded back to a list here, on the way out, before any caller sees it.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        # Already an array — a fake/mock client, or a column the warehouse handed back
        # natively. Normalize to a list and leave it alone.
        return list(value)
    if not isinstance(value, str):
        msg = (
            "BigQuery: expected a JSON string for an array-valued grouped column, "
            f"got {type(value).__name__}"
        )
        raise ValueError(msg)
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        msg = f"BigQuery: could not decode array-valued grouped column {value!r}: {exc}"
        raise ValueError(msg) from exc

    # Guard the DECODED type, not merely the input type. `json.loads` will happily
    # return a dict, a bare string or a number, and any of those flowing out of here
    # would put a non-list into the json-paths column — which
    # `cardinality._process_breakdown` tests with `isinstance(paths, (list, tuple))`
    # and, failing that, reads as ONE path. That is precisely the silent cardinality
    # corruption this function exists to prevent, so it fails loudly instead.
    if decoded is not None and not isinstance(decoded, list):
        msg = (
            "BigQuery: an array-valued grouped column decoded to "
            f"{type(decoded).__name__}, not a list: {value!r}"
        )
        raise ValueError(msg)
    return decoded


def _as_utc_bucket(value: object) -> object:
    """One ``_bucket`` cell, as an aware UTC ``datetime``.

    ``datetime`` is tested BEFORE ``date`` because it is a *subclass* of it. The other
    order matches every TIMESTAMP and DATETIME bucket too and rebuilds it from its
    date part alone, silently moving every non-midnight bucket to midnight — which is
    the one way to get this conversion wrong and still look plausible in a test that
    only checks ``tzinfo``.

    A value that is no kind of date is returned untouched rather than coerced. Reaching
    here with one means the row layout changed and column 0 stopped being the bucket;
    inventing a datetime for it would hide that, and the caller that compares it
    against the chunk window will say so far more clearly.

    This moves the STORED instant of a ``DATETIME`` / ``DATE`` bucket, and rows
    already written are not rewritten (tripl-0zpq.348). Before it, the naive
    value reached a ``timestamptz`` column and was read in the app database
    session's timezone; now it is the same wall clock stamped UTC. Where that
    session ran in UTC — the default of a PostgreSQL image initialised without a
    ``TZ``, and what ``db_config.postgres_connect_args`` now pins — the two are
    the same instant and nothing changes. Where it did not, historical rows for
    such a config sit ``offset`` away from new ones, the unique key sees two rows
    for one logical bucket at the edges of an overlapping re-collection, and the
    documented remedy is a full-history re-collection of that config
    (``website/docs/develop/warehouse-parity.md``, "BigQuery ``DATETIME`` is
    zone-less"). ``TIMESTAMP`` buckets were aware all along and do not move. The
    writers normalize again on their side (``core.bucketing.stored_bucket``), so
    the answer no longer depends on every adapter remembering to.
    """
    if isinstance(value, datetime):
        return to_utc(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return value


def _walk_struct_fields(
    fields: object,
    prefix: str,
    *,
    blocked: bool,
    out: dict[str, bool],
) -> None:
    """Recurse a RECORD's declared subfields, collecting dotted leaf paths.

    ``blocked`` propagates "this leaf sits underneath a repeated field", which makes
    plain dotted access illegal in GoogleSQL (``ARRAY<STRUCT<...>>`` has no fields —
    it needs UNNEST). Such leaves are still *enumerated* so they stay visible in
    discovery; they are flagged unaddressable and rejected loudly if selected.
    """
    for sub in cast("list[bigquery.SchemaField]", fields):
        name = str(sub.name)
        path = f"{prefix}{name}"
        is_repeated = str(sub.mode or "").upper() == _REPEATED_MODE
        is_struct = classify_complex(str(sub.field_type)) is ComplexKind.struct
        if is_struct and sub.fields:
            _walk_struct_fields(sub.fields, f"{path}.", blocked=blocked or is_repeated, out=out)
            continue
        # A repeated *leaf* is fine: `col`.`tags` is an ARRAY<STRING> value, and
        # TO_JSON_STRING renders it like the JSON array ClickHouse would return.
        out[path] = not blocked


def _declared_struct_paths(field: bigquery.SchemaField) -> dict[str, bool]:
    """The dotted leaf paths a STRUCT/RECORD column declares, path -> addressable.

    Unlike a JSON column, a STRUCT's paths come from the *schema*, not from the rows,
    so they are identical for every row and need no warehouse-side enumeration.
    """
    paths: dict[str, bool] = {}
    root_repeated = str(field.mode or "").upper() == _REPEATED_MODE
    _walk_struct_fields(field.fields, "", blocked=root_repeated, out=paths)
    return dict(sorted(paths.items()))


class BigQueryAdapter(BaseAdapter):
    """BigQuery-backed warehouse adapter.

    Auth: service-account JSON stored in DataSource.password_encrypted
    (decrypted upstream and passed in as `credentials_json`). The host field
    holds the GCP project_id; database_name holds the default dataset_id used
    when base_query references a bare table name.

    Semantics mirror the ClickHouse adapter:
      - toStartOfInterval → TIMESTAMP_BUCKET/DATETIME_BUCKET/DATE_BUCKET, and
                            *_TRUNC(..., WEEK(MONDAY)) for weeks
      - JSONAllPaths      → JSON_KEYS (nested leaf paths) for a JSON column, and the
                            declared field schema for a STRUCT/RECORD column
      - GROUPING SETS     → native syntax in BQ standard SQL
      - LIMIT n BY col    → ROW_NUMBER() OVER (PARTITION BY ...) wrapper

    Everything time-related is *type-directed*: the bucket function, the comparison
    literal and the very question of whether a column can be a time column at all are
    decided by the column's declared type (see ``_time_kind``), which the adapter
    introspects lazily on first use.
    """

    # Execution controls, as CLASS-level defaults on purpose. Every test module in the
    # suite — and the ZetaSQL conformance gate — builds this adapter with
    # ``object.__new__(BigQueryAdapter)`` to avoid constructing a live client from
    # service-account credentials. Those instances never run ``__init__``, so an
    # instance-only attribute read from a query path would blow up with AttributeError
    # in exactly the tests that exist to protect the query paths. Declaring the
    # defaults on the class keeps an un-initialized adapter behaving like an
    # unconfigured one (no timeout, no cost guard, default dataset only), which is the
    # pre-existing behavior.
    _timeout_seconds: float | None = None
    _maximum_bytes_billed: int | None = None
    _dataset_allowlist: tuple[str, ...] | None = None

    def __init__(
        self,
        host: str,
        port: int,  # unused for BQ
        database: str,
        username: str = "",  # unused for BQ
        password: str = "",  # service-account JSON
        *,
        location: str | None = None,
        timeout_seconds: int | None = None,
        maximum_bytes_billed: int | None = None,
        dataset_allowlist: list[str] | None = None,
        **kwargs: object,
    ) -> None:
        del port, username, kwargs  # not applicable to BigQuery / forward-compatible
        # These three are ``WarehouseCapabilityError`` (a ValueError subclass, so
        # every ``except ValueError`` still catches them) because the connection
        # probe surfaces that type VERBATIM and genericises everything else. As
        # bare ValueErrors they hit the sanitiser's substring hints — "host" and
        # "credentials" — and came out as "could not reach the data source" /
        # "authentication was rejected", sending the operator to look at a network
        # and a password when the real problem is an empty field. They are also
        # quoted verbatim in website/docs/use/troubleshooting.md, which was
        # therefore describing a message the UI never showed (tripl-rcn8).
        if not host:
            raise WarehouseCapabilityError("BigQuery: host (project_id) is required")
        if not password:
            raise WarehouseCapabilityError(
                "BigQuery: service-account JSON credentials are required"
            )
        try:
            info = cast(dict[str, object], json.loads(password))
        except json.JSONDecodeError as exc:
            # The decoder's text is safe to carry: it reports the syntax position
            # ("line 3 column 5 (char 42)"), never the document's contents, so no
            # part of the pasted key can ride out on it.
            msg = f"BigQuery: invalid service-account JSON: {exc}"
            raise WarehouseCapabilityError(msg) from exc
        creds = cast(
            service_account.Credentials,
            service_account.Credentials.from_service_account_info(info),  # type: ignore[no-untyped-call]
        )
        self._timeout_seconds = float(timeout_seconds) if timeout_seconds else None
        self._maximum_bytes_billed = (
            maximum_bytes_billed if maximum_bytes_billed and maximum_bytes_billed > 0 else None
        )
        self._dataset_allowlist = tuple(dataset_allowlist) if dataset_allowlist else None

        # Both guards ride on the client's DEFAULT job config rather than a per-call
        # ``job_config=`` argument, so every statement this adapter will ever issue —
        # including ones added later — inherits them without a call site having to
        # remember. It also keeps ``self._client.query(sql)`` single-argument, which the
        # fake clients in the test suite and the ZetaSQL gate rely on.
        #
        # ``maximum_bytes_billed`` is the cost guard: BigQuery REFUSES a query whose
        # estimate exceeds it, so a runaway scan is rejected before a byte is billed.
        # ``job_timeout_ms`` is the server-side half of the deadline: it makes BigQuery
        # itself abandon the job, so a worker that is SIGKILLed before it can call
        # ``job.cancel()`` still doesn't leave a query burning slots.
        job_config = bigquery.QueryJobConfig(
            default_dataset=f"{host}.{database}" if database else None,
        )
        if self._maximum_bytes_billed is not None:
            job_config.maximum_bytes_billed = self._maximum_bytes_billed
        if self._timeout_seconds is not None:
            job_config.job_timeout_ms = int(self._timeout_seconds * 1000)
        self._client = bigquery.Client(
            project=host,
            credentials=creds,
            location=location,
            default_query_job_config=job_config,
        )
        self._project = host
        self._dataset = database
        self._allowed_columns: set[str] = set()
        # Declared type per column, captured during get_columns. The bucket and
        # time-window SQL are type-directed (TIMESTAMP vs DATETIME vs DATE), so the
        # adapter has to remember what the warehouse actually said.
        self._column_types: dict[str, str] = {}
        # Declared leaf paths per STRUCT/RECORD column (path -> addressable). A
        # STRUCT's paths come from the schema, not the data, so they are captured
        # alongside the types instead of being enumerated by a query.
        self._struct_paths: dict[str, dict[str, bool]] = {}
        # Columns declared mode=REPEATED. BigQuery does not give an array its own
        # *type* — an ARRAY<STRING> column reports field_type STRING — so array-ness
        # is only visible in the mode, and it has to be remembered: a repeated column
        # is an ARRAY value, which GoogleSQL can neither GROUP BY nor CAST to STRING.
        self._repeated_columns: set[str] = set()

    def close(self) -> None:
        self._client.close()  # type: ignore[no-untyped-call]

    def _query_deadline(self, cap: float | None = None) -> float | None:
        """How long this adapter may wait for one job, in seconds (None = forever).

        ``cap`` is an additional per-call ceiling (schema introspection uses one), never
        a floor: a data source that configures a *shorter* ``timeout_seconds`` than the
        cap still gets the shorter deadline.
        """
        timeout = self._timeout_seconds
        if timeout is not None and timeout <= 0:
            timeout = None
        if timeout is None:
            return cap
        if cap is None:
            return timeout
        return min(timeout, cap)

    def _run_query(
        self, sql: str, *, timeout_cap: float | None = None
    ) -> bigquery.table.RowIterator:
        """Submit one statement and wait for it, bounded by the configured deadline.

        Every BigQuery statement this adapter issues goes through here. It used to be
        the ONLY warehouse adapter with no deadline at all: ClickHouse gets
        ``send_receive_timeout``, Postgres gets ``statement_timeout``, and BigQuery got
        a bare ``job.result()`` that waits forever — so a pathological ``base_query``
        pinned a Celery worker until the 55-minute hard limit killed it.

        On timeout the job is CANCELLED best-effort. A BigQuery job outlives the client
        that started it: giving up on the wait does nothing to the job, which keeps
        scanning (and billing) server-side. ``cancel()`` is the only thing that stops
        it, and it is best-effort by nature — the cancel RPC can itself fail, and the
        job may already have finished — so a failure to cancel is logged, never allowed
        to mask the timeout the caller actually needs to see.
        """
        job = self._client.query(sql)
        deadline = self._query_deadline(timeout_cap)
        try:
            if deadline is None:
                return job.result()
            return job.result(timeout=deadline)
        except TimeoutError as exc:
            # google-cloud-bigquery raises concurrent.futures.TimeoutError, which IS the
            # builtin TimeoutError on the Python this runs on.
            self._cancel(job)
            msg = (
                f"BigQuery: query exceeded the {deadline:g}s timeout configured for this "
                "data source and was cancelled. Narrow the time window, reduce the "
                "columns the base query selects, or raise the data source's timeout."
            )
            raise TimeoutError(msg) from exc

    def _cancel(self, job: object) -> None:
        """Best-effort cancel of a timed-out job. Never raises."""
        try:
            cancel = job.cancel  # type: ignore[attr-defined]
            cancel()
        except Exception:
            logger.warning("BQ: could not cancel timed-out job", exc_info=True)

    def test_connection(self) -> bool:
        row = next(iter(self._run_query("SELECT 1 AS ok")))
        return bool(row["ok"] == 1)

    def _validate_column(self, column: str) -> str:
        if not _IDENTIFIER_RE.match(column):
            msg = f"Invalid column name: {column}"
            raise ValueError(msg)
        if self._allowed_columns and column not in self._allowed_columns:
            msg = f"Column {column!r} not found in query result"
            raise ValueError(msg)
        return column

    def _bucket_expression(self, time_column: str, interval_code: str) -> str:
        """Translate an interval code into GoogleSQL bucket SQL.

        Must agree with ``tripl.core.bucketing.floor_to_bucket``.

        The bucket function is chosen by the column's *declared* type, not assumed:
        GoogleSQL keeps TIMESTAMP (an instant) and DATETIME (a zone-less wall
        clock) in separate type families and rejects a TIMESTAMP_* function applied
        to a DATETIME column. DATE has no time-of-day, so a sub-day interval on a
        DATE column is a configuration error rather than something to silently round.

        Weeks use ``*_TRUNC(..., WEEK(MONDAY))``: a 7-day ``*_BUCKET`` bins off the
        function's origin, and the contract says weeks start on Monday.

        The bucket origin is passed *explicitly* as the Unix epoch. GoogleSQL's
        ``*_BUCKET`` default origin is 1950-01-01, not the epoch; that happens to be a
        whole number of days away from the epoch (7305), so every sub-day interval
        would land on the same boundaries anyway — but the contract anchors at the
        epoch, so say so rather than relying on a coincidence.
        """
        spec = get_interval(interval_code)
        kind = self._time_kind(time_column)
        col = f"`{self._validate_column(time_column)}`"
        prefix = _BUCKET_FN_PREFIX[kind]

        if spec.unit is IntervalUnit.week:
            # TIMESTAMP_TRUNC takes a timezone; DATETIME/DATE are zone-less and do
            # not. Pin TIMESTAMP to UTC so a non-UTC project default cannot shift the
            # week boundary.
            zone = ", 'UTC'" if kind is TimeKind.timestamp else ""
            return f"{prefix}_TRUNC({col}, WEEK(MONDAY){zone})"
        if kind is TimeKind.date and spec.unit in _SUB_DAY_UNITS:
            # ``WarehouseCapabilityError``, not a bare ``ValueError``, for the same
            # reason ``__init__``'s three rejections are (see its comment, tripl-rcn8):
            # nothing configuration-time catches this combination, so the first thing
            # that runs it is a collection tick, and the worker's sanitiser replaces
            # an uncurated exception with "Scan failed due to an internal error." —
            # every tick, forever, for a config the operator can fix in one click if
            # only they are told which one. The message is tripl-authored and carries
            # no host, port or driver text, which is the whole contract of the type.
            msg = (
                f"BigQuery: time column {time_column!r} is a DATE, which has no "
                f"time-of-day, so it cannot be bucketed at {interval_code!r}. "
                "Use the 1d or 1w interval, or a TIMESTAMP/DATETIME column."
            )
            raise WarehouseCapabilityError(msg)
        origin = self._time_literal(kind, EPOCH)
        width = f"INTERVAL {spec.count} {spec.unit.value.upper()}"
        return f"{prefix}_BUCKET({col}, {width}, {origin})"

    def _time_literal(self, kind: TimeKind, value: datetime) -> str:
        """Render a UTC datetime as a literal comparable against a ``kind`` column.

        The literal *type* has to match the column's type family: GoogleSQL will not
        compare a ``TIMESTAMP`` literal against a ``DATETIME`` or ``DATE`` column, so
        emitting ``TIMESTAMP '...'`` everywhere is a hard query error on two of the
        three supported column types.

        Everything is normalized to UTC first (``to_utc``). ``strftime`` on an aware
        non-UTC datetime silently drops the offset and writes the wrong wall clock,
        which is exactly how a window ends up shifted by hours without failing.

        For a DATE column the window bound is floored to its UTC day. The window stays
        half-open (``>= from_day``, ``< to_day``), so adjacent windows still tile; on
        the 1d/1w intervals a DATE column supports, the bounds are day-aligned anyway
        and the flooring is exact.
        """
        moment = to_utc(value)
        if kind is TimeKind.timestamp:
            return f"TIMESTAMP '{format_utc_literal(moment)}'"
        if kind is TimeKind.datetime:
            return f"DATETIME '{moment.strftime(_DATETIME_LITERAL_FMT)}'"
        if kind is TimeKind.date:
            return f"DATE '{moment.strftime(_DATE_LITERAL_FMT)}'"
        msg = f"BigQuery: cannot build a time literal for {kind.value!r}"
        raise ValueError(msg)

    def _complex_kind(self, column: str) -> ComplexKind:
        """How a nested column must be addressed: a JSON document or a STRUCT.

        Falls back to JSON when the column's type was never introspected, preserving
        the behavior of callers that reach a JSON path without a preceding
        ``get_columns``.
        """
        type_name = self._column_types.get(column)
        if type_name is None:
            return ComplexKind.json
        kind = classify_complex(type_name)
        if kind is None:
            msg = (
                f"BigQuery: column {column!r} has scalar type {type_name} and holds no "
                "nested paths. Only JSON and STRUCT/RECORD columns can be path-expanded."
            )
            raise ValueError(msg)
        if kind is ComplexKind.map:
            msg = f"BigQuery: column {column!r} has unsupported nested type {type_name}."
            raise ValueError(msg)
        return kind

    def _struct_field_expression(self, column: str, parts: list[str]) -> str:
        """Address a leaf of a STRUCT/RECORD column with dotted field access.

        A STRUCT is *not* a JSON document: ``JSON_QUERY`` does not apply to it, and its
        legal paths are exactly the ones the schema declares. A path that is not
        declared — or one buried under a repeated field, which GoogleSQL can only reach
        through UNNEST — is rejected here rather than compiled into SQL that either
        fails opaquely in a worker or silently drops the field.
        """
        declared = self._struct_paths.get(column, {})
        path = ".".join(parts)
        addressable = declared.get(path)
        if addressable is None:
            known = ", ".join(declared) or "<none>"
            msg = (
                f"BigQuery: {path!r} is not a declared field of STRUCT column "
                f"{column!r}. Known paths: {known}"
            )
            raise ValueError(msg)
        if not addressable:
            msg = (
                f"BigQuery: STRUCT path {column}.{path} is nested inside a repeated "
                "(ARRAY) field. GoogleSQL cannot address it with dotted field access — "
                "it needs UNNEST, which this adapter does not generate. Select a path "
                "outside the repeated field."
            )
            raise ValueError(msg)
        fields = "".join(f".`{part}`" for part in parts)
        return f"`{column}`{fields}"

    def _json_path_expression(self, column: str, path: str) -> str:
        parts = [part for part in path.split(".") if part]
        if not parts:
            raise ValueError(f"Invalid JSON path: {path}")
        if any(not _IDENTIFIER_PART_RE.match(part) for part in parts):
            raise ValueError(f"Unsupported JSON path: {path}")
        col = self._validate_column(column)
        if self._complex_kind(col) is ComplexKind.struct:
            return self._struct_field_expression(col, parts)
        json_path = "$." + ".".join(parts)
        return f"JSON_QUERY(`{col}`, '{json_path}')"

    def _string_value_expression(self, column: str, *, role: str = "breakdown column") -> str:
        """The scalar STRING rendering of a column, used for breakdown and contract values.

        Matches ClickHouse's ``ifNull(toString(col), '')`` — the NULL-collapsing matters,
        because a grouped-event filter and an enum/regex check both compare against it.

        A REPEATED column is rejected outright. ``CAST(<array> AS STRING)`` is not a
        legal GoogleSQL cast, so a breakdown (or an enum/regex/range contract) on an
        array column would compile to SQL that only fails once a worker runs it. Fail
        loudly here instead, while the caller is still choosing the column. ``role`` only
        shapes the message, so the error names what the caller was actually doing.
        """
        col = self._validate_column(column)
        if col in self._repeated_columns:
            msg = (
                f"BigQuery: column {col!r} is REPEATED (an ARRAY) and cannot be used as a "
                f"{role} — GoogleSQL cannot cast an ARRAY to a single STRING value, nor "
                "group by one. Choose a scalar column."
            )
            raise ValueError(msg)
        return f"IFNULL(CAST(`{col}` AS STRING), '')"

    def _property_value_expression(self, column: str, path: str) -> str:
        """One property (a JSON path of a nested column) as a nullable STRING.

        ``JSON_VALUE`` on a JSON column: the scalar unquoted, and NULL for an
        absent path, a JSON null or a container — so a property behaves like a
        nullable scalar column in breakdowns and contracts (F23, #306). A STRUCT
        field goes through ``_struct_field_expression``, which only addresses
        declared, non-repeated paths, and is cast like a scalar column. The JSON
        path literal is built from segments the identifier grammar has already
        admitted, the same way ``_json_path_expression`` builds its own.
        """
        parts = [part for part in path.split(".") if part]
        if not parts or any(not _IDENTIFIER_PART_RE.match(part) for part in parts):
            raise ValueError(f"Unsupported JSON path: {path}")
        col = self._validate_column(column)
        if self._complex_kind(col) is ComplexKind.struct:
            return f"CAST({self._struct_field_expression(col, parts)} AS STRING)"
        json_path = "$." + ".".join(parts)
        return f"JSON_VALUE(`{col}`, {self._quote_string(json_path)})"

    def _field_value_expression(self, field: str, *, role: str = "breakdown column") -> str:
        """``_string_value_expression`` for a scalar column or a property entry."""
        prop = split_property_field(field)
        if prop is None:
            return self._string_value_expression(field, role=role)
        return f"IFNULL({self._property_value_expression(*prop)}, '')"

    def _validate_breakdown_field(self, field: str) -> str:
        """Validate a breakdown entry: a column, or a property of a known column."""
        prop = split_property_field(field)
        if prop is None:
            return self._validate_column(field)
        self._validate_column(prop[0])
        return field

    def _regular_column_sql(self, column: str) -> tuple[str, str]:
        """``(select_sql, group_sql)`` for a plain (non-nested) column.

        A REPEATED scalar column (``ARRAY<STRING> tags``) carries no distinct *type* —
        it reports field_type STRING — so it is classified as a plain scalar and used to
        be selected and grouped by directly. That is ``GROUP BY <array>``, which
        GoogleSQL rejects, so such a column is grouped by its ``TO_JSON_STRING``
        rendering and decoded back to a list on the way out.

        This is a real dialect divergence, not a workaround: ClickHouse *can* group by
        an ``Array(String)`` and returns the group key as a list. BigQuery cannot, so the
        array is round-tripped through its JSON text to get a groupable scalar. The
        observable result is deliberately the same on both warehouses — one group per
        distinct array value (order-sensitive on both), surfaced to callers as a
        ``list``.
        """
        col = self._validate_column(column)
        if col not in self._repeated_columns:
            return f"`{col}`", f"`{col}`"
        group_sql = f"TO_JSON_STRING(`{col}`)"
        return f"{group_sql} AS `{col}`", group_sql

    def _nested_source(
        self,
        base_query: str,
        where_clause: str,
        json_cols: list[str],
        json_value_paths: dict[str, list[str]],
    ) -> tuple[str, dict[str, str], list[str]]:
        """The FROM source, with every nested column pre-materialized under an alias.

        A JSON column's leaf-path expression is a *correlated subquery* over the column
        (``(SELECT ARRAY_AGG(...) FROM UNNEST(JSON_KEYS(col, ...)) ...)``). GoogleSQL will
        not accept that in a GROUP BY as covering the column it reads: ZetaSQL rejects the
        SELECT-list copy with "UNNEST expression references column <col> which is neither
        grouped nor aggregated" *even when the identical expression is spelled out in the
        GROUP BY*. Grouping by the expression is simply not the same as grouping by the
        column it correlates on.

        So the nested columns are computed ONCE in a prepared subquery and the outer query
        groups by the resulting alias, which is a plain scalar STRING and groups fine. This
        is the same shape ``get_time_bucketed_breakdown_counts_multi`` already uses for its
        GROUPING SETS, and it is why that path was the only nested one that survived.

        With no nested columns there is nothing to materialize and the source stays the
        bare ``(base_query) AS _src`` it has always been.

        Returns ``(from_sql, alias_by_output_name, json_value_names)``. The WHERE clause is
        placed by this method — callers must not re-append it.
        """
        prepared: list[str] = []
        alias_by_name: dict[str, str] = {}
        json_value_names: list[str] = []
        for index, c in enumerate(json_cols):
            alias = f"__np_{index}"
            prepared.append(f"{self._json_paths_expression(c)} AS `{alias}`")
            alias_by_name[c] = alias
        for c in json_cols:
            for path in json_value_paths.get(c, []):
                full_path = f"{c}.{path}"
                alias = f"__nv_{len(json_value_names)}"
                prepared.append(
                    f"TO_JSON_STRING({self._json_path_expression(c, path)}) AS `{alias}`"
                )
                alias_by_name[full_path] = alias
                json_value_names.append(full_path)

        if not prepared:
            return f"({base_query}) AS _src{where_clause}", alias_by_name, json_value_names

        # `SELECT *` keeps every original column visible to the outer query — the time
        # column it buckets, the measure it aggregates, the breakdown column it folds.
        inner = f"SELECT *, {', '.join(prepared)} FROM ({base_query}) AS _src{where_clause}"
        return f"({inner}) AS _prepared", alias_by_name, json_value_names

    def _nested_select_group(
        self,
        names: list[str],
        alias_by_name: dict[str, str],
    ) -> tuple[list[str], list[str]]:
        """Read pre-materialized nested columns without dotted result aliases.

        Public nested value names deliberately use dotted paths (for example
        ``doc.user.id``), but real BigQuery rejects dots in result-field names even
        when the alias is backtick-quoted.  Callers consume rows positionally and get
        the public names from ``json_value_names``, so an invalid dotted output name
        stays under its safe ``__nv_*`` alias inside GoogleSQL.
        """
        select_parts = []
        for name in names:
            source_alias = alias_by_name[name]
            output_alias = name if _IDENTIFIER_PART_RE.match(name) else source_alias
            select_parts.append(f"`{source_alias}` AS `{output_alias}`")
        group_parts = [f"`{alias_by_name[name]}`" for name in names]
        return select_parts, group_parts

    def _decode_rows(
        self,
        rows: list[tuple[object, ...]],
        *,
        offset: int,
        reg_cols: list[str],
        json_cols: list[str],
    ) -> list[tuple[object, ...]]:
        """Decode every array-valued grouped column in ``rows`` back into a list.

        ``offset`` is how many leading positional columns (``_bucket``,
        ``_breakdown_value`` …) precede the regular columns in the row layout; the
        regular columns then run for ``len(reg_cols)``, and the json/struct path columns
        immediately after them. Both groups were grouped as JSON strings (see
        ``_regular_column_sql`` / ``_json_paths_expression``) and must be handed back as
        lists so the documented row contract holds.

        Column 0 is never touched here even when ``offset`` is non-zero; the leading
        positional columns are handled by ``_utc_bucket_rows``.
        """
        array_indexes = {
            offset + index for index, c in enumerate(reg_cols) if c in self._repeated_columns
        }
        array_indexes |= {offset + len(reg_cols) + index for index in range(len(json_cols))}
        if not array_indexes:
            return rows
        return [
            tuple(
                _decode_grouped_array(value) if index in array_indexes else value
                for index, value in enumerate(row)
            )
            for row in rows
        ]

    def _utc_bucket_rows(self, rows: list[tuple[object, ...]]) -> list[tuple[object, ...]]:
        """Every bucketed rowset, with column 0 normalized to an aware UTC datetime.

        GoogleSQL keeps three time families and ``google-cloud-bigquery`` decodes them
        three different ways (see ``google.cloud.bigquery._helpers``): a TIMESTAMP
        becomes an aware ``datetime``, a DATETIME a naive one (``strptime``), and a
        DATE a ``datetime.date``. ``_query_rows`` passes cells through verbatim, so
        without this the type of ``_bucket`` depended on the declared type of a column
        the caller never sees. The consumers assume one type: they compare the bucket
        against a window bound that is aware by construction
        (``floor_to_bucket(datetime.now(UTC), ...)``) and persist it into
        ``DateTime(timezone=True)`` columns. Two of the three families are a
        ``TypeError`` against that bound, and a naive value written to a timestamptz
        means whatever the database session's timezone says it means.

        ``BaseAdapter`` documents each bucketed row's LAYOUT but has never said what
        type column 0 holds, which is why every reader answered it differently. Closing
        it here is the answer that scales: this is the only place that knows which
        GoogleSQL family the cell was decoded from, and the readers are not a closed
        set — ``metric_collect._collect_distinct_user_series`` was given its own
        laundering for this exact ``TypeError`` (tripl-ju0d) and the four remaining
        ``cast(datetime, row[0])`` sites (``chunk_processing``, three in
        ``metric_rows``) were not, which is how the bug survived that fix.

        Unconditional rather than skipped for a declared-TIMESTAMP column: ``to_utc``
        on an already-aware datetime returns an equal value, and making the rewrite
        conditional on the declared family would reintroduce exactly the coupling —
        "what the driver returns" inferred from "what the schema probe said" — that
        this method exists to sever.
        """
        return [(_as_utc_bucket(row[0]), *row[1:]) for row in rows]

    def _quote_string(self, value: str) -> str:
        """A GoogleSQL single-quoted literal holding an arbitrary string value.

        GoogleSQL has no ``''`` escape — the escape character is a BACKSLASH — and a
        quoted (non-triple) literal may not contain a raw newline or carriage return:
        those are an "Unclosed string literal". Both facts were verified against
        ZetaSQL for the sibling helper ``measure_validator.quote_sql_string_literal``,
        whose docstring records the verdicts; this method had only the first half, so
        any value carrying a newline produced a literal that spanned lines and failed
        the statement outright.

        That is a correctness and availability bug rather than an injection one: a
        value can only ever add PAIRED quotes, never an odd one. What it costs is the
        run. Several callers hand this an already-validated identifier, but several do
        not: a breakdown's top-N values, a grouped event-type value and a discovered
        JSON path are warehouse DATA, and an enum option and a contract regex are
        analyst text that ``schemas.field_definition`` bounds only by length and
        compilability — neither of which excludes a line terminator. Nothing sanitises
        any of those on the way to a literal, so one row carrying a newline fails the
        chunk for as long as that value stays in the top N, which for a high-volume
        value is indefinitely.

        Escaping order is load-bearing: the backslash is doubled FIRST, so a value
        ending in a backslash cannot escape the closing quote, and an input containing
        the two characters ``\\n`` is not confused with a real newline.

        Deliberately NOT delegating to ``quote_sql_string_literal``. That helper serves
        the visual condition builder, where its input is one analyst-typed filter
        value: it ``strip()``s and rejects the empty string and NUL. Both are wrong
        here. A breakdown value's leading/trailing whitespace is part of the group key,
        so stripping would merge ``"a"`` and ``"a "`` into one group and misattribute
        their counts; and the empty string is the legitimate NULL-collapsed group value
        that ``_contract_where_clause`` compares against. Raising on a pathological
        character would likewise turn a warehouse-supplied value into a failed run,
        which is the failure mode this fix exists to remove.
        """
        escaped = (
            value.replace("\\", "\\\\")
            .replace("'", "\\'")
            .replace("\n", "\\n")
            .replace("\r", "\\r")
        )
        return f"'{escaped}'"

    def _time_condition(
        self,
        time_column: str | None,
        time_from: datetime | None,
        time_to: datetime | None,
    ) -> str:
        """The bare half-open ``[time_from, time_to)`` predicate, or ``""`` if unbounded.

        The single place a time window becomes SQL: every read path routes through it,
        so the literal type follows the column's type family and the UTC normalization
        happens exactly once. Split out from ``_time_window_where_clause`` because the
        field-contract scan has a second predicate to AND with it, and re-deriving the
        window there is how a window ends up subtly different on one code path.
        """
        if time_column is None or time_from is None or time_to is None:
            return ""
        tc = self._validate_column(time_column)
        kind = self._time_kind(time_column)
        lower = self._time_literal(kind, time_from)
        upper = self._time_literal(kind, time_to)
        return f"`{tc}` >= {lower} AND `{tc}` < {upper}"

    def _time_window_where_clause(
        self,
        time_column: str | None,
        time_from: datetime | None,
        time_to: datetime | None,
    ) -> str:
        """``_time_condition`` as a WHERE clause. Callers with a window always get one."""
        condition = self._time_condition(time_column, time_from, time_to)
        return f" WHERE {condition}" if condition else ""

    def _json_paths_array_expression(self, column: str) -> str:
        """The sorted ARRAY<STRING> of nested leaf paths held by a nested column.

        Mirrors ClickHouse's ``arraySort(JSONAllPaths(col))``, whose elements are the
        *leaf* paths of the document (``user.address.city``), not its top-level keys.
        BigQuery's ``JSON_KEYS(col, depth)`` returns every key down to ``depth``,
        interior ones included, so an interior key is dropped by keeping only the keys
        that no other key extends with a ``.`` — what is left is the leaf set.

        A STRUCT column has no data-dependent shape: its paths are declared by the
        schema and identical for every row, so they are emitted as an array literal
        rather than computed per row.

        This is the ARRAY form. It is NOT groupable — see ``_json_paths_expression``,
        which is what every caller actually selects and groups by.
        """
        col = self._validate_column(column)
        if self._complex_kind(col) is ComplexKind.struct:
            paths = self._struct_paths.get(col, {})
            if not paths:
                return "ARRAY<STRING>[]"
            return "[" + ", ".join(self._quote_string(path) for path in paths) + "]"
        keys = f"JSON_KEYS(`{col}`, {_JSON_PATH_MAX_DEPTH})"
        return (
            f"(SELECT ARRAY_AGG(_path ORDER BY _path) FROM UNNEST({keys}) AS _path "
            f"WHERE NOT EXISTS("
            f"SELECT 1 FROM UNNEST({keys}) AS _child "
            f"WHERE STARTS_WITH(_child, CONCAT(_path, '.'))))"
        )

    def _json_paths_expression(self, column: str) -> str:
        """The leaf-path set of a nested column, as a GROUP-BY-able scalar STRING.

        Every caller puts this in both the SELECT list and the GROUP BY, and GoogleSQL
        rejects an ARRAY in a GROUP BY outright — the computed JSON form with
        ("Grouping by expressions of type ARRAY is not allowed") and the constant STRUCT
        form just as hard ("Cannot GROUP BY literal values"). Both were verified against
        ZetaSQL. So the array is rendered to its JSON text, which is a scalar and groups
        fine, and the *paths semantics are unchanged*: still the sorted set of dotted
        nested leaf paths, one group per distinct path-set.

        The JSON string is an implementation detail of the SQL, not of the row contract:
        ``_decode_rows`` turns it back into the ``list[str]`` that ``BaseAdapter``
        documents and that the cardinality analyzer requires.
        """
        return f"TO_JSON_STRING({self._json_paths_array_expression(column)})"

    supports_json_string_columns = True

    @override
    def json_string_source(self, base_query: str, columns: list[str]) -> str:
        """Parse STRING columns as ``JSON`` in a wrapper over ``base_query``.

        ``SELECT * REPLACE`` keeps every other column as it was. ``SAFE.PARSE_JSON``
        answers NULL for text that is not JSON instead of failing the job, and a
        value that parses but is not an object (a bare scalar, an array) is
        NULL too, so either reads as a row that carries none of the keys.
        ``wide_number_mode => 'round'`` keeps a number too wide for a FLOAT64
        from turning a whole valid document into NULL. The parse runs once per
        row the statement reads, which every caller already bounds.
        """
        replacements: list[str] = []
        for column in columns:
            if not _IDENTIFIER_PART_RE.match(column):
                msg = f"BigQuery: invalid column name {column!r}"
                raise ValueError(msg)
            parsed = f"SAFE.PARSE_JSON(`{column}`, wide_number_mode => 'round')"
            replacements.append(f"IF(JSON_TYPE({parsed}) = 'object', {parsed}, NULL) AS `{column}`")
        if not replacements:
            return base_query
        return f"SELECT * REPLACE ({', '.join(replacements)}) FROM ({base_query}) AS _json_src"

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        schema = self._run_query(f"SELECT * FROM ({base_query}) AS _src LIMIT 0").schema
        columns: list[ColumnInfo] = []
        struct_paths: dict[str, dict[str, bool]] = {}
        repeated: set[str] = set()
        for field in schema:
            type_name = str(field.field_type)
            mode = str(field.mode).upper()
            columns.append(
                ColumnInfo(
                    name=field.name,
                    type_name=type_name,
                    is_nullable=mode != "REQUIRED",
                )
            )
            if mode == _REPEATED_MODE:
                repeated.add(str(field.name))
            if classify_complex(type_name) is ComplexKind.struct:
                struct_paths[str(field.name)] = _declared_struct_paths(field)
        self._allowed_columns = {c.name for c in columns}
        self._column_types = {c.name: c.type_name for c in columns}
        self._struct_paths = struct_paths
        self._repeated_columns = repeated
        return columns

    def _ensure_column_types(self, base_query: str) -> None:
        """Introspect the source's schema once, before generating type-directed SQL.

        The bucket function, the window literal and the nested-path expansion are all
        chosen from the column's declared type, and a metric/preview caller builds a
        fresh adapter and jumps straight to a read — nothing calls ``get_columns``
        first. Guessing TIMESTAMP there is what produced invalid SQL against DATETIME
        and DATE columns, so pay for one ``LIMIT 0`` schema job per adapter instead.
        """
        if not self._column_types:
            self.get_columns(base_query)

    def _time_kind(self, time_column: str) -> TimeKind:
        """Resolve the declared time-type family of a configured time column.

        Raises for a column that carries no date (BigQuery ``TIME``). Every read path
        runs ``_ensure_column_types`` first, so the declared type is known by the time
        this is asked.

        WHERE it fires is not guaranteed to be the preview, and the message is written
        for an operator on the assumption that it might not be. A preview only reaches
        here when the job carries a lookback window: ``worker.tasks.scan`` passes
        ``time_column=... if preview_window else None`` and
        ``resolve_lookback_window`` returns ``None`` with no ``scan_lookback_hours``,
        so ``_time_condition`` returns early and never asks for the kind. Without a
        lookback the first thing to ask is the bucket expression, inside a collection
        tick.

        Falls back to TIMESTAMP only when the column's type was never introspected —
        callers that reach the bucket path without a preceding ``get_columns`` are
        exercising the pre-existing TIMESTAMP-only behavior.
        """
        type_name = self._column_types.get(time_column)
        if type_name is None:
            return TimeKind.timestamp
        kind = classify_time(type_name)
        if kind is TimeKind.unsupported:
            # ``WarehouseCapabilityError`` for the reason spelled out at the DATE
            # rejection in ``_bucket_expression``: this can surface from a worker, and
            # the worker's sanitiser keeps only curated types verbatim. ``type_name``
            # is a declared BigQuery type string read back from the schema probe, not
            # driver text, so the message stays free of host/port/credential material.
            msg = (
                f"BigQuery: time column {time_column!r} has type {type_name}, which "
                "carries no date and cannot be used as a time column. "
                "Use a TIMESTAMP, DATETIME or DATE column."
            )
            raise WarehouseCapabilityError(msg)
        return kind

    def _schema_datasets(self) -> list[str]:
        """The datasets one schema browse spans: validated, deduped, ordered, bounded.

        The connection's default dataset is always first and can never be squeezed out
        by the allowlist; the rest are sorted, so the browse is deterministic no matter
        what order the allowlist was saved in. With no allowlist configured this is
        exactly the single default dataset — the pre-existing behavior and the
        pre-existing cost, so turning this on cannot silently multiply anyone's job
        count.

        project/dataset ids come only from the validated DataSource model, never from a
        request; they are still validated before being interpolated into the catalog
        query, as defense-in-depth. An allowlist entry is validated with exactly the
        same rule as the default dataset — the allowlist is a new way to *reach* the
        interpolation, so it must not be a new way to *weaken* it.
        """
        if len(self._project) > _BQ_IDENTIFIER_MAX_LEN or not _BQ_PROJECT_RE.match(self._project):
            raise ValueError(f"Invalid BigQuery project id: {self._project!r}")

        ordered: list[str] = [self._dataset] if self._dataset else []
        ordered.extend(
            sorted({name for name in (self._dataset_allowlist or ()) if name != self._dataset})
        )
        for name in ordered:
            if len(name) > _BQ_IDENTIFIER_MAX_LEN or not _BQ_DATASET_RE.match(name):
                raise ValueError(f"Invalid BigQuery dataset id: {name!r}")
        if not ordered:
            msg = "BigQuery: no dataset configured — set a default dataset or a dataset allowlist"
            raise ValueError(msg)
        if len(ordered) > _MAX_SCHEMA_DATASETS:
            logger.warning(
                "BQ schema introspection: %s datasets configured, browsing the first %s",
                len(ordered),
                _MAX_SCHEMA_DATASETS,
            )
        return ordered[:_MAX_SCHEMA_DATASETS]

    def get_schema_tables(self) -> list[SchemaTable]:
        """Catalog introspection across every permitted dataset, qualified like the rest.

        ClickHouse and Postgres span every non-system database/schema and QUALIFY names
        that live outside the connection default (`analytics.orders`), leaving names
        inside it bare (`events`). The frontend depends on that convention: a table name
        carries at most one dot, and only when it sits outside the default. BigQuery used
        to be the odd one out — default dataset only, every name bare — so a source whose
        tables lived in a second dataset simply had no autocomplete.

        The cost is bounded on three axes: at most ``_MAX_SCHEMA_DATASETS`` jobs, at most
        ``_SCHEMA_ROW_LIMIT`` rows across ALL of them (the LIMIT shrinks as the budget is
        spent, so the total is a budget rather than a per-dataset allowance), and each job
        deadlined.

        A dataset the credentials cannot read is a *partial* failure, not a total one: it
        is logged and skipped, and the datasets that did work still return their tables.
        Only a browse where every single dataset failed re-raises — silently returning an
        empty catalog there would look exactly like "this project has no tables", which is
        the wrong thing to tell a user staring at an empty autocomplete.
        """
        datasets = self._schema_datasets()
        budget = _SCHEMA_ROW_LIMIT
        columns_by_table: dict[str, list[SchemaColumn]] = {}
        failures: list[tuple[str, Exception]] = []
        succeeded = 0

        for dataset in datasets:
            if budget <= 0:
                logger.warning(
                    "BQ schema introspection: %s-row budget exhausted, skipping dataset %r "
                    "and any after it",
                    _SCHEMA_ROW_LIMIT,
                    dataset,
                )
                break
            sql = (
                "SELECT table_name, column_name, data_type "
                f"FROM `{self._project}.{dataset}.INFORMATION_SCHEMA.COLUMNS` "
                f"ORDER BY table_name, ordinal_position LIMIT {budget}"
            )
            logger.debug("BQ schema introspection query: %s", sql)
            try:
                _, rows = self._query_rows(sql, timeout_cap=_SCHEMA_QUERY_TIMEOUT_SECONDS)
            except Exception as exc:
                logger.warning(
                    "BQ schema introspection skipped dataset %r: %s", dataset, exc, exc_info=True
                )
                failures.append((dataset, exc))
                continue

            succeeded += 1
            budget -= len(rows)
            for table_name, column_name, data_type in rows:
                bare = str(table_name)
                # Bare inside the connection's default dataset, `dataset.table` outside it.
                qualified = bare if dataset == self._dataset else f"{dataset}.{bare}"
                columns_by_table.setdefault(qualified, []).append(
                    SchemaColumn(name=str(column_name), data_type=str(data_type))
                )

        if succeeded == 0 and failures:
            raise failures[0][1]
        return [
            SchemaTable(name=table, columns=columns) for table, columns in columns_by_table.items()
        ]

    def _query_rows(
        self, sql: str, *, timeout_cap: float | None = None
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        iterator = self._run_query(sql, timeout_cap=timeout_cap)
        names = [field.name for field in iterator.schema]
        rows = [tuple(row.values()) for row in iterator]
        return names, rows

    def get_preview_rows(
        self,
        base_query: str,
        limit: int = 10,
        *,
        time_column: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        if time_column is not None:
            # A preview is where a metric's time column gets chosen, so this is where
            # an unusable one (TIME) must fail — and where a DATETIME/DATE one has to
            # be recognized so the window literal matches it.
            self._ensure_column_types(base_query)
        where_clause = self._time_window_where_clause(time_column, time_from, time_to)
        sql = f"SELECT * FROM ({base_query}) AS _src{where_clause} LIMIT {int(limit)}"
        logger.info("BQ preview query: %s", sql)
        return self._query_rows(sql)

    def _contract_where_clause(
        self,
        time_column: str | None,
        time_from: datetime | None,
        time_to: datetime | None,
        group_column: str | None,
        group_value: str | None,
    ) -> str:
        """The contract scan's window, ANDed with the optional grouped-event filter.

        Mirrors ``ClickHouseAdapter._contract_where_clause`` exactly, including the way
        the group value is compared: against the column's NULL-collapsed STRING rendering
        (``ifNull(toString(c), '')`` there, ``IFNULL(CAST(c AS STRING), '')`` here), so a
        NULL group column matches the empty-string group on both warehouses and on the
        Python fallback, which compares ``"" if raw is None else str(raw)``.
        """
        conditions: list[str] = []
        window = self._time_condition(time_column, time_from, time_to)
        if window:
            conditions.append(window)
        if group_column is not None:
            gc = self._validate_column(group_column)
            expected = self._quote_string(group_value or "")
            conditions.append(
                f"{self._string_value_expression(gc, role='grouped-event column')} = {expected}"
            )
        if not conditions:
            return ""
        return " WHERE " + " AND ".join(conditions)

    @override
    def _probe_contract_regex(self, pattern: str) -> None:
        """Have BigQuery compile the pattern with RE2, scanning nothing.

        A statement with no FROM processes no bytes, so the probe costs a job and
        no billed data — which is the only reason a round trip is affordable on
        the engine that bills by the byte. It goes through ``_run_query`` like
        every other statement this adapter issues, so it inherits the deadline
        and the cancel-on-timeout that method exists for.

        One job per DISTINCT pattern per adapter, memoized by
        ``contract_regex_is_compilable``: a config with no regex contract issues
        none at all, and the one that has a regex contract on ten event-type
        groups still issues one. The alternative — compiling the pattern into the
        contract statement and letting a rejection fail the job — is what
        ``validate_field_contracts`` below is built to avoid, since that job is
        the one that reads the whole window.
        """
        self._run_query(f"SELECT REGEXP_CONTAINS('', {self._quote_string(pattern)})")

    def _contract_fragments(
        self,
        expectation: FieldContractExpectation,
        *,
        index: int,
    ) -> tuple[str, str] | None:
        """Compile one expectation to ``(aggregate_sql, struct_sql)``, or None if inert.

        What counts as a BAD row is matched term for term to
        ``ClickHouseAdapter._contract_aggregates`` and
        ``PostgresAdapter._contract_bad_condition``, and through them to the Python
        fallback in ``BaseAdapter``. What counts as a VIOLATION is not decided here at
        all — see the field contract section of ``BaseAdapter`` for where that lives and
        why this method is the one place still allowed to apply it warehouse-side:

        * **required_null_violation** — the NULL *is* the violation, so NULLs are counted
          in the denominator: ``total`` is ``COUNT(*)``, not the non-NULL count. Its sample
          is the literal ``'<NULL>'``, exactly as the fallback records it.
        * **enum / regex / range** — a NULL carries no evidence either way, so it is
          EXCLUDED from the denominator entirely (``COUNTIF(col IS NOT NULL)``), matching
          the fallback's ``if raw_value is None: continue`` *before* it increments
          ``total_count``. Getting this backwards would dilute every bad_rate by the null
          rate and quietly push violations under their threshold.
        * **regex** — GoogleSQL's ``REGEXP_CONTAINS`` is a PARTIAL match (verified against
          ZetaSQL: ``REGEXP_CONTAINS('xu1x', 'u\\\\d')`` is TRUE), which is what
          ClickHouse's ``match()`` and the fallback's ``regex.search()`` both are.
          ``REGEXP_FULL_MATCH`` would be the anchored one and is deliberately NOT used.
        * **range** — a value that is not a number at all is BAD, not "skipped". The
          fallback treats a ``float()`` failure as a violation and ClickHouse counts
          ``isNull(toFloat64OrNull(...))`` as one, so ``SAFE_CAST(... AS FLOAT64) IS NULL``
          is a bad condition here too. Both warehouses cast from the column's STRING
          rendering rather than its native type, so all three agree on what "malformed"
          means.

        The sample is ``MIN(IF(bad, value, NULL))`` rather than an ``ANY_VALUE``: MIN
        ignores NULL inputs, so it can only ever return a value from a row that actually
        violated, and it is deterministic, where ClickHouse's ``anyIf`` is not.

        "Inert" here is the shared list plus what THIS dialect declines — a REPEATED
        column, and a pattern RE2 will not compile — and that split is the whole
        point: what an expectation MEANS is shared, what an engine can compile is
        not. See the field contract section of ``BaseAdapter``, where both
        divergences are declared.
        """
        if self._field_contract_is_inert(expectation):
            return None

        prop = split_property_field(expectation.field_name)
        if prop is None:
            column = self._validate_column(expectation.field_name)
            operand = f"`{column}`"
        else:
            # A property (F23, #306): its extracted STRING is the operand, NULL
            # when the row does not carry it, and it is never REPEATED.
            column = expectation.field_name
            extracted = self._contract_operand(
                expectation, lambda field: self._property_value_expression(*prop)
            )
            if extracted is None:
                return None
            operand = extracted
        present = f"{operand} IS NOT NULL"
        threshold = clamp_field_contract_threshold(expectation.threshold)

        if expectation.drift_type != "required_null_violation" and column in self._repeated_columns:
            # Every branch below but required_null needs the scalar STRING rendering,
            # and there is none for an ARRAY — so `_string_value_expression` raises,
            # and it must keep raising for `role="breakdown column"`, where the caller
            # is still choosing a column and a loud failure is the right answer.
            #
            # Here the caller is a worker replaying contracts a user declared long ago,
            # so the same raise ended the entire collection: `schema_drift` called
            # `validate_field_contracts` bare and `catalog_sync` calls that once per
            # event-type group. `schema_drift` now contains a raise and counts it, but
            # that is a backstop and not a licence to raise from here: it costs the
            # event type every other contract it declared, where declining costs one.
            # An explicit pre-check, rather than wrapping the call in
            # try/except ValueError, is what keeps the two roles' answers separate —
            # and it mirrors the `_allowed_columns` skip in `validate_field_contracts`
            # line for line, which exists for the identical reason (a stale contract).
            logger.warning(
                "BQ field contract skipped: column %r is REPEATED (an ARRAY) and has no "
                "scalar STRING rendering, so its %s contract cannot be compiled. The "
                "other contracts in this scan still run.",
                column,
                expectation.drift_type,
            )
            self._skip_field_contract(expectation)
            return None

        if expectation.drift_type == "required_null_violation":
            # Deliberately does NOT build the STRING rendering: a required-ness check is
            # pure NULL logic and works on any column type, including one this adapter
            # refuses to stringify.
            bad = f"{operand} IS NULL"
            total = "COUNT(*)"
            sample = f"MIN(IF({bad}, '<NULL>', NULL))"
        elif expectation.drift_type == "enum_violation":
            value_expr = self._field_value_expression(column, role="field-contract column")
            options = ", ".join(self._quote_string(option) for option in expectation.enum_options)
            bad = f"{present} AND {value_expr} NOT IN ({options})"
            total = f"COUNTIF({present})"
            sample = f"MIN(IF({bad}, {value_expr}, NULL))"
        elif expectation.drift_type == "regex_violation":
            value_expr = self._field_value_expression(column, role="field-contract column")
            # The assert narrows the type; a pattern-less regex is inert above.
            assert expectation.regex is not None
            # The second reason this engine can decline an expectation the other
            # two would compile, and the mirror of the one above: RE2 has no
            # lookaround and no backreferences, all of which the Python `re` the
            # save gate screens with accepts. Offered to the engine before it
            # rides into the job that reads the window.
            if not self.contract_regex_is_compilable(expectation.regex):
                self._skip_field_contract(expectation)
                return None
            pattern = self._quote_string(expectation.regex)
            bad = f"{present} AND NOT REGEXP_CONTAINS({value_expr}, {pattern})"
            total = f"COUNTIF({present})"
            sample = f"MIN(IF({bad}, {value_expr}, NULL))"
        elif expectation.drift_type == "range_violation":
            value_expr = self._field_value_expression(column, role="field-contract column")
            numeric = f"SAFE_CAST({value_expr} AS FLOAT64)"
            checks = [f"{numeric} IS NULL"]
            # Rendered through the shared helper rather than an f-string of the float.
            # GoogleSQL has no literal for infinity or NaN, so `< -inf` is a parse
            # error the fake client in a unit test happily accepts and a worker only
            # discovers against the real service — and it would take the sibling
            # contracts in the same statement with it.
            if expectation.min_value is not None:
                checks.append(f"{numeric} < {contract_bound_literal(expectation.min_value)}")
            if expectation.max_value is not None:
                checks.append(f"{numeric} > {contract_bound_literal(expectation.max_value)}")
            bad = f"{present} AND ({' OR '.join(checks)})"
            total = f"COUNTIF({present})"
            sample = f"MIN(IF({bad}, {value_expr}, NULL))"
        else:
            return None

        aggregate_sql = (
            f"COUNTIF({bad}) AS _bad_{index}, "
            f"{total} AS _total_{index}, "
            f"{sample} AS _sample_{index}"
        )
        # bad_rate goes through SAFE_DIVIDE, not `/`. GoogleSQL's `/` raises on a zero
        # denominator ("zero divided error" — verified against the emulator), and SQL does
        # not promise that the `total_count > 0` guard in the outer WHERE is evaluated
        # first. That zero is why the other two engines stopped judging in SQL at all;
        # BigQuery keeps doing it because its STRUCT array already gives it a row per
        # expectation to filter, and SAFE_DIVIDE makes the empty window a NULL rate
        # rather than a failed scan.
        #
        # The threshold is spelled with repr(), not `%.12g`. This literal is the ONLY
        # copy of the threshold that is compared anywhere but inside
        # field_contract_verdict, so it has to be the same double that function would
        # have used: repr of a float round-trips exactly, while %.12g silently rounds
        # one with more digits than that — enough for a contract set to 1/3 to fire
        # here and not on the engines that compare in Python.
        struct_sql = (
            "STRUCT("
            f"{self._quote_string(expectation.field_name)} AS field_name, "
            f"{self._quote_string(expectation.drift_type)} AS drift_type, "
            f"_agg._bad_{index} AS bad_count, "
            f"_agg._total_{index} AS total_count, "
            f"CAST({threshold!r} AS FLOAT64) AS threshold, "
            f"IFNULL(SAFE_DIVIDE(_agg._bad_{index}, _agg._total_{index}), 0.0) AS bad_rate, "
            f"_agg._sample_{index} AS sample_value"
            ")"
        )
        return aggregate_sql, struct_sql

    @override
    def validate_field_contracts(
        self,
        base_query: str,
        expectations: list[FieldContractExpectation],
        *,
        time_column: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
        group_column: str | None = None,
        group_value: str | None = None,
        limit: int = 50000,
    ) -> list[FieldContractViolation]:
        """Evaluate field contracts warehouse-side, over the FULL window.

        Replaces ``BaseAdapter``'s fallback, which pulls at most ``limit`` (50,000)
        sampled rows and evaluates them in Python. That fallback cannot see a violation
        that first occurs at row 50,001, and the ``bad_rate`` it reports describes the
        sample, not the data — so a contract could be badly violated and the scan would
        either miss it or under-report it straight past its threshold.

        ONE job AND one scan covers every expectation. The shape both other SQL engines
        started with — a ``UNION ALL`` of one aggregate subquery per expectation — is one
        job but N SCANS of ``base_query``, and BigQuery bills by bytes scanned, so a table
        with ten contracts would be billed ten times over on every scan. Instead the
        per-expectation aggregates are computed side by side in a SINGLE pass, assembled
        into an array of STRUCTs, and unnested into the one-row-per-violation shape the
        caller wants. The threshold/nonzero filtering happens on the unnested rows, so a
        passing contract never crosses the wire; PostgreSQL and ClickHouse reach the same
        single pass but stop at the counts and decide in Python, which the field contract
        section of ``BaseAdapter`` states as the rule and this method as its exception.

        ``limit`` no longer bounds what is *evaluated* (that is the whole point); it stays
        as the bound on how many violation ROWS come back, matching ClickHouse.
        """
        if not expectations:
            return []

        # The window literal's type family (TIMESTAMP vs DATETIME vs DATE) and the
        # scalar/REPEATED decision both come from the declared schema, and a worker
        # constructs a fresh adapter and calls straight into this — nothing calls
        # get_columns first. Introspect before generating any type-directed SQL.
        self._ensure_column_types(base_query)
        where_clause = self._contract_where_clause(
            time_column, time_from, time_to, group_column, group_value
        )

        aggregate_parts: list[str] = []
        struct_parts: list[str] = []
        for index, expectation in enumerate(expectations):
            source_column = expectation.field_name.split(".", 1)[0]
            if self._allowed_columns and source_column not in self._allowed_columns:
                # The fallback skips an expectation whose field is absent from the source
                # (`if field_index is None: continue`). Do the same rather than compiling a
                # reference to a column that does not exist and failing the entire scan —
                # a stale contract on a dropped column must not take the other contracts
                # down with it.
                self._skip_field_contract(expectation)
                continue
            fragments = self._contract_fragments(expectation, index=index)
            if fragments is None:
                continue
            aggregate_sql, struct_sql = fragments
            aggregate_parts.append(aggregate_sql)
            struct_parts.append(struct_sql)

        if not struct_parts:
            return []

        # WITH OFFSET + ORDER BY: UNNEST does not promise it preserves array order, so
        # without this the violation order would be unspecified. Ordering by the offset
        # hands them back in expectation order, deterministically.
        sql = (
            "SELECT _c.field_name AS field_name, _c.drift_type AS drift_type, "
            "_c.bad_count AS bad_count, _c.total_count AS total_count, "
            "_c.threshold AS threshold, _c.bad_rate AS bad_rate, "
            "_c.sample_value AS sample_value "
            "FROM ("
            f"SELECT [{', '.join(struct_parts)}] AS _contracts "
            f"FROM (SELECT {', '.join(aggregate_parts)} "
            f"FROM ({base_query}) AS _src{where_clause}"
            ") AS _agg"
            ") AS _rows "
            "CROSS JOIN UNNEST(_rows._contracts) AS _c WITH OFFSET AS _ord "
            "WHERE _c.total_count > 0 AND _c.bad_count > 0 "
            "AND SAFE_DIVIDE(_c.bad_count, _c.total_count) > _c.threshold "
            f"ORDER BY _ord LIMIT {int(limit)}"
        )
        logger.info("BQ field contract query: %s", sql)
        _, rows = self._query_rows(sql)

        return [
            FieldContractViolation(
                field_name=str(row[0]),
                drift_type=str(row[1]),
                bad_count=int(cast("int", row[2])),
                total_count=int(cast("int", row[3])),
                threshold=float(cast("float", row[4])),
                bad_rate=float(cast("float", row[5])),
                sample_value=None if row[6] is None else str(row[6]),
            )
            for row in rows
        ]

    def get_full_breakdown(
        self,
        base_query: str,
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None = None,
        time_column: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
        limit: int = 50000,
    ) -> tuple[list[str], list[str], list[str], list[tuple[object, ...]]]:
        self._ensure_column_types(base_query)
        reg_cols = [self._validate_column(c) for c in regular_columns]
        json_cols = [self._validate_column(c) for c in json_columns]
        json_value_paths = json_value_paths or {}

        where_clause = self._time_window_where_clause(time_column, time_from, time_to)
        from_sql, alias_by_name, json_value_names = self._nested_source(
            base_query, where_clause, json_cols, json_value_paths
        )

        select_parts: list[str] = []
        group_parts: list[str] = []
        for c in reg_cols:
            select_sql, group_sql = self._regular_column_sql(c)
            select_parts.append(select_sql)
            group_parts.append(group_sql)
        for names in (json_cols, json_value_names):
            nested_select, nested_group = self._nested_select_group(names, alias_by_name)
            select_parts.extend(nested_select)
            group_parts.extend(nested_group)
        select_parts.append("COUNT(*) AS _cnt")

        group_by = ", ".join(group_parts) if group_parts else "()"
        sql = (
            f"SELECT {', '.join(select_parts)} "
            f"FROM {from_sql} "
            f"GROUP BY {group_by} "
            f"ORDER BY _cnt DESC "
            f"LIMIT {int(limit)}"
        )

        short = sql[:300] + ("..." if len(sql) > 300 else "")
        logger.info("BQ breakdown query: %s", short)
        t0 = time.monotonic()
        _, rows = self._query_rows(sql)
        elapsed = time.monotonic() - t0
        logger.info("BQ breakdown done in %.2fs, %s rows", elapsed, len(rows))

        decoded = self._decode_rows(rows, offset=0, reg_cols=reg_cols, json_cols=json_cols)
        return reg_cols, json_cols, json_value_names, decoded

    def get_time_bucketed_counts(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None,
        time_from: datetime,
        time_to: datetime,
        limit: int = 100000,
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        self._ensure_column_types(base_query)
        bucket_expr = self._bucket_expression(time_column, interval)
        where_clause = self._time_window_where_clause(time_column, time_from, time_to)
        reg_cols = [self._validate_column(c) for c in regular_columns]
        json_cols = [self._validate_column(c) for c in json_columns]
        json_value_paths = json_value_paths or {}

        from_sql, alias_by_name, json_value_names = self._nested_source(
            base_query, where_clause, json_cols, json_value_paths
        )

        select_parts: list[str] = [f"{bucket_expr} AS _bucket"]
        group_parts: list[str] = ["_bucket"]
        col_names: list[str] = []
        for c in reg_cols:
            select_sql, group_sql = self._regular_column_sql(c)
            select_parts.append(select_sql)
            group_parts.append(group_sql)
            col_names.append(c)
        nested_select, nested_group = self._nested_select_group(json_cols, alias_by_name)
        select_parts.extend(nested_select)
        group_parts.extend(nested_group)
        col_names.extend(json_cols)
        value_select, value_group = self._nested_select_group(json_value_names, alias_by_name)
        select_parts.extend(value_select)
        group_parts.extend(value_group)
        select_parts.append("COUNT(*) AS _cnt")

        sql = (
            f"SELECT {', '.join(select_parts)} "
            f"FROM {from_sql} "
            f"GROUP BY {', '.join(group_parts)} "
            f"ORDER BY _bucket "
            f"LIMIT {int(limit)}"
        )

        logger.info("BQ bucketed query: %s", sql)
        t0 = time.monotonic()
        _, rows = self._query_rows(sql)
        elapsed = time.monotonic() - t0
        logger.info("BQ bucketed done in %.2fs, %s rows", elapsed, len(rows))

        decoded = self._decode_rows(rows, offset=1, reg_cols=reg_cols, json_cols=json_cols)
        return col_names, json_value_names, self._utc_bucket_rows(decoded)

    def _aggregate_value_sql(self, agg_fn: MetricAggregation, measure_column: str | None) -> str:
        """Validate + escape the measure and build the safe aggregate fragment."""
        measure_sql: str | None = None
        if measure_column is not None:
            measure_sql = f"`{validate_measure_column(measure_column, self._allowed_columns)}`"
        return build_aggregate_sql(agg_fn, measure_sql)

    def _validate_alias(self, alias: str) -> str:
        """Validate a caller-supplied output column alias before interpolation."""
        if not _IDENTIFIER_PART_RE.match(alias):
            msg = f"Invalid aggregate key alias: {alias!r}"
            raise ValueError(msg)
        return alias

    def _conditional_aggregate_sql(self, spec: AggregateSpec) -> str:
        """Build one (optionally conditional) aggregate fragment for a spec.

        With no ``filter_sql`` this reuses the exact single-aggregate fragment
        (``build_aggregate_sql``) so values match the per-metric path. With a
        filter, BigQuery lacks the ``FILTER (WHERE ...)`` clause, so the
        condition is folded into the aggregate per the dialect rules:
        ``count`` -> ``count(CASE WHEN cond THEN 1 END)``; ``count_distinct`` ->
        ``count(DISTINCT IF(cond, col, NULL))``; ``sum/avg/min/max`` ->
        ``agg(CASE WHEN cond THEN col END)``. ``filter_sql`` is a pre-validated
        boolean fragment injected as-is, matching the row-filter trust model.

        The NULL-means-gap rule this implements is stated once, on
        :class:`~tripl.core.adapters.base.BaseAdapter`: a bucket is absent for a
        spec when NO row in it matched ``cond``, and a bucket that does have
        matching rows is a data point even when the aggregate over them is 0.
        Each aggregate spells the row-presence test as cheaply as it can:

        * ``avg`` / ``sum`` / ``min`` / ``max`` over the ``CASE WHEN`` form need
          no test at all — they already return NULL over zero matching rows.
        * ``count`` uses ``NULLIF(count(CASE WHEN cond THEN 1 END), 0)``,
          because that value IS the count of matching rows: 0 and "nothing
          matched" are the same statement. Spelling it as the CASE below was
          rejected — it emits the same verdict from twice the text and a second
          copy of ``cond``.
        * ``count_distinct`` needs an explicit ``COUNTIF(cond)`` probe:
          ``count(DISTINCT IF(cond, m, NULL))`` returns 0 both for a bucket
          nothing matched AND for a bucket whose matching rows all have ``m IS
          NULL``, so its own value cannot answer the question. It used to be
          ``NULLIF(..., 0)`` too, which reported an all-NULL measure over real
          rows as a gap, while ClickHouse — gating on ``countIf(cond)``, a row
          count — kept the bucket and stored the 0.
        """
        measure_sql: str | None = None
        if spec.column is not None:
            measure_sql = f"`{validate_measure_column(spec.column, self._allowed_columns)}`"
        agg = coerce_aggregation(spec.aggregation)
        if spec.filter_sql is None:
            return build_aggregate_sql(agg, measure_sql)
        cond = spec.filter_sql
        if agg is MetricAggregation.count:
            return f"NULLIF(count(CASE WHEN {cond} THEN 1 END), 0)"
        if not measure_sql:
            msg = f"Aggregation {agg.value!r} requires a measure column"
            raise ValueError(msg)
        if agg is MetricAggregation.count_distinct:
            distinct = f"count(DISTINCT IF({cond}, {measure_sql}, NULL))"
            return f"CASE WHEN COUNTIF({cond}) = 0 THEN NULL ELSE {distinct} END"
        return f"{agg.value}(CASE WHEN {cond} THEN {measure_sql} END)"

    def get_time_bucketed_aggregate(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        agg_fn: MetricAggregation,
        measure_column: str | None,
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None,
        time_from: datetime,
        time_to: datetime,
        limit: int = 100000,
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        self._ensure_column_types(base_query)
        bucket_expr = self._bucket_expression(time_column, interval)
        where_clause = self._time_window_where_clause(time_column, time_from, time_to)
        reg_cols = [self._validate_column(c) for c in regular_columns]
        json_cols = [self._validate_column(c) for c in json_columns]
        json_value_paths = json_value_paths or {}
        value_sql = self._aggregate_value_sql(agg_fn, measure_column)

        from_sql, alias_by_name, json_value_names = self._nested_source(
            base_query, where_clause, json_cols, json_value_paths
        )

        select_parts: list[str] = [f"{bucket_expr} AS _bucket"]
        group_parts: list[str] = ["_bucket"]
        col_names: list[str] = []
        for c in reg_cols:
            select_sql, group_sql = self._regular_column_sql(c)
            select_parts.append(select_sql)
            group_parts.append(group_sql)
            col_names.append(c)
        nested_select, nested_group = self._nested_select_group(json_cols, alias_by_name)
        select_parts.extend(nested_select)
        group_parts.extend(nested_group)
        col_names.extend(json_cols)
        value_select, value_group = self._nested_select_group(json_value_names, alias_by_name)
        select_parts.extend(value_select)
        group_parts.extend(value_group)
        select_parts.append(f"{value_sql} AS _value")

        sql = (
            f"SELECT {', '.join(select_parts)} "
            f"FROM {from_sql} "
            f"GROUP BY {', '.join(group_parts)} "
            f"ORDER BY _bucket "
            f"LIMIT {int(limit)}"
        )

        logger.info("BQ bucketed aggregate query: %s", sql)
        t0 = time.monotonic()
        _, rows = self._query_rows(sql)
        elapsed = time.monotonic() - t0
        logger.info("BQ bucketed aggregate done in %.2fs, %s rows", elapsed, len(rows))

        decoded = self._decode_rows(rows, offset=1, reg_cols=reg_cols, json_cols=json_cols)
        return col_names, json_value_names, self._utc_bucket_rows(decoded)

    def _breakdown_value_exprs(
        self,
        base_query: str,
        time_column: str,
        breakdown: str,
        raw_expr: str,
        time_from: datetime,
        time_to: datetime,
        values_limit: int | None,
    ) -> tuple[str, str]:
        """Build the (breakdown_value, is_other) expressions with Other folding."""
        if values_limit is None:
            return raw_expr, "0"
        top_count = max(values_limit - 1, 0)
        top_values = self._top_breakdown_values_multi(
            base_query,
            time_column,
            [breakdown],
            time_from,
            time_to,
            top_count,
        ).get(breakdown, [])
        if not top_values:
            return "'Other'", "1"
        quoted = ", ".join(self._quote_string(v) for v in top_values)
        in_clause = f"{raw_expr} IN ({quoted})"
        breakdown_expr = f"CASE WHEN {in_clause} THEN {raw_expr} ELSE 'Other' END"
        is_other_expr = f"CASE WHEN {in_clause} THEN 0 ELSE 1 END"
        return breakdown_expr, is_other_expr

    def get_time_bucketed_aggregate_breakdown(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        agg_fn: MetricAggregation,
        measure_column: str | None,
        breakdown_column: str,
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None,
        time_from: datetime,
        time_to: datetime,
        values_limit: int | None = None,
        limit: int = 100000,
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        self._ensure_column_types(base_query)
        bucket_expr = self._bucket_expression(time_column, interval)
        where_clause = self._time_window_where_clause(time_column, time_from, time_to)
        reg_cols = [self._validate_column(c) for c in regular_columns]
        json_cols = [self._validate_column(c) for c in json_columns]
        breakdown = self._validate_column(breakdown_column)
        if breakdown not in reg_cols:
            msg = f"Breakdown column must be a scalar column: {breakdown}"
            raise ValueError(msg)
        json_value_paths = json_value_paths or {}
        value_sql = self._aggregate_value_sql(agg_fn, measure_column)

        raw_expr = self._string_value_expression(breakdown)
        breakdown_expr, is_other_expr = self._breakdown_value_exprs(
            base_query,
            time_column,
            breakdown,
            raw_expr,
            time_from,
            time_to,
            values_limit,
        )
        from_sql, alias_by_name, json_value_names = self._nested_source(
            base_query, where_clause, json_cols, json_value_paths
        )

        select_parts: list[str] = [
            f"{bucket_expr} AS _bucket",
            f"{breakdown_expr} AS _breakdown_value",
            f"{is_other_expr} AS _is_other",
        ]
        group_parts: list[str] = ["_bucket", "_breakdown_value", "_is_other"]
        col_names: list[str] = []
        for c in reg_cols:
            if c == breakdown:
                # The breakdown keeps its regular-column slot but carries the
                # FOLDED value there, and is deliberately NOT added to
                # group_parts: see
                # BaseAdapter.get_time_bucketed_aggregate_breakdown for why the
                # raw column may not be a grouping key. `_regular_column_sql` is
                # skipped rather than reused because its only special case is
                # the REPEATED column, and `_string_value_expression` has
                # already refused a REPEATED breakdown a few lines above.
                # The breakdown's slot is grouped BY ITS OWN ALIAS, and the alias
                # is deliberately not the column's name. Both halves were
                # settled against Google's ZetaSQL analyzer, because every
                # fake-client test in this repo passes either way — a fake
                # answers any string, so nothing here can tell you the warehouse
                # would have refused the statement.
                #
                # ZetaSQL does NOT match a repeated expression against an
                # identical one in GROUP BY. Projecting the fold a second time
                # and adding that same expression to the grouping is rejected
                # with `SELECT list expression references column <name> which is
                # neither grouped nor aggregated` — measured, not reasoned:
                # grouping by the expression fails, grouping by the alias bound
                # to it passes. So the grouping names the alias.
                #
                # The alias avoids the column's own name because that name would
                # then mean two things at once — the source column and this
                # slot. GoogleSQL accepts the ambiguity and resolves it, but its
                # two readings differ in VALUE, not just in spelling: the alias
                # is the folded value, the column is the raw one, and grouping
                # by the raw one is the defect tripl-0zpq.58 exists to remove. A
                # name that can only mean one of them cannot regress quietly.
                # Nothing downstream is affected — rows are read positionally
                # and their names come from `col_names`, which still carries the
                # real column name.
                #
                # Neither spelling is needed by the siblings: ClickHouse groups
                # with GROUP BY ALL and PostgreSQL resolves the output name to
                # the same expression tree, so both stay local to BigQuery.
                slot_alias = f"_bd_col{len(col_names)}"
                select_parts.append(f"{breakdown_expr} AS `{slot_alias}`")
                group_parts.append(f"`{slot_alias}`")
            else:
                select_sql, group_sql = self._regular_column_sql(c)
                select_parts.append(select_sql)
                group_parts.append(group_sql)
            col_names.append(c)
        nested_select, nested_group = self._nested_select_group(json_cols, alias_by_name)
        select_parts.extend(nested_select)
        group_parts.extend(nested_group)
        col_names.extend(json_cols)
        value_select, value_group = self._nested_select_group(json_value_names, alias_by_name)
        select_parts.extend(value_select)
        group_parts.extend(value_group)
        select_parts.append(f"{value_sql} AS _value")

        sql = (
            f"SELECT {', '.join(select_parts)} "
            f"FROM {from_sql} "
            f"GROUP BY {', '.join(group_parts)} "
            f"ORDER BY _bucket, _breakdown_value "
            f"LIMIT {int(limit)}"
        )

        logger.info("BQ bucketed aggregate breakdown query for %s: %s", breakdown, sql)
        t0 = time.monotonic()
        _, rows = self._query_rows(sql)
        elapsed = time.monotonic() - t0
        logger.info("BQ bucketed aggregate breakdown done in %.2fs, %s rows", elapsed, len(rows))

        decoded = self._decode_rows(rows, offset=3, reg_cols=reg_cols, json_cols=json_cols)
        return col_names, json_value_names, self._utc_bucket_rows(decoded)

    def build_time_bucketed_multi_aggregate_sql(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        specs: list[AggregateSpec],
        time_from: datetime,
        time_to: datetime,
        *,
        limit: int = 100000,
    ) -> tuple[list[str], str]:
        bucket_expr = self._bucket_expression(time_column, interval)
        where_clause = self._time_window_where_clause(time_column, time_from, time_to)
        if not specs:
            return ["bucket"], ""

        select_parts: list[str] = [f"{bucket_expr} AS _bucket"]
        column_names: list[str] = ["bucket"]
        for spec in specs:
            key = self._validate_alias(spec.key)
            select_parts.append(f"{self._conditional_aggregate_sql(spec)} AS `{key}`")
            column_names.append(spec.key)

        sql = (
            f"SELECT {', '.join(select_parts)} "
            f"FROM ({base_query}) AS _src{where_clause} "
            f"GROUP BY _bucket "
            f"ORDER BY _bucket "
            f"LIMIT {int(limit)}"
        )
        return column_names, sql

    def get_time_bucketed_multi_aggregate(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        specs: list[AggregateSpec],
        time_from: datetime,
        time_to: datetime,
        *,
        limit: int = 100000,
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        self._ensure_column_types(base_query)
        column_names, sql = self.build_time_bucketed_multi_aggregate_sql(
            base_query,
            time_column,
            interval,
            specs,
            time_from,
            time_to,
            limit=limit,
        )
        if not specs:
            return column_names, []

        logger.info("BQ bucketed multi-aggregate query: %s", sql)
        t0 = time.monotonic()
        _, rows = self._query_rows(sql)
        elapsed = time.monotonic() - t0
        logger.info("BQ bucketed multi-aggregate done in %.2fs, %s rows", elapsed, len(rows))

        return column_names, self._utc_bucket_rows(rows)

    def get_time_bucketed_multi_aggregate_breakdown(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        breakdown_column: str,
        specs: list[AggregateSpec],
        time_from: datetime,
        time_to: datetime,
        *,
        values_limit: int | None = None,
        limit: int = 100000,
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        self._ensure_column_types(base_query)
        bucket_expr = self._bucket_expression(time_column, interval)
        where_clause = self._time_window_where_clause(time_column, time_from, time_to)
        breakdown = self._validate_column(breakdown_column)
        if not specs:
            return ["bucket", "breakdown_value", "is_other"], []

        raw_expr = self._string_value_expression(breakdown)
        breakdown_expr, is_other_expr = self._breakdown_value_exprs(
            base_query,
            time_column,
            breakdown,
            raw_expr,
            time_from,
            time_to,
            values_limit,
        )

        select_parts: list[str] = [
            f"{bucket_expr} AS _bucket",
            f"{breakdown_expr} AS _breakdown_value",
            f"{is_other_expr} AS _is_other",
        ]
        column_names: list[str] = ["bucket", "breakdown_value", "is_other"]
        for spec in specs:
            key = self._validate_alias(spec.key)
            select_parts.append(f"{self._conditional_aggregate_sql(spec)} AS `{key}`")
            column_names.append(spec.key)

        sql = (
            f"SELECT {', '.join(select_parts)} "
            f"FROM ({base_query}) AS _src{where_clause} "
            f"GROUP BY _bucket, _breakdown_value, _is_other "
            f"ORDER BY _bucket, _breakdown_value "
            f"LIMIT {int(limit)}"
        )

        logger.info("BQ bucketed multi-aggregate breakdown query for %s: %s", breakdown, sql)
        t0 = time.monotonic()
        _, rows = self._query_rows(sql)
        elapsed = time.monotonic() - t0
        logger.info(
            "BQ bucketed multi-aggregate breakdown done in %.2fs, %s rows", elapsed, len(rows)
        )

        return column_names, self._utc_bucket_rows(rows)

    def get_time_bucketed_breakdown_counts(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        breakdown_column: str,
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None,
        time_from: datetime,
        time_to: datetime,
        values_limit: int | None = None,
        limit: int = 100000,
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        col_names, json_value_names, rows = self.get_time_bucketed_breakdown_counts_multi(
            base_query,
            time_column,
            interval,
            [breakdown_column],
            regular_columns,
            json_columns,
            json_value_paths,
            time_from,
            time_to,
            values_limit=values_limit,
            limit=limit,
        )
        return col_names, json_value_names, [(row[0], row[2], row[3], *row[4:]) for row in rows]

    def _query_top_breakdown_values_multi(
        self,
        base_query: str,
        time_column: str,
        breakdown_columns: list[str],
        time_from: datetime,
        time_to: datetime,
        limit: int,
    ) -> dict[str, list[str]]:
        if limit <= 0 or not breakdown_columns:
            return {column: [] for column in breakdown_columns}

        self._ensure_column_types(base_query)
        where_clause = self._time_window_where_clause(time_column, time_from, time_to)
        cols = [self._validate_breakdown_field(c) for c in breakdown_columns]

        prepared = [
            f"{self._field_value_expression(c)} AS `__bd_raw_{i}`" for i, c in enumerate(cols)
        ]
        grouping_sets = ", ".join(f"(`__bd_raw_{i}`)" for i in range(len(cols)))
        label_branches = " ".join(
            f"WHEN GROUPING(`__bd_raw_{i}`) = 0 THEN {self._quote_string(c)}"
            for i, c in enumerate(cols)
        )
        value_branches = " ".join(
            f"WHEN GROUPING(`__bd_raw_{i}`) = 0 THEN `__bd_raw_{i}`" for i in range(len(cols))
        )

        sql = (
            "SELECT _breakdown_column, _breakdown_value FROM ("
            "SELECT _breakdown_column, _breakdown_value, "
            # _breakdown_value is the tie-break the BaseAdapter top-N contract
            # requires: ranked by count alone, two equally-counted values at the
            # `rn <= limit` cut could swap places between runs over the same
            # window. GoogleSQL's default collation for STRING is binary, so a
            # bare ascending sort already IS the code-point order the contract
            # names; BigQuery has no "C" collation to spell it with.
            "ROW_NUMBER() OVER (PARTITION BY _breakdown_column "
            "ORDER BY _cnt DESC, _breakdown_value) AS rn "
            "FROM ("
            "SELECT "
            f"CASE {label_branches} ELSE '' END AS _breakdown_column, "
            f"CASE {value_branches} ELSE '' END AS _breakdown_value, "
            "COUNT(*) AS _cnt "
            "FROM ("
            f"SELECT {', '.join(prepared)} "
            f"FROM ({base_query}) AS _src{where_clause}"
            ") AS _prepared "
            f"GROUP BY GROUPING SETS ({grouping_sets})"
            ") AS _scored"
            ") AS _ranked "
            f"WHERE rn <= {int(limit)}"
        )
        logger.info("BQ breakdown top-values query: %s", sql)
        top: dict[str, list[str]] = {c: [] for c in cols}
        _, rows = self._query_rows(sql)
        for column, value in rows:
            top.setdefault(str(column), []).append(str(value))
        return top

    def get_time_bucketed_breakdown_counts_multi(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        breakdown_columns: list[str],
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None,
        time_from: datetime,
        time_to: datetime,
        values_limit: int | None = None,
        limit: int = 100000,
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        if not breakdown_columns:
            return [], [], []

        self._ensure_column_types(base_query)
        bucket_expr = self._bucket_expression(time_column, interval)
        where_clause = self._time_window_where_clause(time_column, time_from, time_to)
        reg_cols = [self._validate_column(c) for c in regular_columns]
        json_cols = [self._validate_column(c) for c in json_columns]
        breakdown_cols = [self._validate_breakdown_field(c) for c in breakdown_columns]
        invalid = [c for c in breakdown_cols if not is_breakdown_field_of(c, reg_cols, json_cols)]
        if invalid:
            msg = (
                "Breakdown columns must be scalar columns or properties of a JSON column: "
                f"{', '.join(invalid)}"
            )
            raise ValueError(msg)

        json_value_paths = json_value_paths or {}
        top_values_by_column: dict[str, list[str]] | None = None
        if values_limit is not None:
            top_count = max(values_limit - 1, 0)
            top_values_by_column = self._top_breakdown_values_multi(
                base_query,
                time_column,
                breakdown_cols,
                time_from,
                time_to,
                top_count,
            )

        prepared_parts: list[str] = [f"{bucket_expr} AS _bucket"]
        col_names: list[str] = []
        json_value_names: list[str] = []
        json_value_aliases: list[str] = []
        for c in reg_cols:
            # The outer GROUPING SETS groups by the prepared *alias*, so the alias must
            # already carry a groupable scalar — a REPEATED column is rendered to its
            # JSON text here, exactly as in the flat paths.
            _, group_sql = self._regular_column_sql(c)
            prepared_parts.append(f"{group_sql} AS `{c}`")
            col_names.append(c)
        for c in json_cols:
            prepared_parts.append(f"{self._json_paths_expression(c)} AS `{c}`")
            col_names.append(c)
        for c in json_cols:
            for path in json_value_paths.get(c, []):
                full_path = f"{c}.{path}"
                alias = f"__nv_{len(json_value_names)}"
                prepared_parts.append(
                    f"TO_JSON_STRING({self._json_path_expression(c, path)}) AS `{alias}`"
                )
                json_value_names.append(full_path)
                json_value_aliases.append(alias)

        grouping_columns = [f"`{name}`" for name in [*reg_cols, *json_cols, *json_value_aliases]]
        label_when: list[str] = []
        value_when: list[str] = []
        other_when: list[str] = []
        grouping_sets: list[str] = []

        for idx, column in enumerate(breakdown_cols):
            raw_expr = self._field_value_expression(column)
            value_alias = f"__bd_value_{idx}"
            other_alias = f"__bd_other_{idx}"
            top_values = (
                None if top_values_by_column is None else top_values_by_column.get(column, [])
            )
            if top_values is None:
                breakdown_expr = raw_expr
                is_other_expr = "0"
            elif top_values:
                quoted = ", ".join(self._quote_string(v) for v in top_values)
                in_clause = f"{raw_expr} IN ({quoted})"
                breakdown_expr = f"CASE WHEN {in_clause} THEN {raw_expr} ELSE 'Other' END"
                is_other_expr = f"CASE WHEN {in_clause} THEN 0 ELSE 1 END"
            else:
                breakdown_expr = "'Other'"
                is_other_expr = "1"

            prepared_parts.append(f"{breakdown_expr} AS `{value_alias}`")
            prepared_parts.append(f"{is_other_expr} AS `{other_alias}`")
            grouping_check = f"GROUPING(`{value_alias}`) = 0"
            label_when.append(f"WHEN {grouping_check} THEN {self._quote_string(column)}")
            value_when.append(f"WHEN {grouping_check} THEN CAST(`{value_alias}` AS STRING)")
            other_when.append(f"WHEN {grouping_check} THEN `{other_alias}`")
            grouping_sets.append(
                "("
                + ", ".join(
                    [
                        "_bucket",
                        f"`{value_alias}`",
                        f"`{other_alias}`",
                        *grouping_columns,
                    ]
                )
                + ")"
            )

        select_parts: list[str] = [
            "_bucket",
            f"CASE {' '.join(label_when)} ELSE '' END AS _breakdown_column",
            f"CASE {' '.join(value_when)} ELSE '' END AS _breakdown_value",
            f"CASE {' '.join(other_when)} ELSE 0 END AS _is_other",
            *grouping_columns,
            "COUNT(*) AS _cnt",
        ]

        sql = (
            f"SELECT {', '.join(select_parts)} "
            "FROM ("
            f"SELECT {', '.join(prepared_parts)} "
            f"FROM ({base_query}) AS _src{where_clause}"
            ") AS _prepared "
            f"GROUP BY GROUPING SETS ({', '.join(grouping_sets)}) "
            "ORDER BY _bucket, _breakdown_column, _breakdown_value "
            f"LIMIT {int(limit)}"
        )

        logger.info(
            "BQ bucketed breakdown GROUPING SETS query for %s: %s",
            ", ".join(breakdown_cols),
            sql,
        )
        t0 = time.monotonic()
        _, rows = self._query_rows(sql)
        elapsed = time.monotonic() - t0
        logger.info("BQ bucketed breakdown done in %.2fs, %s rows", elapsed, len(rows))

        # Row layout leads with _bucket, _breakdown_column, _breakdown_value, _is_other.
        decoded = self._decode_rows(rows, offset=4, reg_cols=reg_cols, json_cols=json_cols)
        return col_names, json_value_names, self._utc_bucket_rows(decoded)
