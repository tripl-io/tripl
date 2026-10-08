"""Warehouse wiring for the conformance gates: connect, seed, and skip honestly.

Two rules govern everything here.

**A developer without Docker must not be blocked.** An unreachable warehouse
SKIPS, it does not error, so ``uv run pytest`` on a laptop stays green.

**CI must not be allowed to skip.** A skip in CI means a container failed to come
up, and a gate that silently no-ops reports green while testing nothing — which is
strictly worse than having no gate at all. Setting ``TRIPL_CONFORMANCE_REQUIRED=1``
(CI does) turns every "warehouse unreachable" skip into a hard FAILURE. The CI job
additionally asserts, from the JUnit XML, that a non-zero number of conformance
tests actually ran and that none of them skipped.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, NoReturn

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.adapters.base import BaseAdapter
from tripl.core.adapters.clickhouse import ClickHouseAdapter
from tripl.core.adapters.postgres import PostgresAdapter
from tripl.models import Base
from tripl.tests.conformance.dataset import PIPELINE_ROWS, ROWS, FixtureRow, PipelineRow

# Connection settings. The defaults match the CI service containers. Locally they
# point at a database name (`tripl_conformance`) that the dev stack does not have,
# so a stray `pytest` run connects to nothing and skips rather than seeding tables
# into a developer's working database.
_PG_HOST = os.environ.get("TRIPL_CONF_PG_HOST", "localhost")
_PG_PORT = int(os.environ.get("TRIPL_CONF_PG_PORT", "5432"))
_PG_DB = os.environ.get("TRIPL_CONF_PG_DB", "tripl_conformance")
_PG_USER = os.environ.get("TRIPL_CONF_PG_USER", "tripl")
_PG_PASSWORD = os.environ.get("TRIPL_CONF_PG_PASSWORD", "tripl")

_CH_HOST = os.environ.get("TRIPL_CONF_CH_HOST", "localhost")
_CH_PORT = int(os.environ.get("TRIPL_CONF_CH_PORT", "8123"))
_CH_DB = os.environ.get("TRIPL_CONF_CH_DB", "default")

_BQ_EMULATOR_URL = os.environ.get("TRIPL_CONF_BQ_EMULATOR_URL", "http://localhost:9050")
_BQ_PROJECT = os.environ.get("TRIPL_CONF_BQ_PROJECT", "tripl-test")

#: The table both executing warehouses seed. Distinctive enough that it cannot be
#: confused with anything a developer actually has.
TABLE = "tripl_conformance_events"

#: The pipeline gate's source table (``dataset.PIPELINE_ROWS``). Same two warehouses,
#: same seeding path, a different shape: a ten-bucket series instead of a nine-row
#: window trap.
PIPELINE_TABLE = "tripl_conformance_pipeline"

#: What the pipeline's ScanConfig scans. ``user_id`` is projected because the
#: ``per_distinct_user`` composition runs a ``count_distinct`` over this very query;
#: it has no FieldDefinition, so event generation skips it and the event identity
#: stays ``event_name=<value>``.
PIPELINE_EVENTS_QUERY = f"SELECT ts, event_name, user_id FROM {PIPELINE_TABLE}"

#: What the pipeline's FactTable selects.
PIPELINE_FACTS_QUERY = f"SELECT ts, event_name, user_id, amount, platform FROM {PIPELINE_TABLE}"

#: The non-UTC zone the PostgreSQL gate pins the *role* to, to prove the adapter's
#: session pin and explicit-offset literals survive a hostile server default.
PG_HOSTILE_TZ = "Asia/Kolkata"

_REQUIRED = os.environ.get("TRIPL_CONFORMANCE_REQUIRED") == "1"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark everything in this package ``conformance``.

    Done here rather than per-module so a new test file cannot forget the marker
    and quietly start running (and failing) in the fast local suite.
    """
    for item in items:
        if "tests/conformance/" in item.nodeid or "tests\\conformance\\" in item.nodeid:
            item.add_marker(pytest.mark.conformance)


def unavailable(reason: str) -> NoReturn:
    """Skip locally, FAIL in CI. See the module docstring."""
    if _REQUIRED:
        pytest.fail(
            f"TRIPL_CONFORMANCE_REQUIRED=1 but the warehouse is unreachable: {reason}. "
            "A conformance gate that skips is a gate that reports green while "
            "testing nothing — fix the container, do not relax this."
        )
    pytest.skip(f"conformance warehouse unavailable: {reason}")


# --- PostgreSQL --------------------------------------------------------------


