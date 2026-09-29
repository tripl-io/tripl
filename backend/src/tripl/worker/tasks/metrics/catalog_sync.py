"""Phase 1 of metrics collection: catalog sync via the scan pipeline.

Extracted verbatim from ``collect_metrics`` in ``tasks.py``.  The cardinality
analyzers and ``generate_events`` are passed in as callables so that tests
monkey-patching them on the ``tasks`` module keep taking effect (``tasks``
forwards its module globals at call time).

Phase 1 also samples the observed values of JSON-path variables, which is not
the scan pipeline's own work but has to happen here: it is the one point in a
collection job that runs ONCE, before the per-chunk metric loop, so a replay's
hundreds of chunks cannot multiply it. See ``_collect_json_path_samples``.
"""

from __future__ import annotations

import copy
import dataclasses
import logging
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import ColumnElement, Text, exists, or_, select
from sqlalchemy import cast as sa_cast
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session, lazyload

from tripl.core.adapters.base import BaseAdapter, ColumnInfo
from tripl.core.analyzers._event_generator_variables import (
    SCAN_PROVENANCE_DESCRIPTION,
    VARIABLE_VALUE_SAMPLE_LIMIT,
    VariableIndex,
)
from tripl.core.analyzers.cardinality import BreakdownAnalysis, _is_json_type
from tripl.core.analyzers.event_generator import GenerationResult
from tripl.core.intervals import INTERVALS
from tripl.core.property_drift import clear_stale_findings, type_findings, upsert_findings
from tripl.core.property_schema import infer_property_type
from tripl.json_paths import decode_json_path_value, format_json_path_value
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.property_drift import PropertyDrift, PropertyDriftKind
from tripl.models.scan_config import ScanConfig
from tripl.models.variable import Variable
from tripl.models.variable_value import VariableValue
from tripl.worker.plan_scope import main_branch_id
from tripl.worker.tasks._errors import NO_EVENT_NAMING_MSG, ScanError
from tripl.worker.tasks.metrics.generation import (
    _build_variable_lookup,
    _ensure_event_type_with_fields,
    _load_existing_generation_results,
    _load_latest_generation_snapshot,
)
from tripl.worker.tasks.metrics.schema_drift import (
    _detect_event_type_drift,
    _detect_field_contract_violations,
)

if TYPE_CHECKING:
    from tripl.worker.utils.query_windows import TimeWindow

logger = logging.getLogger(__name__)

# Source rows the observed-value sampler reads. A FLAT limit, so the cost of a
# run does not grow with the number of paths sampled, with the project's size or
# with the collection window — unlike the breakdown and bucketed queries, which
# it must stay a rounding error beside. 500 is a small fraction of what one tick
# already scans on the largest config here, and ample for a 20-value sample: a
# value that shows up in none of 500 rows is not one the plan needs listed.
_SAMPLE_ROW_LIMIT = 500

# Distinct values kept per path. Imported rather than restated because
# ``sample_variable_values`` truncates the stored list to exactly this — asking
# the warehouse for more would buy rows that are thrown away on arrival.
_SAMPLE_VALUES_PER_PATH = VARIABLE_VALUE_SAMPLE_LIMIT

# Paths one run will try to fill. The candidate set is self-extinguishing — a
# path leaves it once every context it hangs off holds an observation — so this
# only decides how fast a project converges, not whether it does: the largest
# project here (~1.8k JSON-path variables) is done in about nine ticks, after
# which the whole sampler costs the two candidate queries, plus the type
# backfill's one (F23.4d), per run and stops there until a new event, or a
# newly referenced field, reopens a path.
_SAMPLED_PATHS_PER_RUN = 200

# Already-observed variables one run samples to type after the fact (F23.4d,
# ``_type_backfill_candidates``). Their paths ride the same adapter call as the
# unfilled ones — the adapter is asked for COLUMNS, so a few more paths in an
# already-sampled column cost nothing in the warehouse — and each is checked
# once, so the backlog drains and then costs one indexed query per run. Small
# because it is a one-off catch-up, not a steady-state job: acme-ios's ~230 used
# JSON-path variables clear in five ticks.
_TYPE_BACKFILL_PER_RUN = 50

# Paths the adapter may enumerate while looking for the ones we asked about.
# Matches the replay sampler's limit in ``tasks``; both are a guard against a
# pathological document, not a tuning knob.
_PATH_DISCOVERY_LIMIT = 2000


class _AnalyzeCardinalityFn(Protocol):
    def __call__(
        self,
        adapter: BaseAdapter,
        base_query: str,
        columns: list[ColumnInfo],
        threshold: int = 100,
        json_value_paths: dict[str, list[str]] | None = None,
        time_column: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
        row_limit: int = 50000,
        **kwargs: object,
    ) -> BreakdownAnalysis: ...


class _AnalyzeCardinalityGroupedFn(Protocol):
    def __call__(
        self,
        adapter: BaseAdapter,
        base_query: str,
        columns: list[ColumnInfo],
        group_column: str,
        threshold: int = 100,
        json_value_paths: dict[str, list[str]] | None = None,
        time_column: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
        row_limit: int = 50000,
        **kwargs: object,
    ) -> tuple[list[str], dict[str, BreakdownAnalysis]]: ...


