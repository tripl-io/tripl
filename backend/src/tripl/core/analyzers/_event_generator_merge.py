"""Event grouping rules, merge logic, and metric-row consolidation."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from tripl.core.analyzers._event_generator_merge_refs import move_dangling_event_references
from tripl.core.analyzers._event_generator_variables import (
    PendingVariableContexts,
    VariableIndex,
    sample_variable_values,
)
from tripl.core.analyzers._event_identity import (
    index_events_by_identity,
    insert_event_claiming_identity,
    scan_identity_winner_order,
)
from tripl.models.alert_delivery_item import AlertDeliveryItem, trim_scope_name
from tripl.models.domain_enums import FieldDefinitionType
from tripl.models.event import Event
from tripl.models.event import EventStatus as _ES
from tripl.models.event import event_status_rank as _rank
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_metric import EventMetric
from tripl.models.event_metric_breakdown import EventMetricBreakdown
from tripl.models.event_photo import EventPhoto
from tripl.models.event_photo_comment import EventPhotoComment
from tripl.models.field_definition import FieldDefinition
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_baseline import MetricBaseline
from tripl.models.metric_breakdown_anomaly import MetricBreakdownAnomaly
from tripl.models.property_drift import PropertyDrift
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import VariableEventValueOverride
from tripl.models.variable_value import VariableValue, VariableValueKind
from tripl.models.variable_value_drift import VariableValueDrift

# Mirrors ``ScanConfig.cardinality_threshold``'s column default and
# ``generate_events``' own, so a caller that does not know the config's value
# folds contexts as one passing 100 would. A fallback, never a preference: a
# caller holding a ``ScanConfig`` should pass ``config.cardinality_threshold``.
DEFAULT_CARDINALITY_THRESHOLD = 100

# A group merge only ever advances the PROGRESSION axis —
# draft -> in_review -> ready_for_dev -> implemented -> live. It never puts the
# surviving event INTO a retired state, and never takes one OUT of one: folding
# a retired member in is not a statement about the group's own retirement, the
# way ``_move_superseded_pointers`` argues for the successor pointer.
#
# ``event_status_rank`` cannot express that by itself, and must not be made to:
# it deliberately orders retirement ABOVE ``live`` (live: 4, deprecated: 5,
# archived: 6) so that a closing implementation ticket cannot drag an already
# retired event back to ``implemented`` (``worker/tasks/implementation_tickets``).
# Ranking is right there and wrong here, so this side excludes the retired band
# rather than reorder the shared table.
_RETIRED_STATUSES = frozenset({_ES.deprecated.value, _ES.archived.value})

# The other half of that partition, derived from the model instead of listed so
# a status added to ``EventStatus`` cannot land outside both bands: every status
# the model defines is either retired or somewhere on the progression axis, and
# ``event_status_rank`` totally orders THAT half — draft 0 < in_review 1 <
# ready_for_dev 2 < implemented 3 < live 4. "Furthest along" means the maximum
# over that total order, and a maximum is commutative: a family merges to the
# same survivor status whichever member the loop reaches first, because a
# retired member folds in as a no-op and every other one folds in as a max.
# ``test_batch3_f1`` pins the partition, the order, and the commutativity.
_PROGRESSION_STATUSES = frozenset(status.value for status in _ES) - _RETIRED_STATUSES

# Where an auto-generated group row starts: the same place ``generate_events``
# starts every scan-minted event, and the floor under the fold above.
_MINTED_GROUP_STATUS = _ES.in_review


@dataclass(frozen=True)
class EventGroupMatch:
    event_name: str
    field_value_overrides: dict[str, str] = field(default_factory=dict)
    matched_rule_name: str | None = None


@dataclass(frozen=True)
class _ContextFacts:
    """The three columns a variable-context fold has to decide.

    Store-agnostic on purpose: one side can be a ``VariableValue`` row and the
    other a dict entry only recorded in memory, and the rule that combines them
    must not care which is which.
    """

    value_kind: str
    observed_count: int
    values: list[str]


def _format_value(raw_val: object) -> str:
    """Format a value for display, showing ints without decimal point."""
    if raw_val is None:
        return ""
    if isinstance(raw_val, float) and raw_val.is_integer():
        return str(int(raw_val))
    return str(raw_val)


def apply_event_group_rules(
    event_name: str,
    values_by_field: Mapping[str, object],
    event_group_rules: Sequence[Mapping[str, object]] | None,
) -> EventGroupMatch:
    """Return the grouped event name for the first matching scan group rule."""
    if not event_group_rules:
        return EventGroupMatch(event_name=event_name)

    for rule in event_group_rules:
        group_name = str(rule.get("name", "")).strip()
        raw_conditions = rule.get("conditions")
        if not group_name or not isinstance(raw_conditions, list):
            continue

        condition_results: list[tuple[str, str, bool]] = []
        for raw_condition in raw_conditions:
            if not isinstance(raw_condition, Mapping):
                continue
            field_name = str(raw_condition.get("field", "")).strip()
            pattern = str(raw_condition.get("pattern", "")).strip()
            if not field_name or not pattern:
                continue
            try:
                # DOTALL so ``.`` spans newlines: some event values carry multi-line
                # free text (e.g. a pasted notification body), and ``^...$`` anchored
                # patterns must still match those against the whole value.
                matched = (
                    re.search(
                        pattern,
                        _format_value(values_by_field.get(field_name)),
                        re.DOTALL,
                    )
                    is not None
                )
            except re.error:
                continue
            condition_results.append((field_name, pattern, matched))

        if not condition_results:
            continue

        logic = str(rule.get("condition_logic", "all")).strip().lower()
        if logic == "any":
            rule_matched = any(matched for _, _, matched in condition_results)
        else:
            rule_matched = all(matched for _, _, matched in condition_results)

        if not rule_matched:
            continue

        # The override keeps the condition's OWN key, dotted or not, because both
        # sinks apply it by exact lookup into their own key space —
        # ``plan_events`` by ``col_meta`` key, ``_create_group_event_from_source``
        # by FieldDefinition name — and that makes a dotted key self-limiting. It
        # lands only where a column or a field is named exactly that, which is
        # where it belongs: a ClickHouse ``Nested`` member arrives as
        # ``params.key``, gets a FieldDefinition under that name, and is offered to
        # the conditions above under the same name by
        # ``_event_values_for_group_matching``. That field must show the pattern it
        # was grouped by, like every other matched field. Against a JSON PATH the
        # same key is simply inert: nothing is named ``payload.action``, so no
        # lookup ever finds it.
        #
        # What must never happen is the reduction ``name_format_base_columns``
        # performs next door. ``payload`` IS a key, and its value is the JSON
        # template ``build_json_value`` produced, so writing the regex literal
        # there strips every ``${payload.path}`` token at once and
        # ``_move_variable_contexts`` below then deletes each context whose target
        # value no longer names its variable — a column's whole observed-value
        # surface for one grouped scan. ``reserved_catalog_columns`` refuses the
        # same reduction on its own side: the two key spaces are one rule, and
        # matching a dotted key is not a licence to shorten it.
        # Both spellings of the identity pseudo-column are excluded, not just
        # the underscored one: ``_event_values_for_group_matching`` sets
        # ``event_name`` alongside ``__event_name``, and the rule editor offers
        # ``event_name`` FIRST — so the obvious choice used to write an override
        # under a key that names no field. Harmless only because the lookup is
        # exact and nothing is usually called ``event_name``.
        overrides = {
            field_name: f"/{pattern}/"
            for field_name, pattern, matched in condition_results
            if matched and field_name not in {"__event_name", "event_name"}
        }
        if len(group_name) > 500:
            group_name = group_name[:497] + "..."
        return EventGroupMatch(
            event_name=group_name,
            field_value_overrides=overrides,
            matched_rule_name=group_name,
        )

    return EventGroupMatch(event_name=event_name)


def merge_existing_events_for_group_rules(
    session: Session,
    *,
    project_id: uuid.UUID,
    event_type_ids: Sequence[uuid.UUID],
    event_group_rules: Sequence[Mapping[str, object]] | None,
    cardinality_threshold: int = DEFAULT_CARDINALITY_THRESHOLD,
) -> int:
    """Apply scan group rules to already-created catalog events.

    No ``pending_variable_contexts``: nothing is generating here, so every
    context this can touch is already a row. ``cardinality_threshold`` reaches
    only the fold that combines two of them.
    """
    if not event_group_rules:
        return 0

    total_merged = 0
    next_event_order = session.execute(
        select(func.max(Event.order)).where(Event.project_id == project_id)
    ).scalar_one()
    next_event_order = 0 if next_event_order is None else int(next_event_order) + 1

    for event_type_id in event_type_ids:
        field_definitions = {
            field_definition.name: field_definition
            for field_definition in session.execute(
                select(FieldDefinition).where(FieldDefinition.event_type_id == event_type_id)
            )
            .scalars()
            .all()
        }
        if not field_definitions:
            continue

        # Same load and same adoption rule as ``generate_events``: this used to
        # adopt EVERY NULL row and file it last-wins, which under
        # ``uq_event_scan_identity`` is an UPDATE the flush refuses whenever
        # two NULL rows share a name, killing the apply-groups job.
        existing_events = (
            session.execute(
                select(Event)
                .where(
                    Event.project_id == project_id,
                    Event.event_type_id == event_type_id,
                )
                .order_by(*scan_identity_winner_order())
            )
            .scalars()
            .all()
        )
        existing_by_identity = index_events_by_identity(existing_events)

        total_merged += _merge_existing_grouped_events(
            session,
            project_id=project_id,
            event_type_id=event_type_id,
            existing_by_identity=existing_by_identity,
            event_group_rules=event_group_rules,
            field_definitions=field_definitions,
            next_event_order=next_event_order,
            cardinality_threshold=cardinality_threshold,
        )
        next_event_order = session.execute(
            select(func.max(Event.order)).where(Event.project_id == project_id)
        ).scalar_one()
        next_event_order = 0 if next_event_order is None else int(next_event_order) + 1

    session.flush()
    return total_merged


def _merge_existing_grouped_events(
    session: Session,
    *,
    project_id: uuid.UUID,
    event_type_id: uuid.UUID,
    existing_by_identity: dict[str, Event],
    event_group_rules: Sequence[Mapping[str, object]] | None,
    field_definitions: dict[str, FieldDefinition],
    next_event_order: int,
    cardinality_threshold: int = DEFAULT_CARDINALITY_THRESHOLD,
    pending_variable_contexts: PendingVariableContexts | None = None,
) -> int:
    """Merge every event a group rule claims into the event that rule names.

    A caller that has already recorded variable contexts against events this pass
    can DELETE must hand its ``pending_variable_contexts`` map over, or those
    entries are written out against a row that no longer exists — see
    ``_reconcile_pending_variable_contexts``.
    """
    if not event_group_rules:
        return 0

    field_name_by_id = {fd.id: name for name, fd in field_definitions.items()}
    # Same rule as ``plan_events``: an override never lands on a
    # JSON field, whose stored value is the template naming every path variable.
    json_field_names = {
        name for name, fd in field_definitions.items() if fd.field_type == FieldDefinitionType.json
    }
    merged = 0

    for identity, source in list(existing_by_identity.items()):
        if source.project_id != project_id or source.event_type_id != event_type_id:
            continue
        if _is_archived(source):
            # Archiving means "put it away". Grouping an archived row rewrites it
            # and then DELETES it in ``_merge_event_into_group``, so a scan whose
            # rules happen to match could destroy plan history the user chose to
            # retire rather than drop.
            continue
        values = _event_values_for_group_matching(source, field_name_by_id)
        match = apply_event_group_rules(identity, values, event_group_rules)
        if match.matched_rule_name is None:
            continue

        target = existing_by_identity.get(match.event_name)
        if target is None:
            # On a lost race this is the row another writer just minted under
            # the group name: the source is merged into it like any existing
            # target, its field values are not copied, and no order number is
            # consumed.
            target, created = _create_group_event_from_source(
                session,
                source=source,
                group_name=match.event_name,
                field_name_by_id=field_name_by_id,
                field_value_overrides={
                    key: literal
                    for key, literal in match.field_value_overrides.items()
                    if key not in json_field_names
                },
                order=next_event_order,
            )
            if created:
                next_event_order += 1
            existing_by_identity[match.event_name] = target
        if _is_archived(target):
            # The group event itself is archived. Merging into it would rewrite
            # this source and then DELETE it, destroying a live row so that its
            # volume could land on one the user has retired.
            #
            # Skipping does NOT leave the source's volume reported under the
            # source: `_build_event_name_from_row` applies the same group rules
            # at collection time, so incoming rows carry the GROUP name either
            # way. What skipping buys is that the catalog row survives and the
            # volume is accounted as archived-and-still-arriving
            # rather than vanishing with a deleted row. Creating a second, live
            # group event under that name is not an option either — it is the
            # same identity as the archived one.
            continue

        if target.id == source.id:
            continue

        _merge_event_into_group(
            session,
            source=source,
            target=target,
            cardinality_threshold=cardinality_threshold,
            pending_variable_contexts=pending_variable_contexts,
        )
        for key, event in list(existing_by_identity.items()):
            if event.id == source.id:
                del existing_by_identity[key]
        merged += 1

    return merged


def _is_archived(event: Event) -> bool:
    """Whether ``event`` is in the terminal, frozen archived state."""
    return event.status == _ES.archived.value


def _event_values_for_group_matching(
    event: Event,
    field_name_by_id: dict[uuid.UUID, str],
) -> dict[str, str]:
    identity = event.source_name or event.name
    values = {"__event_name": identity, "event_name": identity}
    for fv in event.field_values:
        field_name = field_name_by_id.get(fv.field_definition_id)
        if field_name:
            values[field_name] = fv.value
    return values


def _create_group_event_from_source(
    session: Session,
    *,
    source: Event,
    group_name: str,
    field_name_by_id: dict[uuid.UUID, str],
    field_value_overrides: dict[str, str],
    order: int,
) -> tuple[Event, bool]:
    """Mint the group event ``source`` is folded into; ``(row, created)``.

    ``created`` is False when another writer claimed ``group_name`` under this
    type between the caller's load and the INSERT (see
    ``insert_event_claiming_identity``): the holder comes back untouched, with
    none of the source's field values copied onto it — those belong to the row
    that created the group, and the merge that follows carries everything else.
    """
    target, created = insert_event_claiming_identity(
        session,
        Event(
            id=uuid.uuid4(),
            project_id=source.project_id,
            branch_id=source.branch_id,
            event_type_id=source.event_type_id,
            name=group_name,
            source_name=group_name,
            description="Auto-generated event group from data source scan",
            order=order,
            # The group row is auto-generated under a name the user never
            # retired, so minting it retired asserts a retirement nobody made.
            # It starts where every other scan-minted event starts instead
            # (``generate_events`` mints ``in_review`` too), and it starts there
            # UNCONDITIONALLY rather than at the status of whichever member
            # happened to mint it.
            #
            # That member is merged into this row moments later like any other,
            # so a non-retired member's status reaches the survivor through the
            # fold in ``_merge_event_into_group`` regardless. Copying it here
            # only ever mattered when every non-retired member of the family sat
            # BELOW this floor — all of them ``draft`` — where the fold cannot
            # lift the row back up: the group was then born ``draft`` and
            # silently skipped the review queue the scan's own rows start in,
            # or, with a retired member in the family, born ``draft`` or
            # ``in_review`` depending on which row the walk reached first — the
            # walk being ordered by ``last_seen_at`` and nothing else.
            # A constant floor over a commutative fold is the
            # whole of the determinism.
            # ``sunset_at`` and ``superseded_by_event_id`` are deliberately not
            # copied over for the same reason ``_RETIRED_STATUSES`` exists.
            status=_MINTED_GROUP_STATUS.value,
            last_seen_at=source.last_seen_at,
            metric_breakdown_columns=list(source.metric_breakdown_columns or []),
        ),
    )
    if not created:
        return target, False
    for fv in source.field_values:
        field_name = field_name_by_id.get(fv.field_definition_id)
        value = field_value_overrides.get(field_name or "", fv.value)
        session.add(
            EventFieldValue(
                id=uuid.uuid4(),
                event_id=target.id,
                field_definition_id=fv.field_definition_id,
                value=value,
                # A rule override replaces the hand-written value, so authored
                # provenance only survives when the value came through as-is.
                is_authored=fv.is_authored and value == fv.value,
            )
        )
    session.flush()
    return target, True


def _merge_event_into_group(
    session: Session,
    *,
    source: Event,
    target: Event,
    cardinality_threshold: int,
    pending_variable_contexts: PendingVariableContexts | None = None,
) -> None:
    if source.last_seen_at is not None and (
        target.last_seen_at is None or source.last_seen_at > target.last_seen_at
    ):
        target.last_seen_at = source.last_seen_at
    s_status = _ES(source.status) if source.status in _ES._value2member_map_ else _ES.draft
    t_status = _ES(target.status) if target.status in _ES._value2member_map_ else _ES.draft
    if s_status.value in _PROGRESSION_STATUSES and t_status.value in _PROGRESSION_STATUSES:
        # Both sides on the progression axis: the survivor takes the furthest
        # along, by ``event_status_rank`` over the band the two sets partition.
        # Either side retired: the target's status is left exactly as it is —
        # see ``_RETIRED_STATUSES`` for why the rank table cannot decide it. That
        # "leave it" is also what keeps the fold order-independent, because a
        # retired member then contributes nothing from any position in the walk.
        target.status = s_status if _rank(s_status) > _rank(t_status) else t_status
    target.metric_breakdown_columns = sorted(
        set(target.metric_breakdown_columns or []) | set(source.metric_breakdown_columns or [])
    )

    _move_event_tags(session, source=source, target=target)
    _move_event_meta_values(session, source=source, target=target)
    session.execute(
        update(EventPhoto).where(EventPhoto.event_id == source.id).values(event_id=target.id)
    )
    # Event-anchored discussion follows the event, because a comment is human
    # input no later pipeline step can rebuild. Only the event-anchored rows:
    # a photo-anchored comment hangs off ``photo_id`` and rides along with the
    # photo the line above just re-pointed.
    session.execute(
        update(EventPhotoComment)
        .where(EventPhotoComment.event_id == source.id)
        .values(event_id=target.id)
    )
    _merge_event_metric_rows(session, source_ids=[source.id], target_id=target.id)
    _merge_event_metric_breakdown_rows(session, source_ids=[source.id], target_id=target.id)
    _delete_event_anomalies(session, event_ids=[source.id, target.id])
    _move_variable_contexts(
        session, source=source, target=target, cardinality_threshold=cardinality_threshold
    )
    if pending_variable_contexts is not None:
        # Runs for EVERY merge, including one into a target minted moments ago by
        # ``_create_group_event_from_source``: that target is in no plan, so the
        # contexts moved here are the only ones it will ever carry.
        _reconcile_pending_variable_contexts(
            session,
            source=source,
            target=target,
            pending=pending_variable_contexts,
            cardinality_threshold=cardinality_threshold,
        )
    _move_variable_event_overrides(session, source=source, target=target)
    _move_variable_value_drifts(session, source=source, target=target)
    _move_property_drifts(session, source=source, target=target)
    # Everything above re-points a real foreign key. This carries the references
    # that are event ids stored as STRINGS or inside JSON lists, which no
    # database reflection can find and which therefore went unnoticed until the
    # FK ledger was written out by hand.
    move_dangling_event_references(session, source=source, target=target)
    # Only an item whose scope IS the event names it in ``scope_ref``. Other
    # scopes that carry ``event_id`` keep their own reference — a value-drift
    # item's ``scope_ref`` is the drift id and its ``scope_name`` names the
    # variable — so rewriting those would break the Inbox label, search and the
    # rule-state lookup keyed on the drift. They are re-pointed
    # by ``event_id`` alone.
    session.execute(
        update(AlertDeliveryItem)
        .where(
            AlertDeliveryItem.event_id == source.id,
            AlertDeliveryItem.scope_ref == str(source.id),
        )
        # ``scope_name`` is String(255) and ``Event.name`` is String(500), so
        # merging into a long-named survivor used to fail this UPDATE on
        # Postgres — and it runs inside ``run_scan``, whose handler marks the
        # whole ScanJob failed.
        .values(
            event_id=target.id,
            scope_ref=str(target.id),
            scope_name=trim_scope_name(target.name),
        )
    )
    session.execute(
        update(AlertDeliveryItem)
        .where(AlertDeliveryItem.event_id == source.id)
        .values(event_id=target.id)
    )
    session.delete(source)
    session.flush()


def _move_event_tags(session: Session, *, source: Event, target: Event) -> None:
    target_names = {tag.name for tag in target.tags}
    for tag in list(source.tags):
        if tag.name in target_names:
            session.delete(tag)
            continue
        source.tags.remove(tag)
        target.tags.append(tag)
        tag.event_id = target.id
        target_names.add(tag.name)


def _move_event_meta_values(session: Session, *, source: Event, target: Event) -> None:
    target_meta_ids = {value.meta_field_definition_id for value in target.meta_values}
    for meta_value in list(source.meta_values):
        if meta_value.meta_field_definition_id in target_meta_ids:
            session.delete(meta_value)
            continue
        source.meta_values.remove(meta_value)
        target.meta_values.append(meta_value)
        meta_value.event_id = target.id
        target_meta_ids.add(meta_value.meta_field_definition_id)


def _target_value_names_variable(variable: Variable | None, target_value: str | None) -> bool:
    """Whether a context carried onto the target would still assert a true reference.

    Group rules can replace a field value wholesale (``field_value_overrides``),
    and a context migrated onto a literal would claim a reference that is not
    there. Attribution uses the same "any of the variable's tokens" rule as
    ``record_variable_contexts``, so a display name that was slugged away from
    its raw path still matches through ``source_name``/``bindings``.
    """
    if variable is None or target_value is None:
        return False
    return any(f"${{{token}}}" in target_value for token in VariableIndex.tokens_of(variable))


def _fold_context_facts(
    kept: _ContextFacts,
    folded: _ContextFacts,
    *,
    cardinality_threshold: int,
) -> _ContextFacts:
    """Combine two observations of one ``(variable, field)`` pair into one row's worth.

    Neither side is "the incoming one" — these are two records of the same thing
    about to share a row — so every column is decided from BOTH sides:

    * the union is counted BEFORE trimming, because ``values`` is what we can
      show and ``observed_count`` is how many distinct values were seen; the
      count off a trimmed list is the sample size masquerading as a measurement;
    * the kind demotes on the cardinality THRESHOLD, never on the sample cap. A
      ``low`` context promises the reader "All values" and legitimately holds up
      to the threshold's worth of them, so two lows whose union crosses it have
      stopped being an enumeration. Leaving that fold ``low`` parked an uncapped
      over-threshold list behind the badge, since only high rows are trimmed;
    * ``observed_count`` never ends below the values the row holds. A bare
      ``max`` of the two counts leaves exactly that: two low rows of fifteen
      values fold to twenty-five values and a count of fifteen.

    The rule ``preserve_existing_variable_context_values`` and
    ``_merge_replay_variable_samples`` already apply on their own sides; the
    merge sink was the third and is the one that needed fixing.
    """
    merged = sample_variable_values(
        [*kept.values, *folded.values],
        # ``low`` means "no cap" here: the union has to be counted before it is
        # trimmed, exactly as ``distinct_seen`` is on the other two sinks.
        VariableValueKind.low.value,
    )
    distinct_seen = len(merged)
    is_high = distinct_seen > cardinality_threshold or VariableValueKind.high.value in (
        kept.value_kind,
        folded.value_kind,
    )
    value_kind = VariableValueKind.high.value if is_high else VariableValueKind.low.value
    return _ContextFacts(
        value_kind=value_kind,
        observed_count=max(kept.observed_count, folded.observed_count, distinct_seen),
        values=sample_variable_values(merged, value_kind),
    )


def _reconcile_pending_variable_contexts(
    session: Session,
    *,
    source: Event,
    target: Event,
    pending: PendingVariableContexts,
    cardinality_threshold: int,
) -> None:
    """Carry contexts this run has only RECORDED, not yet written, onto the target.

    ``generate_events`` records a context into an in-memory map the moment it
    materialises a planned event, and inserts the whole map long after this pass
    has run. A context recorded for an event this pass then deletes has no row
    for ``_move_variable_contexts`` to re-point — it is a dict entry naming an id
    that no longer exists — and ``insert_variable_contexts`` wrote
    ``context["event_id"]`` out unconditionally, so the flush violated
    ``variable_values_event_id_fkey`` and took the whole ``collect_metrics`` /
    ``run_scan`` job down with an opaque ``IntegrityError``.

    A trailing catch-all rule is the natural way to provoke it: the planner
    matches group rules against RAW warehouse values, this pass re-matches the
    SAME rules against the STORED field values, and those by then hold the group
    name and the ``/pattern/`` override literal the first match wrote — so a
    broad ``^/`` or ``.*`` rule re-matches every group event the specific rules
    just produced.

    Dropping the entries would stop the crash and silently lose the run's
    observations: the surviving event may have been minted by
    ``_create_group_event_from_source`` inside this very pass, so it appears in
    no plan, nothing else records a context for it, and a later scan does not
    bring the values back either. They are reconciled instead, under the three
    cases ``_move_variable_contexts`` applies to the rows already stored.

    Doing it BEFORE ``preserve_existing_variable_context_values`` is what keeps
    the two halves consistent: that reads the database after this pass, so a row
    ``_move_variable_contexts`` just moved onto the target folds into the entry
    moved here instead of colliding with it on ``uq_variable_value_context``.
    """
    moved = [key for key in pending if key[1] == source.id]
    if not moved:
        return

    target_values = {fv.field_definition_id: fv.value for fv in target.field_values}
    variables = {
        variable.id: variable
        for variable in session.execute(
            select(Variable).where(Variable.id.in_({variable_id for variable_id, _, _ in moved}))
        ).scalars()
    }

    for key in moved:
        variable_id, _, field_definition_id = key
        context = pending.pop(key)
        if not _target_value_names_variable(
            variables.get(variable_id), target_values.get(field_definition_id)
        ):
            continue

        target_key = (variable_id, target.id, field_definition_id)
        prior = pending.get(target_key)
        if prior is None:
            context["event_id"] = target.id
            pending[target_key] = context
            continue

        folded = _fold_context_facts(
            _ContextFacts(
                value_kind=str(prior["value_kind"]),
                observed_count=int(prior["observed_count"]),
                values=list(prior["values"] or []),
            ),
            _ContextFacts(
                value_kind=str(context["value_kind"]),
                observed_count=int(context["observed_count"]),
                values=list(context["values"] or []),
            ),
            cardinality_threshold=cardinality_threshold,
        )
        prior["value_kind"] = folded.value_kind
        prior["observed_count"] = folded.observed_count
        prior["values"] = folded.values
        prior["presence_rate"] = _fold_presence(
            prior.get("presence_rate"), context.get("presence_rate")
        )


def _move_variable_contexts(
    session: Session,
    *,
    source: Event,
    target: Event,
    cardinality_threshold: int,
) -> None:
    """Carry the source's observed variable contexts onto the surviving event.

    A ``VariableValue`` says "this event field's value references ``${var}``, and
    here is what was observed for it". ``VariableValue.event_id`` is
    ``ondelete="CASCADE"``, so before this existed the contexts died with
    ``session.delete(source)`` and were never rebuilt — a later scan only records
    a context when the CURRENT run observes that (event, field) pair, so a
    variable whose key had stopped arriving lost its values permanently. That is
    eighteen production variables that a live event's field value
    still names, with an empty ``/values`` list behind an HTTP 200.

    Three cases, in the order the code takes them:

    * **the target's value no longer names the variable** — drop the context;
      see ``_target_value_names_variable`` for why a migrated context can stop
      being true at all;
    * **the target already has a context for that (variable, field)** — fold
      through ``_fold_context_facts``, then delete the source row.
      ``uq_variable_value_context`` is the bare ``(variable_id, event_id,
      field_definition_id)``, so a blanket ``UPDATE ... SET event_id`` would
      raise on the second row instead;
    * **otherwise** — re-point it.

    Target-wins-then-fold matches ``_move_event_tags`` and
    ``_move_event_meta_values`` directly above.

    This handles only rows that already EXIST. Contexts the current run recorded
    in memory and has not inserted yet name an event id that is about to
    disappear and reach no query here at all; those are
    ``_reconcile_pending_variable_contexts``' job, under the same three cases.
    """
    contexts = list(
        session.execute(select(VariableValue).where(VariableValue.event_id == source.id)).scalars()
    )
    if not contexts:
        return

    target_values = {fv.field_definition_id: fv.value for fv in target.field_values}
    existing = {
        (row.variable_id, row.field_definition_id): row
        for row in session.execute(
            select(VariableValue).where(VariableValue.event_id == target.id)
        ).scalars()
    }
    variables = {
        variable.id: variable
        for variable in session.execute(
            select(Variable).where(Variable.id.in_({row.variable_id for row in contexts}))
        ).scalars()
    }

    for context in contexts:
        if not _target_value_names_variable(
            variables.get(context.variable_id), target_values.get(context.field_definition_id)
        ):
            session.delete(context)
            continue

        prior = existing.get((context.variable_id, context.field_definition_id))
        if prior is None:
            context.event_id = target.id
            existing[(context.variable_id, context.field_definition_id)] = context
            continue

        folded = _fold_context_facts(
            _ContextFacts(
                value_kind=prior.value_kind,
                observed_count=prior.observed_count,
                values=list(prior.values or []),
            ),
            _ContextFacts(
                value_kind=context.value_kind,
                observed_count=context.observed_count,
                values=list(context.values or []),
            ),
            cardinality_threshold=cardinality_threshold,
        )
        prior.value_kind = folded.value_kind
        prior.observed_count = folded.observed_count
        prior.values = folded.values
        prior.presence_rate = _fold_presence(prior.presence_rate, context.presence_rate)
        session.delete(context)


def _fold_presence(prior: float | None, incoming: float | None) -> float | None:
    """The presence rate of two contexts folded into one event (F23).

    The row counts behind each rate are gone by now, so the exact weighted
    share cannot be rebuilt. The higher rate is kept: the merged event carried
    the path at least that often in the rows that rate was measured over, and
    a measured rate beats none.
    """
    if prior is None:
        return incoming
    if incoming is None:
        return prior
    return max(prior, incoming)


def _move_variable_event_overrides(session: Session, *, source: Event, target: Event) -> None:
    """Carry hand-authored per-event value lists onto the surviving event.

    ``VariableEventValueOverride`` is written only through the API — the scan
    pipeline never touches one — so letting it cascade away meant a scan
    silently deleting a list a human typed. Target wins on collision
    (``uq_variable_event_value_override`` is ``(variable_id, event_id)``) and
    nothing is folded: two authored lists are two opinions, and merging them
    would invent a third nobody wrote.
    """
    claimed = {
        row.variable_id
        for row in session.execute(
            select(VariableEventValueOverride).where(
                VariableEventValueOverride.event_id == target.id
            )
        ).scalars()
    }
    for override in session.execute(
        select(VariableEventValueOverride).where(VariableEventValueOverride.event_id == source.id)
    ).scalars():
        if override.variable_id in claimed:
            session.delete(override)
            continue
        override.event_id = target.id
        claimed.add(override.variable_id)


def _move_variable_value_drifts(session: Session, *, source: Event, target: Event) -> None:
    """Carry variable value-drift triage onto the surviving event.

    A drift row carries ``accepted`` / ``snoozed`` / ``false_positive`` — a
    decision a person made — and an ``accepted`` row is deliberately frozen
    against rescan. Dropping it re-opens a question that was already answered.
    Target wins on ``uq_variable_value_drift_context`` ``(variable_id,
    event_id)``; the surviving event's own triage is the more recent judgement.
    """
    claimed = {
        row.variable_id
        for row in session.execute(
            select(VariableValueDrift).where(VariableValueDrift.event_id == target.id)
        ).scalars()
    }
    for drift in session.execute(
        select(VariableValueDrift).where(VariableValueDrift.event_id == source.id)
    ).scalars():
        if drift.variable_id in claimed:
            session.delete(drift)
            continue
        drift.event_id = target.id
        claimed.add(drift.variable_id)


def _move_property_drifts(session: Session, *, source: Event, target: Event) -> None:
    """Carry property-drift triage onto the surviving event (F23).

    Same reasoning as ``_move_variable_value_drifts``: the row may hold a
    person's decision. Target wins on ``uq_property_drift`` ``(variable_id,
    event_id, kind)``.
    """
    claimed = {
        (row.variable_id, row.kind)
        for row in session.execute(
            select(PropertyDrift).where(PropertyDrift.event_id == target.id)
        ).scalars()
    }
    for drift in session.execute(
        select(PropertyDrift).where(PropertyDrift.event_id == source.id)
    ).scalars():
        if (drift.variable_id, drift.kind) in claimed:
            session.delete(drift)
            continue
        drift.event_id = target.id
        claimed.add((drift.variable_id, drift.kind))


def _merge_event_metric_rows(
    session: Session,
    *,
    source_ids: list[uuid.UUID],
    target_id: uuid.UUID,
) -> None:
    event_ids = [target_id, *source_ids]
    rows = session.execute(
        select(
            EventMetric.scan_config_id,
            EventMetric.bucket,
            func.sum(EventMetric.count),
        )
        .where(EventMetric.event_id.in_(event_ids))
        .group_by(EventMetric.scan_config_id, EventMetric.bucket)
    ).all()
    if not rows:
        return

    session.execute(delete(EventMetric).where(EventMetric.event_id.in_(event_ids)))
    for scan_config_id, bucket, count in rows:
        session.add(
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=scan_config_id,
                event_id=target_id,
                event_type_id=None,
                bucket=bucket,
                count=int(count or 0),
            )
        )


def _merge_event_metric_breakdown_rows(
    session: Session,
    *,
    source_ids: list[uuid.UUID],
    target_id: uuid.UUID,
) -> None:
    event_ids = [target_id, *source_ids]
    rows = session.execute(
        select(
            EventMetricBreakdown.scan_config_id,
            EventMetricBreakdown.bucket,
            EventMetricBreakdown.breakdown_column,
            EventMetricBreakdown.breakdown_value,
            EventMetricBreakdown.is_other,
            func.sum(EventMetricBreakdown.count),
        )
        .where(EventMetricBreakdown.event_id.in_(event_ids))
        .group_by(
            EventMetricBreakdown.scan_config_id,
            EventMetricBreakdown.bucket,
            EventMetricBreakdown.breakdown_column,
            EventMetricBreakdown.breakdown_value,
            EventMetricBreakdown.is_other,
        )
    ).all()
    if not rows:
        return

    session.execute(
        delete(EventMetricBreakdown).where(EventMetricBreakdown.event_id.in_(event_ids))
    )
    for scan_config_id, bucket, breakdown_column, breakdown_value, is_other, count in rows:
        session.add(
            EventMetricBreakdown(
                id=uuid.uuid4(),
                scan_config_id=scan_config_id,
                event_id=target_id,
                event_type_id=None,
                bucket=bucket,
                breakdown_column=breakdown_column,
                breakdown_value=breakdown_value,
                is_other=bool(is_other),
                count=int(count or 0),
            )
        )


def _delete_event_anomalies(session: Session, *, event_ids: list[uuid.UUID]) -> None:
    scope_refs = [str(event_id) for event_id in event_ids]
    session.execute(
        delete(MetricAnomaly).where(
            MetricAnomaly.scope_type == "event",
            MetricAnomaly.scope_ref.in_(scope_refs),
        )
    )
    session.execute(
        delete(MetricAnomaly).where(
            MetricAnomaly.scope_type == "event",
            MetricAnomaly.event_id.in_(event_ids),
        )
    )
    session.execute(
        delete(MetricBreakdownAnomaly).where(
            MetricBreakdownAnomaly.scope_type == "event",
            MetricBreakdownAnomaly.scope_ref.in_(scope_refs),
        )
    )
    session.execute(
        delete(MetricBreakdownAnomaly).where(
            MetricBreakdownAnomaly.scope_type == "event",
            MetricBreakdownAnomaly.event_id.in_(event_ids),
        )
    )
    # The bands were scored against the pre-merge series of each event, and the
    # table has no ``event_id`` — ``scope_ref`` is the only key. The next metrics
    # run rescores the merged series and writes fresh ones.
    session.execute(
        delete(MetricBaseline).where(
            MetricBaseline.scope_type == "event",
            MetricBaseline.scope_ref.in_(scope_refs),
        )
    )
