from __future__ import annotations

import abc
import logging
import math
import re
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime

from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.json_paths import extract_json_path, json_scalar_text, split_property_field
from tripl.models.domain_enums import MetricAggregation

logger = logging.getLogger(__name__)

# float() raises ValueError on malformed strings and TypeError on non-coercible
# inputs; catch both when coercing a sampled field value to a number.
_NUMERIC_PARSE_ERRORS = (TypeError, ValueError)


@dataclass
class ColumnInfo:
    name: str
    type_name: str
    is_nullable: bool = False


@dataclass(frozen=True)
class AggregateSpec:
    """One conditional aggregate within a multi-aggregate bucketed query.

    A single warehouse scan can compute many aggregates at once. Each spec maps
    to one output column aliased by ``key`` (a caller-stable alias used to read
    the value back out of the result rows).

    ``aggregation`` selects the aggregate function and ``column`` the
    measure/distinct column it operates on (``None`` for plain ``count``).
    ``filter_sql``, when set, is an already-validated boolean WHERE fragment that
    turns the aggregate into a conditional one (e.g. ``sumIf`` / ``... FILTER
    (WHERE ...)``), so specs with different filters can share one scan. It is
    trusted and injected as-is, exactly like the single-metric row-filter path.
    When a bucket makes the resulting cell NULL, and when it must make it 0, is
    the conditional-aggregate contract on :class:`BaseAdapter`; every adapter
    owes the same answer there.
    """

    key: str
    aggregation: MetricAggregation
    column: str | None = None
    filter_sql: str | None = None


@dataclass(frozen=True)
class SchemaColumn:
    name: str
    data_type: str


@dataclass(frozen=True)
class SchemaTable:
    name: str
    columns: list[SchemaColumn]


@dataclass(frozen=True)
class FieldContractExpectation:
    field_name: str
    drift_type: str
    threshold: float
    enum_options: tuple[str, ...] = ()
    regex: str | None = None
    min_value: float | None = None
    max_value: float | None = None


@dataclass(frozen=True)
class FieldContractViolation:
    field_name: str
    drift_type: str
    bad_count: int
    total_count: int
    bad_rate: float
    threshold: float
    sample_value: str | None = None


#: The pattern a regex probe falls back to when it needs a control answer. One
#: literal character is valid in Python ``re``, in RE2 and in POSIX ARE alike, so
#: an engine refusing to compile THIS cannot be refusing a pattern — it is an
#: engine that cannot answer the question at all. See
#: :meth:`BaseAdapter.contract_regex_is_compilable`.
_CONTRACT_REGEX_CONTROL_PATTERN = "a"

#: How many expectations one field-contract statement may carry. Each one
#: contributes three columns to a single flat aggregate, and PostgreSQL refuses
#: a target list of more than 1664 entries, so an event type with contracts on
#: every column of a very wide table could otherwise build a statement the
#: server rejects outright — failing a whole collection rather than the one
#: contract. At 256 the widest statement projects 768 columns, comfortably under
#: that, while leaving every realistic configuration in a single scan. The limit
#: that binds is PostgreSQL's; ClickHouse chunks by the same number anyway, so
#: the two engines issue the same statements for the same expectations instead
#: of diverging for no reason a reader could name.
FIELD_CONTRACT_EXPECTATIONS_PER_QUERY = 256


def clamp_field_contract_threshold(threshold: float) -> float:
    """The threshold a contract is actually judged against: clamped to [0, 1].

    Clamping cannot change a verdict, because a bad rate lives in [0, 1] too: a
    threshold of 9.0 and a threshold of 1.0 both answer "no violation", and -1.0
    and 0.0 both answer "violation" for the ``bad_count > 0`` rows that reach the
    comparison. What it changes is the number REPORTED on the violation, which
    should be the bound that was applied rather than whatever a misconfigured
    contract happens to hold.
    """
    return max(0.0, min(1.0, float(threshold)))


def contract_bound_literal(value: float) -> str:
    """Render a range contract's bound as a numeric literal every dialect parses.

    ``repr`` of a float round-trips exactly and always carries a ``.`` or an
    ``e``, which is what keeps PostgreSQL from resolving the comparison as
    ``numeric >= int8`` (see ``PostgresAdapter._contract_bad_condition``) and
    what stops a bound from reaching a warehouse as a rounded second copy of
    itself — the same reason BigQuery spells its threshold with ``repr``.

    The raise is a last line, not the guard: every caller asks
    :func:`field_contract_is_inert` first and never reaches here with a
    non-finite bound. It stays because the alternative is emitting ``< -inf``
    into a statement — GoogleSQL has no such literal, so the whole contract
    scan would fail in a worker — and a rendering helper that can emit invalid
    SQL is a worse thing to leave lying around than one that refuses.
    """
    number = float(value)
    if not math.isfinite(number):
        msg = f"Contract bound must be a finite number, got {value!r}"
        raise ValueError(msg)
    return repr(number)


def field_contract_is_inert(expectation: FieldContractExpectation) -> bool:
    """True when this expectation cannot be evaluated by ANY engine.

    The shared half of the "what do we do with a contract we cannot compile"
    rule; see the field contract section of :class:`BaseAdapter` for the whole
    of it. All four implementations consult this rather than re-deriving the
    cases, because the three SQL adapters and the Python fallback had derived
    them differently: the SQL adapters dropped an enum with no options, while
    the fallback asked ``text not in ()`` — true of every row — and reported a
    contract that constrains nothing as total drift.

    The "says nothing" cases (an empty enum, a pattern-less regex, a range with
    neither bound, a drift type nothing implements) are ordinary configuration
    states and stay silent. A non-finite bound is not: the save schema rejects
    one, so it can only reach here from a row that predates that rule or from a
    branch operation copying such a row forward. It is logged.
    """
    drift_type = expectation.drift_type
    if drift_type == "required_null_violation":
        # Pure NULL logic: no options, no pattern, no bounds and no rendering of
        # the value, so there is nothing an engine could fail to compile.
        return False
    if drift_type == "enum_violation":
        return not expectation.enum_options
    if drift_type == "regex_violation":
        return not expectation.regex
    if drift_type == "range_violation":
        bounds = (expectation.min_value, expectation.max_value)
        if all(bound is None for bound in bounds):
            return True
        if any(bound is not None and not math.isfinite(float(bound)) for bound in bounds):
            logger.warning(
                "Field contract skipped: %r has range bounds %r/%r, and a bound that is "
                "not a finite number is a comparison no warehouse can be asked to make. "
                "The other contracts in this scan still run.",
                expectation.field_name,
                *bounds,
            )
            return True
        return False
    return True