class _GenerateEventsFn(Protocol):
    def __call__(
        self,
        session: Session,
        project_id: uuid.UUID,
        event_type_id: uuid.UUID,
        analysis: BreakdownAnalysis,
        field_definitions: dict[str, FieldDefinition],
        cardinality_threshold: int = 100,
        event_type_column: str | None = None,
        time_column: str | None = None,
        event_name_format: str | None = None,
        event_group_rules: Sequence[Mapping[str, object]] | None = None,
        reserved_columns: Collection[str] | None = None,
        max_events: int = 10000,
        scan_config_id: uuid.UUID | None = None,
        json_path_samples: Mapping[str, Mapping[str, Sequence[str]]] | None = None,
    ) -> GenerationResult: ...


@dataclass(frozen=True)
class JsonPathSampling:
    """One run's observed-value samples, with the counters that make it visible.

    The counters exist because a scheduled run used to surface NOTHING about
    this sampler: the only sampler counter in ``result_summary``
    (``variable_values_touched``) is bound to the replay path and reads 0 on
    every scheduled run, which is how a production stall (2026-08-31: whole
    cycles moving zero contexts from empty to filled) stayed invisible in weeks
    of job summaries.

    ``ring_size`` is the whole candidate ring, ``paths_sampled`` the slice of
    it this run actually sent to the adapter, ``paths_with_samples`` how many
    of those came back with at least one value — so ring far above sampled
    means backlog, and sampled far above with-samples means paths the sampled
    rows currently carry no values for.
    """

    samples: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    # ``column -> path -> (variable_type, json_schema)`` read off the decoded
    # samples before ``_formatted_samples`` turns them into text (F23.4).
    types: dict[str, dict[str, tuple[str, dict[str, Any] | None]]] = field(default_factory=dict)
    ring_size: int = 0
    paths_sampled: int = 0
    paths_with_samples: int = 0
    variables_typed: int = 0
    type_drifts_detected: int = 0
    # ``column.path`` of every path the adapter answered for this run, the
    # unfilled window and the type backfill alike (F23.4d). ``sync_catalog``
    # stamps the untyped variables on them ``type_checked_at`` after typing the
    # ones whose samples agree, so none is asked about for its type again.
    type_checked_paths: frozenset[str] = frozenset()
    variables_type_checked: int = 0


@dataclass
class CatalogSyncResult:
    gen_results: dict[str, GenerationResult] = field(default_factory=dict)
    single_result: GenerationResult | None = None
    contract_violations_detected: int = 0
    # Event-type groups whose contracts could not be evaluated at all this run —
    # the denominator that keeps the line above honest. Without it a run that
    # checked nothing and a run that found nothing report the same 0, which is
    # how a contract the warehouse had stopped accepting could go unnoticed
    # indefinitely. Counted per group, because that is the unit the check runs
    # in: one bad contract does not cost the groups beside it their check.
    contract_checks_failed: int = 0
    # Single expectations an adapter declined while the rest of the check ran —
    # an engine-refused pattern, a REPEATED column on BigQuery, a non-finite
    # bound. A different unit from the line above, so a separate number: one
    # refused pattern is not a whole event type going unchecked, but it is a
    # contract that is no longer being evaluated (tripl-0zpq.341).
    contract_expectations_skipped: int = 0
    replay_branch_id: uuid.UUID | None = None
    # Stays at its zero default on replay: replay has its own sampler in
    # ``tasks`` and reports through ``variable_values_touched``; only scheduled
    # runs fill this, and only scheduled summaries surface it.
    json_path_sampling: JsonPathSampling = field(default_factory=JsonPathSampling)
    # A VariableIndex, not a name-keyed dict: replay has to match a field
    # value's token through bindings and source_name too, since a scan-created
    # variable's display name is no longer its warehouse path.
    replay_variables_by_token: VariableIndex = field(default_factory=VariableIndex)
    replay_events: list[Event] = field(default_factory=list)


