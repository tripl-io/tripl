"""Variable detection, creation, and context recording for event generation."""

from __future__ import annotations

import copy
import logging
import re
import uuid
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, lazyload

from tripl.core.name_template import VARIABLE_TOKEN_PATTERN
from tripl.models.event import Event
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.variable import Variable
from tripl.models.variable_value import VariableValue, VariableValueKind

logger = logging.getLogger(__name__)

VARIABLE_VALUE_SAMPLE_LIMIT = 20

# The description every scan-created variable carries, and the only provenance
# marker the model has — ``Variable`` has no ``created_by``, no ``origin`` and no
# timestamps. Named here, at its single write site in ``ensure_variable``, so
# ``core.variable_retirement`` can import the exact string it has to match
# instead of keeping a second copy. A user who edits this description has taken
# ownership of the row, which is exactly what retirement reads it for.
SCAN_PROVENANCE_DESCRIPTION = "Auto-detected variable from data source scan"

# One ``${token}`` grammar for the whole codebase, declared beside the ``{key}``
# one it is deliberately NOT (``core.name_template``).
_TOKEN_PATTERN = VARIABLE_TOKEN_PATTERN

# The in-memory context map a generation run fills before any ``VariableValue``
# row exists: ``(variable_id, event_id, field_definition_id)`` -> the payload
# ``insert_variable_contexts`` will write. Named here, beside the four functions
# that read and write it, because the merge pass now has to reconcile it too
# (``_reconcile_pending_variable_contexts``, tripl-gsum) and three modules
# spelling the raw tuple out by hand is how a key order drifts apart.
VariableContextKey = tuple[uuid.UUID, uuid.UUID, uuid.UUID]
PendingVariableContexts = dict[VariableContextKey, dict[str, Any]]


class VariableIndex:
    """Token → Variable lookup across ``name``, ``source_name`` and ``bindings``.

    Built once per generation run so scans adopt manually-created variables
    (matched through user-editable bindings) instead of creating dotted-path
    duplicates. When two variables claim the same token, the first by ``name``
    ordering wins — adoption stays deterministic across runs.
    """

    def __init__(self, variables: Sequence[Variable] = ()) -> None:
        self._by_token: dict[str, Variable] = {}
        # Excluded ids are accumulated here rather than read back off
        # ``_by_token``, because the two answer different questions and only this
        # one is about variables: the map holds the WINNER of each token, so a
        # variable whose every token an earlier-sorted sibling already claimed is
        # absent from its values entirely. See ``excluded_ids`` (tripl-cef2).
        self._excluded_ids: set[uuid.UUID] = set()
        for variable in sorted(variables, key=lambda v: v.name):
            self.add(variable)

    @staticmethod
    def _unique(values: Iterable[str | None]) -> list[str]:
        seen: set[str] = set()
        unique: list[str] = []
        for value in values:
            if value and value not in seen:
                seen.add(value)
                unique.append(value)
        return unique

    @staticmethod
    def tokens_of(variable: Variable) -> list[str]:
        """Which tokens NAME this variable, most likely first.

        The display name leads because that is what a field value carries once
        the scan has normalized its tokens.
        """
        return VariableIndex._unique(
            [variable.name, variable.source_name, *(variable.bindings or [])]
        )

    @staticmethod
    def source_tokens_of(variable: Variable) -> list[str]:
        """Where this variable's value LIVES in the warehouse, most authoritative first.

        The mirror of :meth:`tokens_of`, and the order is inverted on purpose.
        ``tokens_of`` answers "which token names this variable", so it leads with
        the editable display name; this answers "which warehouse column or JSON
        path holds it", so it leads with ``source_name``, the identity the scan
        wrote. Since ``derive_display_name`` began shortening scan-created names
        (``property.Aalter`` -> ``aalter``), the two answers differ for most
        variables, and code that needs the second must not reach for the first.
        Same three fields and the same dedup as ``tokens_of`` -- only the
        precedence changes, which is what keeps the pair consistent.
        """
        return VariableIndex._unique(
            [variable.source_name, *(variable.bindings or []), variable.name]
        )

    def add(self, variable: Variable) -> None:
        for token in self.tokens_of(variable):
            self._by_token.setdefault(token, variable)
        # Recorded whether or not the variable won a single token: it is in the
        # index, so this run has seen it, and that is the whole test (tripl-cef2).
        if variable.excluded_from_scans:
            self._excluded_ids.add(variable.id)

    def resolve(self, token: str) -> Variable | None:
        return self._by_token.get(token)

    def variables(self) -> list[Variable]:
        """Every variable some token resolves to, once each."""
        return list({id(variable): variable for variable in self._by_token.values()}.values())

    def excluded_ids(self) -> set[uuid.UUID]:
        """Ids of the variables this run must not observe.

        Every scan-side WRITER already skips these one at a time — creation,
        normalization, context recording — so the set is what a scan-side
        DELETER needs: no run can re-record a row it takes from an excluded
        variable. Read by ``delete_variable_contexts_for_event_type``, which
        decides per row rather than per token and so cannot ask ``resolve``.

        EVERY excluded variable the index was built from, not the excluded
        subset of the token winners. Those differ whenever an earlier-sorted
        sibling already claims every token an excluded variable names — a live
        variable bound to the excluded one's display name is enough, and the API
        accepts that binding because ``_check_binding_conflicts`` compares
        against other variables' ``bindings`` and ``source_name`` and never
        their ``name``. Reading the winners left such a variable out of the set,
        and the deleter then took its rows on the next rewrite of their
        ``(event, field)`` — the permanent, silent loss the set exists to
        prevent, reappearing for exactly the variables hardest to notice
        (tripl-cef2). Being shadowed is a fact about which variable answers a
        token; it says nothing about whether a stored row can be restated, and
        it cannot, because ``resolve`` hands ``record_variable_contexts`` the
        shadowing sibling instead.
        """
        return set(self._excluded_ids)

    def __len__(self) -> int:
        # Callers gate work on ``if index and ...`` meaning "there are variables
        # to match". A bare object is always truthy, so this has to exist for
        # that reading to survive the index replacing a plain dict.
        return len(self._by_token)


