"""PostgreSQL field contracts, executed against a real server.

The assertion that matters is here: **the warehouse-side SQL and BaseAdapter's
Python fallback must return the same violations from the same rows.** The fallback
is the reference semantics (it is what ClickHouse's native validator was itself
written against), so pinning the two together is what makes "PostgreSQL now
validates natively" a statement about correctness rather than about speed.

And then the one thing the fallback CANNOT do: a violation that first appears at
row 50,001 is invisible to it, because it only ever looks at the first 50,000 rows.
``test_a_violation_past_the_sampling_limit...`` seeds exactly that table and shows
the fallback reporting a clean bill of health while the native validator finds it.

Needs a PostgreSQL. Unreachable => SKIP, so a laptop without Docker stays green:

    docker run --rm -d --name pgcap -p 55440:5432 \
      -e POSTGRES_PASSWORD=x -e POSTGRES_USER=tripl -e POSTGRES_DB=t postgres:18
    TRIPL_CONF_PG_PORT=55440 TRIPL_CONF_PG_DB=t TRIPL_CONF_PG_PASSWORD=x \
      uv run pytest src/tripl/tests/conformance/test_postgres_field_contracts_conformance.py

The variable names above are ``TRIPL_CONF_PG_*`` and the path is this file's own,
because the ``TRIPL_LIVE_PG_*`` block and the ``..._live.py`` name they belonged to
are gone — see the import comment below for why they had to be.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from tripl.core.adapters.base import (
    BaseAdapter,
    FieldContractExpectation,
    FieldContractViolation,
)
from tripl.core.adapters.postgres import PostgresAdapter

# Connection settings come from the conformance conftest, not from a private
# TRIPL_LIVE_PG_* block no workflow ever set. That block is how this file came to
# be collected by nothing: it described a server the CI job did not advertise,
# and the fixture below skipped rather than failed, so the native-validator gate
# silently never ran.
from tripl.tests.conformance.conftest import (  # noqa: E402
    _PG_DB,
    _PG_HOST,
    _PG_PASSWORD,
    _PG_PORT,
    _PG_USER,
    seeding_cursor,
    unavailable,
)

TABLE = "tripl_live_contract_fixture"
BULK_TABLE = "tripl_live_contract_bulk"
BASE = f"SELECT * FROM {TABLE}"
BULK_BASE = f"SELECT * FROM {BULK_TABLE}"

FROM_TIME = datetime(2026, 4, 2, tzinfo=UTC)
TO_TIME = datetime(2026, 4, 9, tzinfo=UTC)

#: BaseAdapter's sampling limit. The bulk table is seeded one row past it.
SAMPLE_LIMIT = 50000

#: (id, ts, event_name, amount, user_id, group_key).
#:
#: `amount` is TEXT on purpose: a numeric column cannot hold 'twelve', and the
#: malformed-number path is the one that behaves differently in every engine —
#: Python's float() raises, ClickHouse's toFloat64OrNull returns NULL, and a naked
#: Postgres ::numeric cast RAISES and takes the whole query with it.
_IN = datetime(2026, 4, 3, 12, 0, tzinfo=UTC)
_OUT = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
ROWS: tuple[tuple[int, datetime, str | None, str | None, str, str], ...] = (
    (1, _IN, "click", "1.5", "u1", "checkout"),
    (2, _IN, "click", "50", "u2", "checkout"),
    (3, _IN, "view", "0", "u3", "checkout"),
    # Over max.
    (4, _IN, "buy", "99", "u4", "checkout"),
    # Under min.
    (5, _IN, "buy", "-3", "u5", "signup"),
    # Malformed: float('twelve') raises -> BAD. A bare ::numeric cast would ERROR.
    (6, _IN, "click", "twelve", "u6", "signup"),
    # Malformed: float('') raises -> BAD.
    (7, _IN, "view", "", "user_7", "signup"),
    # NOT malformed anywhere: float('nan') parses, and every NaN comparison is
    # false, so the fallback calls this in-range. Postgres orders NaN ABOVE every
    # float, so a NaN reaching the comparison would read as "over max" — the SQL
    # excludes it instead, and this row is what proves it.
    (8, _IN, "click", "nan", "u8", "signup"),
    # float('inf') parses to +inf, which IS over max -> BAD, in every engine.
    (9, _IN, "view", "inf", "u9", "checkout"),
    # NULL amount: bad for required_null, and skipped entirely by the other three.
    (10, _IN, "click", None, "u10", "checkout"),
    (11, _IN, None, "2.5", "u11", "checkout"),
    # float8 OVERFLOW: '1e400' cleared the old syntax-only guard and then raised
    # 22003 on the cast, aborting every expectation in the UNION. In numeric it is
    # 10^400 -> over max -> BAD, which is what float('1e400') == inf says too.
    (13, _IN, "click", "1e400", "u13", "checkout"),
    # float8 UNDERFLOW-to-zero: '1e-400' raised for the same reason. 10^-400 is
    # >= 0 and <= 50 -> GOOD, which is what float('1e-400') == 0.0 says too, and it
    # is the half that proves numeric did not simply swallow everything as bad.
    (14, _IN, "view", "1e-400", "u14", "checkout"),
    # Beyond the guard's bounded magnitude: no arm matches, so it stays NULL and is
    # reported BAD -- never raised. float() reads inf, which is also over max.
    (15, _IN, "click", "9" * 310, "u15", "checkout"),
    # The fourth of this block, and it is here to hold amount's bad_rate at exactly
    # 0.5: the three above are two bad and one good, and
    # `test_a_threshold_the_bad_rate_does_not_clear_is_not_a_violation` needs a rate
    # a threshold can equal EXACTLY to show the comparison is strict. Re-deriving
    # that test around 7/13 would have made it turn on PostgreSQL's division
    # agreeing with Python's to the last bit, which is a worse thing to pin than one
    # fixture row. It earns its keep anyway: '5e1' is the guard's exponent arm on a
    # value the cast then has to handle, and 50 is exactly what the inclusive max
    # accepts -- the positive control the three magnitude rows above lack.
    (16, _IN, "view", "5e1", "u16", "checkout"),
    # Outside the window. Every count must ignore it — its amount is wildly bad and
    # its event_name is not in the enum, so a window bug shows up as a count bug.
    (12, _OUT, "explode", "9999", "nope", "checkout"),
)

CONTRACTS: tuple[FieldContractExpectation, ...] = (
    FieldContractExpectation(
        field_name="event_name",
        drift_type="enum_violation",
        threshold=0.0,
        enum_options=("click", "view"),
    ),
    FieldContractExpectation(
        field_name="event_name", drift_type="required_null_violation", threshold=0.0
    ),
    FieldContractExpectation(
        field_name="amount",
        drift_type="range_violation",
        threshold=0.0,
        min_value=0.0,
        max_value=50.0,
    ),
    FieldContractExpectation(
        field_name="amount", drift_type="required_null_violation", threshold=0.0
    ),
    FieldContractExpectation(
        field_name="user_id",
        drift_type="regex_violation",
        threshold=0.0,
        regex="^u[0-9]+$",
    ),
    # A contract nothing violates: every group_key is one of these. It must produce
    # NO violation — a validator that reports one here is inventing drift.
    FieldContractExpectation(
        field_name="group_key",
        drift_type="enum_violation",
        threshold=0.0,
        enum_options=("checkout", "signup"),
    ),
)


def _adapter(**overrides: object) -> PostgresAdapter:
    return PostgresAdapter(
        host=_PG_HOST,
        port=_PG_PORT,
        database=_PG_DB,
        username=_PG_USER,
        password=_PG_PASSWORD,
        **overrides,
    )


def _seed(adapter: PostgresAdapter) -> None:
    with seeding_cursor(adapter) as cur:
        cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
        cur.execute(
            f"CREATE TABLE {TABLE} ("
            "id integer PRIMARY KEY, ts timestamptz NOT NULL, event_name text, "
            "amount text, user_id text NOT NULL, group_key text NOT NULL)"
        )
        cur.executemany(
            f"INSERT INTO {TABLE} (id, ts, event_name, amount, user_id, group_key) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            list(ROWS),
        )

        # One violation, and it lives past the sampling limit. generate_series is
        # ordered, and nothing updates the table afterwards, so a plain seqscan
        # returns the rows in insertion order — the bad row is genuinely the last
        # one the fallback's LIMIT 50000 would have to reach.
        cur.execute(f"DROP TABLE IF EXISTS {BULK_TABLE}")
        cur.execute(
            f"CREATE TABLE {BULK_TABLE} ("
            "id integer PRIMARY KEY, ts timestamptz NOT NULL, status text)"
        )
        cur.execute(
            f"INSERT INTO {BULK_TABLE} (id, ts, status) "
            "SELECT g, TIMESTAMPTZ '2026-04-03 00:00:00+00', "
            f"CASE WHEN g > {SAMPLE_LIMIT} THEN 'banned' ELSE 'active' END "
            f"FROM generate_series(1, {SAMPLE_LIMIT + 1}) AS g"
        )


@pytest.fixture(scope="module")
def contracts_pg() -> Iterator[PostgresAdapter]:
    try:
        adapter = _adapter()
    except Exception as exc:  # noqa: BLE001 — any connect failure means "not available"
        unavailable(f"postgres at {_PG_HOST}:{_PG_PORT}/{_PG_DB}: {exc}")
    try:
        _seed(adapter)
        yield adapter
    finally:
        adapter.close()


def _comparable(
    violations: list[FieldContractViolation],
) -> dict[tuple[str, str], tuple[int, int, float, float]]:
    return {
        (v.field_name, v.drift_type): (v.bad_count, v.total_count, v.bad_rate, v.threshold)
        for v in violations
    }


def _native(adapter: PostgresAdapter, **kwargs: object) -> list[FieldContractViolation]:
    return adapter.validate_field_contracts(BASE, list(CONTRACTS), **kwargs)  # type: ignore[arg-type]


def _fallback(adapter: PostgresAdapter, **kwargs: object) -> list[FieldContractViolation]:
    # BaseAdapter's implementation, unbound: the reference semantics, run against
    # the very same rows through the very same connection.
    return BaseAdapter.validate_field_contracts(adapter, BASE, list(CONTRACTS), **kwargs)  # type: ignore[arg-type]


# --- the headline: SQL == the Python reference -------------------------------


def test_the_native_sql_returns_exactly_what_the_python_fallback_does(
    contracts_pg: PostgresAdapter,
) -> None:
    """Two unrelated implementations, one required answer.

    The fixture holds every case that has ever diverged between engines: NULLs in
    both denominators, a malformed number, an empty string, a NaN, an infinity, a
    float8 overflow, a float8 underflow-to-zero, a magnitude past what the guard
    will cast at all, an enum miss, a regex miss, and a clean contract that must
    stay silent.
    """
    contracts_pg.get_columns(BASE)
    window = {"time_column": "ts", "time_from": FROM_TIME, "time_to": TO_TIME}

    native = _native(contracts_pg, **window)
    fallback = _fallback(contracts_pg, **window)

    assert _comparable(native) == _comparable(fallback)
    # ...and the answer is the RIGHT one, not merely a shared one.
    # Fifteen rows are in the window: ids 1-11 and 13-16. Id 12 is outside it.
    assert _comparable(native) == {
        # 'buy' x2 and NULL-excluded; 14 non-null event_names in the window.
        ("event_name", "enum_violation"): (2, 14, 2 / 14, 0.0),
        ("event_name", "required_null_violation"): (1, 15, 1 / 15, 0.0),
        # 99, -3, 'twelve', '', 'inf', '1e400' and the 310-digit value are bad.
        # 'nan', '1e-400' and '5e1' are NOT. NULL is not counted.
        ("amount", "range_violation"): (7, 14, 0.5, 0.0),
        ("amount", "required_null_violation"): (1, 15, 1 / 15, 0.0),
        # 'user_7' misses ^u[0-9]+$.
        ("user_id", "regex_violation"): (1, 15, 1 / 15, 0.0),
    }
    # The clean contract stayed silent in both.
    assert ("group_key", "enum_violation") not in _comparable(native)


def test_the_sample_value_is_one_of_the_offending_values(contracts_pg: PostgresAdapter) -> None:
    contracts_pg.get_columns(BASE)
    violations = {
        (v.field_name, v.drift_type): v.sample_value
        for v in _native(contracts_pg, time_column="ts", time_from=FROM_TIME, time_to=TO_TIME)
    }
    assert violations[("event_name", "enum_violation")] == "buy"
    assert violations[("amount", "range_violation")] in {
        "-3",
        "",
        "1e400",
        "9" * 310,
        "99",
        "inf",
        "twelve",
    }
    assert violations[("user_id", "regex_violation")] == "user_7"
    # A NULL has no value to show, so it shows that it is one.
    assert violations[("amount", "required_null_violation")] == "<NULL>"


def test_a_malformed_number_does_not_abort_the_query(contracts_pg: PostgresAdapter) -> None:
    """'twelve'::numeric RAISES in Postgres. The regex guard is what stops it.

    Without it, one unparseable row does not merely mis-count its own contract — it
    fails the statement, so EVERY expectation in the UNION returns nothing and the
    scan reports no drift at all.
    """
    contracts_pg.get_columns(BASE)
    violations = _native(contracts_pg, time_column="ts", time_from=FROM_TIME, time_to=TO_TIME)

    # The query ran (a raised cast would have propagated out of execute()), and the
    # other contracts in the same UNION still produced their answers.
    assert {(v.field_name, v.drift_type) for v in violations} >= {
        ("amount", "range_violation"),
        ("event_name", "enum_violation"),
    }
    # And the malformed value was counted BAD, not skipped.
    amount = next(v for v in violations if v.drift_type == "range_violation")
    # 99, -3, 'twelve', '', 'inf' as before, plus '1e400' and the 310-digit value.
    # '1e-400' is GOOD, which is the half that proves numeric did not just swallow
    # everything.
    assert amount.bad_count == 7


def test_the_group_filter_and_the_window_are_honored(contracts_pg: PostgresAdapter) -> None:
    contracts_pg.get_columns(BASE)
    grouped = _comparable(
        _native(
            contracts_pg,
            time_column="ts",
            time_from=FROM_TIME,
            time_to=TO_TIME,
            group_column="group_key",
            group_value="signup",
        )
    )
    fallback = _comparable(
        _fallback(
            contracts_pg,
            time_column="ts",
            time_from=FROM_TIME,
            time_to=TO_TIME,
            group_column="group_key",
            group_value="signup",
        )
    )
    assert grouped == fallback
    # signup holds ids 5-8: amounts -3, 'twelve', '', 'nan'. Three bad of four.
    assert grouped[("amount", "range_violation")] == (3, 4, 0.75, 0.0)
    # ...and only 'buy' (id 5) misses the enum, out of four rows.
    assert grouped[("event_name", "enum_violation")] == (1, 4, 0.25, 0.0)


def test_a_threshold_the_bad_rate_does_not_clear_is_not_a_violation(
    contracts_pg: PostgresAdapter,
) -> None:
    contracts_pg.get_columns(BASE)
    # amount's bad_rate is exactly 0.5. The comparison is strict (>), so a threshold
    # OF 0.5 must not fire, and anything below it must.
    at_the_boundary = contracts_pg.validate_field_contracts(
        BASE,
        [
            FieldContractExpectation(
                field_name="amount",
                drift_type="range_violation",
                threshold=0.5,
                min_value=0.0,
                max_value=50.0,
            )
        ],
        time_column="ts",
        time_from=FROM_TIME,
        time_to=TO_TIME,
    )
    assert at_the_boundary == []


# --- the headline: past the sampling limit -----------------------------------


def test_a_violation_past_the_sampling_limit_is_invisible_to_the_fallback(
    contracts_pg: PostgresAdapter,
) -> None:
    """The bug this suite exists to close, stated as a test.

    50,001 rows; exactly one is bad, and it is the last one. BaseAdapter pulls
    ``limit`` (50,000) rows and counts them in Python, so it never sees row 50,001:
    it reports NO drift on a table that has drift. This is the "before".
    """
    contracts_pg.get_columns(BULK_BASE)
    contract = [
        FieldContractExpectation(
            field_name="status",
            drift_type="enum_violation",
            threshold=0.0,
            enum_options=("active",),
        )
    ]
    fallback = BaseAdapter.validate_field_contracts(contracts_pg, BULK_BASE, contract)
    assert fallback == [], (
        "the sampled fallback was expected to MISS the violation past row 50,000 — "
        "if it now finds it, this test no longer proves anything"
    )


def test_a_violation_past_the_sampling_limit_is_found_warehouse_side(
    contracts_pg: PostgresAdapter,
) -> None:
    """...and this is the "after". Same table, same contract, aggregated in Postgres."""
    contracts_pg.get_columns(BULK_BASE)
    violations = contracts_pg.validate_field_contracts(
        BULK_BASE,
        [
            FieldContractExpectation(
                field_name="status",
                drift_type="enum_violation",
                threshold=0.0,
                enum_options=("active",),
            )
        ],
    )
    assert violations == [
        FieldContractViolation(
            field_name="status",
            drift_type="enum_violation",
            bad_count=1,
            # The FULL window, not the sample: 50,001, not 50,000.
            total_count=SAMPLE_LIMIT + 1,
            bad_rate=1 / (SAMPLE_LIMIT + 1),
            threshold=0.0,
            sample_value="banned",
        )
    ]


# --- TLS material, against a real connection ---------------------------------


def test_certificate_pems_are_materialized_0600_and_cleaned_up_on_close() -> None:
    """The PEM comes from an encrypted column; libpq wants a path. Prove the bridge.

    A real connection is opened with the material on disk (sslmode=prefer, so the
    plain docker server is happy to talk without TLS), which is what shows libpq
    ACCEPTED the files we wrote — a key file it considers world-readable would be
    refused outright, and the connect would fail here.
    """
    ca = "-----BEGIN CERTIFICATE-----\nZmFrZQ==\n-----END CERTIFICATE-----"
    try:
        adapter = _adapter(sslmode="prefer", sslrootcert=ca)
    except Exception as exc:  # noqa: BLE001
        unavailable(f"postgres at {_PG_HOST}:{_PG_PORT}/{_PG_DB}: {exc}")

    directory = adapter._tls_dir  # noqa: SLF001
    assert directory is not None
    root = os.path.join(directory, "sslrootcert.pem")
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    assert adapter.test_connection() is True

    adapter.close()
    assert not os.path.exists(directory), "the private material outlived the connection"
    assert adapter._tls_dir is None  # noqa: SLF001


# --- the regex dialect claim, asked of the engine ------------------------------


def test_a_lookbehind_pattern_is_compiled_rather_than_declined(
    contracts_pg: PostgresAdapter,
) -> None:
    """PostgreSQL's ARE really does accept lookbehind — asked of the server itself.

    ``PostgresAdapter``'s class docstring says so, and since the per-expectation
    regex probe landed that sentence is load-bearing rather than trivia: a pattern
    the engine declines is now DROPPED, silently for the contract that named it.
    RE2 rejects lookaround, so ClickHouse and BigQuery decline this very pattern
    (the divergence "points the other way" for once); PostgreSQL must not, and no
    unit test can settle it — only the engine's own regex compiler can.
    """
    contracts_pg.get_columns(BASE)
    # A digit run preceded by a 'u'. 'user_7' HAS a digit, but it is preceded by
    # '_', so it is the one in-window row that misses — the same row the anchored
    # '^u[0-9]+$' contract catches, reached through a construct RE2 has no answer
    # for. Agreement with the fallback therefore says more than "it compiled".
    pattern = "(?<=u)[0-9]+"
    assert contracts_pg.contract_regex_is_compilable(pattern)

    lookbehind = [
        FieldContractExpectation(
            field_name="user_id",
            drift_type="regex_violation",
            threshold=0.0,
            regex=pattern,
        )
    ]
    window = {"time_column": "ts", "time_from": FROM_TIME, "time_to": TO_TIME}
    native = contracts_pg.validate_field_contracts(BASE, lookbehind, **window)  # type: ignore[arg-type]
    fallback = BaseAdapter.validate_field_contracts(contracts_pg, BASE, lookbehind, **window)  # type: ignore[arg-type]

    assert _comparable(native) == _comparable(fallback)
    assert _comparable(native) == {("user_id", "regex_violation"): (1, 15, 1 / 15, 0.0)}
