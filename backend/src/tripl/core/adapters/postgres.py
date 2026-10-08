from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from typing import override

import psycopg

from tripl.core.adapters.base import (
    FIELD_CONTRACT_EXPECTATIONS_PER_QUERY,
    AggregateSpec,
    BaseAdapter,
    ColumnInfo,
    FieldContractExpectation,
    FieldContractViolation,
    SchemaColumn,
    SchemaTable,
    contract_bound_literal,
    field_contract_verdict,
    is_breakdown_field_of,
)
from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.core.adapters.measure_validator import (
    build_aggregate_sql,
    coerce_aggregation,
    validate_measure_column,
)
from tripl.core.bucketing import EPOCH, WEEK_ORIGIN, format_utc_literal
from tripl.core.intervals import IntervalUnit, get_interval
from tripl.json_paths import split_property_field
from tripl.models.domain_enums import MetricAggregation

logger = logging.getLogger(__name__)

# Hard cap on catalog rows pulled for SQL-editor autocomplete so a schema with
# thousands of wide tables can't blow up the response. The cap is generous
# because introspection now spans every non-system schema (one row per column
# per table across all of them), so a multi-schema database needs plenty of
# headroom before its visible tables get truncated.
_SCHEMA_ROW_LIMIT = 50000

# System schemas that hold Postgres internals, not user data. They are excluded
# from catalog introspection so autocomplete only surfaces queryable user tables.
_SYSTEM_SCHEMAS = ("pg_catalog", "information_schema")

# Cap on how much user-supplied SQL (which may embed warehouse credentials or
# PII column names) we ever emit to logs. Query logs go to DEBUG so the full
# statement never lands at INFO.
_SQL_LOG_MAX_CHARS = 300


def _truncate_sql(sql: str) -> str:
    return sql[:_SQL_LOG_MAX_CHARS] + ("..." if len(sql) > _SQL_LOG_MAX_CHARS else "")


# date_bin() — which every PostgreSQL bucket expression depends on — was added in
# PostgreSQL 14. libpq reports the server version as MMmmmm (140005 == 14.5), so
# this is the integer floor of a supported server. The dialect subclasses that
# bucket without date_bin (``greenplum``, ``redshift``) set their own floor.
_MIN_SERVER_VERSION = 140000

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]*$")
_IDENTIFIER_PART_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _quote_ident(name: str) -> str:
    # Identifiers are pre-validated by _validate_column; this just adds the quoting.
    return '"' + name.replace('"', '""') + '"'


def _is_local_host(host: str) -> bool:
    return host in {"localhost", "127.0.0.1", "::1", ""}


def _format_server_version(version: int) -> str:
    """Render libpq's packed server version (140005) as a human "14.5"."""
    return f"{version // 10000}.{version % 10000}"


# --- connection settings -----------------------------------------------------

#: The connection parameters this adapter understands. Anything else is REJECTED
#: rather than swallowed by ``**kwargs``: a typo'd or inapplicable option that is
#: silently dropped looks configured but isn't, which for a TLS option means the
#: operator believes the link is encrypted when it is not.
_SUPPORTED_PARAMS = (
    "hostaddr",
    "timeout_seconds",
    "sslmode",
    "sslrootcert",
    "sslcert",
    "sslkey",
    "search_path",
)

#: libpq's TLS modes, weakest first. A closed allowlist: an unrecognized value is
#: rejected, never passed through to libpq (which would fall back to its own
#: default) and never dropped (which would silently downgrade the connection).
_SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")

#: Modes that check the server certificate against a CA, and so need one.
_SSL_VERIFYING_MODES = frozenset({"verify-ca", "verify-full"})

#: Default when the data source does not pin a mode. A remote host gets ``require``,
#: not libpq's ``prefer``: ``prefer`` negotiates TLS if the server offers it and
#: silently falls back to PLAINTEXT if it doesn't, so it guarantees nothing and a
#: stripped connection is indistinguishable from a healthy one. ``require`` fails
#: loudly instead; an operator whose server genuinely has no TLS can still ask for
#: ``prefer``/``disable`` explicitly, which is a decision on the record rather than
#: a default nobody chose. localhost keeps ``prefer`` — dev and docker Postgres
#: usually have no certificate at all, and the traffic never leaves the machine.
_DEFAULT_REMOTE_SSLMODE = "require"
_DEFAULT_LOCAL_SSLMODE = "prefer"

#: We are handed certificate/key PEM *content* (it lives in an encrypted DB column),
#: while libpq only takes filesystem paths. Anything without a PEM header is a
#: mistake worth shouting about — most likely a path, which we must not silently
#: treat as a certificate.
_PEM_HEADER = "-----BEGIN"

#: libpq's key-file permission check: a private key readable by group or other is
#: REFUSED outright ("private key file has group or world access"). The temp dir is
#: 0700 and every file in it 0600, so the check passes and the key is never exposed
#: to another user on the host.
_TLS_DIR_MODE = 0o700
_TLS_FILE_MODE = 0o600


@dataclass(frozen=True)
class _TlsFiles:
    """Materialized TLS material: a private temp dir and the libpq paths into it."""

    directory: str | None
    paths: dict[str, str]


def _resolve_sslmode(host: str, sslmode: str | None) -> str:
    if sslmode is None:
        return _DEFAULT_LOCAL_SSLMODE if _is_local_host(host) else _DEFAULT_REMOTE_SSLMODE
    if sslmode not in _SSL_MODES:
        msg = f"Unsupported sslmode: {sslmode!r}. Supported modes are: {', '.join(_SSL_MODES)}."
        raise WarehouseCapabilityError(msg)
    return sslmode


def _tls_pem_material(
    *,
    sslmode: str,
    sslrootcert: str | None,
    sslcert: str | None,
    sslkey: str | None,
) -> dict[str, str]:
    """Validate the TLS inputs and return the PEMs to materialize, by libpq name.

    Rejects the combinations libpq would either ignore or die on much later:
    certificate material handed to a connection that will never negotiate TLS, a
    verifying mode with no CA to verify against, and half of a client-certificate
    pair. All three are "inapplicable parameters", and silently accepting any of
    them means the connection is not what the configuration says it is.
    """
    pems = {"sslrootcert": sslrootcert, "sslcert": sslcert, "sslkey": sslkey}
    provided = {name: pem for name, pem in pems.items() if pem}

    if sslmode == "disable" and provided:
        msg = (
            f"sslmode=disable never negotiates TLS, so {', '.join(sorted(provided))} "
            "cannot be applied. Remove the certificate material or raise sslmode."
        )
        raise WarehouseCapabilityError(msg)
    if sslmode in _SSL_VERIFYING_MODES and not sslrootcert:
        msg = f"sslmode={sslmode} verifies the server certificate but no sslrootcert was given."
        raise WarehouseCapabilityError(msg)
    if bool(sslcert) != bool(sslkey):
        msg = "Client certificate authentication needs both sslcert and sslkey, not one of them."
        raise WarehouseCapabilityError(msg)
    for name, pem in provided.items():
        if _PEM_HEADER not in pem:
            # Never echo the value: sslkey is a private key.
            msg = f"{name} must be PEM content (a '{_PEM_HEADER}...' block), not a file path."
            raise WarehouseCapabilityError(msg)
    return provided


def _materialize_tls_files(pems: dict[str, str]) -> _TlsFiles:
    """Write PEM content to a private 0600 temp file per entry, for libpq to read.

    The directory is created 0700 and the files 0600 *explicitly* (``os.open``'s
    mode is masked by the process umask, so it is not on its own a guarantee).
    Callers own the cleanup: :meth:`PostgresAdapter.close` removes the directory,
    and a failed connect removes it too, so a private key never outlives the
    connection it was created for.
    """
    if not pems:
        return _TlsFiles(None, {})
    directory = tempfile.mkdtemp(prefix="tripl-pg-tls-")
    os.chmod(directory, _TLS_DIR_MODE)
    paths: dict[str, str] = {}
    try:
        for name, pem in pems.items():
            path = os.path.join(directory, f"{name}.pem")
            handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _TLS_FILE_MODE)
            with os.fdopen(handle, "w") as file:
                file.write(pem if pem.endswith("\n") else pem + "\n")
            os.chmod(path, _TLS_FILE_MODE)
            paths[name] = path
    except OSError:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return _TlsFiles(directory, paths)


#: A schema name in a ``search_path``. Deliberately the same shape as
#: ``schemas.data_source._SEARCH_PATH_RE``, which validates the value on the way IN:
#: a legal Postgres identifier may contain ``$`` after the first character, and the
#: two layers must accept exactly the same set or a config the API took would be
#: rejected at connect time. A leading ``$`` is still out, so ``$user`` cannot slip
#: through either.
_SEARCH_PATH_PART_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_$]*$")


def _validated_search_path(search_path: str) -> str:
    """Check a ``search_path`` is a plain identifier list before it is interpolated.

    ``search_path`` is a list of *identifiers*, not a literal, so it cannot be bound
    as a parameter and has to be interpolated into the libpq startup options — which
    makes it an injection vector unless it is checked. Only bare identifiers are
    allowed (no quoting, no ``$user``, no whitespace), which is also what keeps the
    space-separated ``options`` string unambiguous.
    """
    parts = [part.strip() for part in search_path.split(",")]
    if not parts or any(not part for part in parts):
        msg = f"Invalid search_path: {search_path!r}"
        raise WarehouseCapabilityError(msg)
    invalid = [part for part in parts if not _SEARCH_PATH_PART_RE.match(part)]
    if invalid:
        msg = (
            f"Invalid search_path schema name(s): {', '.join(repr(part) for part in invalid)}. "
            "search_path takes a comma-separated list of plain identifiers."
        )
        raise WarehouseCapabilityError(msg)
    return ",".join(parts)


# --- field contracts ---------------------------------------------------------