def build_variable_index(
    session: Session,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
) -> VariableIndex:
    # lazyload: the index reads names, bindings and flags only, and
    # ``Variable.value_contexts`` is ``lazy="selectin"``, which would pull the
    # project's whole context table (and its field definitions) into memory on
    # every run (tripl-0zpq.87, as tripl-xkbb did at the other selects).
    query = (
        select(Variable)
        .where(Variable.project_id == project_id)
        .options(lazyload(Variable.value_contexts))
    )
    if branch_id is not None:
        query = query.where(Variable.branch_id == branch_id)
    return VariableIndex(session.execute(query).scalars().all())


def normalize_variable_tokens(value: str, index: VariableIndex) -> str:
    """Rewrite raw ``${path}`` tokens to the bound variable's display name.

    Deterministic and idempotent: a display name resolves back to its own
    variable, so re-running the scan leaves already-normalized values as-is.
    """
    if "${" not in value:
        return value

    def _replace(match: re.Match[str]) -> str:
        token = match.group(1)
        variable = index.resolve(token)
        if variable is None or variable.excluded_from_scans or variable.name == token:
            return match.group(0)
        return f"${{{variable.name}}}"

    return _TOKEN_PATTERN.sub(_replace, value)


@dataclass(frozen=True)
class VariableObservation:
    name: str
    source_column: str
    value_kind: str
    observed_count: int
    values: list[str]


def sample_variable_values(values: Sequence[str], value_kind: str) -> list[str]:
    seen: set[str] = set()
    unique_values: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique_values.append(value)
    if value_kind == VariableValueKind.low.value:
        return unique_values
    return unique_values[:VARIABLE_VALUE_SAMPLE_LIMIT]


def variable_observations(col_meta: dict[str, dict[str, Any]]) -> list[VariableObservation]:
    observations: list[VariableObservation] = []
    for meta in col_meta.values():
        observations.extend(meta.get("variable_observations") or [])
    return observations