#: Which engine of the PostgreSQL family the ``TRIPL_CONF_PG_*`` server is. The
#: same gates run against Greenplum (``greenplum``) through its adapter; the CI job
#: that sets this starts a Greenplum container instead of ``postgres:18``.
PG_ENGINE = os.environ.get("TRIPL_CONF_PG_ENGINE", "postgres")


def pg_adapter_class() -> type[PostgresAdapter]:
    """The adapter class for ``PG_ENGINE``."""
    if PG_ENGINE == "greenplum":
        from tripl.core.adapters.greenplum import GreenplumAdapter

        return GreenplumAdapter
    if PG_ENGINE != "postgres":
        raise ValueError(f"unknown TRIPL_CONF_PG_ENGINE: {PG_ENGINE!r}")
    return PostgresAdapter


def _pg_adapter(**overrides: object) -> PostgresAdapter:
    return pg_adapter_class()(
        host=_PG_HOST,
        port=_PG_PORT,
        database=_PG_DB,
        username=_PG_USER,
        password=_PG_PASSWORD,
        **overrides,
    )


@contextmanager
def seeding_cursor(adapter: PostgresAdapter) -> Iterator[Any]:
    """A cursor on the adapter's OWN connection, with its read-only pin lifted.

    These fixtures seed through the adapter's connection deliberately: the rows
    have to be written under exactly the session the adapter reads them back
    with, which is the whole reason the hostile-timezone tests mean anything.
    Opening a second connection here would restate ``-c timezone=UTC`` by hand,
    and a hand-copied session setting is free to drift from the adapter's.

    The adapter pins ``default_transaction_read_only=on`` so a warehouse query
    cannot write even where the credential could. That setting is USERSET on
    purpose — it is defence in depth, not a privilege boundary, and
    website/docs/run/security.md says so — which is what lets a session that
    genuinely means to write turn it off. A fixture is such a session. No
    ``base_query`` can be: the shared SQL gate admits a single statement, so
    there is no room for a ``SET`` in front of a ``SELECT``.
    """
    with adapter._conn.cursor() as cur:  # noqa: SLF001 — seed on the adapter's own session
        cur.execute("SET default_transaction_read_only = off")
        try:
            yield cur
        finally:
            cur.execute("SET default_transaction_read_only = on")


def _seed_postgres(adapter: PostgresAdapter) -> None:
    with seeding_cursor(adapter) as cur:
        cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
        cur.execute(
            f"CREATE TABLE {TABLE} ("
            "id integer PRIMARY KEY, "
            # The instant column. Its literals carry an explicit +00:00 offset.
            "ts timestamptz NOT NULL, "
            # The zone-less twin, holding the same UTC wall clock. Only the session
            # timezone pin can keep THIS column correct on a non-UTC server, so it
            # is what makes the hostile-timezone test bite.
            "ts_naive timestamp NOT NULL, "
            "event_name text NOT NULL, "
            "amount double precision, "
            "user_id text NOT NULL, "
            "doc jsonb, "
            # An ARRAY of a JSON type and an ARRAY of a scalar. psycopg's type
            # registry returns the ELEMENT's TypeInfo for both a type's own oid and
            # its array oid, so before that was handled `docs` was reported as plain
            # `jsonb` — which routed it into the JSON path walk, whose
            # `"docs"::jsonb` PostgreSQL refuses ("cannot cast type jsonb[] to
            # jsonb"), failing the whole scan rather than one column. `counts` is
            # the control: same bug, an element type nothing would have walked.
            # Filled by the UPDATE below rather than by the INSERT, so the
            # parameter adaptation of the shared executemany stays untouched.
            "docs jsonb[], "
            "counts integer[]"
            ")"
        )
        cur.executemany(
            f"INSERT INTO {TABLE} (id, ts, ts_naive, event_name, amount, user_id, doc) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)",
            [
                (
                    row.id,
                    row.ts,
                    row.ts.replace(tzinfo=None),
                    row.event_name,
                    row.amount,
                    row.user_id,
                    json.dumps(row.doc),
                )
                for row in ROWS
            ],
        )
        # Built from the columns already there, so the array values need no
        # client-side adaptation and no cast: `ARRAY[doc]` is a jsonb[] because
        # `doc` is a jsonb, and `ARRAY[id, id * 2]` is an int4[] for the same
        # reason. Every row is filled — a column that were NULL everywhere would
        # still carry the right oid, but it would not prove the scan can GROUP BY
        # the value.
        cur.execute(f"UPDATE {TABLE} SET docs = ARRAY[doc], counts = ARRAY[id, id * 2]")