def _unfilled_json_path_candidates(
    session: Session,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    json_columns: Collection[str],
) -> list[tuple[str, str]]:
    """``(column, path)`` pairs whose variable still has a context to fill.

    A variable is a candidate when one of its warehouse identities splits as
    ``<column>.<path>`` over a JSON column of this query — ``source_tokens_of``,
    so ``source_name`` and the user-editable bindings lead and the shortened
    display name (``property.Aalter`` -> ``aalter``) cannot be mistaken for a
    path — and at least one context row for it still holds no observation.

    The unit is the CONTEXT, not the variable, because the context is what the
    fill writes: ``variable_values`` is keyed on (variable, event, field), while
    ONE variable covers every event that references the path — across scan
    configs too, since a Variable is unique on (project, branch, source_name)
    and this query is project/branch-scoped with no config in it. Retiring a
    variable on its first observation anywhere therefore stranded every context
    minted after it: a new event on the path, an existing event that later gained
    the field or the token, a context ``delete_variable_contexts_for_event_type``
    dropped and a later run re-created, and a sibling config's events whenever
    another config sampled the shared path first. Each was written with
    ``observed_count=0`` and could never be asked about again — the production
    symptom this sampler exists to fix, back for everything but the first event.

    "At least one context row" is a requirement, not shorthand for "nothing
    observed yet": a variable with NO context rows is not a candidate. Context
    rows are minted for the event fields that reference the variable's token, so
    a contextless variable is an unused one — nothing in the plan points at it,
    and a sampled value would have no row to land in. An earlier version kept
    such variables as permanent candidates for the just-minted case, and that
    starved the fillable paths: on acme-ios 1545 of 1777 JSON-path variables
    are unused, so the ring ran ~10x its fillable size and a scheduled cycle
    measured on 2026-08-31 moved ZERO contexts from empty to filled while the
    same four already-filled paths were resampled run after run. The just-minted
    case needs no such allowance — ``generate_events`` writes a variable's
    (empty) context rows in the same pass that creates the variable, so it joins
    the ring one run later, the same one-context delay accepted below.

    "Holds no observation" is tested on ``observed_count``, an Integer column, and
    deliberately NOT on ``values``: that column is ``sa.JSON`` and not JSONB, so
    on PostgreSQL ``json = json`` has no operator and a ``!= '[]'`` comparison
    fails outright — the trap ``services._alerting_scope_readiness`` documents at
    length. The two questions have the same answer anyway, because every writer
    of a context sets the count to at least ``len(values)``.

    The sampling stays self-extinguishing, one context later than it used to be:
    a variable enters the candidate set once its first (empty) context exists and
    leaves it once every context it has is observed, so a project that has
    converged pays the two indexed queries below and issues no warehouse call at
    all.
    """
    # ``lazyload`` because ``Variable.value_contexts`` is ``lazy="selectin"``, and
    # each of those rows then selectin-loads its FieldDefinition: hydrating the
    # entities plainly would pull a project's entire context table into memory to
    # answer a question about NAMES. The contexts are answered by the grouped
    # aggregate below instead, which stays one indexed read whatever the size.
    variable_query = (
        select(Variable)
        .where(Variable.project_id == project_id)
        .options(lazyload(Variable.value_contexts))
    )
    if branch_id is not None:
        variable_query = variable_query.where(Variable.branch_id == branch_id)
    variables = list(session.execute(variable_query).scalars())
    if not variables:
        return []

    # MIN over a variable's contexts: 0 means at least one of them is still
    # unfilled. A variable with no contexts at all is absent from the map and is
    # NOT a candidate — no event field references its token, so there is no row
    # a sample could fill (the ring-bloat measurement in the docstring).
    lowest_observed_query = (
        select(VariableValue.variable_id, sa_func.min(VariableValue.observed_count))
        .where(VariableValue.project_id == project_id)
        .group_by(VariableValue.variable_id)
    )
    if branch_id is not None:
        lowest_observed_query = lowest_observed_query.where(VariableValue.branch_id == branch_id)
    lowest_observed: dict[uuid.UUID, int] = {
        variable_id: lowest for variable_id, lowest in session.execute(lowest_observed_query)
    }

    candidates: set[tuple[str, str]] = set()
    for variable in variables:
        lowest = lowest_observed.get(variable.id)
        if variable.excluded_from_scans or lowest is None or lowest > 0:
            continue
        for token in VariableIndex.source_tokens_of(variable):
            column, _, path = token.partition(".")
            if path and column in json_columns:
                candidates.add((column, path))
    return sorted(candidates)


def _untyped_scan_variable_filters() -> tuple[ColumnElement[bool], ...]:
    """A scan-minted variable nobody has typed yet (F23.4).

    Still the scan's own ``string`` with no schema and the scan's description,
    and not excluded. Shared by the type backfill and ``_apply_inferred_types``,
    so the variables sampled for a type are exactly the ones a type may land on.
    """
    return (
        Variable.variable_type == "string",
        # SQL NULL, or the JSON ``null`` a row copied before the column
        # stored None as NULL still holds.
        or_(
            Variable.json_schema.is_(None),
            sa_cast(Variable.json_schema, Text) == "null",
        ),
        Variable.description == SCAN_PROVENANCE_DESCRIPTION,
        Variable.excluded_from_scans.is_(False),
    )