def delete_variable_contexts_for_event_type(
    session: Session,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    event_type_id: uuid.UUID,
    contexts: dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID], dict[str, Any]],
    rewritten_fields: set[tuple[uuid.UUID, uuid.UUID]],
    excluded_variable_ids: Collection[uuid.UUID],
) -> None:
    """Drop the contexts this run replaces or invalidated — never one it cannot restate.

    Two disjoint reasons to delete a row:

    * its ``(variable, event, field)`` key is in ``contexts``, so
      ``insert_variable_contexts`` is about to re-add it and the old row has to
      go first (``uq_variable_value_context``);
    * this run REWROTE the field value the row describes AND the row's variable
      is one this run is allowed to observe. A context means "this event field's
      value references ``${variable}``", so it stops being true exactly when
      ``_upsert_field_values`` changes (or newly writes) that ``(event, field)``
      value — which is what ``rewritten_fields`` carries.

    Everything else is left alone. This used to delete every context for the
    event type unconditionally, which also wiped rows the run had no opinion
    about: values enriched by an earlier replay, and — the case that surfaced it
    — a demo's seeded observed values hanging off authored field values the scan
    is not allowed to touch, so the demo's very first scan emptied its own
    "Variables & value drift" story (bd tripl-jfm3.56). The scheduled metrics
    path already merges into existing rows rather than replacing them
    (``_merge_replay_variable_samples``); this brings the scan path in line.

    ``excluded_variable_ids`` is the second arm's limit, and it is what makes the
    two arms one rule: INVALIDATE only what you could state afresh. That held
    implicitly while every row on a rewritten field was re-recorded by the same
    run, so the second arm never needed to name the variable. It stopped holding
    when excluding a variable became a tombstone instead of a purge (bd
    tripl-95pu): ``record_variable_contexts`` skips an excluded variable, so its
    rows reach ``insert_variable_contexts`` by no route at all, and a rewrite
    here destroyed observations permanently — silently, inside the scan, which is
    the deletion that change was supposed to have removed.

    Excluding is also what CAUSES the rewrite, which is why no guard upstream
    fixes this. ``normalize_variable_tokens`` stops resolving an excluded
    variable's token, so the stored value reverts from the display name a
    previous scan wrote (``${locale}``) to the raw path the planner emits
    (``${payload.locale}``). Suppressing that rewrite would still not be enough:
    this arm keys on ``(event, field)`` and never on the variable, so any other
    reason the field's value changes — a new JSON path appearing beside the old
    one — takes the excluded row with it just the same.
    """
    # Nothing was re-recorded and nothing was rewritten -> no row can be stale.
    if not contexts and not rewritten_fields:
        return

    excluded = frozenset(excluded_variable_ids)

    # A row can only be stale if BOTH its event and its field appear in one of
    # the two key sets, so pre-filter on those in SQL. Both are necessary (not
    # sufficient) conditions, which is why the exact tuple match still runs in
    # Python below. Deliberately two single-column INs rather than one composite
    # ``tuple_(...).in_(...)``: a composite IN would send 2-3 bind params per key
    # and a large scan can carry tens of thousands of keys, which runs into
    # PostgreSQL's 65535-parameter ceiling — these two stay one param per
    # distinct id and prune the scan just as effectively.
    candidate_events = {event_id for _, event_id, _ in contexts}
    candidate_fields = {field_id for _, _, field_id in contexts}
    candidate_events.update(event_id for event_id, _ in rewritten_fields)
    candidate_fields.update(field_id for _, field_id in rewritten_fields)

    event_ids = select(Event.id).where(
        Event.project_id == project_id,
        Event.event_type_id == event_type_id,
        Event.id.in_(candidate_events),
    )
    if branch_id is not None:
        event_ids = event_ids.where(Event.branch_id == branch_id)
    # Only the four columns the staleness test reads — the previous version
    # hydrated whole ``VariableValue`` entities just to look at their keys.
    query = select(
        VariableValue.id,
        VariableValue.variable_id,
        VariableValue.event_id,
        VariableValue.field_definition_id,
    ).where(
        VariableValue.project_id == project_id,
        VariableValue.event_id.in_(event_ids),
        VariableValue.field_definition_id.in_(candidate_fields),
    )
    if branch_id is not None:
        query = query.where(VariableValue.branch_id == branch_id)

    stale_ids = [
        row_id
        for row_id, variable_id, event_id, field_definition_id in session.execute(query)
        if (variable_id, event_id, field_definition_id) in contexts
        or ((event_id, field_definition_id) in rewritten_fields and variable_id not in excluded)
    ]
    if not stale_ids:
        return
    session.execute(delete(VariableValue).where(VariableValue.id.in_(stale_ids)))