#: Timezone names this fixture is allowed to set. `ALTER ROLE ... SET` is a utility
#: statement: PostgreSQL does NOT accept a bind parameter for its value ("syntax error
#: at or near $1"), so the zone has to be interpolated — and is therefore checked
#: against a closed list rather than trusted.
_ALLOWED_TIMEZONES = frozenset({"UTC", PG_HOSTILE_TZ})


def _set_pg_role_timezone(adapter: PostgresAdapter, zone: str) -> None:
    if zone not in _ALLOWED_TIMEZONES:
        raise ValueError(f"refusing to interpolate an unknown timezone: {zone!r}")
    # ALTER is a write, so it needs the read-only pin lifted exactly as seeding
    # does — see ``seeding_cursor``.
    with seeding_cursor(adapter) as cur:
        cur.execute(f"ALTER ROLE \"{_PG_USER}\" SET timezone TO '{zone}'")
        cur.execute(f"ALTER DATABASE \"{_PG_DB}\" SET timezone TO '{zone}'")


@pytest.fixture(scope="session")
def pg() -> Iterator[PostgresAdapter]:
    """A live PostgresAdapter against a seeded ``postgres:18``."""
    try:
        adapter = _pg_adapter()
    except Exception as exc:  # noqa: BLE001 — any connect failure means "not available"
        unavailable(f"postgres at {_PG_HOST}:{_PG_PORT}/{_PG_DB}: {exc}")
    try:
        adapter.test_connection()
        _seed_postgres(adapter)
        yield adapter
    finally:
        adapter.close()


@pytest.fixture(scope="session")
def pg_hostile_tz() -> Iterator[Callable[[], PostgresAdapter]]:
    """A factory for adapters connecting to a server whose ROLE/DB timezone is not UTC.

    The zone is set on the role AND the database, so it is inherited by every new
    connection: nothing but the adapter's own ``-c timezone=UTC`` startup option and
    its explicit-offset literals can keep the window and the buckets correct. Reset
    afterwards so the hostile default cannot leak into another test.
    """
    admin = _pg_adapter()
    _set_pg_role_timezone(admin, PG_HOSTILE_TZ)
    try:
        yield _pg_adapter
    finally:
        _set_pg_role_timezone(admin, "UTC")
        admin.close()


# --- ClickHouse --------------------------------------------------------------

#: The fixture time column is declared in a NON-UTC timezone on purpose. This is the
#: regression that mattered most: the pre-fix adapter emitted an offset-less literal,
#: which ClickHouse reads as the COLUMN's wall clock, so a Tokyo-declared column
#: matched none of the window's rows. Verified on a live server.
CH_COLUMN_TZ = "Asia/Tokyo"


def _ch_adapter() -> ClickHouseAdapter:
    return ClickHouseAdapter(
        host=_CH_HOST,
        port=_CH_PORT,
        database=_CH_DB,
        username=os.environ.get("TRIPL_CONF_CH_USER", "default"),
        password=os.environ.get("TRIPL_CONF_CH_PASSWORD", ""),
        json_path_discovery="all",
    )


def _seed_clickhouse(adapter: ClickHouseAdapter) -> None:
    client = adapter._client  # noqa: SLF001
    settings = {"allow_experimental_json_type": 1}
    client.command(f"DROP TABLE IF EXISTS {TABLE}")
    # `props` and `tup` are ClickHouse-only and deliberately absent from dataset.py:
    # a Map and a Tuple are the two nested families JSONAllPaths rejects, and before
    # that was handled either one made every scan on this table fail with
    # ILLEGAL_TYPE_OF_ARGUMENT. Putting them in the shared `FixtureRow` instead would
    # force the PostgreSQL and BigQuery gates to invent an equivalent for a shape
    # neither engine has. `props` varies its key set per row so the shape column has
    # more than one distinct value to group.
    client.command(
        f"CREATE TABLE {TABLE} ("
        "id UInt32, "
        f"ts DateTime64(6, '{CH_COLUMN_TZ}'), "
        "event_name String, "
        "amount Nullable(Float64), "
        "user_id String, "
        "doc JSON, "
        "props Map(String, String), "
        "tup Tuple(`a` Int32, `b` String)"
        ") ENGINE = MergeTree ORDER BY id",
        settings=settings,
    )

    def _row_literal(row: FixtureRow) -> str:
        # The instant is written with an explicit +00:00 and parsed to a UTC-pinned
        # DateTime64, so the SEEDING cannot itself be shifted by the column's Tokyo
        # timezone — the trap is for the adapter's SQL, not for the fixture loader.
        ts = row.ts.strftime("%Y-%m-%d %H:%M:%S.%f+00:00")
        amount = "NULL" if row.amount is None else repr(row.amount)
        doc = json.dumps(row.doc).replace("\\", "\\\\").replace("'", "\\'")
        # Odd ids carry one extra key, so the Map's shape array is not constant.
        props = (
            f"map('shared', 'v{row.id}', 'odd', 'v{row.id}')"
            if row.id % 2
            else f"map('shared', 'v{row.id}')"
        )
        return (
            f"({row.id}, parseDateTime64BestEffort('{ts}', 6, 'UTC'), "
            f"'{row.event_name}', {amount}, '{row.user_id}', '{doc}', "
            f"{props}, ({row.id}, '{row.event_name}'))"
        )

    values = ", ".join(_row_literal(row) for row in ROWS)
    client.command(f"INSERT INTO {TABLE} VALUES {values}", settings=settings)