def _type_backfill_candidates(
    session: Session,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    json_columns: Collection[str],
    limit: int = _TYPE_BACKFILL_PER_RUN,
) -> list[tuple[str, str]]:
    """``(column, path)`` pairs of untyped variables observed before F23.4.

    F23.4a types a variable in the run its first samples arrive, because the
    sampler only asks about paths with an unfilled context. A variable whose
    contexts were all filled before scans inferred types is never asked about
    again and keeps the placeholder ``string``; this is the catch-up for it.

    A candidate is untyped and scan-owned (``_untyped_scan_variable_filters``),
    has a JSON-path identity over this query's columns, has at least one
    OBSERVED context — an unused variable has nothing to type, and unused ones
    are the 1545-of-1777 bulk ``_unfilled_json_path_candidates`` documents —
    and has never been checked: ``type_checked_at`` is NULL.

    The check is one-off, not a ring. ``sync_catalog`` stamps every untyped
    variable on a path the adapter answered for, whatever came back — a type,
    text, mixed kinds, only nulls, or nothing — so a property whose values stay
    text leaves the set for good instead of being resampled every run, which is
    how the unfilled ring once ran at ten times its useful size. The unfilled
    window's paths are stamped too: they were just sampled, and the run their
    first values arrive is F23.4a's own chance to type them, which does not
    consult the stamp. Editing the bindings clears it
    (``variable_service.update_variable``), and a person can always set the type
    by hand.

    At most ``limit`` variables, in a stable order, so a capped batch never
    skips anyone: the ones it takes are stamped and the next run takes the next.
    """
    observed = exists().where(
        VariableValue.variable_id == Variable.id,
        VariableValue.observed_count > 0,
    )
    query = (
        select(Variable)
        .where(
            Variable.project_id == project_id,
            Variable.type_checked_at.is_(None),
            *_untyped_scan_variable_filters(),
            observed,
        )
        .order_by(Variable.source_name, Variable.id)
        .options(lazyload(Variable.value_contexts))
    )
    if branch_id is not None:
        query = query.where(Variable.branch_id == branch_id)
    candidates: list[tuple[str, str]] = []
    taken = 0
    for variable in session.execute(query).scalars():
        paths = [
            (column, path)
            for column, _, path in (
                token.partition(".") for token in VariableIndex.source_tokens_of(variable)
            )
            if path and column in json_columns
        ]
        if not paths:
            # Not a path over this config's columns: the config that has the
            # column checks it.
            continue
        candidates.extend(paths)
        taken += 1
        if taken >= limit:
            break
    return candidates


def _scheduled_tick(config: ScanConfig, *, fallback: timedelta) -> timedelta:
    """How much wall clock one scheduled run of this config covers.

    ``collect_metrics`` refuses to run a config whose ``interval`` is unset, so
    in production the spec is always there; the fallback is for the callers that
    reach the sampler without one — a direct call from a test, a future one-off —
    and keeps them on the collection window they used to rotate by.
    """
    spec = INTERVALS.get(config.interval or "")
    return spec.delta if spec is not None else fallback


def _rotating_window(
    candidates: Sequence[tuple[str, str]],
    *,
    size: int,
    window_end: datetime,
    tick: timedelta,
) -> list[tuple[str, str]]:
    """A deterministic slice of ``candidates`` that strides by its own size each run.

    A project with thousands of unfilled paths must not try to fill them all in
    one run, and must not spend every run on the same alphabetical prefix either
    — a path whose values never appear in the sampled rows stays a candidate
    forever, and a fixed window would let it block everything behind it.

    The stride is the slice's own SIZE, not one. Advancing one candidate per run
    looked like rotation and was not: consecutive slices overlapped on all but
    one of their 200 paths, so on acme-ios's ~1800-candidate ring a full lap
    took ~75 days and candidate #1600 waited ~67 days for its first attempt —
    the production stall of 2026-08-31, where whole cycles resampled the same
    already-filled prefix and moved nothing from empty to filled. Striding by
    ``size`` covers the ring instead: consecutive starts differ by ``size mod
    len``, so the reachable starts are exactly the multiples of
    ``gcd(size, len)``; adjacent distinct starts sit ``gcd`` apart and
    ``gcd <= size``, so every candidate falls inside some slice within
    ``len // gcd`` runs — full coverage every few runs rather than a 200-wide
    burst once a lap.

    The ordinal is the window end floored to the config's SCHEDULED INTERVAL,
    which is the only quantity in reach that counts RUNS. Two near misses are
    worth writing down. ``variable_values.updated_at`` looks like the obvious key
    and orders nothing: catalog sync deletes and re-inserts every context it
    touches on every run, so the column reads "now" for the whole table. The
    collection window's own span looks like the next one and is not a run count
    at all — ``_resolve_collection_window`` starts it at the last stored bucket,
    which is about three intervals back on a config that is keeping up and thirty
    on one with no metrics yet, so dividing by it would move the slice once every
    few runs and jump it somewhere unrelated the moment the backlog changed
    length.

    ``collect_metrics`` floors the window end onto the interval grid, so this
    ordinal steps by exactly one per scheduled tick and repeats for a retry of
    the same window — a retry re-samples the same slice rather than skipping
    one.
    """
    if len(candidates) <= size:
        return list(candidates)
    tick_seconds = max(int(tick.total_seconds()), 1)
    ordinal = int(window_end.timestamp()) // tick_seconds
    start = (ordinal * size) % len(candidates)
    return [candidates[(start + offset) % len(candidates)] for offset in range(size)]


def _formatted_samples(values: Sequence[object]) -> list[str]:
    """Warehouse sample values as the strings a variable context stores.

    Through ``format_json_path_value``, which is what the replay sampler uses on
    the very same adapter output. Two renderings of one value would be two values
    to the drift detector: a tick that stored ``42`` and a replay that stored
    ``"42"`` would accuse each other of drift forever.
    """
    seen: set[str] = set()
    formatted: list[str] = []
    for value in values:
        text = format_json_path_value(value)
        if text in seen:
            continue
        seen.add(text)
        formatted.append(text)
    return formatted[:_SAMPLE_VALUES_PER_PATH]


