"""Which auto-detected variables nothing references any more.

A scan mints a :class:`~tripl.models.variable.Variable` for every placeholder it
discovers, and nothing has ever retired one. On a project whose warehouse holds
a JSON column keyed by user-typed text — a map, not a struct — that is unbounded
growth in the width of one column: production's ``acme-ios`` carried 1517
variables of which 1296 were referenced by nothing at all, 1279 of them minted
from the keys of a single ``property`` column (``property.Adana``,
``property.Albany, OR``, and two whitespace-only keys). tripl-10h4.

The obvious fix — stop minting above a cardinality threshold — was tried on
paper and rejected. Paths are discovered per COLUMN across the whole scan
window, but they are *stored* per EVENT, and per event the set is small and
load-bearing: ``session_end`` holds ``{"session_time": "${property.session_time}"}``
and ``screen_spot`` holds six named forecast fields. Those are the tracking
plan. A column-level gate cannot tell ``property.session_time`` from
``property.Adana`` — it would refuse both, and every later legitimate field on
that column would render as an unknown token forever.

So the discriminator is not shape, it is USE, evaluated after the fact:

    a variable is retirable when a scan created it, no human has touched it,
    and nothing in the project refers to it.

That splits the production data exactly — 1296 retirable against 221 kept, with
zero human-authored rows in the retirable set (measured, 2026-08-13).

**Both "no observed context" and "no stored ``${token}``" are required, and
neither implies the other.** tripl-xfxa is the standing proof: a group-rule
merge deleted the source event and let its ``VariableValue`` rows cascade away,
leaving eighteen production variables that a live event's field value still
names but that have no contexts at all. A predicate resting on contexts alone
would have deleted precisely those eighteen — the victims of the other bug in
this change. The token pass is what keeps them.

The module is pure on purpose: the danger-zone endpoint runs it on an
``AsyncSession`` and the scan worker runs it on a sync ``Session``, and a second
copy of this predicate is the defect class the rest of this package spends its
docstrings guarding against.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field

from tripl.core.analyzers._event_generator_variables import (
    SCAN_PROVENANCE_DESCRIPTION,
    VariableIndex,
    display_name_candidates,
)
from tripl.core.name_template import variable_tokens
from tripl.models.variable import Variable

__all__ = [
    "SCAN_PROVENANCE_DESCRIPTION",
    "KeptReason",
    "RetirementPlan",
    "is_json_derived",
    "plan_retirement",
    "referenced_tokens",
    "tokens_of",
]

# ``SCAN_PROVENANCE_DESCRIPTION`` is imported from its single WRITE site
# (``_event_generator_variables.ensure_variable``) and re-exported here for
# readers, so the string that decides a deletion cannot drift from the string
# that decides a creation.
#
# It is the only provenance marker the model has — ``Variable`` carries no
# ``created_by``, no ``origin`` column and not even ``TimestampMixin`` — and it
# is safe to lean on because nothing ever updates it after the insert: a user
# who edits the description has, by that act, taken ownership of the row.
#
# ``source_name`` is NOT usable as the marker even though every scan-created row
# has one: adoption back-fills it onto hand-created variables (see "Backfill
# source_name for manually-created variables adopted by their display name or
# binding"), so it marks "a scan has SEEN this" rather than "a scan MADE this".


class KeptReason:
    """Why a variable was not retired, as counted back to the operator.

    Strings rather than an enum because they are dictionary keys on the wire and
    in the audit payload; the API schema pins the exact set.
    """

    REFERENCED = "referenced"
    OBSERVED = "observed"
    DOCUMENTED = "documented"
    USER_EDITED = "user_edited"
    EXCLUDED = "excluded"


@dataclass(frozen=True)
class RetirementPlan:
    """What a retirement pass would do, computed without touching a row.

    ``kept`` is keyed by :class:`KeptReason`; a variable is counted once, under
    the first reason that applied, so the counts sum to the number of variables
    examined minus ``len(retirable)``. Ordering the reasons matters only for the
    report, and it runs human-evidence-first: a row a human excluded is reported
    as excluded even when it is also referenced.
    """

    retirable: list[uuid.UUID] = field(default_factory=list)
    kept: dict[str, int] = field(default_factory=dict)
    scanned: int = 0

    def keep(self, reason: str) -> None:
        self.kept[reason] = self.kept.get(reason, 0) + 1


def tokens_of(variable: Variable) -> set[str]:
    """Every name a ``${token}`` could use to mean *variable*.

    Delegates to ``VariableIndex.tokens_of`` rather than restating it: that is
    the set the scan's own token resolution and context attribution go through
    (display ``name``, the scan identity ``source_name``, every user-editable
    binding), and a retirement that disagreed with it by even one token would
    delete a variable the scan can still resolve. Checking ``name`` alone would
    drop a variable whose stored value says ``${property.session_time}`` because
    its display name was slugged to ``session_time`` — the state 576 of the
    production rows are in.
    """
    return set(VariableIndex.tokens_of(variable))


def referenced_tokens(values: Iterable[str]) -> set[str]:
    """The set of ``${token}`` names appearing anywhere in *values*.

    One pass over the project's stored field values, producing a set the
    per-variable check then hits in O(1). The alternative — a correlated
    ``LIKE '%${name}%'`` per variable — is one full pass per row of the
    catalog, which on the project this was written for is 1517 of them.
    """
    found: set[str] = set()
    for value in values:
        found.update(variable_tokens(value))
    return found


def _human_claim(variable: Variable) -> str | None:
    """The reason *variable* belongs to a human, or ``None`` if it belongs to the scan.

    Every arm reads a field the scan writes ONCE, at ``ensure_variable``, and
    never touches again — so any value other than the one the scan wrote was
    put there by a person. The name is the newest of those arms and the one
    tripl-bwo8 made necessary: with the sweep running on every scheduled cycle
    rather than only under a declared lookback, a JSON key that stops arriving
    for one interval loses its ``${token}`` and its context in that run's
    rewrite and is a fossil by this predicate. Recycling the row is the point of
    the sweep — the next run mints the key afresh — but a re-mint runs
    ``derive_display_name`` again, so the operator's rename is gone and the key
    comes back as ``adana`` under a new id. Keeping a renamed row is what lets
    the sweep run unattended.

    The scan writes exactly one of ``source_name`` itself (every row minted
    before short names existed, and still the fallback when every short name
    is taken) or a ``display_name_candidates`` entry, so a name outside that
    set was typed by a person. The check is an
    approximation in one direction only: a rename that happens to land on a
    LONGER candidate (``adana`` renamed to ``user_adana``) reads as the scan's
    and is not detected. That false negative costs what it cost before this
    arm — the row is re-minted under the short name — and is accepted rather
    than paid for with a provenance column the model does not have.
    """
    if variable.excluded_from_scans:
        # A tombstone is a deliberate instruction to the scan, and the row is
        # what carries it: deleting an excluded variable un-excludes the name
        # and the next scan mints it again.
        return KeptReason.EXCLUDED
    if variable.allowed_values:
        return KeptReason.DOCUMENTED
    if variable.description != SCAN_PROVENANCE_DESCRIPTION:
        return KeptReason.USER_EDITED
    # No scan writes a schema yet, so one is a person's. When scans infer
    # schemas (F23.4) this arm needs the same provenance the description has.
    if variable.json_schema:
        return KeptReason.USER_EDITED
    # A scan writes ``bindings=[source_name]`` and never touches them again, so
    # anything else is a hand-written binding — the mechanism the whole variables
    # rework exists to support (a user binds ``variant`` to
    # ``page_data.extra.variant``), and never something to delete.
    expected_bindings = [variable.source_name] if variable.source_name else []
    if list(variable.bindings or []) != expected_bindings:
        return KeptReason.USER_EDITED
    # Same reasoning for the display name — see the docstring. Only checked
    # when ``source_name`` is set: the scan never writes a row without one, so
    # a row lacking it has no scan-written name to compare against and falls
    # through to the arms above exactly as it did before this check existed.
    if (
        variable.source_name
        and variable.name != variable.source_name
        and variable.name not in display_name_candidates(variable.source_name)
    ):
        return KeptReason.USER_EDITED
    return None


def is_json_derived(variable: Variable, json_columns: Collection[str]) -> bool:
    """Whether *variable* was minted from a path inside a JSON-typed column.

    True when ``source_name`` is set, dotted, and ANY proper dotted prefix of
    it names a column in *json_columns* — the base column or a longer prefix,
    because a ClickHouse ``Nested`` column is itself returned as a dotted name
    and its FieldDefinition is stored that way. ``nested.col`` with no JSON
    column named ``nested`` is therefore scalar, and "contains a dot" is not a
    safe discriminator on its own (observed-values-2026-08-30). BigQuery is not
    the case that needs the longer prefixes — its adapter emits one column per
    top-level field, so a ``RECORD`` reaches this as the undotted ``payload``.

    The discriminator is the stored ``FieldDefinition.field_type`` rather than
    the shape of the bindings for two reasons. First, the shape lies: the scan
    writes ``bindings=[source_name]`` for a scalar column and a JSON path alike,
    and the only difference between ``payload.user.plan`` and a Nested column's
    ``nested.col`` is what the warehouse says the base column is — which the
    catalog sync recorded as the FieldDefinition's type when it created it
    (``generation._ensure_event_type_with_fields``). Second, the question this
    answers is about the REWRITE hazard, not about the token: a scalar column's
    stored value can flip from ``${token}`` to a literal on the cardinality of
    one window (``event_plan.plan_column_meta`` sets ``is_low`` only on the
    non-JSON branch), and a JSON column has no such arm. That hazard follows the
    column's type, not its spelling.

    A variable with hand-written bindings never reaches this check:
    ``_human_claim`` keeps it as ``USER_EDITED`` first, so a user binding
    ``variant`` to both a scalar column and a JSON path is never a sweep
    candidate and never has to be classified.
    """
    source = variable.source_name
    if not source or "." not in source:
        return False
    segments = source.split(".")
    return any(".".join(segments[:take]) in json_columns for take in range(1, len(segments)))


def plan_retirement(
    variables: Sequence[Variable],
    *,
    referenced: set[str],
    with_contexts: set[uuid.UUID],
    with_drifts: set[uuid.UUID],
    with_overrides: set[uuid.UUID],
) -> RetirementPlan:
    """Split *variables* into "nothing refers to this" and "kept, for this reason".

    ``referenced`` is the output of :func:`referenced_tokens` over the project's
    stored field values. The three id sets are presence checks the caller runs
    as indexed anti-joins — a variable in any of them is evidence of use
    (``with_contexts``) or of a human decision (``with_drifts`` carries
    accepted/snoozed/false-positive triage, ``with_overrides`` carries
    hand-authored per-event value lists).

    Writes nothing and loads nothing; the caller decides what to do with
    ``retirable``.
    """
    plan = RetirementPlan(scanned=len(variables))
    for variable in variables:
        claimed = _human_claim(variable)
        if claimed is not None:
            plan.keep(claimed)
            continue
        if variable.id in with_drifts or variable.id in with_overrides:
            plan.keep(KeptReason.DOCUMENTED)
            continue
        if variable.id in with_contexts:
            plan.keep(KeptReason.OBSERVED)
            continue
        if tokens_of(variable) & referenced:
            # No observed context but a live value still names it: the
            # tripl-xfxa shape. Keeping it is the whole reason this check is
            # separate from ``with_contexts``.
            plan.keep(KeptReason.REFERENCED)
            continue
        plan.retirable.append(variable.id)
    return plan
