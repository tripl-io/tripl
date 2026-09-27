"""Every reference to an event must be a decision, not an oversight (tripl-xfxa).

``_event_generator_merge._merge_event_into_group`` migrates a merged-away
event's rows onto the surviving group event and then calls
``session.delete(source)``. Eighteen columns FK to ``events.id``; the merge
handled six of them, and the other twelve went quietly over the cliff. Nothing
failed, nothing logged, and the loss only surfaced months later as variables
whose ``/values`` list had gone empty behind an HTTP 200.

The defect was not "somebody wrote the wrong code". It was that the list of
things a merge has to think about lived nowhere — not in a docstring, not in a
test, only in whichever columns the author happened to remember. So this module
derives that list from the schema instead, and forces every entry into one of
three buckets with a written reason. Adding a new ``ForeignKey("events.id")``
anywhere in the codebase now fails here until someone decides what a merge
should do with it.

This is a schema-shape test, not a behaviour test: it never opens a database.
The behavioural counterparts live in ``test_event_generator.py``.
"""

from __future__ import annotations

from pathlib import Path

import tripl.models  # noqa: F401  (imports every model so Base.metadata is complete)
from tripl.models.base import Base
from tripl.services._event_reference_cleanup import DELETE_PATH_COLUMNS

EVENT_PRIMARY_KEY = "events.id"

# The merge re-points or folds these onto the target before deleting the source.
# Value: where in ``_event_generator_merge.py`` that happens.
MIGRATED: dict[tuple[str, str], str] = {
    ("event_tags", "event_id"): "_move_event_tags: re-points, target wins on duplicate name",
    ("event_meta_values", "event_id"): (
        "_move_event_meta_values: re-points, target wins per meta field definition"
    ),
    (
        "event_photos",
        "event_id",
    ): "_merge_event_into_group: blanket UPDATE, photos have no unique key",
    ("events", "superseded_by_event_id"): (
        "MERGE: _move_superseded_pointers re-points every event whose successor was the "
        "merged-away source onto the target — 'send this instead' is a statement about what to "
        "do from now on, and the target is what to send now. No fold: the column is not unique "
        "and any number of retired events may name the same successor. ONE row is exempt, the "
        "TARGET itself: re-pointing it would write superseded_by_event_id = its own id, an event "
        "telling clients to send itself instead, which _resolve_successor then 400s on every save "
        "while the event is deprecated (tripl-0zpq.86). Where the target really did name the "
        "source, its pointer is CLEARED rather than moved: the successor it named is the row "
        "being merged into it, so 'send this instead' has no referent left. "
        "DELETE: the FK is ondelete SET NULL and that is the entire policy — a deleted "
        "successor means there is no successor to name, so the pointer clears itself. Nothing "
        "is added to _event_reference_cleanup, because a second mechanism for one rule is how "
        "the two drift apart."
    ),
    ("event_photo_comments", "event_id"): (
        "MERGE: _merge_event_into_group re-points the event-anchored discussion onto the "
        "survivor with a blanket UPDATE — the table has no unique key on event_id, and a "
        "comment is human input no later pipeline step can rebuild. Replies carry the same "
        "anchor as their parent, so one UPDATE moves whole threads. Photo-anchored rows are "
        "NOT touched there: they hang off photo_id and ride along with the photo the "
        "preceding statement re-points. DELETE: the FK is ondelete CASCADE and that is the "
        "right answer — there is no survivor to carry the thread to, and the discussion is "
        "about a row that no longer exists."
    ),
    ("event_metrics", "event_id"): (
        "_merge_event_metric_rows: sums both events' counts per (scan_config, bucket) onto target"
    ),
    ("event_metric_breakdowns", "event_id"): (
        "_merge_event_metric_breakdown_rows: sums both events' counts onto the target"
    ),
    ("alert_delivery_items", "event_id"): (
        "_merge_event_into_group: UPDATE re-points event_id and rewrites scope_ref/scope_name"
    ),
    ("variable_values", "event_id"): (
        "_move_variable_contexts: re-points, folds on uq_variable_value_context, "
        "drops when the target's field value no longer names the variable"
    ),
    ("variable_event_value_overrides", "event_id"): (
        "_move_variable_event_overrides: re-points, target wins on uq_variable_event_value_override"
    ),
    ("variable_value_drifts", "event_id"): (
        "_move_variable_value_drifts: re-points, target wins on uq_variable_value_drift_context"
    ),
    ("metric_definitions", "numerator_event_id"): (
        "_move_metric_composition_operands: re-points. No fold — nothing constrains the operand "
        "columns, and two metrics may legally name the same event. When BOTH operands of a ratio "
        "land on the target the metric would compute a constant 1.0, so it is re-pointed and then "
        "driven to the error state naming both originals (tripl-jtnv)."
    ),
    ("metric_definitions", "denominator_event_id"): (
        "_move_metric_composition_operands, same rule as numerator_event_id. Read for 'ratio' "
        "only — 'single' and 'per_distinct_user' ignore it — but re-pointed regardless, because a "
        "dangling NULL is worse than an unread id."
    ),
    # Both operand entries describe the MERGE. On a DELETE there is no survivor,
    # so the SET NULL stands and nothing here re-points it — the metric is driven
    # red at collection instead, by event_composition_binding_error, which asks
    # about the BINDING rather than about the door. One kind-level guard covers
    # a deleted event, a deleted event type and any future SET NULL on those
    # four columns; a per-door copy is the "seventh call site" failure again
    # (tripl-nmn3).
}