def _collect_json_path_samples(
    session: Session,
    *,
    adapter: BaseAdapter,
    config: ScanConfig,
    columns: list[ColumnInfo],
    catalog_scan_window: TimeWindow | None,
    time_from_dt: datetime,
    time_to_dt: datetime,
) -> JsonPathSampling:
    """Observed values for the JSON-path variables that have none yet.

    Answers the question the breakdown rows cannot: a JSON path becomes a
    variable precisely when the scan config does NOT list it as a kept value, so
    the rows the catalog scan already fetched hold a value for every path except
    the ones that need one (``plan_column_meta`` argues this out in full). One
    extra bounded query per run closes that gap; widening the existing ones
    cannot.

    Called once per job, outside the per-chunk loop, and never on a replay —
    replay has its own sampler in ``tasks``, over the events it replayed.

    The samples come back wrapped in a ``JsonPathSampling`` with the run's
    counters, which ``collect_metrics`` copies into ``result_summary`` — the
    class docstring explains why the counters have to exist.

    The adapter is asked for the COLUMNS that carry a candidate, and the answer is
    then narrowed to the paths this run actually wants. Narrowing on the way in
    would be better — an explicit path list is the one thing that would let
    ClickHouse skip its discovery query entirely — but ``get_json_path_samples``
    takes columns and not paths on every adapter today. Until that parameter
    exists the filter below is what stops a run from minting observations for
    paths no variable references.
    """
    json_columns = {column.name for column in columns if _is_json_type(column.type_name)}
    if not json_columns:
        return JsonPathSampling()

    branch_id = main_branch_id(session, config.project_id)
    candidates = _unfilled_json_path_candidates(
        session,
        project_id=config.project_id,
        branch_id=branch_id,
        json_columns=json_columns,
    )
    backfill = _type_backfill_candidates(
        session,
        project_id=config.project_id,
        branch_id=branch_id,
        json_columns=json_columns,
    )
    if not candidates and not backfill:
        return JsonPathSampling()

    wanted: dict[str, set[str]] = {}
    for column, path in _rotating_window(
        candidates,
        size=_SAMPLED_PATHS_PER_RUN,
        window_end=time_to_dt,
        tick=_scheduled_tick(config, fallback=time_to_dt - time_from_dt),
    ):
        wanted.setdefault(column, set()).add(path)
    paths_sampled = sum(len(paths) for paths in wanted.values())
    # Backfill paths are sampled for their TYPE only: their contexts are already
    # filled, and their values reaching ``generate_events`` would rewrite
    # observations the unfilled sampler never asked for.
    type_only: dict[str, set[str]] = {}
    for column, path in backfill:
        if path not in wanted.get(column, ()):
            type_only.setdefault(column, set()).add(path)

    try:
        discovered = adapter.get_json_path_samples(
            config.base_query,
            sorted(wanted.keys() | type_only.keys()),
            time_column=config.time_column if catalog_scan_window else None,
            time_from=catalog_scan_window[0] if catalog_scan_window else None,
            time_to=catalog_scan_window[1] if catalog_scan_window else None,
            path_limit=_PATH_DISCOVERY_LIMIT,
            sample_limit=_SAMPLE_VALUES_PER_PATH,
            sample_row_limit=_SAMPLE_ROW_LIMIT,
        )
    except Exception:
        # Caught here rather than left to the caller because the caller is
        # ``collect_metrics``' single try: an adapter timeout, a column dropped
        # since the last run, or one unaddressable path would otherwise fail the
        # whole ScanJob and stop metrics collection for this config. Observed
        # values are an enrichment — everything below plans correctly without
        # them, and the next tick asks again.
        logger.warning(
            "Observed-value sampling failed for scan config %s; continuing without samples",
            config.id,
            exc_info=True,
        )
        # The counters still report what was attempted: a summary showing paths
        # sampled but none coming back is the signature of a failing adapter, and
        # all-zeros would hide that behind "nothing to do". The backfill is not
        # stamped, so the next run asks about the same variables again.
        return JsonPathSampling(ring_size=len(candidates), paths_sampled=paths_sampled)

    samples: dict[str, dict[str, list[str]]] = {}
    types: dict[str, dict[str, tuple[str, dict[str, Any] | None]]] = {}
    for column, path_samples in discovered.items():
        paths = wanted.get(column, set())
        typed_paths = type_only.get(column, set())
        if not paths and not typed_paths:
            continue
        for path, values in path_samples.items():
            if path not in paths and path not in typed_paths:
                continue
            decoded = (
                [decode_json_path_value(value) for value in values]
                # getattr: test doubles implement the sampler alone.
                if getattr(adapter, "json_path_samples_are_text", False)
                else list(values)
            )
            inferred = infer_property_type(decoded)
            if inferred is not None:
                types.setdefault(column, {})[path] = inferred
            if path not in paths:
                continue
            formatted = _formatted_samples(values)
            if formatted:
                samples.setdefault(column, {})[path] = formatted
    return JsonPathSampling(
        samples=samples,
        types=types,
        ring_size=len(candidates),
        paths_sampled=paths_sampled,
        # Only non-empty formatted lists are stored, so this is exactly "came
        # back with at least one value".
        paths_with_samples=sum(len(column_samples) for column_samples in samples.values()),
        type_checked_paths=frozenset(
            f"{column}.{path}"
            for sent in (wanted, type_only)
            for column, paths in sent.items()
            for path in paths
        ),
    )