def preserve_existing_variable_context_values(
    session: Session,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    contexts: dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID], dict[str, Any]],
    cardinality_threshold: int = 100,
) -> dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID], list[str]]:
    """Fold every stored row into the planned context about to replace it.

    Returns the stored ``values`` per context key, snapshotted before any
    merging, so ``insert_variable_contexts`` can tell a rewrite that changed a
    row from one that restored it byte-for-byte.
    """
    if not contexts:
        return {}

    variable_ids = {variable_id for variable_id, _, _ in contexts}
    event_ids = {event_id for _, event_id, _ in contexts}
    field_definition_ids = {field_id for _, _, field_id in contexts}
    query = select(VariableValue).where(
        VariableValue.project_id == project_id,
        VariableValue.variable_id.in_(variable_ids),
        VariableValue.event_id.in_(event_ids),
        VariableValue.field_definition_id.in_(field_definition_ids),
    )
    if branch_id is not None:
        query = query.where(VariableValue.branch_id == branch_id)

    existing_contexts = session.execute(query).scalars().all()
    prior_values: dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID], list[str]] = {}
    for existing in existing_contexts:
        key = (existing.variable_id, existing.event_id, existing.field_definition_id)
        context = contexts.get(key)
        if context is None:
            continue

        if context.get("presence_rate") is None:
            # A run that could not measure presence (a replay, rows without
            # counts) keeps the last measurement rather than erasing it.
            context["presence_rate"] = existing.presence_rate
        context_values = list(context.get("values") or [])
        existing_values = list(existing.values or [])
        prior_values[key] = existing_values
        if not context_values and existing_values:
            context["values"] = sample_variable_values(existing_values, existing.value_kind)
            # The KIND is restored with the values: a planned JSON-path
            # observation is always ``high``, and letting it stand over a
            # restored low enumeration would flip the row to high with its
            # full list — after which the next merge trims it to the sample
            # cap. Restoring both keeps the row exactly what it was.
            context["value_kind"] = existing.value_kind

        # A first observation IS reported as drift. That is the settled decision,
        # not an omission. A stored ``observed_count`` of 0 means the values
        # arriving now are the backlog — every value the path has ever carried
        # surfacing at once because something finally sampled it — and a variable
        # with a documented list reports the lot as novel. No guard here can tell
        # that from a change: marking the context only defers the report by a tick
        # (the row is then filled, so the sampler skips it, the observation
        # arrives empty, and the restore above hands the detector exactly those
        # values on the next run), and suppressing RESTORED values instead would
        # silence replay-enriched ones, which reach the detector by no other
        # route.
        #
        # No durable column rescues it either. ``first_observed_at`` decides
        # nothing alone — "was the list documented before we looked" has to be
        # compared against when ``allowed_values`` was written, and ``Variable``
        # carries no timestamps at all (``VariableValue`` and the event overrides
        # do); adding them would still not answer it, because a row timestamp
        # moves when a description is edited and the question is about one
        # column. A baseline BOOLEAN is the in-memory marker made durable:
        # cleared on the second observation it defers by that same tick, and
        # never cleared it silences genuinely new values arriving later on this
        # row — strictly worse than reporting the backlog. Only a stored copy of
        # the values present at the first observation could exempt a backlog and
        # still report a later arrival, and it silences the case that must not be
        # silenced: documenting a list on day 30 is exactly a request to hear
        # about everything already observed outside it, which a baseline written
        # on day 1 buries. Only the detector knows whether a contract existed when
        # the values were seen, and it reads these rows without ever writing them.
        #
        # So the backlog surfaces, and the variables guide tells operators to
        # expect a batch the first time they document a list.

        if context_values and existing_values:
            # A non-empty payload is a WINDOW, not a census: the rotating
            # sampler reads 1-3h of traffic, so a stored value absent from the
            # window is still real. Replacing the list with the window dropped
            # one historical value per context per scheduled cycle on
            # production (2026-08-31); union instead, existing values first so
            # the stored order — and the chips rendered from it — stays
            # stable. Same order as ``_extend_unique_values`` on the metrics
            # sink, this write path's sibling.
            merged = sample_variable_values(
                # ``low`` means "no cap" here: the union has to be measured
                # before any trimming, like ``distinct_seen`` on the sink.
                [*existing_values, *context_values],
                VariableValueKind.low.value,
            )
            distinct_seen = len(merged)
            # The demotion bound is the cardinality THRESHOLD, not the sample
            # cap: a low row is an exact enumeration and legitimately holds up
            # to the threshold's worth of values untrimmed — the replay sink
            # (generation.py's low branch) spells out why trimming it to the
            # sample cap makes the "All values" badge lie. Demoting at the cap
            # here rewrote every 21..100-value enumeration as a 20-value
            # sample on its first rescan. Decided BEFORE any trim, because
            # ``sample_variable_values`` trims only high rows.
            if (
                distinct_seen > cardinality_threshold
                or existing.value_kind == VariableValueKind.high.value
            ):
                context["value_kind"] = VariableValueKind.high.value
            else:
                # The union of two observed-value sets below the threshold is
                # still an exact enumeration, whatever the incoming side
                # claimed: a sampled JSON-path observation always arrives
                # ``high`` (event_plan plans it that way), and inheriting that
                # kind here would trim a stored low enumeration to the sample
                # cap — the destruction this arm exists to remove, back
                # through the side door. The replay sink keeps low the same
                # way.
                context["value_kind"] = VariableValueKind.low.value
            context["values"] = sample_variable_values(merged, context["value_kind"])
            context["observed_count"] = max(
                int(context.get("observed_count") or 0),
                distinct_seen,
            )

        context["observed_count"] = max(
            int(context.get("observed_count") or 0),
            existing.observed_count,
            len(context.get("values") or []),
        )
        if existing.value_kind == VariableValueKind.high.value:
            context["value_kind"] = VariableValueKind.high.value
    return prior_values