# The merge intentionally lets these go. Nothing rebuilds them; losing them is
# the accepted cost of collapsing two catalog rows into one.
DELIBERATELY_DROPPED: dict[tuple[str, str], str] = {
    ("event_changes", "event_id"): (
        "Append-only edit history of the row being deleted. Re-pointing would attribute the "
        "source's edits to the group event, which is a lie about who changed what."
    ),
    ("shadow_event_candidates", "accepted_event_id"): (
        "Cosmetic resolution-audit pointer ('a human triaged this ghost identity into that "
        "event'), read only by the reconciliation list response; nothing operational consumes it."
    ),
}

# Known defects, parked here so the ledger stays complete without pretending
# they are settled.
#
# A reason string that says "this is a bug" inside a set called
# DELIBERATELY_DROPPED is only as good as the reader's willingness to read it,
# and a ledger like this gets skimmed by set NAME years after anyone remembers
# why. A fourth name means an open gap cannot be mistaken for a decision.
# Entries move to MIGRATED when fixed; the healthy state of this set is empty.
KNOWN_GAPS: dict[tuple[str, str], str] = {}

# Recreated by a later step, so losing them on merge is harmless.
DELIBERATELY_CASCADES: dict[tuple[str, str], str] = {
    ("alert_pending_items", "event_id"): (
        "An alert matched but NOT yet delivered, waiting for its destination's next digest "
        "window. MERGE: the FK is ondelete CASCADE (unlike alert_delivery_items.event_id, which "
        "is SET NULL because a sent message is history and must keep its record), so the source's "
        "buffered rows die with the source event and _prepare_alert_deliveries re-buffers the "
        "survivor's own anomaly on the next metrics collection — under the correct scope_ref, "
        "scope_name and correlation_group_id, none of which a blanket re-point would fix. "
        "Re-pointing would also have to fold on uq_alert_pending_item_scope whenever both events "
        "were buffered for the same rule. DELETE: same CASCADE, and the right answer for the same "
        "reason — a digest must not name an event that no longer exists."
    ),
    ("event_field_values", "event_id"): (
        "_create_group_event_from_source already wrote the target's own values with the rule "
        "overrides applied; the source's are redundant and would collide on "
        "uq_event_field_value_event_field anyway."
    ),
    ("metric_anomalies", "event_id"): (
        "NOTE the FK is ondelete=SET NULL, not CASCADE — this bucket means 'not carried onto a "
        "survivor', and something else has to do the removing on every path. MERGE: "
        "_delete_event_anomalies deletes BOTH events' rows on purpose (merging the series "
        "invalidates the target's baseline too) and detect.py rescores on the next "
        "collect_metrics. DELETE: _event_reference_cleanup deletes them, by event_id AND by "
        "scope_ref. Until it did, a deleted event left its anomalies behind with a NULL event_id, "
        "and NULL reads as 'allow' at both gates — signals.py keeps a row whose joined event is "
        "NULL, and filter_matches_anomaly returns True for a NULL actual — so the orphans passed "
        "every event filter. Archiving an event suppressed its alerts; deleting one un-suppressed "
        "them (tripl-xjuv)."
    ),
    ("metric_breakdown_anomalies", "event_id"): (
        "Same as metric_anomalies.event_id in every respect, breakdown variant: the FK is SET "
        "NULL, not CASCADE. MERGE: _delete_event_anomalies removes both events' rows. DELETE: "
        "_event_reference_cleanup removes them by event_id AND scope_ref. Rescored by detect.py."
    ),
    ("release_regressions", "event_id"): (
        "MERGE: wiped and recomputed in full per scan_config on every collect_metrics "
        "(worker/tasks/metrics/regression.py), and event_id is re-derived from the event_metrics "
        "rows the merge already re-pointed. DELETE: _event_reference_cleanup deletes them by both "
        "keys, because the recompute only LOOKS like a fix. The FK is SET NULL, so until the next "
        "collection an orphan sits there with scope_type='event' and a live scope_ref, signals "
        "lifts it into a drift candidate, and drift candidates match on a bare "
        "all(filter_matches_anomaly(...)) — where a NULL event_id satisfies every event filter. "
        "Self-healing within one scan interval is not the same as harmless."
    ),
    ("lifecycle_findings", "event_id"): (
        "Derived sunset-watch state, rebuilt by the daily check-lifecycle-findings beat "
        "(worker/tasks/lifecycle.py compute_lifecycle_findings), which reconciles the whole table "
        "against what holds now. MERGE: the FK is ondelete CASCADE, so the source's findings die "
        "with the source row; if the surviving event is deprecated and still overdue or has a "
        "silent successor, the next sweep opens its own finding under the right event. Carrying "
        "the source's row would also have to fold on uq_lifecycle_finding_event_kind. DELETE: "
        "same CASCADE, and right for the same reason — a finding about a deleted event has no "
        "page to show it on and no migration left to run."
    ),
    ("lifecycle_findings", "related_event_id"): (
        "The successor named by a successor_silent finding, copied from the deprecated event's "
        "superseded_by_event_id on every sweep. NOTE the FK is ondelete SET NULL, not CASCADE. "
        "MERGE: _move_superseded_pointers re-points the deprecated event's superseded_by_event_id "
        "onto the target, and the next check-lifecycle-findings run rewrites related_event_id "
        "from it — with the target's own volume deciding whether the finding stays open. "
        "DELETE: the successor's deletion SET NULLs superseded_by_event_id too, so the condition "
        "no longer holds and the next sweep resolves the finding; until then the NULL only drops "
        "the finding from the successor's page, which no longer exists."
    ),
    ("search_documents", "parent_event_id"): (
        "Derived index, and the cascade does the deleting: the FK is ON DELETE CASCADE, so the "
        "SOURCE's documents go with the source row — the reindex's actual job here is minting the "
        "TARGET's missing document. Both catalog-mutating tasks in worker/tasks/scan.py now "
        "reindex after their commit; apply_event_groups did not until tripl-68l3."
    ),
}