def _apply_inferred_types(
    session: Session,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    types: Mapping[str, Mapping[str, tuple[str, dict[str, Any] | None]]],
) -> int:
    """Give scan-minted JSON-path variables the type their samples show (F23.4).

    Once, and only to a variable nobody has typed: still the scan's own
    ``string`` with no schema, the scan's description, not excluded. A type a
    person chose is never touched, and neither is one this function already
    wrote — a later sample of another kind is drift to report (F23.5), not a
    type to rewrite silently. Returns how many variables it typed.

    A variable is typed in the run its first values arrive; one already
    observed before F23.4 is sampled once more, for its type alone, by the
    backfill (``_type_backfill_candidates``) and typed here the same way.
    """
    wanted = {
        f"{column}.{path}": inferred
        for column, paths in types.items()
        for path, inferred in paths.items()
        if inferred != ("string", None)
    }
    if not wanted:
        return 0
    query = (
        select(Variable)
        .where(Variable.project_id == project_id, *_untyped_scan_variable_filters())
        .options(lazyload(Variable.value_contexts))
    )
    if branch_id is not None:
        query = query.where(Variable.branch_id == branch_id)
    typed = 0
    for variable in session.execute(query).scalars():
        inferred = next(
            (
                wanted[token]
                for token in VariableIndex.source_tokens_of(variable)
                if token in wanted
            ),
            None,
        )
        if inferred is None:
            continue
        variable.variable_type, variable.json_schema = inferred[0], copy.deepcopy(inferred[1])
        typed += 1
    return typed


def _mark_type_checked(
    session: Session,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    tokens: Collection[str],
) -> int:
    """Stamp the untyped variables on this run's sampled paths as checked.

    After ``_apply_inferred_types``, so a variable typed this run has already
    left the type backfill's candidate set, and one whose samples stayed text,
    mixed or empty leaves it here. Only the timestamp is written, never a type
    or a schema. Returns how many variables were stamped.
    """
    if not tokens:
        return 0
    query = (
        select(Variable)
        .where(
            Variable.project_id == project_id,
            Variable.type_checked_at.is_(None),
            *_untyped_scan_variable_filters(),
        )
        .options(lazyload(Variable.value_contexts))
    )
    if branch_id is not None:
        query = query.where(Variable.branch_id == branch_id)
    now = datetime.now(UTC)
    stamped = 0
    for variable in session.execute(query).scalars():
        if any(token in tokens for token in VariableIndex.source_tokens_of(variable)):
            variable.type_checked_at = now
            stamped += 1
    return stamped


def _detect_type_drifts(
    session: Session,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    scan_config_id: uuid.UUID,
    types: Mapping[str, Mapping[str, tuple[str, dict[str, Any] | None]]],
) -> int:
    """Record a ``type_change`` for each typed variable its samples contradict.

    Runs after ``_apply_inferred_types``, so a variable typed this very run
    agrees with its samples and is not reported.
    """
    observed = {
        f"{column}.{path}": inferred
        for column, paths in types.items()
        for path, inferred in paths.items()
    }
    if not observed:
        return 0
    query = (
        select(Variable)
        .where(Variable.project_id == project_id)
        .options(lazyload(Variable.value_contexts))
    )
    if branch_id is not None:
        query = query.where(Variable.branch_id == branch_id)
    variables = list(session.execute(query).scalars())
    findings = type_findings(variables, observed)
    checked = [
        variable.id
        for variable in variables
        if any(token in observed for token in VariableIndex.source_tokens_of(variable))
    ]
    clear_stale_findings(
        session,
        project_id=project_id,
        kinds=(PropertyDriftKind.type_change,),
        scope=PropertyDrift.variable_id.in_(checked),
        still_found={(f.variable_id, f.event_id, f.kind.value) for f in findings},
    )
    return upsert_findings(
        session, project_id=project_id, scan_config_id=scan_config_id, findings=findings
    )