def record_variable_contexts(
    contexts: dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID], dict[str, Any]],
    *,
    event: Event,
    field_values: Sequence[tuple[uuid.UUID, str, str]],
    col_meta: dict[str, dict[str, Any]],
    index: VariableIndex,
    presence: Mapping[str, float] | None = None,
) -> None:
    """Record one planned row's observations against ``event``.

    ``presence`` (raw token -> rate) is ``fold_json_properties``' measure of
    how often this row's identity carried each JSON path; a path it could not
    measure is absent.
    """
    presence = presence or {}
    for field_definition_id, col_name, value in field_values:
        observations: list[VariableObservation] = (
            col_meta.get(col_name, {}).get("variable_observations") or []
        )
        if not observations:
            continue
        for observation in observations:
            variable = index.resolve(observation.name)
            if variable is None or variable.excluded_from_scans:
                continue
            # Match the stored value by ANY of the variable's tokens: a
            # hand-authored ${variant} attributes to an observation named
            # page_data.extra.variant through the variable's binding.
            tokens = VariableIndex.tokens_of(variable)
            if not any(f"${{{token}}}" in value for token in tokens):
                continue
            key = (variable.id, event.id, field_definition_id)
            existing = contexts.get(key)
            if existing is None:
                contexts[key] = {
                    "variable_id": variable.id,
                    "event_id": event.id,
                    "field_definition_id": field_definition_id,
                    "source_column": observation.source_column,
                    "value_kind": observation.value_kind,
                    "observed_count": observation.observed_count,
                    "values": list(observation.values),
                    "presence_rate": presence.get(observation.name),
                }
                continue

            existing["observed_count"] = max(
                int(existing["observed_count"]),
                observation.observed_count,
            )
            if observation.value_kind == VariableValueKind.high.value:
                existing["value_kind"] = VariableValueKind.high.value
            existing["values"] = sample_variable_values(
                [*existing["values"], *observation.values],
                existing["value_kind"],
            )