# Reflection cannot see these: they are String/JSON columns that happen to hold
# an event's uuid, with no ForeignKey for ``Base.metadata`` to report. The list
# is therefore MANUAL — nothing detects a new one for you, so a new polymorphic
# ``scope_ref`` or a new JSON payload embedding an event id has to be added here
# by hand. The tests below only check that what is pinned still exists and is
# still not a foreign key.
NON_FK_EVENT_REFERENCES: dict[tuple[str, str], str] = {
    ("metric_anomalies", "scope_ref"): (
        "str(event.id) when scope_type == 'event'. MERGE: _delete_event_anomalies deletes by "
        "scope_ref as well as by event_id, for both events. DELETE: _event_reference_cleanup does "
        "the same two-key delete. Two keys because there is no FK on scope_ref, which is also why "
        "changing the event_id FK to CASCADE would not have been a fix — it could only ever reach "
        "half the rows."
    ),
    ("metric_breakdown_anomalies", "scope_ref"): (
        "Same as metric_anomalies.scope_ref, breakdown variant. MERGE: _delete_event_anomalies. "
        "DELETE: _event_reference_cleanup. Two-key delete on both paths."
    ),
    ("metric_baselines", "scope_ref"): (
        "str(event.id) when scope_type == 'event'; the table has no event_id column, so this is "
        "its only key. MERGE: _delete_event_anomalies deletes the rows for both events — the "
        "bands describe pre-merge series and the next metrics run rescores. DELETE: "
        "_event_reference_cleanup deletes them; nothing else would, so an orphan band would be "
        "kept forever for a series that no longer exists."
    ),
    ("alert_delivery_items", "scope_ref"): (
        "str(event.id) copied off the anomaly for event-scope items; other scopes carry their "
        "own id (a value drift's is the drift id). Handled: _merge_event_into_group rewrites it "
        "to str(target.id) only where it equals str(source.id), and re-points event_id for all."
    ),
    ("alert_delivery_items", "details_path"): (
        "Frozen '/monitoring/event/{event_id}' link. NOT rewritten by the merge — a delivered "
        "alert is a historical record of what was said, so the stale link is tolerated."
    ),
    ("alert_delivery_items", "monitoring_path"): (
        "Frozen '/monitoring/event/{scope_ref}' link, same historical-record argument as "
        "details_path; NOT rewritten by the merge."
    ),
    ("alert_rule_states", "scope_ref"): (
        "The open/closed incident + cooldown key, keyed on str(event.id) with no event_id column. "
        "NOT rewritten, ON PURPOSE — and an earlier version of this entry claiming the incident is "
        "stranded was simply wrong. The state closes on the very next dispatch, because the dead "
        "scope stops producing candidates and _prepare_alert_deliveries closes every active state "
        "it did not match. What guarantees the scope goes quiet differs by path, and both have to "
        "be named or this argument is only half checked: _delete_event_anomalies on the MERGE, "
        "_event_reference_cleanup's two-key delete on the DELETE. Re-pointing would be the actual "
        "regression — it hands the target the source's last_notified_at and suppresses the group's "
        "first genuine alert."
    ),
    ("anomaly_scope_overrides", "scope_ref"): (
        "The per-scope false-positive sigma ratchet, keyed on str(event.id) with no event_id "
        "column. MERGE: _move_anomaly_scope_overrides re-points it, folding per scan_config_id "
        "(NULL matched to NULL, so both uq_anomaly_scope_override_scope and the partial "
        "uq_anomaly_scope_override_metric_scope index hold) with max sigma, max min_expected_count "
        "and summed false_positive_count — the ratchet only ever tightens, so max is 'never undo "
        "a click'. DELETE: _event_reference_cleanup removes the row. There is no survivor to "
        "tighten, the model says deleting the row IS the undo, and leaving it would strand a "
        "threshold in Detection settings naming an event nobody can open."
    ),
    ("release_regressions", "scope_ref"): (
        "str(event.id) beside the real event_id FK. MERGE: not rewritten, and harmless there — "
        "the whole table is wiped and recomputed per scan_config on every collect_metrics. "
        "DELETE: _event_reference_cleanup removes the row, because between the delete and that "
        "recompute the orphan alerts past every event filter (see the event_id entry)."
    ),
    ("chart_annotations", "scope_ref"): (
        "str(event.id) when scope_type == 'event', no event_id column. MERGE: "
        "_move_chart_annotations re-points event-scoped rows only. The label and description are "
        "left exactly as written — the annotation's TEXT is history, its scope_ref is only where "
        "the marker gets drawn. DELETE: _event_reference_cleanup removes the row; with no event "
        "there is no chart to draw it on and the reader can never select it again. Promoting it "
        "to a project-wide marker was rejected — that would paint a deleted event's annotation "
        "onto every chart in the project."
    ),
    ("implementation_tickets", "event_ids"): (
        "JSON list of event uuid STRINGS the ticket covers, read back to flip events to "
        "'implemented' when the ticket closes. MERGE: _move_implementation_ticket_event_ids "
        "rewrites the list whole (no MutableList is mapped anywhere in this repo, so an in-place "
        "edit is silently discarded) and de-duplicates. DELETE: _event_reference_cleanup drops "
        "the id. Inert either way on the delete path — both steps of the ticket sync already skip "
        "ids that resolve to nothing — but dropped so the two paths state one rule rather than "
        "one rule and an exception. OPEN tickets only on both: a closed one records what shipped."
    ),
    ("alert_rule_filters", "values"): (
        "JSON list of str(event.id) when field == 'event'. MERGE: _move_alert_rule_filter_values "
        "rewrites the list whole and de-duplicates. DELETE: the id is dropped, and if that empties "
        "the list the FILTER ROW goes — an emptied 'in' matches nothing and an emptied 'not_in' "
        "matches everything, so leaving it would silently invert the rule, and deleting the row "
        "alone would WIDEN an 'in' rule to everything its destination watches. An emptied "
        "inclusive filter therefore also disables its rule and clears its states (the "
        "disable_rules_bound_to_scan precedent: inert and visibly off, never silently re-aimed); "
        "an emptied exclusive one leaves the rule enabled, because 'exclude these three' really "
        "does degrade to 'exclude nothing'. An empty values list is never persisted: "
        "AlertRuleFilterResponse inherits the at-least-one-value validator, so one would 500 the "
        "destinations endpoint for the whole project. Matching runs in Python on both paths, not "
        "as JSON containment, because that operator is PostgreSQL-only and the suite runs on "
        "SQLite — a portable predicate is what makes this testable."
    ),
    ("scan_jobs", "result_summary"): (
        "JSON generation snapshot embedding str(event.id) per generated event, read back by the "
        "metric replay path. NOT rewritten, and the earlier version of this entry overstated the "
        "danger: a snapshot records what one run produced, and poisoning a replay takes a "
        "crash-committed merge PLUS a failed follow-up run PLUS a manual replay — after which the "
        "insert dies on a real FK having written nothing, which is loud rather than silent. The "
        "fix belongs in the reader, not in rewriting a record."
    ),
    ("search_documents", "entity_id"): (
        "event.id for entity_type == 'event', no FK of its own. Covered anyway: the same row's "
        "parent_event_id FK cascades, and the reindex rebuilds it."
    ),
    ("search_documents", "route_path"): (
        "'/p/{slug}/monitoring/event/{event.id}'. Covered by the same parent_event_id cascade "
        "plus reindex as entity_id."
    ),
    ("plan_revisions", "payload"): (
        "JSON plan snapshot serialising each event as str(ev.id). Deliberately frozen: a revision "
        "records what the plan looked like then, so rewriting it would falsify history."
    ),
    ("alert_deliveries", "payload_snapshot"): (
        "JSON record of exactly what one delivery said, event ids included. Deliberately frozen "
        "for the same reason as plan_revisions.payload."
    ),
    ("audit_log", "payload"): (
        "JSON audit entries embedding str(event_id). Deliberately immutable — an audit trail that "
        "gets rewritten is not an audit trail."
    ),
    ("alert_correlation_states", "correlation_group_id"): (
        "The worst-hidden one: an event id HASHED into a plain uuid column. "
        "_correlation_group_id computes uuid5(ns, '{scan_config}:{rule}:{scope_type}:{scope_ref}"
        ":{direction}') and scope_ref is str(event.id) for event scope, so the reference survives "
        "neither reflection nor a scope_ref grep. "
        "NEITHER carried NOR deleted, and both halves of that are deliberate (tripl-crow). "
        "Not carried: the decision was made about a different series, and handing it to the "
        "survivor would suppress the group's FIRST genuine alert on a baseline the merge just "
        "wiped — the same argument that keeps alert_rule_states.scope_ref in place. The durable "
        "half of the judgement, the false-positive ratchet, IS carried, by "
        "_move_anomaly_scope_overrides. "
        "Not deleted either, which is the less obvious half: the row cannot suppress anything "
        "once its tuple stops being minted, _reopen_closed_incidents releases acknowledged / "
        "resolved / false-positive on the next dispatch anyway, and _effective_inbox_status reads "
        "a missing row as 'open' — exactly what reopening produces. So deleting buys the same "
        "status while destroying the operator's note and their acted_by/acted_at attribution. "
        "The one accepted cost is an in-force timed mute, which does not transfer."
    ),
    ("signal_triage", "scope_ref"): (
        "Signal acknowledge / mute / expected state, keyed on str(event.id) when "
        "scope_type == 'event', with no event_id column. NEITHER carried NOR deleted on MERGE or "
        "DELETE: a triage row only matters while a signal matches it, and both paths delete that "
        "event's anomalies (_delete_event_anomalies, _event_reference_cleanup), so no signal can "
        "match the orphan again. Not carried for the same reason as alert_rule_states.scope_ref: "
        "the survivor's baseline was just wiped, and inheriting a mute would hide its first "
        "genuine signal."
    ),
}