def field_contract_verdict(
    expectation: FieldContractExpectation,
    *,
    bad_count: int,
    total_count: int,
    sample_value: str | None,
) -> FieldContractViolation | None:
    """Judge one expectation from its counted rows: a violation, or ``None``.

    The single definition of what a field-contract violation IS. See the field
    contract section of :class:`BaseAdapter` for why it lives in Python rather
    than once per dialect.

    Returning before the division is not a stylistic guard: ``total_count`` is 0
    for a window with no rows at all, which is a perfectly ordinary scan of a
    quiet event type, and every SQL spelling of this rule needed its own defence
    against that same zero (``SAFE_DIVIDE``, ``if(total_count = 0, ...)``, or —
    on PostgreSQL — the hope that the planner tests ``total_count > 0`` before it
    evaluates the division sitting next to it in the same ``AND``).
    """
    if total_count <= 0 or bad_count <= 0:
        return None
    threshold = clamp_field_contract_threshold(expectation.threshold)
    bad_rate = bad_count / total_count
    # Strict: a rate that exactly MEETS its threshold has not exceeded it.
    if bad_rate <= threshold:
        return None
    return FieldContractViolation(
        field_name=expectation.field_name,
        drift_type=expectation.drift_type,
        bad_count=bad_count,
        total_count=total_count,
        bad_rate=bad_rate,
        threshold=threshold,
        sample_value=sample_value,
    )


def is_breakdown_field_of(field: str, regular_columns: list[str], json_columns: list[str]) -> bool:
    """Whether ``field`` can be a breakdown of a scan reading these columns.

    A scalar column must be one of the regular columns, as it always had to be.
    A property entry (``<json_column>.<path>``, F23 #306) must name a path of one
    of the nested columns: the adapter extracts its value per row, so it groups
    like a nullable scalar. Raises ``ValueError`` for an entry that breaks the
    property path grammar.
    """
    prop = split_property_field(field)
    if prop is None:
        return field in regular_columns
    return prop[0] in json_columns


def rank_top_n_once(
    adapter: BaseAdapter, time_from: datetime, time_to: datetime
) -> AbstractContextManager[None]:
    """``adapter.top_n_ranking_window(time_from, time_to)`` for a chunk loop.

    The chunk loops in ``worker/tasks/metrics/`` enter this rather than the
    method so a stand-in that is not a :class:`BaseAdapter` — a test double
    answering canned rows — needs no top-N machinery: it has no pre-query to
    share, and gets a no-op.
    """
    if isinstance(adapter, BaseAdapter):
        return adapter.top_n_ranking_window(time_from, time_to)
    return nullcontext()