def insert_variable_contexts(
    session: Session,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    contexts: dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID], dict[str, Any]],
    prior_values: Mapping[tuple[uuid.UUID, uuid.UUID, uuid.UUID], list[str]] | None = None,
) -> int:
    """Write one row per planned context; count the writes a reader can see.

    The return value is the number of rows this call left holding a NON-EMPTY
    values list that is new or changed — a row restored to exactly its stored
    values does not count, and neither does one created empty. ``prior_values``
    is the pre-merge snapshot ``preserve_existing_variable_context_values``
    took of the rows this run deletes and re-inserts; without it every insert
    looks new. The semantics are a pinned contract: the scan task publishes
    the sum in ``result_summary`` under ``variable_values_written``.
    """
    prior = prior_values or {}
    variable_values_written = 0
    for key, context in contexts.items():
        payload = {
            "id": uuid.uuid4(),
            "project_id": project_id,
            "variable_id": context["variable_id"],
            "event_id": context["event_id"],
            "field_definition_id": context["field_definition_id"],
            "source_column": context["source_column"],
            "value_kind": context["value_kind"],
            "observed_count": context["observed_count"],
            "values": context["values"],
            "presence_rate": context.get("presence_rate"),
        }
        if branch_id is not None:
            payload["branch_id"] = branch_id
        session.add(VariableValue(**payload))
        values = list(context["values"] or [])
        if values and values != prior.get(key):
            variable_values_written += 1
    return variable_values_written


def resolve_main_branch_id(session: Session, project_id: uuid.UUID) -> uuid.UUID | None:
    """Return the project's existing main-branch id, or ``None`` if not created yet.

    Scans write plan entities to the main branch (see ``default_branch_id``).
    Variable uniqueness is enforced per branch — ``(project_id, branch_id,
    source_name)`` — so the working copies a project's plan branches hold can
    legitimately share a ``source_name``. Scoping the scan's existence checks to
    the main branch keeps that constraint meaningful and stops a same-name row on
    another branch from making the lookup raise ``MultipleResultsFound``.
    """
    return (
        session.execute(
            select(PlanBranch.id).where(
                PlanBranch.project_id == project_id,
                PlanBranch.kind == BranchKind.main.value,
            )
        )
        .scalars()
        .first()
    )


_NAME_CLEAN_PATTERN = re.compile(r"[^a-z0-9_]+")


def display_name_candidates(token: str) -> list[str]:
    """Every display name a scan could give *token*, in the order it tries them.

    The pure half of :func:`derive_display_name`: candidates grow from the
    trailing path segments (``variant`` → ``extra_variant`` →
    ``page_data_extra_variant``), normalized to the strict variable-name grammar
    (lower-cased, anything outside ``[a-z0-9_]`` collapsed to ``_``, a ``v_``
    prefix when the result does not start with a letter). A non-dotted token has
    exactly one candidate, itself, untouched — the scan never normalizes a plain
    column name. Split out so ``core.variable_retirement`` can answer "did the
    scan write this name?" without an index and without restating the rules.
    """
    if "." not in token:
        return [token]
    segments = token.split(".")
    candidates: list[str] = []
    for take in range(1, len(segments) + 1):
        candidate = _NAME_CLEAN_PATTERN.sub("_", "_".join(segments[-take:]).lower()).strip("_")
        if not candidate:
            continue
        if not candidate[0].isalpha():
            candidate = f"v_{candidate}"
        candidates.append(candidate)
    return candidates