_ALL_CLASSIFICATIONS: dict[str, dict[tuple[str, str], str]] = {
    "MIGRATED": MIGRATED,
    "DELIBERATELY_DROPPED": DELIBERATELY_DROPPED,
    "DELIBERATELY_CASCADES": DELIBERATELY_CASCADES,
    "KNOWN_GAPS": KNOWN_GAPS,
}

_UNCLASSIFIED_HINT = (
    "Decide what a group-event merge should do with each column before this test can pass:\n"
    "  * if the merge should carry the row onto the surviving event, teach\n"
    "    _merge_event_into_group to move it (mind the unique constraint — a blanket\n"
    "    UPDATE ... SET event_id raises on the first collision) and add it to MIGRATED;\n"
    "  * if the row is rebuilt by a later pipeline step, add it to DELIBERATELY_CASCADES\n"
    "    naming the step that rebuilds it;\n"
    "  * if the row should simply die with the source event, add it to DELIBERATELY_DROPPED;\n"
    "  * if it SHOULD be carried but is not yet, add it to KNOWN_GAPS — an honest gap beats\n"
    "    a decision nobody made.\n"
    "All three of the latter require a written reason — the whole point of this ledger is\n"
    "that the next person can see the decision instead of re-deriving it.\n"
    "Then answer the SECOND question, because an event is removed by two kinds of path and\n"
    "they want opposite things: a merge has a survivor to re-point at, a delete has none.\n"
    "The buckets above classify the MERGE; say what the DELETE does too.\n"
    "See core/analyzers/_event_generator_merge_refs.py (merge, sync) and\n"
    "services/_event_reference_cleanup.py (delete, async)."
)