#: A decimal or scientific-notation number of BOUNDED magnitude, and nothing else.
#: Guards the ``::numeric`` cast in the range-contract SQL: Postgres RAISES on a bad
#: cast rather than yielding NULL the way ClickHouse's ``toFloat64OrNull`` does, and
#: one malformed value in one row would abort the whole contract query. The CASE only
#: casts what already matched, so a malformed value falls out as NULL — i.e. as BAD.
#:
#: The bounds on the digit runs are the other half of the guard, not tidiness.
#: Syntax alone is not enough: ``1e999999999`` is a perfectly well-formed number
#: that no Postgres numeric type can hold, so an unbounded pattern still let the
#: cast raise. ``numeric`` holds at most 131072 digits before and 16383 after the
#: decimal point. POSIX ARE bounds (``{m,n}``) only accept counts from 0 to 255, so
#: a wider run is spelled as consecutive runs of at most 255 each.
#:
#: The widths are those of the float8 domain the OTHER engines parse in, which is
#: what this guard is standing in for: a double prints with at
#: most 309 integer digits (``1.8e308``) and at most 1074 fractional places (the
#: smallest subnormal, ``5e-324``, written out in full). So the integer run takes
#: up to 510 digits and the fractional run up to 1275. A single 255-digit cap,
#: as this used to be, turned a 256-digit number — which Python's ``float`` and
#: ClickHouse's ``toFloat64OrNull`` both parse — into BAD on PostgreSQL alone.
#: With the four-digit exponent that caps
#: what reaches the cast at 10^10509 with at most 11274 fractional places, inside
#: both ``numeric`` limits.
#:
#: Residual, by design: a literal with more digits than that, or a five-digit
#: exponent, matches no arm of the CASE, stays NULL and is reported BAD by the
#: ``COALESCE(NOT (...), TRUE)`` below. That is the verdict the guard already gives
#: ``'twelve'``, and it never raises — which is the property that matters here.
#:
#: Spelled as up to N exact 255-digit blocks and then one ``{m,255}`` tail rather
#: than as N adjacent ``{0,255}`` runs: the adjacent form is ambiguous about where
#: one run ends, which costs PostgreSQL's DFA nothing but sends a backtracking
#: engine (Python's ``re``, which the tests match it with) exponential on a long
#: non-matching input.
_INTEGER_DIGITS = "([0-9]{255})?[0-9]{1,255}"
_FRACTION_DIGITS = "([0-9]{255}){0,4}[0-9]{0,255}"
_LEADING_FRACTION_DIGITS = "([0-9]{255}){0,4}[0-9]{1,255}"
_FINITE_NUMBER_RE = (
    rf"^[+-]?({_INTEGER_DIGITS}(\.{_FRACTION_DIGITS})?|\.{_LEADING_FRACTION_DIGITS})"
    r"([eE][+-]?[0-9]{1,4})?$"
)

#: The non-finite spellings ``float()`` and ``toFloat64OrNull`` both accept, matched
#: case-insensitively (``~*``). They are handled apart from the cast because
#: PostgreSQL orders NaN as GREATER THAN every other number (this is true of
#: ``numeric`` exactly as it was of ``double precision``), while Python and
#: ClickHouse make every NaN comparison false — so a NaN routed through the cast
#: would report as "above max" on Postgres and as "in range" everywhere else.
#: Keeping NaN out of the comparison entirely is what makes the three agree.
_POSITIVE_INF_RE = r"^[+]?inf(inity)?$"
_NEGATIVE_INF_RE = r"^[-]inf(inity)?$"
_NAN_RE = r"^[+-]?nan$"


def _jsonb_object(value_sql: str) -> str:
    """Coerce a json/jsonb expression to a jsonb *object*, or an empty object.

    ``json`` has to be cast to ``jsonb`` before any of the jsonb operators work,
    and a SQL NULL, a JSON scalar or a JSON array at the root has no keys to walk
    — all three collapse to ``{}`` so the path walk yields no rows instead of
    erroring or returning NULL.
    """
    return (
        f"CASE WHEN jsonb_typeof({value_sql}::jsonb) = 'object' "
        f"THEN {value_sql}::jsonb ELSE '{{}}'::jsonb END"
    )


#: Recursive descent over a jsonb document, emitting one row per *leaf*: a dotted
#: path and the jsonb value at it. Objects are the only thing recursed into, so an
#: array or a JSON ``null`` is a leaf at its own path (never indexed or dropped),
#: which is what ClickHouse's ``JSONAllPaths`` reports for the same document.
#: The caller supplies the seed relation as ``{seed}`` (any relation with a single
#: jsonb-object column ``_doc``).
_JSON_LEAF_WALK = (
    "_walk(_path, _value) AS ("
    "SELECT _kv.key, _kv.value FROM {seed} AS _seed, LATERAL jsonb_each(_seed._doc) AS _kv "
    "UNION ALL "
    "SELECT _walk._path || '.' || _kv.key, _kv.value "
    "FROM _walk, LATERAL jsonb_each(_walk._value) AS _kv "
    "WHERE jsonb_typeof(_walk._value) = 'object'"
    ")"
)


def epoch_bucket_expression(time_sql: str, interval_code: str) -> str:
    """``floor_to_bucket`` in epoch arithmetic, for servers without ``date_bin``.

    The same grid ``date_bin`` draws: whole multiples of the interval measured from
    ``EPOCH``, or from ``WEEK_ORIGIN`` for a week. ``date_part('epoch', ...)`` reads
    a ``timestamp`` column's wall clock as UTC — the session timezone, which every
    adapter of this family pins — so naive and aware columns land on the same grid.
    The offset is floored, not truncated, so an instant before the origin falls into
    the bucket that starts before it, as ``floor_to_bucket`` says.

    Every interval is a whole number of seconds (``IntervalUnit`` stops at a week),
    which is what lets the bucket be an integer count of seconds after the origin.
    """
    spec = get_interval(interval_code)
    origin = WEEK_ORIGIN if spec.unit is IntervalUnit.week else EPOCH
    width = int(spec.delta.total_seconds())
    origin_seconds = int(origin.timestamp())
    offset = f"date_part('epoch', {time_sql}) - {origin_seconds}"
    return (
        f"(TIMESTAMPTZ '{format_utc_literal(origin)}' + "
        f"CAST(floor(({offset}) / {width}) * {width} AS BIGINT) * INTERVAL '1 second')"
    )