def sync_catalog(
    session: Session,
    *,
    adapter: BaseAdapter,
    config: ScanConfig,
    columns: list[ColumnInfo],
    skip_cols: set[str],
    json_value_path_map: dict[str, list[str]],
    scan_row_limit: int,
    metrics_row_limit: int,
    time_from_dt: datetime,
    time_to_dt: datetime,
    catalog_scan_window: TimeWindow | None,
    is_replay: bool,
    analyze_cardinality_fn: _AnalyzeCardinalityFn,
    analyze_cardinality_grouped_fn: _AnalyzeCardinalityGroupedFn,
    generate_events_fn: _GenerateEventsFn,
) -> CatalogSyncResult:
    out = CatalogSyncResult()
    # Fetched before the generation calls below because both of them consume it,
    # and ONCE for the whole config — the grouped branch included, where every
    # group is then handed the same map. That makes JSON paths the one
    # observation in this function that is not group-scoped, and the asymmetry is
    # chosen rather than overlooked, so it is written down here: a regular
    # column's ``sample_values`` reaches ``plan_column_meta`` inside that group's
    # own BreakdownAnalysis and means "values THIS GROUP emitted", while a JSON
    # path's sampled values mean "values this path carried anywhere in the
    # config's rows over the collection window", and nothing narrower.
    #
    # Per-group sampling would need a predicate ``get_json_path_samples`` takes on
    # no adapter — it takes a base query and columns; only
    # ``validate_field_contracts`` carries the group_column/group_value pair —
    # and it would multiply the warehouse cost by the group count on every tick
    # (twice over on ClickHouse, whose override discovers the paths before
    # sampling them), for good rather than until convergence: a group that never
    # emits the path leaves that context at zero, so it would keep asking
    # forever. The variable the values hang off is config-wide regardless. It is
    # unique on (project, branch, source_name): one row shared by every group,
    # every event and every scan config that references the path.
    #
    # What that costs, stated plainly: the values are examples and never a
    # group's enumeration — ``plan_column_meta`` marks them ``high`` for exactly
    # this kind of reason — and where a variable documents ``allowed_values``, a
    # drift can be raised against an event whose group never emitted the value.
    # The value did occur in the config's rows; only the event it is attributed
    # to is the wrong one.
    if not is_replay:
        out.json_path_sampling = _collect_json_path_samples(
            session,
            adapter=adapter,
            config=config,
            columns=columns,
            catalog_scan_window=catalog_scan_window,
            time_from_dt=time_from_dt,
            time_to_dt=time_to_dt,
        )
        sampling_branch_id = main_branch_id(session, config.project_id)
        variables_typed = _apply_inferred_types(
            session,
            project_id=config.project_id,
            branch_id=sampling_branch_id,
            types=out.json_path_sampling.types,
        )
        out.json_path_sampling = dataclasses.replace(
            out.json_path_sampling,
            variables_typed=variables_typed,
            variables_type_checked=_mark_type_checked(
                session,
                project_id=config.project_id,
                branch_id=sampling_branch_id,
                tokens=out.json_path_sampling.type_checked_paths,
            ),
            type_drifts_detected=_detect_type_drifts(
                session,
                project_id=config.project_id,
                branch_id=sampling_branch_id,
                scan_config_id=config.id,
                types=out.json_path_sampling.types,
            ),
        )
    json_path_samples: dict[str, dict[str, list[str]]] = out.json_path_sampling.samples

    if is_replay:
        (
            out.gen_results,
            out.single_result,
            out.replay_branch_id,
        ) = _load_latest_generation_snapshot(
            session,
            config=config,
        )
        # "No snapshot at all", never "the snapshot came back thinner than it
        # went in". ``_load_latest_generation_snapshot`` drops the events whose
        # catalog rows have since been deleted — replaying them would aim an
        # INSERT at a missing foreign-key parent — and a result that loses an
        # entry that way is still the right result: its ``col_meta`` holds the
        # historical name templates, which is the whole reason tripl-0zpq.19
        # made this path reachable. Widening the condition to "or the events are
        # empty" would hand every such replay back to the heuristic rebuild and
        # undo that fix for the surviving events too.
        if out.single_result is None and not out.gen_results:
            out.gen_results, out.single_result = _load_existing_generation_results(
                session,
                config=config,
                columns=columns,
            )
        replay_event_mappings = sum(
            len(result.events_by_name) for result in out.gen_results.values()
        )
        if out.single_result is not None:
            replay_event_mappings += len(out.single_result.events_by_name)
        logger.info(
            "Metrics replay: skipped catalog sync and loaded %s existing event mapping(s)",
            replay_event_mappings,
        )
    elif config.event_type_column:
        # Grouped scan: same as _scan_with_grouping in scan.py
        group_values, grouped_analyses = analyze_cardinality_grouped_fn(
            adapter,
            config.base_query,
            columns,
            group_column=config.event_type_column,
            threshold=config.cardinality_threshold,
            json_value_paths=json_value_path_map,
            time_column=config.time_column if catalog_scan_window else None,
            time_from=catalog_scan_window[0] if catalog_scan_window else None,
            time_to=catalog_scan_window[1] if catalog_scan_window else None,
            row_limit=scan_row_limit,
        )
        if any(
            getattr(analysis, "row_limit_reached", False) for analysis in grouped_analyses.values()
        ):
            msg = (
                "Grouped scan query reached configured row limit "
                f"({scan_row_limit}); increase scan_row_limit to avoid partial generation"
            )
            # ScanError, not ValueError: the message names the setting to change,
            # and user_facing_error only surfaces ScanError verbatim — anything
            # else is replaced by "Scan failed due to an internal error."
            # (tripl-embs).
            raise ScanError(msg)
        logger.info(f"Grouped scan: {len(group_values)} groups for {config.event_type_column!r}")

        # Catalog sync targets the main plan; a working branch deep-copies
        # event types under the same names, so lookups must be branch-scoped.
        plan_branch = main_branch_id(session, config.project_id)
        for et_name in group_values:
            existing_et = session.execute(
                select(EventType).where(
                    EventType.project_id == config.project_id,
                    EventType.branch_id == plan_branch,
                    EventType.name == et_name,
                )
            ).scalar_one_or_none()
            _detect_event_type_drift(
                session,
                existing_event_type=existing_et,
                columns=columns,
                skip_columns=skip_cols,
                scan_config_id=config.id,
                cardinality_results=getattr(grouped_analyses[et_name], "results", None),
            )
            contracts = _detect_field_contract_violations(
                session,
                adapter=adapter,
                event_type=existing_et,
                base_query=config.base_query,
                columns=columns,
                skip_columns=skip_cols,
                scan_config_id=config.id,
                time_column=config.time_column,
                time_from=time_from_dt,
                time_to=time_to_dt,
                group_column=config.event_type_column,
                group_value=et_name,
                limit=metrics_row_limit,
            )
            out.contract_violations_detected += contracts.violations_detected
            out.contract_checks_failed += contracts.checks_failed
            out.contract_expectations_skipped += contracts.expectations_skipped
            et = _ensure_event_type_with_fields(
                session,
                config.project_id,
                et_name,
                columns,
                skip_cols,
            )
            field_defs = {fd.name: fd for fd in et.field_definitions}
            result = generate_events_fn(
                session,
                config.project_id,
                et.id,
                grouped_analyses[et_name],
                field_defs,
                cardinality_threshold=config.cardinality_threshold,
                event_type_column=config.event_type_column,
                time_column=config.time_column,
                event_name_format=config.event_name_format,
                event_group_rules=config.event_group_rules,
                # The same set that kept these columns from getting a
                # FieldDefinition above, so the generator does not then report
                # their absence as a plan gap (tripl-jfm3.90).
                reserved_columns=skip_cols,
                # Stamps provenance on the variable value drifts this generates.
                # Alert dispatch filters drifts by scan config, so an unstamped
                # row is detected but can never be alerted on (tripl-l33u.1).
                scan_config_id=config.id,
                json_path_samples=json_path_samples,
            )
            out.gen_results[et_name] = result
            logger.info(
                f"  {et_name!r}: {result.events_created} created, {result.events_skipped} updated"
            )

    elif config.event_type_id:
        # Single event type: same as run_scan single-type path
        analysis = analyze_cardinality_fn(
            adapter,
            config.base_query,
            columns,
            threshold=config.cardinality_threshold,
            json_value_paths=json_value_path_map,
            time_column=config.time_column if catalog_scan_window else None,
            time_from=catalog_scan_window[0] if catalog_scan_window else None,
            time_to=catalog_scan_window[1] if catalog_scan_window else None,
            row_limit=scan_row_limit,
        )
        if getattr(analysis, "row_limit_reached", False):
            msg = (
                "The scan query reached the configured row limit "
                f"({scan_row_limit}); increase scan_row_limit to avoid partial generation"
            )
            raise ScanError(msg)  # curated wording, see the grouped guard above

        event_type = session.get(EventType, config.event_type_id)
        if event_type is None:
            msg = f"EventType {config.event_type_id} not found"
            raise ValueError(msg)

        _detect_event_type_drift(
            session,
            existing_event_type=event_type,
            columns=columns,
            skip_columns=skip_cols,
            scan_config_id=config.id,
            cardinality_results=getattr(analysis, "results", None),
        )
        contracts = _detect_field_contract_violations(
            session,
            adapter=adapter,
            event_type=event_type,
            base_query=config.base_query,
            columns=columns,
            skip_columns=skip_cols,
            scan_config_id=config.id,
            time_column=config.time_column,
            time_from=time_from_dt,
            time_to=time_to_dt,
            limit=metrics_row_limit,
        )
        out.contract_violations_detected += contracts.violations_detected
        out.contract_checks_failed += contracts.checks_failed
        out.contract_expectations_skipped += contracts.expectations_skipped
        field_defs = {fd.name: fd for fd in event_type.field_definitions}
        out.single_result = generate_events_fn(
            session,
            config.project_id,
            config.event_type_id,
            analysis,
            field_defs,
            cardinality_threshold=config.cardinality_threshold,
            event_type_column=config.event_type_column,
            time_column=config.time_column,
            event_name_format=config.event_name_format,
            event_group_rules=config.event_group_rules,
            reserved_columns=skip_cols,
            scan_config_id=config.id,
            json_path_samples=json_path_samples,
        )
        logger.info(
            f"Single scan: {out.single_result.events_created} created, "
            f"{out.single_result.events_skipped} updated"
        )
    else:
        raise ValueError(NO_EVENT_NAMING_MSG)

    if is_replay:
        if out.replay_branch_id is None and out.single_result and out.single_result.events_by_name:
            out.replay_branch_id = next(iter(out.single_result.events_by_name.values())).branch_id
        if out.replay_branch_id is None:
            for generation_result in out.gen_results.values():
                if generation_result.events_by_name:
                    out.replay_branch_id = next(
                        iter(generation_result.events_by_name.values())
                    ).branch_id
                    break
        if out.single_result:
            out.replay_events.extend(out.single_result.events_by_name.values())
        for generation_result in out.gen_results.values():
            out.replay_events.extend(generation_result.events_by_name.values())
        out.replay_variables_by_token = _build_variable_lookup(
            session,
            project_id=config.project_id,
            branch_id=out.replay_branch_id,
        )

    return out