def _event_foreign_keys() -> set[tuple[str, str]]:
    """Every ``(table, column)`` in the mapped schema that FKs to ``events.id``."""
    references: set[tuple[str, str]] = set()
    for table_name, table in Base.metadata.tables.items():
        for column in table.columns:
            for foreign_key in column.foreign_keys:
                if foreign_key.target_fullname == EVENT_PRIMARY_KEY:
                    references.add((table_name, column.name))
    return references


def test_reflection_actually_finds_the_event_foreign_keys():
    """Guard the guard: a broken walk would make every test below pass vacuously."""
    references = _event_foreign_keys()
    assert references, (
        "Reflection found NO foreign keys to events.id. Either `import tripl.models` no longer "
        "registers every model on Base.metadata, or the target_fullname comparison against "
        f"{EVENT_PRIMARY_KEY!r} has drifted. Fix the walk — do not relax the assertion."
    )


def test_every_foreign_key_to_events_is_classified():
    """A new FK to events.id is a new decision the merge has to make."""
    references = _event_foreign_keys()

    unclassified = sorted(
        reference
        for reference in references
        if not any(reference in bucket for bucket in _ALL_CLASSIFICATIONS.values())
    )

    assert not unclassified, (
        "These columns reference events.id but no one has said what "
        "_merge_event_into_group should do with them:\n"
        + "".join(f"  - {table}.{column}\n" for table, column in unclassified)
        + _UNCLASSIFIED_HINT
    )


