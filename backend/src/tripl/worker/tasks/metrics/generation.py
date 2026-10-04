"""Catalog-generation, replay-sampling, and window-chunking helpers.

These back the Phase 1 (event sync) and replay paths of ``collect_metrics``:
loading generation snapshots, rebuilding generation results from existing
catalog rows, accumulating/merging replay variable samples, and splitting a
collection window into bounded sub-windows. They are pure with respect to the
warehouse — they only read/write Postgres via the passed-in session — so they
live here, keeping ``__init__`` focused on task orchestration.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import or_, select
from sqlalchemy.orm import InstrumentedAttribute, Session, selectinload

from tripl.core.adapters.base import ColumnInfo
from tripl.core.analyzers._event_generator_variables import (
    VARIABLE_VALUE_SAMPLE_LIMIT,
    VariableIndex,
    build_variable_index,
)
from tripl.core.analyzers._json_object_properties import is_object_property
from tripl.core.analyzers.cardinality import _is_json_type
from tripl.core.analyzers.event_generator import GenerationResult, event_name_format_columns
from tripl.core.intervals import get_interval
from tripl.core.name_template import VARIABLE_TOKEN_PATTERN
from tripl.json_paths import format_json_path_value
from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.models.variable import Variable
from tripl.models.variable_value import VariableValue, VariableValueKind
from tripl.worker.plan_scope import main_branch_id
from tripl.worker.tasks.metrics.metric_rows import _get_scan_json_value_path_map
from tripl.worker.utils.event_types import ensure_event_type_with_fields

logger = logging.getLogger(__name__)

_VARIABLE_TEMPLATE_PATTERN = re.compile(r"\$\{[^}]+\}")
# One ``${token}`` grammar for the codebase (``core.name_template``); the
# non-capturing sibling above answers a different question and stays local.
_VARIABLE_NAME_PATTERN = VARIABLE_TOKEN_PATTERN
_JSON_PATH_PART_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
# The key filter already restricts the candidate set to ``run_scan`` rows, so
# this bound exists only so a malformed or future-version payload at the head
# of that list cannot cost the caller a good snapshot sitting one row behind it.
_SNAPSHOT_JOB_SCAN_LIMIT = 5
# Bind-parameter budget per identity-resolution batch. PostgreSQL caps a
# statement at 65,535 binds; 500 leaves a wide margin even when the two ``IN``
# clauses of the identity lookup are both full alongside the event-type list.
_SNAPSHOT_IDENTITY_CHUNK = 500
# How many dropped identities the warning names before it stops. Enough to act
# on, bounded so a mass deletion cannot write a megabyte into the worker log.
_SNAPSHOT_LOGGED_IDENTITIES = 10


def _iter_window_chunks(
    time_from: datetime,
    time_to: datetime,
    *,
    interval_delta: timedelta,
    chunk_interval_code: str | None,
) -> list[tuple[datetime, datetime]]:
    """Split ``[time_from, time_to)`` into interval-aligned sub-windows.

    Each chunk spans ``chunk_interval_code`` worth of wall-clock, rounded down to
    a whole number of buckets (never below one bucket). This bounds the per-query
    range so a long replay runs several queries instead of one that times out.
    ``chunk_interval_code`` of ``None`` keeps the legacy single-query behavior.
    Boundaries stay interval-aligned because ``time_from``/``time_to`` come from
    ``_floor_to_interval``/``_ceil_to_interval`` — which bin on
    ``core.bucketing.floor_to_bucket``'s grid, weeks from Monday included — and
    the step is a whole multiple of the interval, so no bucket is ever split
    across two chunks. That last claim only became true once the window helpers
    stopped anchoring weeks at 2000-01-01, a Saturday: a
    Saturday-bounded weekly replay wrote a full Monday bucket from one chunk and
    then overwrote it with the two-day tail from the next.
    """
    if chunk_interval_code is None or time_from >= time_to:
        return [(time_from, time_to)]

    chunk_delta = get_interval(chunk_interval_code).delta
    buckets_per_chunk = max(1, int(chunk_delta // interval_delta))
    step = interval_delta * buckets_per_chunk

    chunks: list[tuple[datetime, datetime]] = []
    cursor = time_from
    while cursor < time_to:
        chunk_to = min(cursor + step, time_to)
        chunks.append((cursor, chunk_to))
        cursor = chunk_to
    return chunks


# The body moved to ``worker.utils.event_types`` so the scan
# task can create the same rows without importing this package (that import
# would pull the whole ``collect_metrics`` task graph into ``worker.tasks.scan``).
# The private name stays bound here because ``catalog_sync`` and the metrics
# tests reach for it by this path, and moving them is churn for no benefit.
_ensure_event_type_with_fields = ensure_event_type_with_fields


def _field_template(values: list[str]) -> str | None:
    """Return an existing variable template for high-cardinality replay fields.

    Replay does not run cardinality analysis, but generated events persist the
    template values that analysis produced earlier. Reusing those templates lets
    no-format scans still match rows such as ``user_id=123`` to catalog events
    named with ``user_id=${user_id}``.
    """
    templates = sorted({value for value in values if _VARIABLE_TEMPLATE_PATTERN.search(value)})
    return templates[0] if len(templates) == 1 else None


def _warehouse_token_candidates(token: str, variable_index: VariableIndex) -> list[str]:
    """Strings that might name where ``${token}``'s value lives, best first.

    The token itself leads: a field value still carrying a raw ``${column.path}``
    needs no lookup at all, and that population is the one replay has always been
    able to sample. Everything after it comes from the variable the token
    resolves to — ``source_name``, then bindings, then the display name — because
    a token is a LABEL, not an address. Since the scan began shortening
    scan-created names (``property.Aalter`` -> ``aalter``, identity kept on
    ``source_name``), the two answers differ for almost every variable, and
    reading the label as an address is why a shortened variable could never be
    sampled.
    """
    variable = variable_index.resolve(token)
    if variable is None:
        return [token]
    source_tokens = VariableIndex.source_tokens_of(variable)
    return [token, *(other for other in source_tokens if other != token)]


# How many JSON paths replay is willing to let one column carry. Each path
# becomes its own extract expression in the bucketed-counts SELECT and its own
# cell in every row that comes back, so the map's width is a warehouse cost paid
# once per row of the whole replay window.
#
# The augmentation below used to be bounded by accident: only a field value
# still holding a raw ``${column.path}`` could contribute, and after names were
# shortened almost none did. Resolving tokens through the variable index removes
# that accident — on the project this was measured against, a single JSON column
# backs over a thousand derived variables — so the bound has to be stated
# instead of assumed. Paths the scan config asked for are counted but never
# dropped: the cap only ever refuses a NEW path.
_MAX_REPLAY_JSON_PATHS_PER_COLUMN = 200


class _ReplayJsonPathMap:
    """The one gate a JSON path passes through on its way into replay's map.

    Validation lives here rather than at the call site because these strings are
    interpolated into the adapter's JSON-path expression and the candidates now
    reaching it include ``bindings`` — user-editable text — not just tokens the
    scan itself wrote. Dedup and the per-column cap sit in the same place for the
    same reason: one object to reason about what the map can end up containing.
    """

    def __init__(self, base: dict[str, list[str]], *, json_columns: list[str]) -> None:
        self._json_columns = set(json_columns)
        self.paths = {column: list(values) for column, values in base.items()}
        self._seen = {column: set(values) for column, values in self.paths.items()}
        self.capped_columns: set[str] = set()

    def admit(self, token: str) -> bool:
        """Add ``column.path`` if admissible; True once the map carries it.

        A path that was already there also answers True, so a caller walking a
        variable's source tokens stops at the first one that lands instead of
        also admitting a stale binding's path for a variable already covered.
        """
        column, _, path = token.partition(".")
        if not path or column not in self._json_columns:
            return False
        if any(not _JSON_PATH_PART_PATTERN.match(part) for part in path.split(".")):
            return False

        seen = self._seen.setdefault(column, set())
        if path in seen:
            return True
        if len(seen) >= _MAX_REPLAY_JSON_PATHS_PER_COLUMN:
            self.capped_columns.add(column)
            return False
        seen.add(path)
        self.paths.setdefault(column, []).append(path)
        return True


def _augment_json_value_paths_for_replay_tokens(
    *,
    json_value_path_map: dict[str, list[str]],
    json_columns: list[str],
    replay_events: list[Event],
    variable_index: VariableIndex,
) -> dict[str, list[str]]:
    """Greedy replay mode: include JSON paths referenced by ${token} tokens.

    This lets replay collect concrete values for JSON-bound variables even if the
    scan config's json_value_paths does not list those paths explicitly. Each
    token is resolved to its variable's warehouse home first (see
    ``_warehouse_token_candidates``); only the first candidate that lands is
    admitted, so a variable widens the query by one path, not by one per binding.
    """
    if not json_columns or not replay_events:
        return json_value_path_map

    path_map = _ReplayJsonPathMap(json_value_path_map, json_columns=json_columns)
    for event in replay_events:
        for field_value in event.field_values:
            for token in _VARIABLE_NAME_PATTERN.findall(field_value.value):
                variable = variable_index.resolve(token)
                if variable is not None and is_object_property(variable):
                    # An object property (F23.4e): extracting it would make a
                    # whole object a grouping key of every replay row. Its
                    # values come from the sampler's whole objects instead.
                    continue
                for candidate in _warehouse_token_candidates(token, variable_index):
                    if path_map.admit(candidate):
                        break

    for column in sorted(path_map.capped_columns):
        # The operator-facing half of the cap: without this, a project past it
        # sees the same "no observed values" symptom this ticket started from,
        # with nothing in the log to distinguish it from a resolution failure.
        logger.warning(
            "Replay JSON path cap reached for column %r (%s paths); "
            "variables beyond it collect no sample values this run",
            column,
            _MAX_REPLAY_JSON_PATHS_PER_COLUMN,
        )
    return path_map.paths


def _extend_unique_values(existing: list[str], incoming: list[str]) -> list[str]:
    seen = set(existing)
    out = list(existing)
    for value in incoming:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _build_variable_lookup(
    session: Session,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
) -> VariableIndex:
    """Everything replay may have to resolve a ``${token}`` back to.

    Delegating to ``build_variable_index`` instead of keeping a name-keyed dict
    is strictly wider in the one direction that matters: the scan writes a
    shortened display name and keeps the raw path on ``source_name``/
    ``bindings``, so a name-only lookup could only answer for the tokens the
    scan has stopped writing. The index also adopts a hand-authored
    ``${variant}`` through the binding its author attached to it, which the dict
    never could. Kept as a named step of the replay path — ``catalog_sync``
    calls it — even though the body is now one delegation.
    """
    return build_variable_index(session, project_id=project_id, branch_id=branch_id)


def _row_value_for_variable(
    variable: Variable,
    *,
    data_row: tuple[object, ...],
    reg_index: dict[str, int],
    json_value_index: dict[str, int],
) -> tuple[str, str] | None:
    """Locate this variable's value in one warehouse row: (source token, value).

    Walks the variable's source tokens most-authoritative first and takes the
    first that ADDRESSES a cell in this row — ``source_name``, then a binding,
    then the display name. Addressing, not "has a value", is the test for a
    regular column: an existing column that is NULL in this row is a real
    observation (recorded as ``""``, as it always was), and treating it as a miss
    would let a stale binding answer for a column that is present and empty. A
    JSON extract does not get that benefit because a null extract and an absent
    path are indistinguishable at this point, so it falls through to the next
    candidate.

    A binding therefore only ever answers when ``source_name`` cannot, which is
    what makes a binding that points at a different column safe: it is a
    fallback address, not a competing one.
    """
    for token in VariableIndex.source_tokens_of(variable):
        reg_idx = reg_index.get(token)
        if reg_idx is not None and reg_idx < len(data_row):
            cell = data_row[reg_idx]
            return token, str(cell) if cell is not None else ""

        json_idx = json_value_index.get(token)
        if json_idx is not None and json_idx < len(data_row):
            cell = data_row[json_idx]
            if cell is not None:
                return token, str(cell)
    return None


def _accumulate_replay_variable_samples(
    accum: dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID], dict[str, object]],
    *,
    event: Event,
    data_row: tuple[object, ...],
    reg_index: dict[str, int],
    n_reg: int,
    n_json: int,
    json_value_names: list[str],
    variable_index: VariableIndex,
) -> None:
    json_value_index = {name: n_reg + n_json + idx for idx, name in enumerate(json_value_names)}
    for field_value in event.field_values:
        tokens = _VARIABLE_NAME_PATTERN.findall(field_value.value)
        if not tokens:
            continue
        for token in tokens:
            # Two questions, two lookups: the index answers which variable this
            # token MEANS, the row walk answers where that variable LIVES.
            variable = variable_index.resolve(token)
            # Excluding a variable FREEZES its observed values; it no longer
            # deletes them (``variable_service.update_variable`` sets the flag
            # and purges nothing). So the rows this sampler would reach are the
            # ones the popover badges "Excluded" and presents as a finished
            # reading — a low context there still promises "All values" — and
            # what these entries feed, ``_merge_replay_variable_samples``, grows
            # exactly that: it unions new values in and raises
            # ``observed_count``. Sampling an excluded variable would
            # therefore keep enlarging a surface the UI calls frozen, after the
            # operator asked scanning to stop. The scan path asks the same flag
            # in ``record_variable_contexts`` and the JSON-path sampler asks it
            # in ``_unfilled_json_path_candidates``; replay is the third door
            # and was the one left open.
            if variable is None or variable.excluded_from_scans:
                continue

            located = _row_value_for_variable(
                variable,
                data_row=data_row,
                reg_index=reg_index,
                json_value_index=json_value_index,
            )
            if located is None:
                continue
            source_token, raw_value = located

            key = (variable.id, event.id, field_value.field_definition_id)
            entry = accum.setdefault(
                key,
                {
                    "variable_id": variable.id,
                    "event_id": event.id,
                    "field_definition_id": field_value.field_definition_id,
                    # The warehouse address the value actually came from, not
                    # the token that led here — for a shortened variable those
                    # are different strings and only the former names a column.
                    "source_column": source_token,
                    "values": [],
                },
            )
            entry["values"] = _extend_unique_values(
                cast(list[str], entry["values"]),
                [raw_value],
            )


def _merge_replay_variable_samples(
    session: Session,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    cardinality_threshold: int,
    accumulated: dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID], dict[str, object]],
) -> int:
    if not accumulated:
        return 0

    variable_ids = {cast(uuid.UUID, payload["variable_id"]) for payload in accumulated.values()}
    event_ids = {cast(uuid.UUID, payload["event_id"]) for payload in accumulated.values()}
    field_ids = {
        cast(uuid.UUID, payload["field_definition_id"]) for payload in accumulated.values()
    }

    existing_query = select(VariableValue).where(
        VariableValue.project_id == project_id,
        VariableValue.variable_id.in_(variable_ids),
        VariableValue.event_id.in_(event_ids),
        VariableValue.field_definition_id.in_(field_ids),
    )
    if branch_id is not None:
        existing_query = existing_query.where(VariableValue.branch_id == branch_id)
    existing = session.execute(existing_query).scalars().all()
    existing_by_key = {
        (row.variable_id, row.event_id, row.field_definition_id): row for row in existing
    }

    touched = 0
    for key, payload in accumulated.items():
        values = cast(list[str], payload["values"])
        if not values:
            continue

        current = existing_by_key.get(key)
        if current is None:
            kind = (
                VariableValueKind.low.value
                if len(values) <= cardinality_threshold
                else VariableValueKind.high.value
            )
            new_row = VariableValue(
                id=uuid.uuid4(),
                project_id=project_id,
                variable_id=cast(uuid.UUID, payload["variable_id"]),
                event_id=cast(uuid.UUID, payload["event_id"]),
                field_definition_id=cast(uuid.UUID, payload["field_definition_id"]),
                source_column=cast(str, payload["source_column"]),
                value_kind=kind,
                observed_count=len(values),
                values=(
                    values
                    if kind == VariableValueKind.low.value
                    else values[:VARIABLE_VALUE_SAMPLE_LIMIT]
                ),
            )
            if branch_id is not None:
                new_row.branch_id = branch_id
            session.add(new_row)
            touched += 1
            continue

        if current.value_kind == VariableValueKind.low.value:
            # A low context promises the reader "All values" — the popover says
            # exactly that — so this list must not be trimmed to a sample the
            # way the high branch is, or the badge starts lying. What it can do
            # is stop being low: the kind means "fewer distinct values than the
            # threshold", and merging window after window is precisely how a
            # column that once looked low grows past it. So the threshold, not
            # a sample size, is the bound here; crossing it demotes the row and
            # the sample cap then applies — the same rule the new-row branch
            # above uses on first write. Without this the list grew forever.
            merged = _extend_unique_values(current.values or [], values)
            # Counted BEFORE any truncation, because the two columns answer
            # different questions: ``values`` is what we can show, and
            # ``observed_count`` is how many distinct values were seen. Taking
            # the count off the truncated list makes a row that has just seen
            # its 101st value report 100 — the sample size masquerading as a
            # measurement.
            distinct_seen = len(merged)
            # The demotion is its own reason to write, not a rider on the values
            # changing: a row holding 20 values that this merge grows past the
            # threshold truncates straight back to the same 20, so comparing
            # lists alone would report "nothing happened" about a row whose kind
            # just changed — and leave it out of the run's touched count.
            demoted = distinct_seen > cardinality_threshold
            if demoted:
                merged = merged[:VARIABLE_VALUE_SAMPLE_LIMIT]
            # And the count is a third reason, for the same reason the demotion
            # is a second one: a row whose stored count sits below the distinct
            # values it already holds — the scan path can write one, since it
            # maxes two observations' counts while unioning their values — would
            # otherwise never be corrected by a merge that shows it nothing new.
            count_grew = distinct_seen > current.observed_count
            if demoted or count_grew or merged != (current.values or []):
                if demoted:
                    current.value_kind = VariableValueKind.high.value
                current.values = merged
                current.observed_count = max(current.observed_count, distinct_seen)
                touched += 1
        else:
            # Existing high-cardinality contexts still benefit from sampled
            # examples in replay results (bounded list for UI visibility).
            # Same split as the low branch: count every distinct value seen,
            # store only the sample.
            merged = _extend_unique_values(current.values or [], values)
            distinct_seen = len(merged)
            merged = merged[:VARIABLE_VALUE_SAMPLE_LIMIT]
            # The count growing is this branch's own reason to write, and here it
            # is the ONLY one that can ever fire again on a row already holding
            # the sample cap: from that moment the truncated list is identical
            # every run, so a values-only test freezes ``observed_count`` at
            # whatever it was when the row filled up — which is the one number
            # counting before truncation exists to keep honest, and fixing one
            # sibling and not the other is how the pair drifts apart. Both
            # conditions together are exactly "this write would change the row",
            # so ``touched`` still counts changes rather than visits.
            count_grew = distinct_seen > current.observed_count
            if count_grew or merged != (current.values or []):
                current.values = merged
                current.observed_count = max(current.observed_count, distinct_seen)
                touched += 1

    return touched


def _sampled_values_for_variable(
    variable: Variable,
    values_by_token: dict[str, list[str]],
) -> tuple[str, list[str]] | None:
    """First of this variable's source tokens the sampler found values for.

    The ``json_path_samples`` twin of ``_row_value_for_variable``: same
    most-authoritative-first walk, a different table to look in. Kept separate
    rather than generalized — folding two lookups this small into one shape
    would hide which of them the caller is actually asking about.
    """
    for token in VariableIndex.source_tokens_of(variable):
        values = values_by_token.get(token)
        if values:
            return token, values
    return None


def _accumulate_replay_json_samples_from_events(
    accum: dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID], dict[str, object]],
    *,
    events: list[Event],
    json_path_samples: dict[str, dict[str, list[object]]],
    variable_index: VariableIndex,
) -> None:
    """Sample from paths the warehouse volunteered, without a second query.

    The cheaper of the two replay sampling routes: ``get_json_path_samples``
    already walked the JSON columns, so this only has to attribute what came
    back. It is keyed by warehouse address for the same reason
    ``_accumulate_replay_variable_samples`` is, and was broken for the same
    reason — a shortened token never matched a ``column.path`` key.
    """
    if not json_path_samples:
        return

    values_by_token: dict[str, list[str]] = {}
    for column, path_samples in json_path_samples.items():
        for path, samples in path_samples.items():
            token = f"{column}.{path}"
            formatted = [format_json_path_value(sample) for sample in samples]
            if not formatted:
                continue
            values_by_token[token] = _extend_unique_values(
                values_by_token.get(token, []), formatted
            )

    if not values_by_token:
        return

    for event in events:
        for field_value in event.field_values:
            tokens = _VARIABLE_NAME_PATTERN.findall(field_value.value)
            if not tokens:
                continue
            for token in tokens:
                variable = variable_index.resolve(token)
                # Same flag the row-walk accumulator honours, for the same
                # reason: an excluded variable's values are frozen, not gone,
                # and the cheaper sampling route must not be the one that keeps
                # growing them.
                if variable is None or variable.excluded_from_scans:
                    continue
                sampled = _sampled_values_for_variable(variable, values_by_token)
                if sampled is None:
                    continue
                source_token, token_values = sampled
                key = (variable.id, event.id, field_value.field_definition_id)
                entry = accum.setdefault(
                    key,
                    {
                        "variable_id": variable.id,
                        "event_id": event.id,
                        "field_definition_id": field_value.field_definition_id,
                        "source_column": source_token,
                        "values": [],
                    },
                )
                entry["values"] = _extend_unique_values(
                    cast(list[str], entry["values"]),
                    token_values,
                )


def _generation_result_from_snapshot(
    snapshot: dict[str, object],
    *,
    project_id: uuid.UUID,
    default_event_type_id: uuid.UUID | None = None,
) -> tuple[GenerationResult, uuid.UUID | None]:
    col_meta: dict[str, dict[str, object]] = {}
    for column, meta_payload in cast(dict[str, object], snapshot.get("col_meta") or {}).items():
        if not isinstance(meta_payload, dict):
            continue
        col_meta[column] = {
            key: value
            for key, value in meta_payload.items()
            if key in {"is_json", "is_low", "template", "json_passthrough_paths"}
        }

    columns_analyzed_raw = snapshot.get("columns_analyzed")

    result = GenerationResult(
        columns_analyzed=int(cast(int | str | float, columns_analyzed_raw or 0)),
        details=[str(item) for item in cast(list[object], snapshot.get("details") or [])],
        col_meta=col_meta,
    )
    event_type_id = snapshot.get("event_type_id")
    if event_type_id is not None:
        result.event_type_id = uuid.UUID(str(event_type_id))
    elif default_event_type_id is not None:
        result.event_type_id = default_event_type_id

    events_by_name: dict[str, Event] = {}
    branch_id: uuid.UUID | None = None
    for event_payload in cast(list[object], snapshot.get("events") or []):
        if not isinstance(event_payload, dict):
            continue
        event_id = uuid.UUID(str(event_payload.get("event_id")))
        event_branch_raw = event_payload.get("branch_id")
        event_branch_id = uuid.UUID(str(event_branch_raw)) if event_branch_raw else None
        if branch_id is None and event_branch_id is not None:
            branch_id = event_branch_id
        event = Event(
            id=event_id,
            project_id=project_id,
            branch_id=event_branch_id or branch_id,
            event_type_id=result.event_type_id or default_event_type_id,
            name=str(event_payload.get("name") or ""),
            source_name=(
                str(event_payload.get("source_name"))
                if event_payload.get("source_name") is not None
                else None
            ),
            description="",
            status=str(event_payload.get("status", "live")),
            metric_breakdown_columns=list(event_payload.get("metric_breakdown_columns") or []),
        )
        event.field_values = []
        for field_payload in cast(list[object], event_payload.get("field_values") or []):
            if not isinstance(field_payload, dict):
                continue
            field_value = EventFieldValue(
                id=uuid.uuid4(),
                event_id=event.id,
                field_definition_id=uuid.UUID(str(field_payload.get("field_definition_id"))),
                value=str(field_payload.get("value") or ""),
            )
            event.field_values.append(field_value)
        events_by_name[str(event_payload.get("identity") or event.name)] = event

    result.events_by_name = events_by_name
    return result, branch_id


def _archived_identities_by_event_type(
    session: Session,
    *,
    project_id: uuid.UUID,
) -> dict[uuid.UUID, set[str]]:
    """Scan identities of archived events, keyed by event type.

    Keyed by ``event_type_id`` rather than filtered on a branch: the caller only
    ever looks up types it resolved on the main plan, and a working branch's
    deep copy carries its own type ids, so branch rows can never leak in.
    """
    by_event_type: dict[uuid.UUID, set[str]] = {}
    rows = session.execute(
        select(Event.event_type_id, Event.source_name, Event.name).where(
            Event.project_id == project_id,
            Event.status == "archived",
        )
    ).all()
    for event_type_id, source_name, name in rows:
        by_event_type.setdefault(event_type_id, set()).add(source_name or name)
    return by_event_type


def _live_ids(
    session: Session,
    column: InstrumentedAttribute[uuid.UUID],
    candidates: set[uuid.UUID],
) -> set[uuid.UUID]:
    """Which of ``candidates`` still have a row, asked in bounded batches.

    Primary-key ``IN`` lookups, so the cost is the batch count and not the
    catalog size. Chunked because a snapshot's event list is unbounded and
    PostgreSQL caps a statement at 65,535 bind parameters.
    """
    ordered = sorted(candidates)
    found: set[uuid.UUID] = set()
    for start in range(0, len(ordered), _SNAPSHOT_IDENTITY_CHUNK):
        found.update(
            session.execute(
                select(column).where(column.in_(ordered[start : start + _SNAPSHOT_IDENTITY_CHUNK]))
            ).scalars()
        )
    return found


def _resolve_snapshot_event_identities(
    session: Session,
    *,
    project_id: uuid.UUID,
    results: list[GenerationResult],
) -> list[str]:
    """Re-point every snapshot event at an ``events`` row that still exists.

    ``_generation_result_from_snapshot`` rebuilds TRANSIENT ``Event`` objects
    straight from JSON, historical ``event_id`` and all, and every consumer of
    ``events_by_name`` writes that id into a foreign key:
    ``event_metrics.event_id`` and ``event_metric_breakdowns.event_id`` (both
    ``ondelete="SET NULL"``, which governs the PARENT's deletion and licenses
    nothing on INSERT) and ``variable_values.event_id`` (``NOT NULL``,
    ``ondelete="CASCADE"``). An event deleted after the snapshot was written
    therefore aims a replay's INSERT at a parent that is gone, and PostgreSQL
    refuses the statement — killing the replay mid-window, because
    ``process_chunk`` commits per chunk and the chunk loop has no per-chunk
    handler, and reporting it as "Scan failed due to an internal error" because
    an ``IntegrityError`` is not one of ``user_facing_error``'s curated cases.

    Nothing else on the path checks. ``_archived_identities_by_event_type`` only
    ADDS identities, and ``drop_dangling_event_references`` — which every
    deliberate delete door calls — does not reach ``ScanJob.result_summary``, so
    the snapshot keeps naming a dead uuid for as long as it is the newest
    ``run_scan`` row. The flaw is as old as the snapshot format; what made it
    reachable is a change moving this path from "the gap between a scan and
    its first collection tick" to every replay, however old the snapshot.

    Three outcomes, in this order, and the order IS the product decision:

    RESOLVE — the id is still live: keep the snapshot's event untouched. The
    common case, and the whole point of the snapshot: its ``col_meta``
    templates are the historical ones, which is what makes a replay of an old
    window attribute rows the way that window's own scan did.

    RE-POINT — the id is gone but a live row still holds the same scan identity:
    adopt that row's id. ``uq_event_scan_identity`` makes
    ``(event_type_id, source_name)`` unique, and an event type lives on exactly
    one branch of one project, so the lookup is exact and cannot pull in a
    working branch's deep copy. This is the case a plan-branch merge or revert
    creates — both DELETE the main row and INSERT a replacement under the same
    identity — and discarding there would punch a silent hole in the replayed
    series of an event that plainly still exists. It is also the rule this
    codebase already applies to the same question elsewhere:
    ``_merge_event_into_group`` re-points ``event_metrics`` at the survivor
    rather than dropping the history.

    DISCARD — neither the id nor the identity survives: drop the entry. Not a
    choice to lose data, the absence of anywhere to put it: a per-event row
    needs an ``events.id`` and there is none. The volume is NOT lost — the
    identity falls through to the shadow-candidate branch in
    ``chunk_processing`` and its rows still land in the event-type and
    project-total series, which is exactly what the heuristic
    ``_load_existing_generation_results`` rebuild produced for a deleted event
    before this path became reachable. Logged, because an operator
    staring at a hole in one event's replayed series deserves to find the reason
    in the worker log rather than infer it.

    What this deliberately does NOT do is abandon the snapshot and fall back to
    the live catalog when one event has died. That would throw away the
    historical ``col_meta`` of every surviving event to accommodate one dead
    one — the heuristic rebuild is the thing this path exists to stop
    reaching for.

    Returns the identities that were dropped.
    """
    snapshot_ids = {event.id for result in results for event in result.events_by_name.values()}
    if not snapshot_ids:
        return []

    live = _live_ids(session, Event.id, snapshot_ids)
    if live == snapshot_ids:
        return []

    # Second hop only for what the first one missed, and scoped to it: the
    # identities and event types of the stale entries alone, never a catalog
    # sweep. Strictly cheaper than the fallback it exists to avoid, which reads
    # every event of every type WITH its field values.
    stale = [
        (identity, event)
        for result in results
        for identity, event in result.events_by_name.items()
        if event.id not in live
    ]
    stale_event_type_ids = {
        event.event_type_id for _identity, event in stale if event.event_type_id is not None
    }
    stale_identities = sorted({identity for identity, _event in stale})

    live_id_by_identity: dict[tuple[uuid.UUID, str], uuid.UUID] = {}
    if stale_event_type_ids and stale_identities:
        for start in range(0, len(stale_identities), _SNAPSHOT_IDENTITY_CHUNK):
            chunk = stale_identities[start : start + _SNAPSHOT_IDENTITY_CHUNK]
            rows = session.execute(
                select(Event.id, Event.event_type_id, Event.source_name, Event.name).where(
                    Event.project_id == project_id,
                    Event.event_type_id.in_(stale_event_type_ids),
                    or_(Event.source_name.in_(chunk), Event.name.in_(chunk)),
                )
            ).all()
            for row_id, row_event_type_id, source_name, name in rows:
                # ``source_name or name`` is the identity key both the snapshot
                # and ``_load_existing_generation_result`` build, but only
                # ``source_name`` carries the unique constraint — so a row
                # matched by its display name must never displace one matched by
                # its scan identity.
                if source_name:
                    live_id_by_identity[(row_event_type_id, source_name)] = row_id
                else:
                    live_id_by_identity.setdefault((row_event_type_id, name), row_id)

    dropped: list[str] = []
    for result in results:
        resolved: dict[str, Event] = {}
        for identity, event in result.events_by_name.items():
            if event.id in live:
                resolved[identity] = event
                continue
            successor = (
                live_id_by_identity.get((event.event_type_id, identity))
                if event.event_type_id is not None
                else None
            )
            if successor is None:
                dropped.append(identity)
                continue
            event.id = successor
            for field_value in event.field_values:
                field_value.event_id = successor
            resolved[identity] = event
        # A NEW mapping rather than one mutated in place: the dict this replaces
        # is the only record of what the payload said.
        result.events_by_name = resolved

    if dropped:
        logger.warning(
            "Replay snapshot: %s event identit%s no longer in the catalog (%s); no "
            "per-event metrics are written for them this run, and their volume lands "
            "in the event-type and project totals instead",
            len(dropped),
            "y" if len(dropped) == 1 else "ies",
            ", ".join(sorted(dropped)[:_SNAPSHOT_LOGGED_IDENTITIES]),
        )
    return dropped


def _prune_snapshot_field_values(
    session: Session,
    *,
    results: list[GenerationResult],
) -> int:
    """Drop reconstructed field values whose ``FieldDefinition`` is gone.

    The sibling of the event check above, on the second column of the same
    INSERT: ``_accumulate_replay_variable_samples`` keys on
    ``(variable.id, event.id, field_value.field_definition_id)`` and
    ``_merge_replay_variable_samples`` writes that triple into
    ``variable_values``, where ``field_definition_id`` is ``NOT NULL`` with a
    foreign key of its own. Deleting a field definition does not delete its
    events, so a snapshot can carry a live event whose field value names a
    definition that no longer exists — a replay-killing INSERT that the event
    check alone would let through.

    Dropping is the only outcome available here: the identity of a field
    definition is its row, there is no stable natural key to re-point at, and a
    variable context anchored to a deleted definition could not be displayed
    even if it were written (``VariableValue.field_name`` dereferences it).
    Returns the number of field values dropped.
    """
    field_ids = {
        field_value.field_definition_id
        for result in results
        for event in result.events_by_name.values()
        for field_value in event.field_values
    }
    if not field_ids:
        return 0

    live = _live_ids(session, FieldDefinition.id, field_ids)
    if live == field_ids:
        return 0

    pruned = 0
    for result in results:
        for event in result.events_by_name.values():
            kept = [fv for fv in event.field_values if fv.field_definition_id in live]
            if len(kept) != len(event.field_values):
                pruned += len(event.field_values) - len(kept)
                event.field_values = kept

    if pruned:
        logger.warning(
            "Replay snapshot: dropped %s reconstructed field value(s) whose field "
            "definition no longer exists; variables bound to them collect no sample "
            "values this run",
            pruned,
        )
    return pruned


def _resolve_snapshot_identities(
    session: Session,
    *,
    project_id: uuid.UUID,
    results: list[GenerationResult],
) -> None:
    """Make every id a snapshot hands the writers point at a row that exists."""
    _resolve_snapshot_event_identities(session, project_id=project_id, results=results)
    _prune_snapshot_field_values(session, results=results)


def _load_latest_generation_snapshot(
    session: Session,
    *,
    config: ScanConfig,
) -> tuple[dict[str, GenerationResult], GenerationResult | None, uuid.UUID | None]:
    # Filter on the KEY, not merely on "the newest completed job with a summary".
    # Only ``run_scan`` writes ``generation_snapshot``; ``collect_metrics``
    # (collection AND replay), ``apply_event_groups`` and the demo runtime tick
    # all complete ScanJobs on the SAME ``scan_config_id`` with summaries that
    # lack it. Taking the newest completed row therefore made the snapshot
    # reachable only in the gap between a scan and its first collection tick —
    # from the tick after that, forever, every replay silently fell through to
    # the heuristic ``_load_existing_generation_results`` rebuild.
    #
    # ``.as_string()`` is load-bearing and must not be "simplified" to bare
    # indexed access: ``result_summary`` is ``sa.JSON``, not JSONB, so ``has_key``
    # is unavailable, and ``result_summary["generation_snapshot"]`` compiles to
    # ``JSON_QUOTE(JSON_EXTRACT(...))`` on SQLite — where ``json_quote(NULL)`` is
    # the TEXT ``'null'``, so the predicate is true for every row and the whole
    # filter a no-op under the test database. ``.as_string()`` gives ``->>`` on
    # Postgres and bare ``JSON_EXTRACT`` on SQLite; both are SQL NULL when the key
    # is absent. ``nullslast()`` because Postgres orders DESC NULLS FIRST.
    summaries = (
        session.execute(
            select(ScanJob.result_summary)
            .where(
                ScanJob.scan_config_id == config.id,
                ScanJob.status == ScanJobStatus.completed.value,
                ScanJob.result_summary["generation_snapshot"].as_string().isnot(None),
            )
            .order_by(ScanJob.completed_at.desc().nullslast())
            .limit(_SNAPSHOT_JOB_SCAN_LIMIT)
        )
        .scalars()
        .all()
    )

    snapshot: dict[str, object] | None = None
    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        candidate = summary.get("generation_snapshot")
        if not isinstance(candidate, dict):
            continue
        try:
            version = int(candidate.get("version") or 0)
        except ValueError, TypeError:
            continue
        if version == 1:
            snapshot = candidate
            break
    if snapshot is None:
        return {}, None, None

    # The snapshot only serializes ``events_by_name``, which archived events are
    # already absent from, so their identities have to come back from the catalog
    # — otherwise a replay refiles every archived identity as a shadow candidate
    # and drops its volume out of the coverage numerator.
    archived_by_event_type = _archived_identities_by_event_type(
        session,
        project_id=config.project_id,
    )

    if config.event_type_column:
        group_results_raw = snapshot.get("group_results")
        if not isinstance(group_results_raw, dict):
            return {}, None, None

        group_results: dict[str, GenerationResult] = {}
        replay_branch_id: uuid.UUID | None = None
        for group_name, group_payload in group_results_raw.items():
            if not isinstance(group_payload, dict):
                continue
            result, branch_id = _generation_result_from_snapshot(
                group_payload,
                project_id=config.project_id,
            )
            if result.event_type_id is not None:
                result.archived_identities = archived_by_event_type.get(result.event_type_id, set())
            group_results[str(group_name)] = result
            if replay_branch_id is None:
                replay_branch_id = branch_id
        _resolve_snapshot_identities(
            session,
            project_id=config.project_id,
            results=list(group_results.values()),
        )
        return group_results, None, replay_branch_id

    single_payload = snapshot.get("single_result")
    if not isinstance(single_payload, dict):
        return {}, None, None

    result, branch_id = _generation_result_from_snapshot(
        single_payload,
        project_id=config.project_id,
        default_event_type_id=config.event_type_id,
    )
    if result.event_type_id is not None:
        result.archived_identities = archived_by_event_type.get(result.event_type_id, set())
    _resolve_snapshot_identities(session, project_id=config.project_id, results=[result])
    return {}, result, branch_id


def _load_existing_generation_result(
    session: Session,
    *,
    project_id: uuid.UUID,
    event_type_id: uuid.UUID,
    columns: list[ColumnInfo],
    config: ScanConfig,
) -> GenerationResult:
    field_definitions = {
        fd.name: fd
        for fd in session.execute(
            select(FieldDefinition).where(FieldDefinition.event_type_id == event_type_id)
        ).scalars()
    }
    events = (
        session.execute(
            select(Event)
            .options(selectinload(Event.field_values))
            .where(
                Event.project_id == project_id,
                Event.event_type_id == event_type_id,
            )
        )
        .scalars()
        .all()
    )
    values_by_field_id: dict[uuid.UUID, list[str]] = {}
    for event in events:
        for field_value in event.field_values:
            values_by_field_id.setdefault(field_value.field_definition_id, []).append(
                field_value.value
            )

    name_columns = event_name_format_columns(config.event_name_format)
    json_value_path_map = _get_scan_json_value_path_map(config)
    col_meta: dict[str, dict[str, object]] = {}
    details: list[str] = []

    for column in columns:
        if column.name == config.event_type_column or column.name == config.time_column:
            continue
        fd = field_definitions.get(column.name)
        if fd is None:
            details.append(f"Skipped column {column.name!r}: no matching field definition")
            continue

        meta: dict[str, object] = {"fd_id": fd.id, "col_name": column.name}
        if _is_json_type(column.type_name):
            meta["is_json"] = True
            meta["json_passthrough_paths"] = [
                f"{column.name}.{path}" for path in json_value_path_map.get(column.name, [])
            ]
        else:
            template = (
                None
                if column.name in name_columns
                else _field_template(values_by_field_id.get(fd.id, []))
            )
            meta["is_json"] = False
            if template is None:
                meta["is_low"] = True
            else:
                meta["is_low"] = False
                meta["template"] = template
        col_meta[column.name] = meta

    return GenerationResult(
        columns_analyzed=len(col_meta),
        details=details,
        col_meta=col_meta,
        # Key on the stable scan identity (source_name), not the editable display name, so
        # metrics still attach to events the user has renamed. Falls back to name for legacy
        # rows whose source_name has not been backfilled yet.
        # Exclude archived events so they are ignored during metrics collection,
        # but keep their identities so the collector can tell "put away" from
        # "never planned" and leave coverage alone.
        events_by_name={
            (event.source_name or event.name): event
            for event in events
            if event.status != "archived"
        },
        archived_identities={
            (event.source_name or event.name) for event in events if event.status == "archived"
        },
    )


def _load_existing_generation_results(
    session: Session,
    *,
    config: ScanConfig,
    columns: list[ColumnInfo],
) -> tuple[dict[str, GenerationResult], GenerationResult | None]:
    """Build replay-time event matching metadata without warehouse cardinality scans."""
    if config.event_type_column:
        # Branch-scoped, like catalog_sync's own lookup. A working branch
        # deep-copies event types under the SAME names, so an unscoped query
        # feeding a name-keyed dict lets the branch copy win: the main-plan
        # series then stops at the last write and the detector reads it as
        # "dropped to zero", while a bucket holding rows from both branches
        # double-counts. Documented at length on
        # ``tasks.main_plan_event_types_by_name``; the replay path was the one
        # place still missing it.
        plan_branch = main_branch_id(session, config.project_id)
        event_types = (
            session.execute(
                select(EventType).where(
                    EventType.project_id == config.project_id,
                    EventType.branch_id == plan_branch,
                )
            )
            .scalars()
            .all()
        )
        return (
            {
                event_type.name: _load_existing_generation_result(
                    session,
                    project_id=config.project_id,
                    event_type_id=event_type.id,
                    columns=columns,
                    config=config,
                )
                for event_type in event_types
            },
            None,
        )

    if config.event_type_id:
        return (
            {},
            _load_existing_generation_result(
                session,
                project_id=config.project_id,
                event_type_id=config.event_type_id,
                columns=columns,
                config=config,
            ),
        )

    return {}, None