@pytest.fixture(scope="session")
def ch() -> Iterator[ClickHouseAdapter]:
    """A live ClickHouseAdapter against a seeded ``clickhouse-server``."""
    try:
        adapter = _ch_adapter()
        adapter.test_connection()
        _seed_clickhouse(adapter)
    except Exception as exc:  # noqa: BLE001
        unavailable(f"clickhouse at {_CH_HOST}:{_CH_PORT}: {exc}")
    try:
        yield adapter
    finally:
        adapter.close()


# --- BigQuery emulator (analysis only) ---------------------------------------


def _post_query(sql: str) -> dict[str, object]:
    request = urllib.request.Request(  # noqa: S310 — fixed localhost emulator URL
        f"{_BQ_EMULATOR_URL}/bigquery/v2/projects/{_BQ_PROJECT}/queries",
        data=json.dumps({"query": sql, "useLegacySql": False}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            return dict(json.loads(response.read()))
    except urllib.error.HTTPError as exc:
        return dict(json.loads(exc.read()))


def _analysis_error(sql: str) -> str | None:
    """``None`` if ZetaSQL accepts the statement, else the analyzer's message."""
    body = _post_query(sql)
    error = body.get("error")
    if not error:
        return None
    if isinstance(error, dict):
        return str(error.get("message", error))
    return str(error)


@pytest.fixture(scope="session")
def bq_analyze() -> Callable[[str], str | None]:
    """Submit GoogleSQL to the emulator's real ZetaSQL analyzer; return its verdict.

    Do NOT use the ``google-cloud-bigquery`` Python client against the emulator — it
    hangs. Plain REST only.
    """
    try:
        probe = _analysis_error("SELECT 1 AS x")
    except OSError as exc:
        unavailable(f"bigquery-emulator at {_BQ_EMULATOR_URL}: {exc}")
    if probe is not None:
        unavailable(f"bigquery-emulator at {_BQ_EMULATOR_URL} rejected SELECT 1: {probe}")
    return _analysis_error


# --- the pipeline gate: source tables, warehouses, and a REAL app database ----


@dataclass(frozen=True)
class PipelineWarehouse:
    """One executing warehouse, as the metrics pipeline sees it.

    ``new_adapter`` is a FACTORY, not an adapter: the worker builds an adapter per
    task and ``close()``s it in a ``finally``, so handing every call the same
    long-lived instance would leave the second task talking to a closed connection.
    """

    db_type: str
    new_adapter: Callable[[], BaseAdapter]
    events_query: str
    facts_query: str


def _seed_postgres_pipeline(adapter: PostgresAdapter) -> None:
    with seeding_cursor(adapter) as cur:
        cur.execute(f"DROP TABLE IF EXISTS {PIPELINE_TABLE}")
        cur.execute(
            f"CREATE TABLE {PIPELINE_TABLE} ("
            "ts timestamptz NOT NULL, "
            "event_name text NOT NULL, "
            "user_id text NOT NULL, "
            "amount double precision, "
            "platform text NOT NULL"
            ")"
        )
        cur.executemany(
            f"INSERT INTO {PIPELINE_TABLE} (ts, event_name, user_id, amount, platform) "
            "VALUES (%s, %s, %s, %s, %s)",
            [
                (row.ts, row.event_name, row.user_id, row.amount, row.platform)
                for row in PIPELINE_ROWS
            ],
        )


def _seed_clickhouse_pipeline(adapter: ClickHouseAdapter) -> None:
    client = adapter._client  # noqa: SLF001
    client.command(f"DROP TABLE IF EXISTS {PIPELINE_TABLE}")
    client.command(
        f"CREATE TABLE {PIPELINE_TABLE} ("
        # Declared in Tokyo, exactly like the scan fixture: the pipeline's windows
        # and buckets have to survive a non-UTC column too, not just the adapter's
        # unit-level calls.
        f"ts DateTime64(6, '{CH_COLUMN_TZ}'), "
        "event_name String, "
        "user_id String, "
        "amount Nullable(Float64), "
        "platform String"
        ") ENGINE = MergeTree ORDER BY ts"
    )

    def _row_literal(row: PipelineRow) -> str:
        ts = row.ts.strftime("%Y-%m-%d %H:%M:%S.%f+00:00")
        amount = "NULL" if row.amount is None else repr(row.amount)
        return (
            f"(parseDateTime64BestEffort('{ts}', 6, 'UTC'), '{row.event_name}', "
            f"'{row.user_id}', {amount}, '{row.platform}')"
        )

    values = ", ".join(_row_literal(row) for row in PIPELINE_ROWS)
    client.command(f"INSERT INTO {PIPELINE_TABLE} VALUES {values}")


@pytest.fixture(scope="session")
def pg_pipeline() -> Iterator[PipelineWarehouse]:
    """PostgreSQL, seeded with the pipeline series."""
    try:
        seeder = _pg_adapter()
        seeder.test_connection()
        _seed_postgres_pipeline(seeder)
    except Exception as exc:  # noqa: BLE001
        unavailable(f"postgres at {_PG_HOST}:{_PG_PORT}/{_PG_DB}: {exc}")
    try:
        yield PipelineWarehouse(
            db_type="postgres",
            new_adapter=_pg_adapter,
            events_query=PIPELINE_EVENTS_QUERY,
            facts_query=PIPELINE_FACTS_QUERY,
        )
    finally:
        seeder.close()


@pytest.fixture(scope="session")
def ch_pipeline() -> Iterator[PipelineWarehouse]:
    """ClickHouse, seeded with the pipeline series on a Tokyo-declared time column."""
    try:
        seeder = _ch_adapter()
        seeder.test_connection()
        _seed_clickhouse_pipeline(seeder)
    except Exception as exc:  # noqa: BLE001
        unavailable(f"clickhouse at {_CH_HOST}:{_CH_PORT}: {exc}")
    try:
        yield PipelineWarehouse(
            db_type="clickhouse",
            new_adapter=_ch_adapter,
            events_query=PIPELINE_EVENTS_QUERY,
            facts_query=PIPELINE_FACTS_QUERY,
        )
    finally:
        seeder.close()


#: The pipeline gate's APPLICATION database — the one the worker writes
#: events/event_metrics/metric_values/anomalies into. It is a REAL PostgreSQL (the
#: same container the warehouse gate uses, a different set of tables), NOT the SQLite
#: file the rest of the worker tests use, for two load-bearing reasons:
#:
#: * SQLite has no timezone. It hands every ``DateTime(timezone=True)`` column back
#:   NAIVE, while a warehouse adapter returns AWARE buckets. Code that aligns a
#:   stored series against a fresh warehouse series (``per_distinct_user``) would
#:   then compare naive to aware, match nothing, and report a silent zero — a
#:   failure invented entirely by the fixture. On real PostgreSQL both sides are
#:   aware and the comparison is the one production makes.
#: * The UPSERTs are dialect-branched (``_upsert_metric_values_rows`` and friends
#:   pick ``sqlite_insert`` or ``pg_insert`` off ``session.bind``). Every existing
#:   test drives the SQLite branch; production runs the PostgreSQL one. This gate
#:   executes the branch that ships, named constraints and all.
_APP_DB_URL = f"postgresql+psycopg://{_PG_USER}:{_PG_PASSWORD}@{_PG_HOST}:{_PG_PORT}/{_PG_DB}"


@pytest.fixture(scope="session")
def app_db() -> Iterator[sessionmaker[Session]]:
    """The worker's own database: real PostgreSQL, schema created and dropped here."""
    try:
        engine = create_engine(_APP_DB_URL)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        unavailable(f"app database at {_PG_HOST}:{_PG_PORT}/{_PG_DB}: {exc}")
    # The ORM tables only. The warehouse fixture tables above are not part of
    # ``Base.metadata``, so create_all/drop_all cannot touch them.
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