def test_no_column_is_classified_two_ways():
    """One column, one decision — otherwise the ledger says two things at once."""
    duplicates = sorted(
        (reference, sorted(names))
        for reference in _event_foreign_keys()
        if len(
            names := [name for name, bucket in _ALL_CLASSIFICATIONS.items() if reference in bucket]
        )
        > 1
    )
    assert not duplicates, f"Columns classified in more than one bucket: {duplicates}"


def test_the_classification_ledger_has_no_stale_entries():
    """A ledger that outlives its schema is worse than none — it reads as verified."""
    references = _event_foreign_keys()
    stale = sorted(
        (name, reference)
        for name, bucket in _ALL_CLASSIFICATIONS.items()
        for reference in bucket
        if reference not in references
    )
    assert not stale, (
        "These entries no longer reference events.id — the column was renamed, dropped, or its "
        f"foreign key removed. Delete the entry: {stale}"
    )


def test_every_deliberate_classification_carries_a_reason():
    """The reason is the deliverable; the set membership alone explains nothing."""
    missing = sorted(
        f"{name}[{table}.{column}]"
        for name, bucket in _ALL_CLASSIFICATIONS.items()
        for (table, column), reason in bucket.items()
        if not reason.strip()
    )
    assert not missing, f"Classified without a written reason: {missing}"