class BaseAdapter(abc.ABC):
    """What every warehouse adapter must implement, and what callers may assume.

    Four implementations exist: ClickHouse, PostgreSQL and BigQuery emit SQL,
    while the in-memory synthetic adapter answers from Python lists. These
    docstrings are the entire specification an adapter author reads, so a wrong
    one here is a defect rather than a typo.

    Top-N breakdown folding (``values_limit``)
    ------------------------------------------
    Every method below that takes ``values_limit`` collapses the tail of a
    breakdown column into a single ``'Other'`` row, and all four
    implementations do it identically. The rule is stated here once on purpose:
    it used to be restated per method, and two of those copies disagreed about
    the ``- 1``.

    * ``values_limit is None`` keeps every distinct value and marks none of
      them as ``Other``.
    * Otherwise the top ``values_limit - 1`` values survive and everything else
      collapses into one ``'Other'`` row. The ``- 1`` is deliberate rather than
      an off-by-one: ``'Other'`` occupies one of the ``values_limit`` slots, so
      a caller asking for N series gets N-1 real values plus the rollup and
      never renders N+1 of them.
    * Values are ranked by ROW COUNT over the requested window, descending,
      with ties broken by the breakdown value itself ascending in code-point
      order. Row count stays the ranking key even on the aggregate methods:
      ranking by the aggregate was rejected because the multi-aggregate methods
      carry several specs with different filters in one scan, so there is no
      single number to rank by, and one shared rule keeps a count series and an
      aggregate series over the same column showing the same values.
    * The tie-break is load-bearing rather than cosmetic. The surviving set
      comes from a separate pre-query, and callers make one adapter call per
      chunk of the window (the chunk loops in ``worker/tasks/metrics/``), so
      that pre-query runs many times per collection. Ranked by count alone —
      the original shape — an engine may return either of two equally-counted
      values at the cutoff, so ranking the same window twice, as a retried
      chunk or a replay does, can keep a different set than the first pass did
      and silently reshape the stored series.
    * "The requested window" is the CALLER's whole collection window, not the
      chunk. Ranked per chunk, a chunked collection kept
      ``{a,b,c,d}`` for its first week and ``{a,b,e,f}`` for its last, so one
      series stored real rows for ``c`` early and none late — its counts sat
      inside ``'Other'`` there with nothing recording the demotion. A chunk
      loop therefore runs inside :meth:`top_n_ranking_window`, which makes
      every top-N pre-query rank over the loop's whole window, once, and hand
      the same set to every chunk. Outside that context the pre-query ranks
      over the method's own ``time_from`` / ``time_to``, as before.
    * "The caller" is the METRIC (or scan config) whose series is being
      written, never a batch of them. The batched fact path shares one
      breakdown scan between metrics, but it keys that scan on each limited
      metric's own window and ranks over that window, so the set a metric keeps
      does not depend on which group-mates happen to lag, and it matches the
      per-metric collectors that stay as its conformance oracle.
    * The ranking pre-query is the one statement of a chunked collection that
      spans the whole window: chunking bounds the bucketed scans, not the
      ranking. That is the price of one consistent set per collection; it is a
      single ``GROUP BY`` over the breakdown column(s) — no bucketing, no
      aggregates — and runs once per distinct pre-query, not once per chunk.
    * ``_is_other`` / ``is_other`` is the integer 1 on the rollup row and 0
      elsewhere in all four implementations, not a boolean.

    Conditional aggregates (``AggregateSpec.filter_sql``)
    -----------------------------------------------------
    Every method below that takes ``specs`` returns one cell per spec per
    bucket, and the engines must agree on when that cell is NULL, because NULL
    is not a value on this path: ``_index_multi_aggregate``
    (``worker/tasks/metrics/metric_collect.py``) skips NULL cells, so a NULL
    leaves a GAP in the stored series rather than a zero.

    * A bucket with NO row matching ``filter_sql`` is that gap. This is what
      makes the batched path agree with the per-metric path, whose separately
      filtered scan returns no row at all for such a bucket.
    * A bucket that HAS matching rows is a data point, even when the aggregate
      over them computes to 0. The presence test is therefore a count of
      matching ROWS and never the aggregate's own value: ClickHouse
      ``countIf(cond)``, PostgreSQL ``count(*) FILTER (WHERE cond)``, BigQuery
      ``COUNTIF(cond)``, and, in the in-memory adapter, "did any row match".
    * ``count_distinct`` is why this has to be written down rather than left to
      each adapter. ``count(DISTINCT m)`` is 0 BOTH for a bucket nothing
      matched and for a bucket whose matching rows all have ``m IS NULL``, so
      an adapter that tests the aggregate for 0 — the shape PostgreSQL and
      BigQuery carried, against ClickHouse's row-count gate — reports the
      second as a gap on two engines and as 0 on the third. Plain ``count`` is
      the one aggregate whose filtered value IS the row-presence count, so
      testing it for 0 asks the same question and stays spelled that way.
    * Nothing here overrides what an aggregate returns over rows it DID match:
      ``sum`` / ``avg`` / ``min`` / ``max`` over matching rows whose measure is
      NULL throughout are whatever the aggregate itself computes, not something
      the presence gate decides. The three SQL adapters hand that answer back
      untouched — ``sum(m) FILTER (WHERE cond)``, ``SUM(CASE WHEN cond THEN m
      END)``, and ``sumIf`` under a sentinel that fires only when NOTHING
      matched — so on them the cell is that engine's own ``sum`` over an input
      holding no non-NULL value: NULL under standard SQL, and therefore a gap.
    * All four agree on that last case, the in-memory adapter included:
      ``synthetic._aggregate`` answers ``None`` for ``sum`` / ``avg`` / ``min``
      / ``max`` over a set with no non-NULL measure, and ``count_distinct``
      counts 0 on all four. It used to answer ``0.0`` for ``sum`` alone, so the
      demo warehouse stored a zero where the SQL engines leave a gap — reachable,
      because ``amount`` is in ``synthetic._EVENTS_NULLABLE`` and every
      screen-view row carries NULL in it. Nothing pins the SQL
      side of the comparison by execution: the conformance fixture keeps all-NULL
      breakdown groups out by construction (``tests/conformance/dataset.py``).
    * A spec with no ``filter_sql`` is unconditional and none of this applies.

    Field contracts (``validate_field_contracts``)
    ----------------------------------------------
    ``validate_field_contracts`` below is the reference implementation, from
    sampled rows; the three SQL adapters override it and count in the
    warehouse. Whichever runs, three things are fixed.

    * ONE scan of the window per statement. Every expectation's aggregates ride
      side by side over a single ``FROM (base_query)``. A UNION ALL of one
      aggregate subquery per expectation — the shape PostgreSQL and ClickHouse
      both started with — is one statement but N scans of the same window, and
      the caller multiplies it: ``catalog_sync`` calls this once per event-type
      group, so N scans of the window become N x G reads of the same rows for a
      single collection. PostgreSQL and ClickHouse therefore hold at most
      ``FIELD_CONTRACT_EXPECTATIONS_PER_QUERY`` expectations per statement and
      issue another statement (another single scan) beyond that, rather than
      letting the target list grow with the contract count.
    * The warehouse COUNTS; the verdict is decided in Python. Each engine
      produces ``bad_count``, ``total_count`` and one ``sample_value`` per
      expectation and nothing else, and :func:`field_contract_verdict` turns
      those three numbers into a violation or into nothing. That rule —
      ``total_count > 0`` and ``bad_count > 0`` and ``bad_count / total_count``
      strictly greater than the threshold, the threshold clamped into
      ``[0, 1]`` — is stated once, there.
    * An expectation that cannot be compiled is INERT, never fatal. It
      contributes no columns to the statement, produces no violation, and — the
      half that was not shared — does not take the expectations beside it, or
      the collection around them, down with it. Neither call site used to defend
      against a raise: ``schema_drift._detect_field_contract_violations`` called
      this bare, and ``catalog_sync`` calls that once per event-type group
      inside a loop, so a single stale contract ended every group after it too.
      That call is now wrapped (the failure is counted and reported, never
      swallowed silently), which makes the wrap the second line and this rule
      still the first: an adapter that raises where it could decline costs the
      event type every OTHER contract it declared.
      :func:`field_contract_is_inert` holds the cases every engine agrees on and
      all four implementations ask it rather than re-deriving them.

    A bound that is not a finite number is in that list because it is the one
    input the four engines could not have been made to agree on by rendering it
    more carefully. GoogleSQL has no literal for infinity or NaN at all, so
    ``< -inf`` is a parse error rather than a comparison; PostgreSQL's
    ``numeric`` takes ``'Infinity'`` but orders NaN ABOVE every number;
    ClickHouse has both literals and compares them as Python does; and the
    fallback is Python. Declaring the expectation inert is the only verdict all
    four can give. Rendering just the finite bound and dropping the other was
    rejected: it reads correctly for a ``-inf`` minimum, which does mean
    "unbounded below", and inverts the contract for a ``+inf`` one, where every
    row is below the bound and the honest reading is "everything is bad".

    What an engine can compile is NOT itself shared, and the known divergences
    are declared rather than accidental.

    The first is the regex dialect. A ``contract_regex`` is screened at save time
    by Python's ``re`` (``schemas/field_definition``) and then compiled by
    whichever engine holds the data: RE2 on ClickHouse and BigQuery, POSIX ARE on
    PostgreSQL, ``re`` again in the fallback. No two of those accept quite the
    same language — RE2 rejects the lookaround that ``re`` and ARE both accept,
    ARE rejects ``re``'s ``(?P<name>...)`` — so a pattern that saved cleanly can
    be one THIS engine refuses, and it used to refuse it from inside the contract
    statement, which took every other expectation riding in that statement, and
    the collection around it, down with it. :meth:`contract_regex_is_compilable`
    asks the engine itself, once per distinct pattern per adapter, BEFORE the
    pattern reaches a statement; a refusal makes that one expectation inert here,
    says so in the log, and records it (:meth:`_skip_field_contract`) so the run
    summary reports it as ``contract_expectations_skipped``. The divergence
    itself is not fixable and is not worth
    faking: see that method for why a portable-subset screen was rejected, and
    ``PostgresAdapter``'s class docstring for the cases where all three dialects
    compile a pattern and disagree about what it MEANS, which no probe can catch.

    The second is BigQuery's. It refuses enum/regex/range on a REPEATED
    (ARRAY) column because GoogleSQL cannot cast an ARRAY to a single STRING to
    compare; ClickHouse's ``toString`` and PostgreSQL's ``::text`` both render
    one, and neither adapter reads column types on this path at all. Forcing
    the three to agree would mean spending a catalog round-trip per contract
    scan on two engines in order to DELETE a check that works there — and the
    two that work already disagree about the text they check it against
    (``['a','b']`` against ``{a,b}``), so there is no shared answer to converge
    on. The failure MODE is shared instead, which is the part that was broken:
    BigQuery raised out of ``validate_field_contracts`` before any SQL existed,
    and now skips that one expectation, logs it, records it for the caller the
    same way, and runs the rest.
    ``required_null_violation`` stays legal on such a column everywhere — it is
    pure NULL logic and needs no rendering.

    The third is PostgreSQL's range comparison domain. The
    fallback, ClickHouse and BigQuery parse the value into float64 and compare
    there; PostgreSQL compares in exact ``numeric``, because float8's input
    function raises 22003 on overflow AND underflow-to-zero and one such row
    would abort the statement every expectation shares. Exact and rounded
    comparison give different verdicts only near a bound or outside float64's
    range: ``'9007199254740993'`` against ``max_value=9007199254740992.0`` is BAD
    on PostgreSQL and in range elsewhere (both round to the same double), and
    ``'-1e-400'`` against ``min_value=0.0`` is BAD on PostgreSQL and in range in
    the fallback (``-0.0``). A numeric string longer than
    ``postgres._FINITE_NUMBER_RE`` admits (510 integer or 1275 fractional
    digits) is BAD on PostgreSQL alone as well. Closing it would mean casting
    ``numeric`` to float8, which is exactly the raise being avoided.

    Deciding in Python rather than in each dialect is what keeps the engines
    from disagreeing about a borderline rate: the comparison used to be written
    four times, once per dialect plus the fallback's own
    ``bad_rate <= expectation.threshold``, and each of the three SQL spellings
    had to defend its division against a zero denominator as well.

    BigQuery is the documented exception to the second rule and only to the
    second: it evaluates the identical expression warehouse-side, inside the
    same single pass, because its array-of-STRUCTs shape already carries one
    row per expectation and filtering there keeps a passing contract off the
    wire. That is only safe while the expression really is identical, which is
    why the threshold it interpolates has to round-trip the double exactly.

    For the three SQL engines ``limit`` bounds only how many violation rows come
    back, never what is evaluated — evaluating everything is the point of
    counting in the warehouse. The fallback is the exception it cannot help
    being: it counts the rows it sampled, so there ``limit`` bounds both.
    """

    # Set only inside ``top_n_ranking_window``.
    _top_n_ranking_window: tuple[datetime, datetime] | None = None
    _top_n_ranking_cache: dict[object, object] | None = None

    @contextmanager
    def top_n_ranking_window(self, time_from: datetime, time_to: datetime) -> Iterator[None]:
        """Rank every top-N pre-query over ``[time_from, time_to)``, once.

        Wrap a chunk loop in this so each chunk folds the SAME surviving values
        into ``'Other'`` — see the top-N section of the class docstring. Each
        distinct pre-query (base query, time column, breakdown columns, limit,
        ranking window) runs once for the whole block and is reused by every
        later chunk.

        Nested blocks switch the window but keep the OUTERMOST block's cache,
        which is safe because the window is part of every cache key. The
        batched fact path relies on that: one outer block spans its chunk loop
        and each limited breakdown scan re-enters with its metric's own window,
        so each window is still ranked once rather than once per chunk.
        """
        previous = (self._top_n_ranking_window, self._top_n_ranking_cache)
        self._top_n_ranking_window = (time_from, time_to)
        if self._top_n_ranking_cache is None:
            self._top_n_ranking_cache = {}
        try:
            yield
        finally:
            self._top_n_ranking_window, self._top_n_ranking_cache = previous

    def _ranking_window(self, time_from: datetime, time_to: datetime) -> tuple[datetime, datetime]:
        """The window a top-N pre-query ranks over: the caller's, else the call's."""
        return self._top_n_ranking_window or (time_from, time_to)

    def _ranked_once[RankT](self, key: object, rank: Callable[[], RankT]) -> RankT:
        """Run ``rank`` once per ``key`` inside ``top_n_ranking_window``.

        Outside the context there is nothing to share, so ``rank`` simply runs.
        """
        cache = self._top_n_ranking_cache
        if cache is None:
            return rank()
        if key not in cache:
            cache[key] = rank()
        return cache[key]  # type: ignore[return-value]

    def _top_breakdown_values_multi(
        self,
        base_query: str,
        time_column: str,
        breakdown_columns: list[str],
        time_from: datetime,
        time_to: datetime,
        limit: int,
    ) -> dict[str, list[str]]:
        """Each column's surviving top-N values, ranked over the ranking window.

        The SQL adapters' shared seam: it resolves the window (the caller's
        whole one inside :meth:`top_n_ranking_window`) and runs the engine's
        :meth:`_query_top_breakdown_values_multi` once per distinct pre-query
        there.
        """
        rank_from, rank_to = self._ranking_window(time_from, time_to)
        key = (
            "top_breakdown_values",
            base_query,
            time_column,
            tuple(breakdown_columns),
            limit,
            rank_from,
            rank_to,
        )
        ranked = self._ranked_once(
            key,
            lambda: self._query_top_breakdown_values_multi(
                base_query, time_column, breakdown_columns, rank_from, rank_to, limit
            ),
        )
        return {column: list(values) for column, values in ranked.items()}

    def _query_top_breakdown_values_multi(
        self,
        base_query: str,
        time_column: str,
        breakdown_columns: list[str],
        time_from: datetime,
        time_to: datetime,
        limit: int,
    ) -> dict[str, list[str]]:
        """The engine's top-N pre-query over exactly ``[time_from, time_to)``."""
        raise NotImplementedError

    @abc.abstractmethod
    def test_connection(self) -> bool: ...

    @abc.abstractmethod
    def get_columns(self, base_query: str) -> list[ColumnInfo]: ...

    @abc.abstractmethod
    def get_schema_tables(self) -> list[SchemaTable]:
        """Read-only catalog introspection: list tables and their columns.

        Returns the tables visible in the data source's configured database /
        schema / dataset, each with its ordered columns. Used to power SQL
        editor autocomplete; the query is a bounded, read-only catalog scan and
        takes no user-supplied input.
        """
        ...

    @abc.abstractmethod
    def get_preview_rows(
        self,
        base_query: str,
        limit: int = 10,
        *,
        time_column: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
    ) -> tuple[list[str], list[tuple[object, ...]]]: ...

    #: Whether :meth:`json_string_source` is implemented (F23.9, #306).
    supports_json_string_columns: bool = False

    def json_string_source(self, base_query: str, columns: list[str]) -> str:
        """``base_query`` with each of ``columns`` (String-typed) parsed as JSON.

        The opt-in behind a scan config's ``json_string_columns``: the returned
        query projects every column of ``base_query`` unchanged except these,
        which become the dialect's JSON type. Everything downstream then reads
        them as it reads a native JSON column — type introspection reports JSON,
        so path discovery, sampling, the breakdown's shape column, property
        extraction and contracts need no second code path. A row whose text is
        not a JSON object must not fail the query: it reads as an empty (or
        NULL) document, i.e. as a row that carries none of the keys.

        Callers go through ``core.json_string_columns``, which checks the
        columns' types first and caches the result per adapter.
        """
        msg = (
            "This data source cannot parse String columns as JSON; "
            "only ClickHouse and BigQuery can."
        )
        raise WarehouseCapabilityError(msg)

    #: Whether ``get_json_path_samples`` returns each value as JSON TEXT
    #: (``'"42"'`` for a string, ``'42'`` for a number) rather than decoded.
    #: The fallback below returns decoded values, so decoding them again would
    #: turn the string ``"123"`` into a number; only the warehouse-side
    #: overrides emit text (F23.4 type inference reads this).
    json_path_samples_are_text: bool = False

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
        """Best-effort JSON path discovery for adapters without native support.

        Concrete adapters can override this with a warehouse-side path discovery
        query. The default keeps behavior compatible by sampling more rows than
        the visible preview and flattening JSON locally.

        Paths are leaves. ``include_objects`` also reports every non-empty
        nested object at its own path, sampled whole from one row, so an object
        property's sub-schema can be inferred (F23.4e). The scan asks for them
        only when it holds an object property still waiting for values; the
        preview's path picker never does.
        """
        if not json_columns or path_limit <= 0 or sample_limit <= 0 or sample_row_limit <= 0:
            return {column: {} for column in json_columns}

        from tripl.json_paths import (
            decode_json_path_value,
            flatten_json_paths,
            format_json_path_value,
        )

        column_names, rows = self.get_preview_rows(
            base_query,
            limit=sample_row_limit,
            time_column=time_column,
            time_from=time_from,
            time_to=time_to,
        )
        index_by_name = {name: index for index, name in enumerate(column_names)}
        samples_by_column: dict[str, dict[str, list[object]]] = {
            column: {} for column in json_columns
        }
        seen_by_column: dict[str, dict[str, set[str]]] = {column: {} for column in json_columns}

        for row in rows:
            for column in json_columns:
                index = index_by_name.get(column)
                if index is None or index >= len(row):
                    continue
                parsed_value = decode_json_path_value(row[index])
                for path, raw_value in flatten_json_paths(
                    parsed_value, include_objects=include_objects
                ):
                    column_samples = samples_by_column.setdefault(column, {})
                    if path not in column_samples and len(column_samples) >= path_limit:
                        continue
                    seen = seen_by_column.setdefault(column, {}).setdefault(path, set())
                    sample_text = format_json_path_value(raw_value)
                    if sample_text in seen or len(seen) >= sample_limit:
                        continue
                    seen.add(sample_text)
                    column_samples.setdefault(path, []).append(raw_value)

        return samples_by_column

    #: Memo for :meth:`contract_regex_is_compilable`, created on first use.
    #: ``None`` at class level rather than a dict, for two reasons: an adapter is
    #: built per task and never runs ``BaseAdapter.__init__`` (the unit tests
    #: build one with ``object.__new__``, which is why
    #: ``PostgresAdapter._tls_dir`` carries a class-level default too), and a
    #: dict declared here would be ONE dict shared by every instance of the
    #: class — it would answer for a server this adapter never connected to, and
    #: outlive the connection whose version decided the answer.
    _contract_regex_support: dict[str, bool] | None = None

    def contract_regex_is_compilable(self, pattern: str) -> bool:
        """Whether THIS engine's regex library accepts ``pattern``.

        The gate every implementation puts in front of a ``regex_violation``
        expectation, so that a pattern this engine refuses is inert here instead
        of fatal to the whole scan — the third fixed rule in the field contract
        section of this class. Which patterns those are is the first of the
        declared divergences documented there.

        Asking the engine is the whole design. The alternative was a static
        screen for a "portable subset", either here or at the save boundary, and
        it is wrong in both directions: it would have to reject the lookahead a
        PostgreSQL-only project is entitled to use, and it would still be
        guessing at the grammar of a library that can simply be asked.

        Answers are memoized per adapter, so the cost is one tiny statement per
        DISTINCT pattern per task no matter how many event-type groups
        ``catalog_sync`` runs, and nothing at all for a config with no regex
        contract. A refusal is NOT retried within the run; a probe that could not
        RUN is not cached at all, because the next call may be past whatever was
        wrong with the connection.

        The two failure modes are told apart by asking the same question about a
        pattern that cannot be the problem, rather than by reading driver
        exception types — each engine spells those differently, and a taxonomy
        written here would be three claims about three libraries that only a live
        warehouse could check. If the control pattern compiles, the engine is
        answering questions and its refusal was about the pattern. If it does
        not, we learned nothing, and the honest answer is to leave the contract
        in the scan: the real statement will fail moments later and the caller
        contains that (``worker/tasks/metrics/schema_drift``), which is a far
        better outcome than silently retiring a working contract because a
        connection blinked.
        """
        cache = self._contract_regex_support
        if cache is None:
            cache = {}
            self._contract_regex_support = cache
        remembered = cache.get(pattern)
        if remembered is not None:
            return remembered

        engine = type(self).__name__
        try:
            self._probe_contract_regex(pattern)
        except Exception:
            try:
                self._probe_contract_regex(_CONTRACT_REGEX_CONTROL_PATTERN)
            except Exception:
                logger.warning(
                    "%s could not probe the contract pattern %r, so it is left in this "
                    "scan unjudged: the probe itself failed, which says nothing about "
                    "the pattern.",
                    engine,
                    pattern,
                    exc_info=True,
                )
                return True
            # Logged with the engine's own message: "invalid perl operator: (?!"
            # names the construct, which is the only thing that tells an operator
            # what to change. Nothing else reports this — the contract simply
            # stops being checked on this engine — so it is a warning and it
            # carries the pattern.
            logger.warning(
                "Field contract skipped: %s cannot compile the pattern %r, so its "
                "regex contract is not evaluated here. The other contracts in this "
                "scan still run.",
                engine,
                pattern,
                exc_info=True,
            )
            cache[pattern] = False
            return False
        cache[pattern] = True
        return True

    #: Expectations this adapter declined to evaluate since the caller last
    #: asked, in the order it declined them. ``None`` at class level for the same
    #: reason as ``_contract_regex_support``: a dict or list declared here would
    #: be shared by every instance, and an adapter built with ``object.__new__``
    #: never runs an ``__init__`` that could create it.
    _skipped_field_contracts: list[FieldContractExpectation] | None = None

    def _skip_field_contract(self, expectation: FieldContractExpectation) -> None:
        """Record that ``expectation`` is being dropped from this scan unevaluated.

        Called at every place an implementation of ``validate_field_contracts``
        declines ONE expectation it was handed and still runs the rest: a
        pattern this engine will not compile, a non-finite range bound, a column
        this engine cannot render (BigQuery's REPEATED), a column absent from the
        result. Each of those used to leave nothing but a log line, so a contract
        that silently stopped being evaluated reported exactly like one that was
        being met. The caller reads the record
        through :meth:`take_skipped_field_contracts` and reports it.

        The "says nothing" inert cases — an empty enum, a pattern-less regex, a
        range with no bound — are NOT recorded: they are ordinary configuration
        states that no engine could check, not checks that stopped running.
        """
        skipped = self._skipped_field_contracts
        if skipped is None:
            skipped = []
            self._skipped_field_contracts = skipped
        skipped.append(expectation)

    def _field_contract_is_inert(self, expectation: FieldContractExpectation) -> bool:
        """:func:`field_contract_is_inert`, recording the one inert case that is a skip.

        A range bound that is not a finite number is the only shared inert case
        that is not a configuration state (see that function), so it is the only
        one :meth:`_skip_field_contract` hears about.
        """
        if not field_contract_is_inert(expectation):
            return False
        if expectation.drift_type == "range_violation" and any(
            bound is not None and not math.isfinite(float(bound))
            for bound in (expectation.min_value, expectation.max_value)
        ):
            self._skip_field_contract(expectation)
        return True

    def _contract_operand(
        self, expectation: FieldContractExpectation, render: Callable[[str], str]
    ) -> str | None:
        """``render(field_name)``, or ``None`` for a property this engine cannot extract.

        A property contract (``<json_column>.<path>``, F23 #306) is derived from
        the plan, not typed against this warehouse's schema, so its path can be
        one the engine cannot address — a nested path of a ClickHouse ``Map``,
        an undeclared BigQuery STRUCT field, a column the query stopped
        returning. That drops the one expectation, recorded as a skip, instead
        of failing every contract riding in the same statement. A scalar
        column's error propagates exactly as before.
        """
        try:
            return render(expectation.field_name)
        except ValueError:
            if split_property_field(expectation.field_name) is None:
                raise
            logger.warning(
                "Field contract skipped: %s cannot extract the property %r, so its %s "
                "contract is not evaluated here. The other contracts in this scan still run.",
                type(self).__name__,
                expectation.field_name,
                expectation.drift_type,
                exc_info=True,
            )
            self._skip_field_contract(expectation)
            return None

    def take_skipped_field_contracts(self) -> list[FieldContractExpectation]:
        """The expectations skipped since the last call, and forget them.

        ``worker/tasks/metrics/schema_drift`` calls this after every
        ``validate_field_contracts`` call and reports the count as
        ``contract_expectations_skipped`` beside ``contract_checks_failed``: the
        latter counts event-type groups whose whole check could not run, this
        counts single expectations dropped from a check that did run.
        """
        skipped = self._skipped_field_contracts or []
        self._skipped_field_contracts = None
        return skipped

    def _probe_contract_regex(self, pattern: str) -> None:
        """Ask this engine to compile ``pattern``; raise if it will not.

        The default is Python's ``re``, and that is an answer rather than a stub:
        the fallback below matches with ``re.search``, so ``re`` IS the regex
        engine of every adapter that does not override this. It also closes the
        fallback's own copy of the bug — ``re.compile`` of a stored pattern used
        to run unguarded inside the row loop, where it raises for a row that
        predates the save-time screen or was copied in by a branch operation.

        SQL adapters override it with a table-less statement over the same
        function their contract SQL uses.
        """
        re.compile(pattern)

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
        """Fallback field-contract validation from sampled rows.

        Native adapters should override this with aggregate warehouse queries.
        The fallback preserves behavior for adapters without a custom
        implementation and is intentionally bounded by ``limit``: it counts only
        the rows it pulled back, so it is the one implementation for which
        ``limit`` bounds what is EVALUATED rather than what is returned.

        It counts differently from the SQL adapters — row by row in Python — but
        it judges identically: the counting ends at
        :func:`field_contract_verdict`, the same function the warehouse-side
        adapters hand their aggregates to. The conformance gate asserts native
        == fallback over a fixture built from every case that has ever diverged
        between engines, and a second copy of the comparison here is exactly how
        that assertion would come to compare two rules instead of one.

        It skips the same inert expectations for the same reason: the loop below
        has no branch that could decline one, so an empty enum would reach
        ``text not in ()`` and report every row it counted as drift, where the
        three SQL adapters compile no column for it at all.
        """
        if not expectations:
            return []

        column_names, rows = self.get_preview_rows(
            base_query,
            limit=limit,
            time_column=time_column,
            time_from=time_from,
            time_to=time_to,
        )
        index_by_name = {name: index for index, name in enumerate(column_names)}
        group_index = index_by_name.get(group_column) if group_column else None
        if group_column and group_index is None:
            msg = f"Group column {group_column!r} not found in query result"
            raise ValueError(msg)

        violations: list[FieldContractViolation] = []
        for expectation in expectations:
            if self._field_contract_is_inert(expectation):
                continue
            # A property (``<json_column>.<path>``) is read out of its column's
            # document row by row: absent or JSON null is NULL, like the SQL
            # adapters' extraction.
            prop = split_property_field(expectation.field_name)
            field_index = index_by_name.get(expectation.field_name if prop is None else prop[0])
            if field_index is None:
                self._skip_field_contract(expectation)
                continue
            bad_count = 0
            total_count = 0
            sample_value: str | None = None
            regex: re.Pattern[str] | None = None
            if expectation.drift_type == "regex_violation":
                # The assert narrows the type; a pattern-less regex is inert above.
                assert expectation.regex is not None
                # The same gate the three SQL adapters apply, for the same reason
                # and with the same blast radius: a pattern this engine cannot
                # compile drops its own expectation and nothing else.
                #
                # WHICH engine that is depends on ``self``, not on this module.
                # For an adapter that does not override ``_probe_contract_regex``
                # the probe is Python's ``re``, i.e. the compile that used to sit
                # unguarded on this line. It is also called UNBOUND on a live SQL
                # adapter — ``conformance/test_postgres_field_contracts_conformance``
                # runs ``BaseAdapter.validate_field_contracts(adapter, ...)`` as
                # its "Python fallback" — and there the compilability answer is
                # the SERVER's while the matching below is still ``re.search``.
                # So that gate is NOT an independent second opinion about which
                # patterns compile: a Python-style named group (``(?P<name>...)``,
                # which the PostgresAdapter docstring names as invalid POSIX ARE)
                # is skipped on BOTH sides and the comparison passes over two
                # empty results. The divergence class the gate exists to police
                # is the one it can no longer fail on; closing that needs a
                # reference whose probe is ``re`` — not a comment here.
                if not self.contract_regex_is_compilable(expectation.regex):
                    self._skip_field_contract(expectation)
                    continue
                regex = re.compile(expectation.regex)

            for row in rows:
                if group_index is not None:
                    raw_group = row[group_index]
                    if ("" if raw_group is None else str(raw_group)) != group_value:
                        continue

                raw_value = row[field_index]
                if prop is not None:
                    raw_value = json_scalar_text(extract_json_path(raw_value, prop[1]))
                is_bad = False
                if expectation.drift_type == "required_null_violation":
                    total_count += 1
                    is_bad = raw_value is None
                else:
                    if raw_value is None:
                        continue
                    total_count += 1
                    text = str(raw_value)
                    if expectation.drift_type == "enum_violation":
                        is_bad = text not in expectation.enum_options
                    elif expectation.drift_type == "regex_violation" and regex is not None:
                        is_bad = regex.search(text) is None
                    elif expectation.drift_type == "range_violation":
                        try:
                            numeric = float(text)
                        except _NUMERIC_PARSE_ERRORS:
                            is_bad = True
                        else:
                            is_bad = (
                                expectation.min_value is not None
                                and numeric < expectation.min_value
                            ) or (
                                expectation.max_value is not None
                                and numeric > expectation.max_value
                            )

                if is_bad:
                    bad_count += 1
                    if sample_value is None:
                        sample_value = "<NULL>" if raw_value is None else str(raw_value)

            violation = field_contract_verdict(
                expectation,
                bad_count=bad_count,
                total_count=total_count,
                sample_value=sample_value,
            )
            if violation is not None:
                violations.append(violation)

        return violations

    @abc.abstractmethod
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
        """Single GROUP BY ALL query that returns everything.

        Builds: SELECT reg1, reg2, ..., JSONAllPaths(j1), ...,
                       keep_json_value1, ..., count() AS _cnt
                FROM (base_query) [WHERE time_col >= ? AND time_col < ?]
                GROUP BY ALL ORDER BY _cnt DESC LIMIT limit

        Returns (regular_col_names, json_col_names, json_value_names, rows).
        Row layout: (reg_val1, ..., json_paths_array1, ..., keep_json_value1, ..., count).
        """
        ...

    @abc.abstractmethod
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
        """Time-bucketed GROUP BY ALL, like get_full_breakdown but with a time bucket.

        Builds: SELECT toStartOfInterval(time_col, INTERVAL ...) AS _bucket,
                       col1, col2, ..., keep_json_value1, ..., count() AS _cnt
                FROM (base_query) WHERE time_col >= ? AND time_col < ?
                GROUP BY ALL ORDER BY _bucket LIMIT limit

        Returns (column_names, json_value_names, rows).
        Row layout: (_bucket, col1_val, col2_val, ..., keep_json_value1, ..., count).
        """
        ...

    @abc.abstractmethod
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
        """Time-bucketed counts grouped by one breakdown column in the database.

        ``values_limit`` folds the tail into ``'Other'`` under the top-N contract
        on :class:`BaseAdapter`.

        Returns (column_names, json_value_names, rows).
        Row layout: (
            _bucket, _breakdown_value, _is_other,
            col1_val, col2_val, ..., keep_json_value1, ..., count
        ).
        """
        ...

    @abc.abstractmethod
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
        """Time-bucketed counts for multiple independent breakdown columns.

        Implementations should aggregate in the database. For ClickHouse this
        uses GROUPING SETS so selected breakdown dimensions share one source scan.
        ``values_limit`` folds each column's tail into ``'Other'`` independently,
        under the top-N contract on :class:`BaseAdapter`.

        Returns (column_names, json_value_names, rows).
        Row layout: (
            _bucket, _breakdown_column, _breakdown_value, _is_other,
            col1_val, col2_val, ..., keep_json_value1, ..., count
        ).
        """
        ...

    @abc.abstractmethod
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
        """Time-bucketed AGGREGATE, mirroring get_time_bucketed_counts.

        Computes ``agg_fn`` over ``measure_column`` per time bucket instead of a
        plain ``count()``. ``count`` ignores ``measure_column`` and aggregates
        rows; ``count_distinct``/``sum``/``avg``/``min``/``max`` require it. The
        measure column is validated against the ``get_columns`` allowlist and
        escaped with the same identifier helper as the count path; literals are
        escaped identically and there are NO bound parameters.

        Returns (column_names, json_value_names, rows) — the SAME bucketed shape
        the count methods return, so downstream parsing stays uniform.
        Row layout: (
            _bucket, col1_val, ..., json_paths1, ..., keep_json_value1, ...,
            aggregate_value
        ), where the final positional column is the aggregate value (the slot
        the count methods fill with ``count``). The ``keep_json_value`` block is
        one column per entry of the returned ``json_value_names``, in that
        order, exactly as on get_time_bucketed_counts.
        """
        ...

    @abc.abstractmethod
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
        """Time-bucketed aggregate grouped by one breakdown column.

        Mirrors get_time_bucketed_breakdown_counts but emits ``agg_fn`` over
        ``measure_column`` instead of ``count()``. ``values_limit`` folds the
        tail into an ``'Other'`` bucket under the top-N contract on
        :class:`BaseAdapter`, exactly like the count path.

        The breakdown column is grouped by its FOLDED value ONLY. It also
        occupies its own slot in the regular-column block — ClickHouse,
        PostgreSQL and BigQuery all reject a ``breakdown_column`` that is not
        also in ``regular_columns``, and the in-memory adapter accepts either —
        and that slot repeats the folded value rather than the raw one. Grouping
        by the raw column as well would give one ``'Other'`` row per raw value
        that fell into it, which is the one thing the rollup exists to prevent,
        and would likewise split a nullable column's NULL and ``''`` rows even
        though both render to ``''``. The sibling
        get_time_bucketed_multi_aggregate_breakdown never had the problem
        because it carries no regular columns at all.

        This is therefore the ONE place the "mirrors the count path" sentence
        above stops holding: get_time_bucketed_breakdown_counts(_multi) still
        groups every regular column raw, breakdown included, in all four
        implementations. That is a different situation rather than the same
        defect left unfixed — the count path's consumer in
        ``worker/tasks/metrics/`` re-aggregates its rows in Python under a key
        that never carries the raw value, so a fan-out there costs rows fetched
        and nothing else, while THIS method's rows go straight into an upsert
        whose conflict target is the folded key.

        Repeating the folded value was chosen over dropping the breakdown
        column from the projection: dropping it would shorten every row and
        shift the JSON blocks left, and callers read this layout positionally.

        Returns (column_names, json_value_names, rows).
        Row layout: (
            _bucket, _breakdown_value, _is_other,
            col1_val, ..., json_paths1, ..., keep_json_value1, ...,
            aggregate_value
        ), where the ``col_val`` for ``breakdown_column`` equals
        ``_breakdown_value``.
        """
        ...

    @abc.abstractmethod
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
        """Many bucketed aggregates from ONE source scan.

        Builds: SELECT <bucket(time_column)> AS bucket,
                       <agg_expr(spec1)> AS k1, <agg_expr(spec2)> AS k2, ...
                FROM (base_query)
                WHERE time_column >= time_from AND time_column < time_to
                GROUP BY bucket ORDER BY bucket [LIMIT limit]

        Each spec becomes one conditional/plain aggregate column aliased by its
        ``key``. The measure/distinct column is validated against the
        ``get_columns`` allowlist and escaped with the same identifier helper as
        the single-aggregate path; ``filter_sql`` is a pre-validated boolean
        fragment injected as-is to form a conditional aggregate. There are NO
        bound parameters.

        Returns (column_names, rows) where ``column_names`` is
        ``["bucket", spec1.key, spec2.key, ...]`` and each row is
        ``(bucket, k1_value, k2_value, ...)``. Unlike the single-aggregate
        method, this does NOT return a json_value_names element.
        """
        ...

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
        """Build the primary multi-aggregate statement without executing it.

        Real SQL-backed adapters override this with the same builder used by
        :meth:`get_time_bucketed_multi_aggregate`. Adapters without an SQL
        representation (currently the in-memory synthetic adapter) deliberately
        keep the default error.
        """
        del base_query, time_column, interval, specs, time_from, time_to, limit
        raise NotImplementedError("Adapter does not generate warehouse SQL")

    @abc.abstractmethod
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
        """Many bucketed aggregates grouped by one breakdown column, ONE scan.

        Like get_time_bucketed_multi_aggregate but additionally groups by
        ``breakdown_column``. ``values_limit`` folds the tail into an ``'Other'``
        rollup row (``is_other = 1``) under the top-N contract on
        :class:`BaseAdapter`, identically to the single breakdown method
        get_time_bucketed_aggregate_breakdown. This docstring used to restate
        that cut and restated it wrongly — it promised the top ``values_limit``
        while every implementation kept one fewer — which is why the number now
        lives in exactly one place and each method only points at it.

        Returns (column_names, rows) where ``column_names`` is
        ``["bucket", "breakdown_value", "is_other", spec1.key, spec2.key, ...]``
        and each row is
        ``(bucket, breakdown_value, is_other, k1_value, k2_value, ...)``. Unlike
        the single-aggregate method, this does NOT return a json_value_names
        element.
        """
        ...

    @abc.abstractmethod
    def close(self) -> None: ...