class PostgresAdapter(BaseAdapter):
    """Postgres-backed warehouse adapter mirroring the ClickHouse semantics.

    Maps ClickHouse-specific features to standard SQL:
      - toStartOfInterval → date_bin (PostgreSQL >= 14; see test_connection)
      - JSONAllPaths      → a recursive jsonb_each walk (_JSON_LEAF_WALK) that
                            enumerates full nested leaf paths ("user.address.city"),
                            not just the top-level keys jsonb_object_keys returns
      - GROUPING SETS     → same syntax (Postgres supports it natively)
      - multiIf           → CASE WHEN / THEN
      - LIMIT n BY col    → ROW_NUMBER() OVER (PARTITION BY ...) wrapper
      - countIf/anyIf     → count(*) / min(...) FILTER (WHERE ...)
      - toFloat64OrNull   → a regex-guarded ::double precision cast (Postgres RAISES
                            on a bad cast where ClickHouse returns NULL)

    Everything is UTC: the session timezone is pinned on connect and every window
    bound is an explicit-offset TIMESTAMPTZ literal, so neither a non-UTC server
    nor a non-UTC role can shift a window or a bucket. See
    :mod:`tripl.core.bucketing` for the contract.

    **Known dialect divergence — regex contracts.** ``regex_violation`` compiles the
    stored pattern with Postgres's ``~`` operator, which is POSIX ARE, while the
    Python fallback uses ``re.search`` and ClickHouse uses RE2. All three are
    unanchored partial matches, and all three agree on ordinary patterns (literals,
    character classes, anchors, ``|``, quantifiers, ``\\d``/``\\w``/``\\s``). They do
    NOT agree on everything, and we do not pretend otherwise:

    * ``\\b`` is a word boundary in Python; in POSIX ARE it is a BACKSPACE character
      (the word-boundary escape is ``\\y``). A ``\\b``-anchored pattern therefore
      matches nothing here and flags every row as bad.
    * ``$`` in Python also matches just before a trailing newline; in POSIX ARE it
      only matches at the very end of the string.
    * Python-style named groups (``(?P<name>...)``, ``(?<name>...)``) are not valid
      ARE. A pattern this server will not compile no longer reaches a statement at
      all: ``_probe_contract_regex`` offers it to the server first and the
      expectation is dropped if it is refused, which is the shared rule stated in
      the field contract section of ``BaseAdapter``. Before that, a rejected
      pattern failed the whole contract statement — every other expectation with
      it, and the collection around it — so this bullet used to end "a rejected
      pattern is not a per-expectation problem". It is one now.
    * Lookaround (``(?=``, ``(?!``, ``(?<=``, ``(?<!``) and backreferences are valid
      here — Postgres has had lookbehind constraints since 9.6 and this adapter
      requires 14+ — and valid in Python, but RE2 rejects all of them, so ClickHouse
      ``match()`` and BigQuery ``REGEXP_CONTAINS`` are the ones that refuse such a
      pattern, and refuse it through the same per-expectation probe. The
      divergences do not all point the same way; this one is the other engines'.

    A contract that sticks to portable regex syntax gets identical answers from all
    three warehouses; that is what the conformance gate pins.
    """

    #: Set only when TLS material had to be written to disk. A class-level default
    #: keeps ``close()`` safe on an adapter built with ``object.__new__`` (the unit
    #: tests do that to exercise SQL generation without a server).
    _tls_dir: str | None = None

    # --- dialect seams -------------------------------------------------------
    # Greenplum and Redshift speak this wire protocol and most of this SQL; what
    # differs is collected here and in the methods marked "dialect seam", so a
    # subclass overrides a named difference instead of copying a query.

    #: The engine's name in user-facing errors.
    engine_label = "PostgreSQL"
    #: libpq's packed ``server_version`` floor, checked by ``test_connection``.
    min_server_version = _MIN_SERVER_VERSION
    #: Why ``min_server_version`` is the floor, and what to upgrade to, for the
    #: refusal message.
    min_server_version_reason = "every time-bucket query uses date_bin(), which was added in"
    min_server_upgrade_target = "14"
    #: Schemas never offered for autocomplete.
    system_schemas: tuple[str, ...] = _SYSTEM_SCHEMAS
    #: The unbounded text type every value is rendered as for comparison.
    text_type = "text"
    #: The domain a range contract compares in, and the guard a value must match
    #: before it is cast there (see ``_contract_bad_condition``).
    contract_number_type = "numeric"
    contract_number_re = _FINITE_NUMBER_RE
    #: Whether the session settings go in libpq's startup ``options``; when not,
    #: they are applied with ``SET`` right after the connection opens.
    session_settings_at_startup = True

    def __init__(
        self,
        host: str,
        port: int,
        database: str,
        username: str = "",
        password: str = "",
        timeout_seconds: int | None = None,
        sslmode: str | None = None,
        sslrootcert: str | None = None,
        sslcert: str | None = None,
        sslkey: str | None = None,
        search_path: str | None = None,
        hostaddr: str | None = None,
        **kwargs: object,
    ) -> None:
        # An unknown connection parameter is a hard error, not something **kwargs
        # quietly eats. The alternative — accepting `sslmod=verify-full` and
        # connecting in plaintext anyway — is the failure mode this guard exists
        # to prevent ("rejected, not ignored").
        if kwargs:
            unknown = ", ".join(sorted(kwargs))
            msg = (
                f"Unsupported PostgreSQL connection parameter(s): {unknown}. "
                f"Supported: {', '.join(_SUPPORTED_PARAMS)}."
            )
            raise ValueError(msg)

        # connect_timeout bounds the TCP/handshake; statement_timeout (set
        # server-side via libpq options) caps every query so a runaway
        # base_query is cancelled by Postgres long before Celery's hard limit
        # SIGKILLs the worker. statement_timeout is in milliseconds.
        connect_timeout = timeout_seconds if timeout_seconds and timeout_seconds > 0 else None
        # timezone=UTC pins the *session*, not just our literals: an offset-less
        # timestamp column is compared and binned in the session timezone, so a
        # server or role whose TimeZone is, say, Europe/Berlin would otherwise
        # shift every window bound and every bucket edge by its UTC offset. Set
        # through the same libpq `options` channel as statement_timeout so there
        # is exactly one mechanism, applied before the first query runs.
        #
        # standard_conforming_strings=on pins what our own literals MEAN.
        # `_quote_string` escapes a value by doubling the quote and doing nothing
        # else, which is sound only while a backslash is an ordinary character;
        # under the legacy `off` a `\'` escapes the quote instead, so a warehouse
        # value ending in a backslash closes the string early. The values that
        # reach `_quote_string` include end-user event data (the top-N breakdown
        # values), the connection is autocommit, and psycopg sends these
        # parameterless statements over the simple query protocol, which runs
        # every statement in the string — so a literal that closes early is
        # statement injection that commits. The GUC has defaulted to on since
        # PostgreSQL 9.1, but a role or a database can still be ALTERed to off for
        # legacy compatibility, and no server-side default may be allowed to
        # change the meaning of SQL we generated.
        #
        # default_transaction_read_only=on is defence in depth and is deliberately
        # NOT described as the write barrier. With autocommit each statement is its
        # own implicit transaction, so the GUC applies to every one of them, and it
        # covers things no keyword blocklist can name — writes through a function
        # the user's base_query calls, sequence advancement, large-object writes.
        # It is USERSET, so SQL that can run `SET default_transaction_read_only =
        # off` undoes it: the actual barrier is the warehouse credential's own
        # privileges, which is the posture ClickHouse states for itself in
        # `ClickHouseAdapter.get_schema_tables`. Nothing of ours is affected — every
        # statement this adapter issues is a SELECT (grep `cur.execute` in this
        # file) and none of them writes a temp table.
        session_settings = self._session_settings(connect_timeout, search_path)
        options = (
            " ".join(f"-c {name}={value}" for name, value in session_settings)
            if self.session_settings_at_startup
            else None
        )

        mode = _resolve_sslmode(host, sslmode)
        tls = _materialize_tls_files(
            _tls_pem_material(
                sslmode=mode,
                sslrootcert=sslrootcert,
                sslcert=sslcert,
                sslkey=sslkey,
            )
        )
        self._tls_dir = tls.directory

        try:
            self._conn = psycopg.connect(
                host=host,
                # A vetted address (hosted): libpq connects here and still
                # verifies TLS against ``host``. None is dropped from the conninfo.
                hostaddr=hostaddr,
                port=port,
                dbname=database,
                user=username or "postgres",
                password=password or "",
                autocommit=True,
                connect_timeout=connect_timeout,
                # None (settings applied below instead) is dropped from the conninfo.
                options=options,
                sslmode=mode,
                # libpq reads the certificate material from disk; a None is dropped
                # from the conninfo rather than sent as an empty path.
                sslrootcert=tls.paths.get("sslrootcert"),
                sslcert=tls.paths.get("sslcert"),
                sslkey=tls.paths.get("sslkey"),
            )
            if not self.session_settings_at_startup:
                try:
                    with self._conn.cursor() as cur:
                        for name, value in session_settings:
                            cur.execute(f"SET {name} TO {value}")
                except BaseException:
                    # An open session missing a pin must not be kept either.
                    self._conn.close()
                    raise
        except BaseException:
            # A refused connection must not leave a private key on disk.
            self._remove_tls_files()
            raise
        self._allowed_columns: set[str] = set()
        self._type_names: dict[int, str] = {}

    def server_name(self, server_version: int) -> str:
        """How the refusal message names the server: ``PostgreSQL 13.23``."""
        return f"{self.engine_label} {_format_server_version(server_version)}"

    def _session_settings(
        self, statement_timeout_seconds: int | None, search_path: str | None
    ) -> list[tuple[str, str]]:
        """Dialect seam: the session settings pinned before the first query.

        Each value is a bare token — the validated search path included — so it
        is safe both in the space-separated startup ``options`` and after ``SET``.
        See ``__init__`` for why each one is pinned.
        """
        settings = [
            ("timezone", "UTC"),
            ("standard_conforming_strings", "on"),
            ("default_transaction_read_only", "on"),
        ]
        if statement_timeout_seconds is not None:
            settings.append(("statement_timeout", str(statement_timeout_seconds * 1000)))
        if search_path is not None:
            # Validated to a bare identifier list, so it can neither inject SQL nor
            # smuggle whitespace into the space-separated options string.
            settings.append(("search_path", _validated_search_path(search_path)))
        return settings

    def _remove_tls_files(self) -> None:
        if self._tls_dir is None:
            return
        shutil.rmtree(self._tls_dir, ignore_errors=True)
        self._tls_dir = None

    def close(self) -> None:
        try:
            self._conn.close()
        finally:
            # The certificate and key files exist only for this connection's
            # lifetime; the key in particular must not be left behind.
            self._remove_tls_files()

    def test_connection(self) -> bool:
        """Probe the server and refuse one too old to bucket time correctly.

        Every time-bucketed query goes through ``date_bin``, which only exists on
        PostgreSQL 14+. Without this check an older server fails much later, deep
        inside a scan or a metric collection, as an opaque "function date_bin(...)
        does not exist". The version comes from the libpq handshake, so the guard
        costs no extra round trip.
        """
        server_version = self._conn.info.server_version
        if server_version < self.min_server_version:
            minimum = _format_server_version(self.min_server_version).removesuffix(".0")
            msg = (
                f"{self.server_name(server_version)} is too old for tripl: "
                f"{self.min_server_version_reason} PostgreSQL {minimum}. "
                f"Upgrade the server to {self.min_server_upgrade_target} or newer."
            )
            raise WarehouseCapabilityError(msg)
        with self._conn.cursor() as cur:
            cur.execute("SELECT 1")
            row = cur.fetchone()
            return bool(row and row[0] == 1)

    def _type_name(self, oid: int) -> str:
        """Report a result column's Postgres type, distinguishing arrays.

        psycopg's type registry is keyed by BOTH a type's own oid and its array
        oid, and both lookups return the same ``TypeInfo`` — the ELEMENT's. So
        ``types.get(1007).name`` is ``'int4'``, not ``'int4[]'``, and reporting
        that name verbatim told every downstream classifier that an ``int4[]``
        column was a plain ``int4`` and a ``jsonb[]`` column was ``jsonb``. The
        jsonb case is the one that bites: the JSON path walk emits
        ``"items"::jsonb``, which Postgres refuses for a ``jsonb[]``, so the whole
        scan fails rather than treating the column as an opaque scalar.

        ``oid == info.array_oid`` is the discriminator. A lookup that found a
        ``TypeInfo`` matched either its element oid or its array oid, so equality
        with ``array_oid`` means "this is the array". Types with no array type
        report ``array_oid`` 0, and 0 is not a real oid, so that case cannot
        misfire. The cache stays keyed by oid — element and array are separate
        keys and cache independently.

        Preferred over ``SELECT format_type(oid, NULL)``: that renames every
        SCALAR too (``int4`` -> ``integer``, ``timestamptz`` -> ``timestamp with
        time zone``), which would churn stored ``observed_type`` values and
        ``FactTableColumn.native_type`` for columns that were never broken, and
        it costs a round trip per unseen oid. This costs nothing and moves only
        the names that were wrong.
        """
        cached = self._type_names.get(oid)
        if cached is not None:
            return cached
        info = self._conn.adapters.types.get(oid)
        if info is None:
            name = f"oid_{oid}"
        elif oid == info.array_oid:
            name = f"{info.name}[]"
        else:
            name = info.name
        self._type_names[oid] = name
        return name

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT * FROM ({base_query}) AS _src LIMIT 0")
            columns: list[ColumnInfo] = []
            for desc in cur.description or []:
                type_name = self._type_name(desc.type_code)
                # Postgres cursor descriptions don't expose nullability — be
                # conservative and assume nullable; the analyzer side rechecks.
                columns.append(ColumnInfo(name=desc.name, type_name=type_name, is_nullable=True))
        self._allowed_columns = {c.name for c in columns}
        return columns

    def get_schema_tables(self) -> list[SchemaTable]:
        # Introspect every non-system schema, not just the search_path: on real
        # Postgres the user's tables often live in a schema outside the connection
        # search_path, or they reference `schema.table` across schemas.
        # `table_schema = current_schema()` is computed server-side so the
        # "bare vs qualified" decision tracks the connection's default schema
        # (the first existing entry of search_path, typically `public`).
        sql = self._schema_columns_sql()
        logger.debug("PG schema introspection query: %s", _truncate_sql(sql))
        with self._conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        # Tables in the default schema keep their bare name (`events`); tables in
        # any other schema are qualified as `schema.table` (`analytics.orders`).
        # The frontend relies on this: a table name has at most one dot, and only
        # when it lives outside the default schema.
        columns_by_table: dict[str, list[SchemaColumn]] = {}
        for table_schema, table_name, column_name, data_type, is_current_schema in rows:
            qualified_name = (
                str(table_name) if is_current_schema else f"{table_schema}.{table_name}"
            )
            columns_by_table.setdefault(qualified_name, []).append(
                SchemaColumn(name=str(column_name), data_type=str(data_type))
            )
        return [
            SchemaTable(name=table, columns=columns) for table, columns in columns_by_table.items()
        ]

    def _schema_columns_sql(self) -> str:
        """Dialect seam: one row per user column, ``(schema, table, column, type,
        is_current_schema)``, ordered by schema, table and position."""
        excluded = ", ".join(self._quote_string(name) for name in self.system_schemas)
        return (
            "SELECT table_schema, table_name, column_name, data_type, "
            "(table_schema = current_schema()) AS is_current_schema "
            "FROM information_schema.columns "
            f"WHERE table_schema NOT IN ({excluded}) "
            f"ORDER BY table_schema, table_name, ordinal_position LIMIT {_SCHEMA_ROW_LIMIT}"
        )

    def get_preview_rows(
        self,
        base_query: str,
        limit: int = 10,
        *,
        time_column: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        where_clause = self._time_window_where_clause(time_column, time_from, time_to)
        sql = f"SELECT * FROM ({base_query}) AS _src{where_clause} LIMIT {int(limit)}"
        logger.debug("PG preview query: %s", _truncate_sql(sql))
        with self._conn.cursor() as cur:
            cur.execute(sql)
            names = [d.name for d in cur.description or []]
            rows = [tuple(r) for r in cur.fetchall()]
        return names, rows

    def _validate_column(self, column: str) -> str:
        if not _IDENTIFIER_RE.match(column):
            msg = f"Invalid column name: {column}"
            raise ValueError(msg)
        if self._allowed_columns and column not in self._allowed_columns:
            msg = f"Column {column!r} not found in query result"
            raise ValueError(msg)
        return column

    def _bucket_expression(self, time_column: str, interval_code: str) -> str:
        """Dialect seam: translate an interval code into PostgreSQL bucket SQL.

        Must agree with ``tripl.core.bucketing.floor_to_bucket``. ``date_bin``
        measures from whatever origin it is handed, so the origin is what encodes
        the contract: sub-week intervals bin from the epoch, and a week bins from
        ``WEEK_ORIGIN`` (the first Monday) rather than from the epoch itself, which
        was a Thursday. The origin literal carries an explicit ``+00:00`` offset so
        a session running in a non-UTC ``TimeZone`` cannot slide the grid.
        """
        spec = get_interval(interval_code)
        col = _quote_ident(self._validate_column(time_column))
        if spec.unit is IntervalUnit.week:
            origin = format_utc_literal(WEEK_ORIGIN)
            return f"date_bin(INTERVAL '{7 * spec.count} days', {col}, TIMESTAMPTZ '{origin}')"
        origin = format_utc_literal(EPOCH)
        return (
            f"date_bin(INTERVAL '{spec.count} {spec.unit.value}s', {col}, TIMESTAMPTZ '{origin}')"
        )

    def _json_path_expression(self, column: str, path: str) -> str:
        parts = [part for part in path.split(".") if part]
        if not parts:
            raise ValueError(f"Invalid JSON path: {path}")
        if any(not _IDENTIFIER_PART_RE.match(part) for part in parts):
            raise ValueError(f"Unsupported JSON path: {path}")

        expr = _quote_ident(self._validate_column(column))
        for part in parts[:-1]:
            expr += f" -> '{part}'"
        # Last step uses ->> only if we want text; for JSONB-string emission we
        # keep -> and cast to text.
        expr += f" -> '{parts[-1]}'"
        return expr

    def _string_value_expression(self, column: str) -> str:
        return f"COALESCE({_quote_ident(self._validate_column(column))}::{self.text_type}, '')"

    def _property_value_expression(self, column: str, path: str) -> str:
        """One property (a JSON path of a json/jsonb column) as nullable text.

        ``#>>`` with a ``text[]`` path: the scalar unquoted, a nested value as
        JSON text, and SQL NULL for an absent path or a JSON null — so a
        property behaves like a nullable scalar column in breakdowns and
        contracts (F23, #306). ``#>>`` is defined for both ``json`` and
        ``jsonb``, so no cast is needed. Every segment is validated by
        ``_json_path_expression``'s grammar and quoted with ``_quote_string``.
        """
        parts = [part for part in path.split(".") if part]
        if not parts or any(not _IDENTIFIER_PART_RE.match(part) for part in parts):
            raise ValueError(f"Unsupported JSON path: {path}")
        quoted = _quote_ident(self._validate_column(column))
        path_array = ", ".join(self._quote_string(part) for part in parts)
        return f"({quoted} #>> ARRAY[{path_array}]::text[])"

    def _field_operand(self, field: str) -> str:
        """A breakdown / drift / contract field as a nullable SQL operand.

        A scalar column is its quoted name, exactly as before; a property entry
        (``<json_column>.<path>``) is its extracted text.
        """
        prop = split_property_field(field)
        if prop is None:
            return _quote_ident(self._validate_column(field))
        return self._property_value_expression(*prop)

    def _field_value_expression(self, field: str) -> str:
        """``_string_value_expression`` for a scalar column or a property entry."""
        if split_property_field(field) is None:
            return self._string_value_expression(field)
        return f"COALESCE({self._field_operand(field)}, '')"

    def _validate_breakdown_field(self, field: str) -> str:
        """Validate a breakdown entry: a column, or a property of a known column."""
        prop = split_property_field(field)
        if prop is None:
            return self._validate_column(field)
        self._validate_column(prop[0])
        return field

    def _quote_string(self, value: str) -> str:
        """Dialect seam: a string literal. Doubling the quote is the whole escape
        only because ``standard_conforming_strings`` is pinned on (``__init__``)."""
        return "'" + value.replace("'", "''") + "'"

    def _count_where(self, condition: str) -> str:
        """Dialect seam: the number of rows matching ``condition``."""
        return f"count(*) FILTER (WHERE {condition})"

    def _aggregate_where(
        self, aggregation: MetricAggregation, measure_sql: str | None, condition: str
    ) -> str:
        """Dialect seam: ``aggregation`` over only the rows matching ``condition``.

        ``FILTER (WHERE ...)`` applies to ``count(*)`` and ``count(DISTINCT m)``
        alike, so no aggregate needs the ``CASE`` folding a dialect without it does.
        """
        return f"{build_aggregate_sql(aggregation, measure_sql)} FILTER (WHERE {condition})"

    def _byte_ordered(self, expression: str) -> str:
        """Dialect seam: ``expression`` sorted in byte (code-point) order."""
        return f'{expression} COLLATE "C"'

    def _time_window_condition(
        self,
        time_column: str,
        time_from: datetime,
        time_to: datetime,
    ) -> str:
        """The half-open ``time_from <= t < time_to`` predicate, in UTC.

        The one place a window bound is rendered. Bounds go through
        ``format_utc_literal`` and are emitted as ``TIMESTAMPTZ`` with an explicit
        ``+00:00``: an offset-less literal (what ``strftime`` used to produce) is
        read in the *session* timezone, silently sliding the whole window on a
        non-UTC server. A ``timestamp``-typed column is promoted to timestamptz for
        the comparison using the session timezone, which __init__ pins to UTC, so
        naive and aware columns agree.
        """
        quoted = _quote_ident(self._validate_column(time_column))
        return (
            f"{quoted} >= TIMESTAMPTZ '{format_utc_literal(time_from)}' "
            f"AND {quoted} < TIMESTAMPTZ '{format_utc_literal(time_to)}'"
        )

    def _time_window_where_clause(
        self,
        time_column: str | None,
        time_from: datetime | None,
        time_to: datetime | None,
    ) -> str:
        """The optional-window variant: a leading-space WHERE clause, or ``""``."""
        if time_column is None or time_from is None or time_to is None:
            return ""
        return f" WHERE {self._time_window_condition(time_column, time_from, time_to)}"

    def _json_paths_expression(self, column: str) -> str:
        """Sorted text[] of the document's TOP-LEVEL keys, for one row.

        This is the *scan* path: it is evaluated once per row across the whole
        window, so its per-row cost is multiplied by every row scanned.

        It deliberately does NOT enumerate nested leaf paths, unlike
        :meth:`get_json_path_samples` below. ClickHouse's ``JSONAllPaths`` returns
        full nested paths essentially for free — it is a columnar metadata read —
        but PostgreSQL has no such primitive, and the recursive ``jsonb_each`` walk
        that produces the same answer costs **~44 microseconds per row**, measured on
        PostgreSQL 18 over a 30k-row fixture with a 4-level document::

            scalar columns only ...     16 ms   (the floor)
            top-level keys (this) ..    245 ms
            nested leaf paths .....    1744 ms  (7x this, 108x the floor)

        ``EXPLAIN`` shows both why and that it is irreducible: the recursive CTE runs
        with ``loops=30000`` — once per row — and rewriting it as a LATERAL join
        (1847 ms) or as an unrolled depth-limited expansion (2976 ms) is no better.
        The walk *alone*, with no grouping at all, already costs 1320 ms. On a 10M-row
        window that is minutes of CPU spent purely on parsing paths.

        So nested paths live where they are both needed and affordable:
        ``get_json_path_samples``, which is bounded by ``sample_row_limit`` (~44 ms)
        and is what the UI uses to show which paths exist and let the user pick one.
        Scan-time grouping only has to tell document *shapes* apart, which top-level
        keys already do.

        The consequence — a coarser "distinct shapes" count on PostgreSQL than on
        ClickHouse — is a real divergence, and is documented as one in the capability
        matrix with these numbers rather than passed off as parity.

        Semantics are unchanged from the nested form except for depth. In particular a
        **SQL NULL** column, or a JSON scalar/array at the *root*, has no keys and
        still yields ``ARRAY[]::text[]`` — an empty array, never NULL, so it groups as
        a value like any other.
        """
        c = _quote_ident(self._validate_column(column))
        return (
            "(SELECT COALESCE(array_agg(_key ORDER BY _key), ARRAY[]::text[]) "
            f"FROM jsonb_object_keys({_jsonb_object(c)}) AS _key)"
        )

    json_path_samples_are_text = True

    @override
    def get_json_path_samples(
        self,
        base_query: str,
        json_columns: list[str],
        *,
        time_column: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
        path_limit: int = 1000,
        sample_limit: int = 3,
        sample_row_limit: int = 1000,
        include_objects: bool = False,
    ) -> dict[str, dict[str, list[object]]]:
        """Discover nested JSON paths (and samples) warehouse-side, not row-side.

        Replaces BaseAdapter's fallback, which pulled whole rows back and flattened
        them in Python. One query per JSON column walks the document server-side,
        so the UI sees path candidates from a much wider row sample at a fraction of
        the transfer. The walk is bounded by ``sample_row_limit`` source rows
        (recursion aside, a GROUP BY would otherwise scan the whole source), by
        ``path_limit`` distinct paths and by ``sample_limit`` values per path.

        Samples come back as JSON text (``"foo"``, ``42``, ``{"a":1}``), matching
        ClickHouse's ``toJSONString`` output, which the shared
        ``decode_json_path_value`` helper parses. Paths whose keys the extraction
        expression cannot address are skipped visibly (logged), not silently
        emitted and then broken at scan time.

        ``include_objects`` keeps the walk's non-empty object nodes too, each
        one row's whole object at its own path (see ``BaseAdapter``).
        """
        if not json_columns or path_limit <= 0 or sample_limit <= 0 or sample_row_limit <= 0:
            return {column: {} for column in json_columns}

        where_clause = self._time_window_where_clause(time_column, time_from, time_to)
        samples_by_column: dict[str, dict[str, list[object]]] = {}
        # An empty object has no key to type, so it is no sample of one.
        node_filter = (
            "_value <> '{}'::jsonb" if include_objects else "jsonb_typeof(_value) <> 'object'"
        )

        for column in json_columns:
            c = self._validate_column(column)
            seed = (
                f"(SELECT {_jsonb_object(_quote_ident(c))} AS _doc "
                f"FROM ({base_query}) AS _src{where_clause} LIMIT {int(sample_row_limit)})"
            )
            # A JSON null renders as the text 'null'; it is ordered last so it only
            # occupies a sample slot for a path that has nothing else to show.
            sql = (
                f"WITH RECURSIVE {_JSON_LEAF_WALK.format(seed=seed)}, "
                "_leaf AS ("
                "SELECT DISTINCT _path, _value::text AS _text FROM _walk "
                f"WHERE {node_filter}"
                "), _ranked AS ("
                "SELECT _path, _text, "
                "DENSE_RANK() OVER (ORDER BY _path) AS _prank, "
                "ROW_NUMBER() OVER (PARTITION BY _path ORDER BY (_text = 'null'), _text) AS _vrank "
                "FROM _leaf"
                ") "
                "SELECT _path, _text FROM _ranked "
                f"WHERE _prank <= {int(path_limit)} AND _vrank <= {int(sample_limit)} "
                "ORDER BY _path, _vrank"
            )
            logger.debug("PG JSON path discovery query: %s", _truncate_sql(sql))
            with self._conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()

            column_samples: dict[str, list[object]] = {}
            for raw_path, raw_text in rows:
                path = str(raw_path)
                try:
                    self._json_path_expression(c, path)
                except ValueError:
                    logger.info("Skipping unsupported JSON path %s.%s", c, path)
                    continue
                # The path is registered even when its only value is a JSON null,
                # so a null-valued key is still discoverable (with no samples).
                samples = column_samples.setdefault(path, [])
                if raw_text is None or raw_text == "null":
                    continue
                samples.append(raw_text)
            samples_by_column[c] = column_samples

        return samples_by_column

    def _contract_where_clause(
        self,
        time_column: str | None,
        time_from: datetime | None,
        time_to: datetime | None,
        group_column: str | None,
        group_value: str | None,
    ) -> str:
        """The scan window plus the optional event-type filter, as one WHERE clause."""
        conditions: list[str] = []
        if time_column is not None and time_from is not None and time_to is not None:
            conditions.append(self._time_window_condition(time_column, time_from, time_to))
        if group_column is not None:
            # COALESCE(col::text, '') = 'value' — the same NULL-as-empty-string
            # comparison ClickHouse makes with ifNull(toString(col), '').
            expected = self._quote_string(group_value or "")
            conditions.append(f"{self._string_value_expression(group_column)} = {expected}")
        if not conditions:
            return ""
        return " WHERE " + " AND ".join(conditions)

    @override
    def _probe_contract_regex(self, pattern: str) -> None:
        """Have PostgreSQL compile the pattern as a POSIX ARE, over no rows.

        ``'' ~ pattern`` reads no table and still has to build the regex, so a
        pattern ARE rejects raises here, where it costs one expectation, instead
        of from inside the contract statement, where it took every contract in
        the scan with it (this class's docstring named that hazard for the whole
        time nothing defended against it).

        Safe to run mid-scan only because the connection is autocommit (see
        ``__init__``): each statement is its own transaction, so a rejected
        pattern leaves nothing aborted behind it. Inside an open transaction this
        probe would poison every statement that followed — the cure would be
        worse than the disease.

        The pattern is quoted with the same helper the contract statement uses,
        so what is offered to the engine is exactly what would be compiled;
        ``standard_conforming_strings`` is pinned on the session, which is what
        makes doubling the quote and nothing else sufficient for both.
        """
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT '' ~ {self._quote_string(pattern)}")

    def _contract_bad_condition(self, expectation: FieldContractExpectation) -> str | None:
        """The predicate that makes one row BAD, or ``None`` if the contract is a no-op.

        Mirrors ``ClickHouseAdapter._contract_aggregates`` clause for clause,
        because the two must agree on every row of every fixture:

        * ``required_null_violation`` — a NULL *is* the violation.
        * everything else — a NULL row is neither bad nor counted; it is skipped.
        * ``range_violation`` — a value that does not parse as a number is BAD, the
          same verdict ``float(text)`` raising gives the Python fallback and
          ``toFloat64OrNull`` returning NULL gives ClickHouse.

        An expectation this engine cannot evaluate yields ``None`` and contributes
        no columns, so it cannot manufacture a violation out of a contract that
        says nothing, and — the part that is not local to this adapter — cannot
        end the scan the expectations beside it are riding in. Which ones those
        are is ``field_contract_is_inert``'s to say, not this branch's; see the
        field contract section of ``BaseAdapter``.
        """
        if self._field_contract_is_inert(expectation):
            return None

        quoted = self._contract_operand(expectation, self._field_operand)
        if quoted is None:
            return None
        value_expr = f"COALESCE({quoted}::{self.text_type}, '')"
        present = f"{quoted} IS NOT NULL"

        if expectation.drift_type == "required_null_violation":
            return f"{quoted} IS NULL"
        if expectation.drift_type == "enum_violation":
            options = ", ".join(self._quote_string(option) for option in expectation.enum_options)
            return f"{present} AND {value_expr} NOT IN ({options})"
        if expectation.drift_type == "regex_violation":
            # `~` is an unanchored POSIX match, so it accepts exactly what the
            # fallback's re.search accepts — for the patterns the two dialects
            # share. POSIX ARE is NOT Python `re`: see the class docstring.
            # The assert narrows the type; a pattern-less regex is inert above.
            assert expectation.regex is not None
            # And where the two dialects do NOT share a construct, ARE says so by
            # refusing to compile — from inside the statement, which is the
            # hazard this class has documented all along. Ask first, and a
            # pattern this server will not take drops only its own expectation.
            if not self.contract_regex_is_compilable(expectation.regex):
                self._skip_field_contract(expectation)
                return None
            pattern = self._quote_string(expectation.regex)
            return f"{present} AND NOT ({value_expr} ~ {pattern})"
        if expectation.drift_type == "range_violation":
            # Cast only what already looks like a number AND is small enough to
            # hold (see _FINITE_NUMBER_RE): a bare cast on 'twelve' RAISES and
            # takes the whole contract query down with it. Anything unmatched
            # stays NULL here.
            #
            # The domain is `numeric`, not `double precision`, and that is the
            # point of this branch rather than a style choice. float8's input
            # function raises 22003 on BOTH overflow and underflow-to-zero, so
            # '1e400' and '1e-400' cleared the old syntax-only guard and then
            # aborted the statement — and every expectation shares one statement
            # (they are columns of one aggregate in validate_field_contracts), so
            # one event row could take down a whole config's contract check.
            # numeric has no float range to leave, so it never raises here.
            #
            # It does NOT always agree with the other engines on the verdict, and
            # this comment used to say it did. base.py's fallback,
            # ClickHouse's toFloat64OrNull and BigQuery's SAFE_CAST all compare in
            # float64, which rounds; numeric compares exact decimals. So a value
            # and a bound that are different decimals but the same double disagree:
            # '9007199254740993' against max 9007199254740992.0 is BAD here and in
            # range on the other three, and '-1e-400' against min 0.0 is BAD here
            # (exactly below zero) while the fallback reads it as -0.0, in range.
            # Only values within one float64 rounding step of a bound, or beyond
            # float64's range, are affected. It is declared in the field contract
            # section of BaseAdapter and in warehouse-parity.md rather than closed:
            # comparing as float8 means casting numeric to float8, which raises
            # 22003 on exactly the overflow and underflow this branch removes.
            #
            # The domain and its guard are a dialect seam (`contract_number_type`,
            # `contract_number_re`): Redshift's numeric holds 38 digits, so it
            # compares in a magnitude-guarded float8 instead.
            number_type = self.contract_number_type
            bounds: list[str] = []
            # The bound is cast too. An unadorned decimal constant is already
            # `numeric` to PostgreSQL — only an integer-looking one starts life as
            # int4/int8 — and `contract_bound_literal` is `repr(float)`, which
            # always emits a '.' or an 'e'. So this cast changes no type today; it
            # is here to state the comparison domain where a reader checks it, and
            # so that a later change to that helper's spelling cannot quietly
            # resolve the operator as `numeric >= double precision` and reintroduce
            # the overflow this branch exists to avoid.
            #
            # The helper lives in base.py now rather than here: it used to be this
            # file's private `_float_literal`, which made PostgreSQL the only engine
            # that refused a non-finite bound — by RAISING, which ended the whole
            # collection, while ClickHouse compared against `-inf` and BigQuery
            # emitted a literal GoogleSQL cannot parse. One helper, one rule, and
            # the decision to skip such an expectation now precedes the call.
            if expectation.min_value is not None:
                bounds.append(f">= {contract_bound_literal(expectation.min_value)}::{number_type}")
            if expectation.max_value is not None:
                bounds.append(f"<= {contract_bound_literal(expectation.max_value)}::{number_type}")
            finite_in_range = " AND ".join(
                f"{value_expr}::{number_type} {bound}" for bound in bounds
            )
            # An infinity is never cast: its verdict is known here, from which
            # bounds exist (the bounds themselves are finite — a non-finite one
            # makes the expectation inert). +inf is above every max and meets
            # every min; -inf the reverse. Casting it would need 'Infinity' in
            # the numeric domain, which only PostgreSQL 14+ has.
            positive_inf_in_range = "TRUE" if expectation.max_value is None else "FALSE"
            negative_inf_in_range = "TRUE" if expectation.min_value is None else "FALSE"
            in_range = (
                f"CASE WHEN {value_expr} ~ {self._quote_string(self.contract_number_re)} "
                f"THEN {finite_in_range} "
                f"WHEN {value_expr} ~* {self._quote_string(_POSITIVE_INF_RE)} "
                f"THEN {positive_inf_in_range} "
                f"WHEN {value_expr} ~* {self._quote_string(_NEGATIVE_INF_RE)} "
                f"THEN {negative_inf_in_range} "
                "END"
            )
            # COALESCE(..., TRUE) is what turns "did not parse" into BAD: an
            # unparseable value leaves the comparison NULL, and NULL would
            # otherwise be dropped by the aggregate FILTER rather than counted.
            # NaN is excluded first — Postgres sorts NaN above every number, while
            # Python and ClickHouse make every NaN comparison false, so letting it
            # reach the comparison is the one way these three disagree.
            return (
                f"{present} AND NOT ({value_expr} ~* {self._quote_string(_NAN_RE)}) "
                f"AND COALESCE(NOT ({in_range}), TRUE)"
            )
        return None

    def _contract_aggregate_sql(
        self,
        expectation: FieldContractExpectation,
        bad_condition: str,
        *,
        index: int,
    ) -> str:
        """The three columns one expectation contributes to the shared scan.

        ``_bad_{i}``, ``_total_{i}``, ``_sample_{i}`` — the same three aliases in
        the same order that ``BigQueryAdapter._contract_fragments`` emits and
        that ``ClickHouseAdapter.validate_field_contracts`` numbers its own
        aggregates with, so the row a reader has to index by position is laid
        out identically on all three engines.
        Nothing else about the expectation reaches the warehouse: the threshold
        and the comparison are ``field_contract_verdict``'s job.
        """
        quoted = self._field_operand(expectation.field_name)
        value_expr = f"COALESCE({quoted}::{self.text_type}, '')"
        is_required = expectation.drift_type == "required_null_violation"
        # required_null counts NULLs in the denominator (a NULL is the thing being
        # measured); every other drift type measures only the non-null population.
        total_expr = "count(*)" if is_required else self._count_where(f"{quoted} IS NOT NULL")
        # One sample value, like ClickHouse's anyIf — min() rather than an arbitrary
        # pick so the sample is deterministic, and because it needs O(1) memory even
        # when millions of rows are bad (array_agg would materialize all of them).
        sample_source = f"'<NULL>'::{self.text_type}" if is_required else value_expr
        sample_expr = self._aggregate_where(MetricAggregation.min, sample_source, bad_condition)
        return (
            f"{self._count_where(bad_condition)} AS _bad_{int(index)}, "
            f"{total_expr} AS _total_{int(index)}, "
            f"{sample_expr} AS _sample_{int(index)}"
        )

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
        """Evaluate every field contract warehouse-side, over the FULL window.

        Replaces BaseAdapter's fallback, which pulled ``limit`` (50,000) rows back
        and counted them in Python: a violation that first appears at row 50,001 was
        invisible to it, and the ``bad_rate`` it reported described the sample rather
        than the data. Here the counting happens in Postgres over every row the
        window selects, so both numbers are exact whatever the table's size.

        One statement AND one scan for all expectations: their aggregates sit side
        by side in one flat SELECT over a single ``FROM (base_query)``. The shape
        this replaced was a UNION ALL of one aggregate subquery per expectation —
        still one statement, but Postgres does not deduplicate an identical inline
        subquery across UNION arms, so it re-read the whole window once per
        contract, and ``catalog_sync`` calls this once per event-type group.

        Only the counts come back; the threshold comparison is
        ``field_contract_verdict``'s, per the contract on ``BaseAdapter``. That is
        also what retires the ``ORDER BY _ord`` this used to need: the violations
        are built by walking the expectations, so they are in the caller's order by
        construction.
        """
        if not expectations:
            return []

        where_clause = self._contract_where_clause(
            time_column,
            time_from,
            time_to,
            group_column,
            group_value,
        )
        # Compiled up front, before any statement is built: an expectation with
        # nothing to check contributes no columns, and a set of expectations that
        # are ALL inert must still touch the warehouse not at all.
        compiled = [
            (expectation, condition)
            for expectation in expectations
            if (condition := self._contract_bad_condition(expectation)) is not None
        ]
        if not compiled:
            return []

        violations: list[FieldContractViolation] = []
        t0 = time.monotonic()
        for start in range(0, len(compiled), FIELD_CONTRACT_EXPECTATIONS_PER_QUERY):
            chunk = compiled[start : start + FIELD_CONTRACT_EXPECTATIONS_PER_QUERY]
            selects = ", ".join(
                self._contract_aggregate_sql(expectation, condition, index=index)
                for index, (expectation, condition) in enumerate(chunk)
            )
            sql = f"SELECT {selects} FROM ({base_query}) AS _src{where_clause}"
            logger.debug("PG field contract query: %s", _truncate_sql(sql))
            with self._conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()
            # An ungrouped aggregate returns exactly one row even over an empty
            # window, so this only fires for a driver (or a test double) that hands
            # back nothing — better than an IndexError inside the loop below.
            if not rows:
                continue
            row = rows[0]
            for index, (expectation, _condition) in enumerate(chunk):
                sample = row[index * 3 + 2]
                violation = field_contract_verdict(
                    expectation,
                    bad_count=int(row[index * 3]),
                    total_count=int(row[index * 3 + 1]),
                    sample_value=None if sample is None else str(sample),
                )
                if violation is not None:
                    violations.append(violation)
        elapsed = time.monotonic() - t0
        logger.info("PG field contracts done in %.2fs, %s violations", elapsed, len(violations))

        # ``limit`` bounds the violation ROWS handed back, never the rows scanned —
        # the LIMIT clause it used to be did the same, and the caller stores one
        # drift row per violation.
        return violations[: max(0, int(limit))]

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
        reg_cols = [self._validate_column(c) for c in regular_columns]
        json_cols = [self._validate_column(c) for c in json_columns]
        json_value_paths = json_value_paths or {}
        json_value_names: list[str] = []

        select_parts: list[str] = []
        group_parts: list[str] = []
        for c in reg_cols:
            select_parts.append(_quote_ident(c))
            group_parts.append(_quote_ident(c))
        for c in json_cols:
            expr = self._json_paths_expression(c)
            select_parts.append(f"{expr} AS {_quote_ident(c)}")
            group_parts.append(expr)
        for c in json_cols:
            for path in json_value_paths.get(c, []):
                full_path = f"{c}.{path}"
                value_expr = f"({self._json_path_expression(c, path)})::text"
                select_parts.append(f"{value_expr} AS {_quote_ident(full_path)}")
                group_parts.append(value_expr)
                json_value_names.append(full_path)
        select_parts.append("count(*) AS _cnt")

        # No grouping column is one group over every row: an ungrouped aggregate,
        # spelled without the `GROUP BY ()` Redshift does not parse.
        group_by = f"GROUP BY {', '.join(group_parts)} " if group_parts else ""
        where_clause = self._time_window_where_clause(time_column, time_from, time_to)
        sql = (
            f"SELECT {', '.join(select_parts)} "
            f"FROM ({base_query}) AS _src{where_clause} "
            f"{group_by}"
            f"ORDER BY _cnt DESC "
            f"LIMIT {int(limit)}"
        )

        logger.debug("PG breakdown query: %s", _truncate_sql(sql))
        t0 = time.monotonic()
        with self._conn.cursor() as cur:
            cur.execute(sql)
            rows = [tuple(r) for r in cur.fetchall()]
        elapsed = time.monotonic() - t0
        logger.info("PG breakdown done in %.2fs, %s rows", elapsed, len(rows))

        return reg_cols, json_cols, json_value_names, rows

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
        tc = self._validate_column(time_column)

        reg_cols = [self._validate_column(c) for c in regular_columns]
        json_cols = [self._validate_column(c) for c in json_columns]
        json_value_paths = json_value_paths or {}

        bucket_expr = self._bucket_expression(tc, interval)
        select_parts: list[str] = [f"{bucket_expr} AS _bucket"]
        group_parts: list[str] = ["_bucket"]
        col_names: list[str] = []
        json_value_names: list[str] = []
        for c in reg_cols:
            select_parts.append(_quote_ident(c))
            group_parts.append(_quote_ident(c))
            col_names.append(c)
        for c in json_cols:
            expr = self._json_paths_expression(c)
            select_parts.append(f"{expr} AS {_quote_ident(c)}")
            group_parts.append(expr)
            col_names.append(c)
        for c in json_cols:
            for path in json_value_paths.get(c, []):
                full_path = f"{c}.{path}"
                value_expr = f"({self._json_path_expression(c, path)})::text"
                select_parts.append(f"{value_expr} AS {_quote_ident(full_path)}")
                group_parts.append(value_expr)
                json_value_names.append(full_path)
        select_parts.append("count(*) AS _cnt")

        window = self._time_window_condition(tc, time_from, time_to)
        sql = (
            f"SELECT {', '.join(select_parts)} "
            f"FROM ({base_query}) AS _src "
            f"WHERE {window} "
            f"GROUP BY {', '.join(group_parts)} "
            f"ORDER BY _bucket "
            f"LIMIT {int(limit)}"
        )

        logger.debug("PG bucketed query: %s", _truncate_sql(sql))
        t0 = time.monotonic()
        with self._conn.cursor() as cur:
            cur.execute(sql)
            rows = [tuple(r) for r in cur.fetchall()]
        elapsed = time.monotonic() - t0
        logger.info("PG bucketed done in %.2fs, %s rows", elapsed, len(rows))

        return col_names, json_value_names, rows

    def _aggregate_value_sql(self, agg_fn: MetricAggregation, measure_column: str | None) -> str:
        """Validate + escape the measure and build the safe aggregate fragment."""
        measure_sql: str | None = None
        if measure_column is not None:
            measure_sql = _quote_ident(
                validate_measure_column(measure_column, self._allowed_columns)
            )
        return build_aggregate_sql(agg_fn, measure_sql)

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
        tc = self._validate_column(time_column)

        reg_cols = [self._validate_column(c) for c in regular_columns]
        json_cols = [self._validate_column(c) for c in json_columns]
        json_value_paths = json_value_paths or {}
        value_sql = self._aggregate_value_sql(agg_fn, measure_column)

        bucket_expr = self._bucket_expression(tc, interval)
        select_parts: list[str] = [f"{bucket_expr} AS _bucket"]
        group_parts: list[str] = ["_bucket"]
        col_names: list[str] = []
        json_value_names: list[str] = []
        for c in reg_cols:
            select_parts.append(_quote_ident(c))
            group_parts.append(_quote_ident(c))
            col_names.append(c)
        for c in json_cols:
            expr = self._json_paths_expression(c)
            select_parts.append(f"{expr} AS {_quote_ident(c)}")
            group_parts.append(expr)
            col_names.append(c)
        for c in json_cols:
            for path in json_value_paths.get(c, []):
                full_path = f"{c}.{path}"
                value_expr = f"({self._json_path_expression(c, path)})::text"
                select_parts.append(f"{value_expr} AS {_quote_ident(full_path)}")
                group_parts.append(value_expr)
                json_value_names.append(full_path)
        select_parts.append(f"{value_sql} AS _value")

        window = self._time_window_condition(tc, time_from, time_to)
        sql = (
            f"SELECT {', '.join(select_parts)} "
            f"FROM ({base_query}) AS _src "
            f"WHERE {window} "
            f"GROUP BY {', '.join(group_parts)} "
            f"ORDER BY _bucket "
            f"LIMIT {int(limit)}"
        )

        logger.debug("PG bucketed aggregate query: %s", _truncate_sql(sql))
        t0 = time.monotonic()
        with self._conn.cursor() as cur:
            cur.execute(sql)
            rows = [tuple(r) for r in cur.fetchall()]
        elapsed = time.monotonic() - t0
        logger.info("PG bucketed aggregate done in %.2fs, %s rows", elapsed, len(rows))

        return col_names, json_value_names, rows

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
        tc = self._validate_column(time_column)

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

        bucket_expr = self._bucket_expression(tc, interval)
        select_parts: list[str] = [
            f"{bucket_expr} AS _bucket",
            f"{breakdown_expr} AS _breakdown_value",
            f"{is_other_expr} AS _is_other",
        ]
        group_parts: list[str] = ["_bucket", "_breakdown_value", "_is_other"]
        col_names: list[str] = []
        json_value_names: list[str] = []
        for c in reg_cols:
            if c == breakdown:
                # The breakdown keeps its regular-column slot but carries the
                # FOLDED value there, and is deliberately NOT added to
                # group_parts: see
                # BaseAdapter.get_time_bucketed_aggregate_breakdown for why the
                # raw column may not be a grouping key. The expression is
                # repeated rather than the alias reused, because an output alias
                # is not visible to the SELECT list in Postgres; `GROUP BY
                # _breakdown_value` resolves to this same expression, so the
                # planner sees the projection as grouped.
                select_parts.append(f"{breakdown_expr} AS {_quote_ident(c)}")
            else:
                select_parts.append(_quote_ident(c))
                group_parts.append(_quote_ident(c))
            col_names.append(c)
        for c in json_cols:
            expr = self._json_paths_expression(c)
            select_parts.append(f"{expr} AS {_quote_ident(c)}")
            group_parts.append(expr)
            col_names.append(c)
        for c in json_cols:
            for path in json_value_paths.get(c, []):
                full_path = f"{c}.{path}"
                value_expr = f"({self._json_path_expression(c, path)})::text"
                select_parts.append(f"{value_expr} AS {_quote_ident(full_path)}")
                group_parts.append(value_expr)
                json_value_names.append(full_path)
        select_parts.append(f"{value_sql} AS _value")

        window = self._time_window_condition(tc, time_from, time_to)
        sql = (
            f"SELECT {', '.join(select_parts)} "
            f"FROM ({base_query}) AS _src "
            f"WHERE {window} "
            f"GROUP BY {', '.join(group_parts)} "
            f"ORDER BY _bucket, _breakdown_value "
            f"LIMIT {int(limit)}"
        )

        logger.debug(
            "PG bucketed aggregate breakdown query for %s: %s", breakdown, _truncate_sql(sql)
        )
        t0 = time.monotonic()
        with self._conn.cursor() as cur:
            cur.execute(sql)
            rows = [tuple(r) for r in cur.fetchall()]
        elapsed = time.monotonic() - t0
        logger.info("PG bucketed aggregate breakdown done in %.2fs, %s rows", elapsed, len(rows))

        return col_names, json_value_names, rows

    def _spec_aggregate_sql(self, spec: AggregateSpec) -> str:
        """Build one (optionally conditional) aggregate fragment for a spec.

        Reuses the single-aggregate validation/escaping path
        (``_aggregate_value_sql`` -> validate_measure_column + build_aggregate_sql)
        and, when ``filter_sql`` is set, wraps the aggregate as a Postgres
        ``FILTER (WHERE ...)`` conditional so specs with different filters can
        share one scan. Postgres supports ``FILTER`` on ``count(*)`` and
        ``count(DISTINCT col)`` alike, so the conditional aggregate itself never
        needs a CASE fallback the way BigQuery's does (no ``FILTER`` there, so
        the condition folds into ``CASE`` / ``IF`` instead). ``filter_sql`` is a
        pre-validated boolean fragment injected as-is, the same trust model as
        the single-metric row-filter path.

        The NULL-means-gap rule this implements is stated once, on
        :class:`~tripl.core.adapters.base.BaseAdapter`: a bucket is absent for a
        spec when NO row in it matched ``cond``, and a bucket that does have
        matching rows is a data point even when the aggregate over them is 0.
        Each aggregate spells the row-presence test as cheaply as it can:

        * ``avg`` / ``sum`` / ``min`` / ``max`` need no test at all — over zero
          matching rows they already return NULL.
        * ``count`` uses ``NULLIF(count(*) FILTER (WHERE cond), 0)``, because
          its filtered value IS the count of matching rows: 0 and "nothing
          matched" are the same statement. Spelling it as the CASE below was
          rejected — it emits the same verdict from twice the text and a second
          copy of ``cond``.
        * ``count_distinct`` needs an explicit ``count(*) FILTER`` probe:
          ``count(DISTINCT m) FILTER`` returns 0 both for a bucket nothing
          matched AND for a bucket whose matching rows all have ``m IS NULL``,
          so its own value cannot answer the question. It used to be
          ``NULLIF(..., 0)`` too, which reported an all-NULL measure over real
          rows as a gap, while ClickHouse — gating on ``countIf(cond)``, a row
          count — kept the bucket and stored the 0.
        """
        if not spec.filter_sql:
            return self._aggregate_value_sql(spec.aggregation, spec.column)
        agg = coerce_aggregation(spec.aggregation)
        measure_sql: str | None = None
        if spec.column is not None:
            measure_sql = _quote_ident(validate_measure_column(spec.column, self._allowed_columns))
        filtered = self._aggregate_where(agg, measure_sql, spec.filter_sql)
        if agg is MetricAggregation.count:
            return f"NULLIF({filtered}, 0)"
        if agg is MetricAggregation.count_distinct:
            matching_rows = self._count_where(spec.filter_sql)
            return f"CASE WHEN {matching_rows} = 0 THEN NULL ELSE {filtered} END"
        return filtered

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
        tc = self._validate_column(time_column)

        bucket_expr = self._bucket_expression(tc, interval)
        select_parts: list[str] = [f"{bucket_expr} AS _bucket"]
        col_names: list[str] = ["bucket"]
        for spec in specs:
            select_parts.append(f"{self._spec_aggregate_sql(spec)} AS {_quote_ident(spec.key)}")
            col_names.append(spec.key)

        window = self._time_window_condition(tc, time_from, time_to)
        sql = (
            f"SELECT {', '.join(select_parts)} "
            f"FROM ({base_query}) AS _src "
            f"WHERE {window} "
            f"GROUP BY _bucket "
            f"ORDER BY _bucket "
            f"LIMIT {int(limit)}"
        )
        return col_names, sql

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
        col_names, sql = self.build_time_bucketed_multi_aggregate_sql(
            base_query,
            time_column,
            interval,
            specs,
            time_from,
            time_to,
            limit=limit,
        )

        logger.debug("PG bucketed multi-aggregate query: %s", _truncate_sql(sql))
        t0 = time.monotonic()
        with self._conn.cursor() as cur:
            cur.execute(sql)
            rows = [tuple(r) for r in cur.fetchall()]
        elapsed = time.monotonic() - t0
        logger.info("PG bucketed multi-aggregate done in %.2fs, %s rows", elapsed, len(rows))

        return col_names, rows

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
        tc = self._validate_column(time_column)

        breakdown = self._validate_column(breakdown_column)

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

        bucket_expr = self._bucket_expression(tc, interval)
        select_parts: list[str] = [
            f"{bucket_expr} AS _bucket",
            f"{breakdown_expr} AS _breakdown_value",
            f"{is_other_expr} AS _is_other",
        ]
        col_names: list[str] = ["bucket", "breakdown_value", "is_other"]
        for spec in specs:
            select_parts.append(f"{self._spec_aggregate_sql(spec)} AS {_quote_ident(spec.key)}")
            col_names.append(spec.key)

        window = self._time_window_condition(tc, time_from, time_to)
        sql = (
            f"SELECT {', '.join(select_parts)} "
            f"FROM ({base_query}) AS _src "
            f"WHERE {window} "
            f"GROUP BY _bucket, _breakdown_value, _is_other "
            f"ORDER BY _bucket, _breakdown_value "
            f"LIMIT {int(limit)}"
        )

        logger.debug(
            "PG bucketed multi-aggregate breakdown query for %s: %s",
            breakdown,
            _truncate_sql(sql),
        )
        t0 = time.monotonic()
        with self._conn.cursor() as cur:
            cur.execute(sql)
            rows = [tuple(r) for r in cur.fetchall()]
        elapsed = time.monotonic() - t0
        logger.info(
            "PG bucketed multi-aggregate breakdown done in %.2fs, %s rows", elapsed, len(rows)
        )

        return col_names, rows

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

        tc = self._validate_column(time_column)
        cols = [self._validate_breakdown_field(c) for c in breakdown_columns]
        window = self._time_window_condition(tc, time_from, time_to)

        prepared = [
            f'{self._field_value_expression(c)} AS "__bd_raw_{i}"' for i, c in enumerate(cols)
        ]
        # One GROUPING SETS scan: per column, GROUP BY that single label so
        # ROW_NUMBER ranks values within that column only.
        grouping_sets = ", ".join(f'("__bd_raw_{i}")' for i in range(len(cols)))
        label_branches = " ".join(
            f'WHEN GROUPING("__bd_raw_{i}") = 0 THEN {self._quote_string(c)}'
            for i, c in enumerate(cols)
        )
        value_branches = " ".join(
            f'WHEN GROUPING("__bd_raw_{i}") = 0 THEN "__bd_raw_{i}"' for i in range(len(cols))
        )

        sql = (
            "SELECT _breakdown_column, _breakdown_value FROM ("
            "SELECT _breakdown_column, _breakdown_value, "
            # _breakdown_value is the tie-break the BaseAdapter top-N contract
            # requires: ranked by count alone, two equally-counted values at the
            # `rn <= limit` cut could swap places between runs over the same
            # window. COLLATE "C" rather than a bare sort on the value, because
            # an unqualified text sort follows the database collation: the same
            # tie breaks one way under C and another under en_US.UTF-8, and a
            # PG12+ nondeterministic ICU collation would leave it a tie. "C" is
            # byte order, which over UTF-8 is the code-point order the other
            # three adapters rank by, and it ships with every server.
            "ROW_NUMBER() OVER (PARTITION BY _breakdown_column "
            f"ORDER BY _cnt DESC, {self._byte_ordered('_breakdown_value')}) AS rn "
            "FROM ("
            "SELECT "
            f"CASE {label_branches} ELSE '' END AS _breakdown_column, "
            f"CASE {value_branches} ELSE '' END AS _breakdown_value, "
            "count(*) AS _cnt "
            "FROM ("
            f"SELECT {', '.join(prepared)} "
            f"FROM ({base_query}) AS _src "
            f"WHERE {window}"
            ") AS _prepared "
            f"GROUP BY GROUPING SETS ({grouping_sets})"
            ") AS _scored"
            ") AS _ranked "
            f"WHERE rn <= {int(limit)}"
        )
        logger.debug("PG breakdown top-values query: %s", _truncate_sql(sql))
        top: dict[str, list[str]] = {c: [] for c in cols}
        with self._conn.cursor() as cur:
            cur.execute(sql)
            for column, value in cur.fetchall():
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

        tc = self._validate_column(time_column)

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

        bucket_expr = self._bucket_expression(tc, interval)
        prepared_parts: list[str] = [f"{bucket_expr} AS _bucket"]
        col_names: list[str] = []
        json_value_names: list[str] = []
        for c in reg_cols:
            prepared_parts.append(f"{_quote_ident(c)} AS {_quote_ident(c)}")
            col_names.append(c)
        for c in json_cols:
            prepared_parts.append(f"{self._json_paths_expression(c)} AS {_quote_ident(c)}")
            col_names.append(c)
        for c in json_cols:
            for path in json_value_paths.get(c, []):
                full_path = f"{c}.{path}"
                prepared_parts.append(
                    f"({self._json_path_expression(c, path)})::text AS {_quote_ident(full_path)}"
                )
                json_value_names.append(full_path)

        grouping_columns = [
            _quote_ident(name) for name in [*reg_cols, *json_cols, *json_value_names]
        ]
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

            prepared_parts.append(f"{breakdown_expr} AS {_quote_ident(value_alias)}")
            prepared_parts.append(f"{is_other_expr} AS {_quote_ident(other_alias)}")
            grouping_check = f"GROUPING({_quote_ident(value_alias)}) = 0"
            label_when.append(f"WHEN {grouping_check} THEN {self._quote_string(column)}")
            value_when.append(
                f"WHEN {grouping_check} THEN {_quote_ident(value_alias)}::{self.text_type}"
            )
            other_when.append(f"WHEN {grouping_check} THEN {_quote_ident(other_alias)}")
            grouping_sets.append(
                "("
                + ", ".join(
                    [
                        "_bucket",
                        _quote_ident(value_alias),
                        _quote_ident(other_alias),
                        *grouping_columns,
                    ]
                )
                + ")"
            )

        select_parts = [
            "_bucket",
            f"CASE {' '.join(label_when)} ELSE '' END AS _breakdown_column",
            f"CASE {' '.join(value_when)} ELSE '' END AS _breakdown_value",
            f"CASE {' '.join(other_when)} ELSE 0 END AS _is_other",
            *grouping_columns,
            "count(*) AS _cnt",
        ]

        window = self._time_window_condition(tc, time_from, time_to)
        sql = (
            f"SELECT {', '.join(select_parts)} "
            "FROM ("
            f"SELECT {', '.join(prepared_parts)} "
            f"FROM ({base_query}) AS _src "
            f"WHERE {window}"
            ") AS _prepared "
            f"GROUP BY GROUPING SETS ({', '.join(grouping_sets)}) "
            "ORDER BY _bucket, _breakdown_column, _breakdown_value "
            f"LIMIT {int(limit)}"
        )

        logger.debug(
            "PG bucketed breakdown GROUPING SETS query for %s: %s",
            ", ".join(breakdown_cols),
            _truncate_sql(sql),
        )
        t0 = time.monotonic()
        with self._conn.cursor() as cur:
            cur.execute(sql)
            rows = [tuple(r) for r in cur.fetchall()]
        elapsed = time.monotonic() - t0
        logger.info("PG bucketed breakdown done in %.2fs, %s rows", elapsed, len(rows))

        return col_names, json_value_names, rows