def test_non_fk_event_references_are_pinned_and_still_invisible_to_reflection():
    """The manual half of the ledger, checked against the schema it describes.

    Reflection cannot find a uuid stored in a ``String`` or buried in a JSON
    payload, so ``NON_FK_EVENT_REFERENCES`` is hand-maintained and nothing here
    can prove it complete. What these assertions do buy: the pinned columns
    still exist under those names, and none of them has quietly grown a real
    foreign key — in which case the entry belongs in one of the reflected sets
    instead, where the completeness check applies.
    """
    assert NON_FK_EVENT_REFERENCES, "The manual ledger is empty; see the module docstring."

    reflected = _event_foreign_keys()
    problems: list[str] = []
    for (table_name, column_name), reason in NON_FK_EVENT_REFERENCES.items():
        table = Base.metadata.tables.get(table_name)
        if table is None:
            problems.append(f"{table_name}.{column_name}: table no longer exists")
            continue
        if column_name not in table.columns:
            problems.append(f"{table_name}.{column_name}: column no longer exists")
            continue
        if (table_name, column_name) in reflected:
            problems.append(
                f"{table_name}.{column_name}: now a real FK to events.id — move it into "
                "MIGRATED / DELIBERATELY_DROPPED / DELIBERATELY_CASCADES"
            )
        if not reason.strip():
            problems.append(f"{table_name}.{column_name}: pinned without a written reason")

    assert not problems, "Manual event-reference ledger is out of date:\n" + "\n".join(
        f"  - {problem}" for problem in problems
    )


