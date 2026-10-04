"""The event references a group merge must carry that no foreign key can find.

``_merge_event_into_group`` re-points every row whose ``event_id`` is a real FK.
This module handles the rest: an event id stored as a STRING or inside a JSON
list, invisible to database reflection and therefore invisible to the automatic
half of ``tests/test_event_fk_classification.py``. Those were found by hand while
classifying the FK ledger, together with the one real FK the ledger
had parked as a known gap.

Split out of ``_event_generator_merge`` rather than appended to it: that module
is already past 600 lines against this repo's 800-line ceiling, and it was
itself split out of ``event_generator`` for the same reason.

**The test that decides whether a reference belongs here** is whether the row
asserts *what was true at a moment* or *what should be true from now on*. Only
the second kind is carried:

* a sensitivity override says "be stricter on this stream, permanently", and the
  stream survives as the target;
* an alert filter says "alert on this stream" / "never alert on this stream";
* a chart annotation's TEXT is history, but its ``scope_ref`` is only where the
  marker gets drawn;
* an OPEN implementation ticket says "this is not built yet" — a closed one is a
  record of what shipped, so it is left alone.

Records of the past stay frozen and are deliberately NOT here: ``plan_revisions``,
``alert_deliveries.payload_snapshot``, ``audit_log.payload``,
``scan_jobs.result_summary``, and the stored links on delivery items. Each
carries a written reason in the ledger.

Nothing in this module may raise. It runs inside ``apply_event_groups`` and
``run_scan``, whose handlers mark the whole ``ScanJob`` failed — losing a scan
because an annotation could not be re-pointed would be a far worse trade than
the dangling reference it was fixing. It also may not import ``tripl.worker``:
``core`` never does, which is why ``mark_collection_error`` moved onto the model.
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from tripl.core.event_references import replace_preserving_order
from tripl.models.alert_delivery_item import trim_scope_name
from tripl.models.alert_rule import AlertRule
from tripl.models.alert_rule_filter import AlertRuleFilter
from tripl.models.anomaly_scope_override import AnomalyScopeOverride
from tripl.models.chart_annotation import ChartAnnotation
from tripl.models.domain_enums import (
    AlertRuleFilterField,
    ChartAnnotationScopeType,
    MetricComposition,
    MetricScopeType,
)
from tripl.models.event import Event
from tripl.models.implementation_ticket import ImplementationTicket
from tripl.models.metric_definition import MetricDefinition

__all__ = ["move_dangling_event_references"]

# An implementation ticket only speaks about unbuilt work while it is open.
_TICKET_STATUS_OPEN = "open"


def move_dangling_event_references(session: Session, *, source: Event, target: Event) -> None:
    """Re-point every non-FK (and SET NULL) reference from *source* to *target*.

    Called from ``_merge_event_into_group`` before ``session.delete(source)``.
    """
    _move_metric_composition_operands(session, source=source, target=target)
    _move_anomaly_scope_overrides(session, source=source, target=target)
    _move_alert_rule_filter_values(session, source=source, target=target)
    _move_chart_annotations(session, source=source, target=target)
    _move_implementation_ticket_event_ids(session, source=source, target=target)
    _move_superseded_pointers(session, source=source, target=target)


def _move_metric_composition_operands(session: Session, *, source: Event, target: Event) -> None:
    """Carry an event_composition metric's operands onto the surviving event.

    ``numerator_event_id`` / ``denominator_event_id`` are real foreign keys with
    ``ondelete="SET NULL"``, and the resulting failure is silent and permanent:
    ``_read_event_metric_series`` returns ``{}`` when both the
    event and event-type refs are NULL, ``_collect_event_composition`` then
    reports ``{"values": 0, "grids": 0}`` — a SUCCESS — so
    ``last_collection_status`` never goes red, and ``_event_composition_due``
    returns ``False`` forever after, so the scheduler stops trying. The metric
    flatlines with nothing anywhere saying why, while the series it wants sits
    under the target: ``_merge_event_metric_rows`` has already summed both
    events' counts onto it.

    Nothing constrains these columns (``metric_definitions`` is unique only on
    ``(project_id, name)``), so there is no fold and no target-wins rule — two
    metrics may legally name the same event.

    ONE case needs more than a re-point. If a ``ratio`` metric had its numerator
    on one merged event and its denominator on the other, both operands now name
    the same event and the metric computes a constant 1.0 on every bucket —
    arithmetic that looks healthy and means nothing. A dangling NULL is still
    worse, so the operands are re-pointed and the metric is driven red instead,
    naming both originals so an operator can see what happened and redefine it.

    That red is gated on ``composition == ratio``, which is the only composition
    the 1.0 argument is about: ``single`` and ``per_distinct_user`` read the
    numerator alone, so a leftover denominator matching it changes nothing they
    compute. The schema now refuses to STORE such a row, but one written before
    that (only a hand-written API call could make one) was being marked red here
    with a message about a ratio it is not.

    Deliberately does NOT touch ``numerator_event_type_id`` /
    ``denominator_event_type_id``. They have the identical SET NULL shape when an
    event TYPE is deleted, which is a different trigger on a different path;
    filed separately rather than half-fixed here.
    """
    definitions = session.execute(
        select(MetricDefinition).where(
            MetricDefinition.project_id == target.project_id,
            or_(
                MetricDefinition.numerator_event_id == source.id,
                MetricDefinition.denominator_event_id == source.id,
            ),
        )
    ).scalars()

    for definition in definitions:
        if definition.numerator_event_id == source.id:
            definition.numerator_event_id = target.id
        if definition.denominator_event_id == source.id:
            definition.denominator_event_id = target.id

        if (
            definition.composition == MetricComposition.ratio
            and definition.numerator_event_id is not None
            and definition.numerator_event_id == definition.denominator_event_id
        ):
            # Names THIS merge's source and target rather than the operands as
            # they were originally authored: when several events collapse into
            # one group they merge one at a time, so by the time the collision
            # appears the other operand has already become the target and
            # reporting it would just print the survivor's id twice.
            definition.mark_collection_error(
                "Both operands of this ratio now point at the same event: "
                f"{target.name!r} ({target.id}), after event {source.id} was merged "
                "into it by a scan group rule. The ratio would compute a constant "
                "1.0 — redefine the metric against two distinct events."
            )


def _move_anomaly_scope_overrides(session: Session, *, source: Event, target: Event) -> None:
    """Carry the false-positive ratchet onto the surviving event.

    An override is live detector configuration a person produced by clicking
    "this was a false positive" — the model calls the ratchet permanent and
    non-decaying, undone only by deleting the row. Dropping it silently reverts
    that decision AND leaves the target ratcheting again from the project
    defaults, so the operator ends up with a stale row whose effect they cannot
    see and a fresh one they have to re-earn click by click.

    There can be SEVERAL source rows — the scope is partitioned by scan config —
    so the fold runs per ``scan_config_id``, with ``None`` matched to ``None``,
    because both ``uq_anomaly_scope_override_scope`` and the partial
    ``uq_anomaly_scope_override_metric_scope`` index have to hold. A blanket
    ``UPDATE`` would raise on the first config where the target already has a
    row, and raising here fails the entire scan.

    The fold keeps the STRICTER value on both knobs and sums the click counts.
    That is not a coin toss: ``ratchet_sigma_threshold`` and
    ``ratchet_min_expected_count`` are both monotonically non-decreasing and
    capped, so taking the maximum is precisely "never undo a click", and the
    summed ``false_positive_count`` keeps the number Detection settings shows
    honest.

    Only ``event``-scope rows are touched — ``scope_ref`` is polymorphic, and a
    metric, event-type or project-total ref must never be rewritten.
    """
    source_ref = str(source.id)
    target_ref = str(target.id)
    rows = list(
        session.execute(
            select(AnomalyScopeOverride).where(
                AnomalyScopeOverride.project_id == target.project_id,
                AnomalyScopeOverride.scope_type == MetricScopeType.event.value,
                AnomalyScopeOverride.scope_ref.in_([source_ref, target_ref]),
            )
        ).scalars()
    )
    existing = {row.scan_config_id: row for row in rows if row.scope_ref == target_ref}

    for row in rows:
        if row.scope_ref != source_ref:
            continue
        prior = existing.get(row.scan_config_id)
        if prior is None:
            row.scope_ref = target_ref
            # Both arms relabel from ``Event.name`` (String(500)) into a
            # String(255) column, and this module may not raise: an override
            # that could not be relabelled would fail the entire scan.
            row.scope_name = trim_scope_name(target.name)
            existing[row.scan_config_id] = row
            continue
        prior.sigma_threshold = max(prior.sigma_threshold, row.sigma_threshold)
        prior.min_expected_count = max(prior.min_expected_count, row.min_expected_count)
        prior.false_positive_count += row.false_positive_count
        prior.scope_name = trim_scope_name(target.name)
        session.delete(row)


def _move_alert_rule_filter_values(session: Session, *, source: Event, target: Event) -> None:
    """Rewrite the event ids an alert rule filters on.

    A filter row holds ``values`` as a JSON list of ``str(event_id)``. Leaving a
    dead id there breaks the rule in BOTH directions, and neither is visible: an
    ``in``/``eq`` rule silently stops covering traffic it was written to watch,
    while a ``not_in``/``ne`` rule starts paging on traffic that was explicitly
    suppressed, because the live anomaly's id no longer matches the exclusion.

    Only ``event``-field filters are touched. The match runs in Python rather
    than as a JSON containment query because that operator is PostgreSQL-only
    and the suite runs on SQLite — a portable predicate is what lets this
    behaviour be tested at all.

    Re-points rather than disabling the rule. ``disable_rules_bound_to_scan`` is
    the house precedent for "the thing this rule watched is gone", but here it
    is NOT gone: it survives as the target, so re-pointing is what preserves the
    author's intent.
    """
    source_ref = str(source.id)
    target_ref = str(target.id)
    for row in session.execute(
        select(AlertRuleFilter)
        .join(AlertRule, AlertRuleFilter.rule_id == AlertRule.id)
        .where(AlertRuleFilter.field == AlertRuleFilterField.event.value)
    ).scalars():
        values = [str(value) for value in (row.values or [])]
        if source_ref not in values:
            continue
        row.values = replace_preserving_order(values, source_ref, target_ref)


def _move_superseded_pointers(session: Session, *, source: Event, target: Event) -> None:
    """Re-point every event whose successor was the merged-away source.

    "Send this instead" is an instruction about what to do from now on, which
    is the test this module applies: the target IS what to send now, so the
    pointer follows it. Nothing to fold — the column is not unique and any
    number of retired events may name the same successor.

    ONE row is not re-pointed, and it is the target itself. If the target had
    already named the SOURCE as its successor, the blanket re-point set
    ``target.superseded_by_event_id = target.id`` — an event that says "send this
    instead: itself". Nothing at the database level stops it:
    ``models/event.py``'s column is a plain FK with ON DELETE SET NULL and
    c3a81f6d40b2 adds no CHECK. The damage outlives the merge, because the event
    form re-sends ``superseded_by_event_id`` on every save while an event is
    deprecated, so ``_resolve_successor`` then answers 400 on every save until
    the event is un-deprecated. The target's stated successor is being merged
    INTO the target, so "send this instead" has no referent left and the pointer
    is cleared rather than moved.
    """
    for row in session.execute(
        select(Event).where(
            Event.superseded_by_event_id == source.id,
            Event.id != target.id,
        )
    ).scalars():
        row.superseded_by_event_id = target.id
    if target.superseded_by_event_id == source.id:
        target.superseded_by_event_id = None


def _move_chart_annotations(session: Session, *, source: Event, target: Event) -> None:
    """Re-point event-scoped chart markers at the surviving event.

    The annotation's ``label`` and ``description`` are history and are left
    exactly as written; ``scope_ref`` only decides which chart draws it. A
    hand-authored marker that silently stops rendering is unrecoverable — nobody
    knows to go looking for it.

    ``chart_annotations`` declares no ``__table_args__`` at all, so there is no
    constraint to fold against and both events' markers can coexist on the
    survivor's chart. That is right: each was placed deliberately.

    Rows with a NULL ``scope_type`` are project-wide markers and stay untouched,
    as do metric- and event-type-scoped ones.
    """
    for row in session.execute(
        select(ChartAnnotation).where(
            ChartAnnotation.project_id == target.project_id,
            ChartAnnotation.scope_type == ChartAnnotationScopeType.event.value,
            ChartAnnotation.scope_ref == str(source.id),
        )
    ).scalars():
        row.scope_ref = str(target.id)


def _move_implementation_ticket_event_ids(
    session: Session, *, source: Event, target: Event
) -> None:
    """Keep an open delivery ticket pointing at the event that still exists.

    ``event_ids`` is a JSON list of ``str(event_id)``. The ticket sync coerces
    those ids and then selects the events matching them, and BOTH steps drop
    misses silently — so a merged-away event is never flipped to ``implemented``
    when the ticket closes, and the plan quietly disowns work that shipped.

    OPEN tickets only. A closed ticket is a record of what was delivered;
    rewriting it would falsify history, and the sync does not read it anyway.
    """
    source_ref = str(source.id)
    target_ref = str(target.id)
    for ticket in session.execute(
        select(ImplementationTicket).where(
            ImplementationTicket.project_id == target.project_id,
            ImplementationTicket.status == _TICKET_STATUS_OPEN,
        )
    ).scalars():
        event_ids = [str(value) for value in (ticket.event_ids or [])]
        if source_ref not in event_ids:
            continue
        ticket.event_ids = replace_preserving_order(event_ids, source_ref, target_ref)