def derive_display_name(token: str, index: VariableIndex) -> str:
    """Short human name for a scan-discovered dotted path.

    The first :func:`display_name_candidates` entry not already claimed by any
    token in the index wins. Falls back to the raw path (legacy style) when
    every candidate is taken. Non-dotted tokens pass through unchanged — their
    single candidate is the token itself, and it is returned without consulting
    the index, exactly as before the split.
    """
    if "." not in token:
        return token
    for candidate in display_name_candidates(token):
        if index.resolve(candidate) is None:
            return candidate
    return token


def _backfill_source_name(session: Session, variable: Variable, token: str) -> None:
    """Stamp *token* as the adopted variable's scan identity, unless another holds it.

    ``source_name`` is unique per project and branch. A variable renamed through
    the API keeps its old ``source_name``, so a hand-made variable adopted by that
    same token must not claim it: the flush would violate
    ``uq_variable_project_source_name`` and fail every run that plans the column
    (tripl-0zpq.81). The adoption itself still stands for this run.
    """
    holder = session.scalar(
        select(Variable.id).where(
            Variable.project_id == variable.project_id,
            Variable.branch_id == variable.branch_id,
            Variable.source_name == token,
            Variable.id != variable.id,
        )
    )
    if holder is not None:
        return
    session.flush()
    try:
        with session.begin_nested():
            variable.source_name = token
            session.flush()
    except IntegrityError:
        # A concurrent writer took the token between the check and the flush.
        logger.info(
            "source_name already held; leaving the adopted variable unstamped",
            extra={"variable_id": str(variable.id), "token": token},
        )


def ensure_variable(
    session: Session,
    project_id: uuid.UUID,
    name: str,
    inferred_type: str,
    branch_id: uuid.UUID | None = None,
    index: VariableIndex | None = None,
    json_schema: Mapping[str, Any] | None = None,
) -> int:
    """Create a Variable if it doesn't exist. Returns 1 if created, 0 if already exists.

    ``json_schema`` is the schema a CREATED variable starts with (an object
    property's ``{"type": "object"}``); an adopted variable keeps its own.

    Adoption goes through the ``VariableIndex`` (name, source_name and
    user-editable bindings), so a manually-created ``variant`` bound to
    ``page_data.extra.variant`` is adopted instead of duplicated. The index is
    scoped to ``branch_id`` (the scan's main branch) so a same-named variable on
    another plan branch is not treated as an existing match. Callers that loop
    over many columns should build the index once and pass it in; created
    variables are registered on it so later lookups in the same run see them.
    """
    if index is None:
        index = build_variable_index(session, project_id=project_id, branch_id=branch_id)

    existing = index.resolve(name)
    if existing is not None:
        # Backfill source_name for manually-created variables adopted by their
        # display name or binding, so later scans keep matching them.
        if existing.source_name is None:
            _backfill_source_name(session, existing, name)
            index.add(existing)
        return 0

    var = Variable(
        id=uuid.uuid4(),
        project_id=project_id,
        # Dotted paths get a short display name; identity stays on
        # source_name/bindings so adoption and attribution are unaffected.
        name=derive_display_name(name, index),
        source_name=name,
        variable_type=inferred_type,
        json_schema=copy.deepcopy(dict(json_schema)) if json_schema is not None else None,
        description=SCAN_PROVENANCE_DESCRIPTION,
        bindings=[name],
    )
    if branch_id is not None:
        var.branch_id = branch_id
    # Race-safe insert: two concurrent scan workers can discover the same
    # variable in the same bucket and attempt to insert simultaneously.
    # Keep this operation isolated in a SAVEPOINT so an IntegrityError does not
    # poison the outer transaction. The caller's pending work is flushed first:
    # ``begin_nested`` would otherwise flush it inside the ``try`` and a
    # violation of the caller's would be swallowed as this insert's.
    session.flush()
    try:
        with session.begin_nested():
            session.add(var)
            session.flush()
    except IntegrityError:
        logger.info(
            "Variable already inserted concurrently; skipping duplicate",
            # "name" is a reserved LogRecord attribute (the logger name); using it
            # as an extra raises KeyError, so log the variable name as
            # "variable_name".
            extra={
                "project_id": str(project_id),
                "variable_name": name,
                "branch_id": str(branch_id),
            },
        )
        return 0
    index.add(var)
    return 1