def test_the_delete_path_policy_is_pinned_against_the_executor() -> None:
    """The ledger's delete-path claims must match what the delete path touches.

    Reasons are prose, and prose drifts. This is the one mechanical tie: every
    (table, column) the async cleanup declares it handles has to be pinned
    somewhere in this ledger with a reason that actually mentions the delete
    path — otherwise a column could gain a delete-path policy in the code while
    its entry here still described only the merge, which is precisely the state
    this file was in before tripl-xjuv.
    """
    pinned = {
        **NON_FK_EVENT_REFERENCES,
        **MIGRATED,
        **DELIBERATELY_DROPPED,
        **DELIBERATELY_CASCADES,
    }

    problems: list[str] = []
    for key in sorted(DELETE_PATH_COLUMNS):
        reason = pinned.get(key)
        if reason is None:
            problems.append(f"{key[0]}.{key[1]}: cleared by the delete path but not pinned here")
            continue
        if "DELETE" not in reason:
            problems.append(
                f"{key[0]}.{key[1]}: pinned, but its reason never says what the delete path does"
            )

    assert not problems, (
        "services/_event_reference_cleanup.py and this ledger disagree:\n"
        + "\n".join(f"  - {problem}" for problem in problems)
    )


# Every module that calls the delete-path executor.
#
# The buckets above classify what a MERGE does, because that is the path with a
# survivor to re-point onto. Every other way an event disappears drops the
# references instead — and the list of those ways is exactly the thing that
# lived nowhere before this file existed, which is how the merge came to handle
# six columns out of eighteen.
#
# All five drop, and none of them has a survivor to consider: the merge's two
# deletes remove MAIN rows whose event type the branch removed or that the
# branch deleted on purpose, and revert and delete_branch remove branch-local
# rows outright (tripl-a64t).
DELETE_PATH_CALLERS: dict[str, str] = {
    "services/event_service.py": "delete_event, bulk_delete_events",
    "services/event_type_service.py": "delete_event_type — its events go by DB cascade, unseen",
    "services/plan_branch_merge_service.py": (
        "_apply_merge, twice: main events orphaned by an event type the branch removed, "
        "and main events the branch deleted"
    ),
    "services/plan_branch_revert_service.py": (
        "revert_change, the 'added' arm — the branch-local event, or its event type's "
        "events via the same invisible cascade"
    ),
    "services/plan_branch_service.py": "delete_branch — every event on the branch, by cascade",
}


def test_every_caller_of_the_delete_path_is_pinned_here() -> None:
    """A new door must decide, in writing, that dropping is right for it.

    The merge spent months handling six of eighteen columns because nothing
    listed what a merge had to think about. The same hole is open one level up:
    nothing lists the ways an event can disappear. A source scan closes it — add
    a caller and this fails until the caller is named and explained.
    """
    backend_src = Path(__file__).resolve().parents[2]
    found: set[str] = set()
    for path in backend_src.rglob("*.py"):
        relative = path.relative_to(backend_src).as_posix()
        if relative.startswith("tripl/tests/") or relative.endswith("_event_reference_cleanup.py"):
            continue
        if "drop_dangling_event_references(" in path.read_text(encoding="utf-8"):
            found.add(relative.removeprefix("tripl/"))

    assert found, (
        "no caller of drop_dangling_event_references was found at all — the scan is broken, "
        "and a scan that finds nothing would let every assertion below pass vacuously"
    )
    assert found == set(DELETE_PATH_CALLERS), (
        "the delete path's callers and DELETE_PATH_CALLERS disagree.\n"
        f"  in code, not pinned: {sorted(found - set(DELETE_PATH_CALLERS))}\n"
        f"  pinned, not in code: {sorted(set(DELETE_PATH_CALLERS) - found)}\n"
        "Every way an event can disappear has to say, here, what it does with the "
        "references to it — that list living nowhere is the defect this file exists for."
    )
    for module, reason in DELETE_PATH_CALLERS.items():
        assert reason.strip(), f"{module}: pinned without saying which function or why"
