"""Scan columns that are metric dimensions or identity, not tracked event fields.

Lives here rather than in ``worker.tasks.metrics.tasks`` because the scan task
needs it too, and importing the metrics task module for one pure helper would
couple two Celery entry points for no reason.
"""

from __future__ import annotations

from collections.abc import Mapping

from tripl.core.analyzers.event_generator import name_format_base_columns
from tripl.json_paths import normalize_json_value_paths
from tripl.models.scan_config import ScanConfig


def _event_group_rule_columns(config: ScanConfig) -> set[str]:
    """Columns the scan's event group rules match on.

    A group rule decides WHICH catalog event a row folds into, so the columns it
    reads are identity inputs — exactly like ``event_type_column`` — not tracked
    properties of the resulting event. They were missing from the reserved set,
    which is the second grouping column the catalog sync never knew about: it
    auto-created a FieldDefinition for the rule's column on every event type,
    and the scan then captured a sample value for it. On the demo that produced
    a "Screen View" field literally rendering the rule's own pattern,
    ``/^Home\\ Screen\\ View$/``.

    Read defensively: ``event_group_rules`` is a JSON column, so a row written
    by an older release (or by hand) may not match the current shape.
    """
    # The config's own list of the dotted names that are JSON paths rather than
    # columns. A DOT is not that test and never was: a warehouse column really can
    # be named with one — a ClickHouse ``Nested`` member comes back from
    # ``SELECT *`` as ``params.key``, which is the idiom for event parameters on
    # the warehouse this product is mostly pointed at — and a rule reading one is
    # a rule column like any other. (BigQuery cannot produce one at all: its
    # result field names hold no dot, which is why that adapter returns nested
    # paths separately from columns.)
    json_value_paths = frozenset(normalize_json_value_paths(config.json_value_paths))
    columns: set[str] = set()
    for rule in config.event_group_rules or ():
        if not isinstance(rule, Mapping):
            continue
        conditions = rule.get("conditions")
        if not isinstance(conditions, list):
            continue
        for condition in conditions:
            if not isinstance(condition, Mapping):
                continue
            # Only a real string is a column name. Coercing with str() turned a
            # ``{"field": null}`` condition into the literal column "None" and
            # reserved it, which would silently exempt a column actually named
            # "None" from plan-gap reporting (Copilot review, PR #72).
            field = condition.get("field")
            if not isinstance(field, str):
                continue
            name = field.strip()
            if not name:
                continue
            if name in json_value_paths:
                # A DECLARED JSON path reserves NOTHING — not the path, not the
                # base column. ``name_format_base_columns`` next door DOES reduce
                # ``{event.category}`` to ``event``, and copying that here is the
                # trap: that reduction runs in the SUBTRACTIVE direction, where a
                # wrong answer costs one unnecessary FieldDefinition. This set is
                # ADDITIVE. Reserving ``event`` for a rule on ``event.category``
                # denies that column its FieldDefinition, ``plan_column_meta``
                # drops it from ``col_meta`` entirely, and every JSON-path variable
                # under it goes with it — tripl-lpin's mechanism reached from the
                # other side, and silent where lpin at least raised. On production
                # every variable is JSON-path derived, so that is a column's whole
                # variable surface for one reserved name.
                continue
            # Everything else passes through verbatim, dot or no dot. The two
            # wrong answers are not the same size. Reserving a name no column
            # carries costs one line in the dry-run's ``reserved_columns`` list and
            # nothing else — every consumer only ever tests membership against
            # real column names. NOT reserving a real dotted column hands it back
            # to ``catalog_sync``, which auto-creates its FieldDefinition, and the
            # merge then paints the rule's own regex into it: jfm3.57 again, on a
            # column the scan groups BY. So an ambiguous dotted name reserves. What
            # this half must never do is INVENT a name — it passes a condition field
            # through or drops it whole, and the base reduction lives in the
            # subtraction below.
            columns.add(name)
    return columns


def reserved_catalog_columns(config: ScanConfig) -> set[str]:
    """Scan columns that are metric dimensions or identity, not tracked event fields.

    The event-type/time grouping columns and the app-version & platform breakdown
    columns are collected into metric tables (EventMetric / EventMetricBreakdown),
    never the catalog. Excluding them from FieldDefinition creation keeps them off
    the events table, where they'd otherwise show up as useless columns carrying
    non-deterministic per-event sample values. Event-group-rule columns join them
    for the same reason — see ``_event_group_rule_columns``.

    Two consumers, both catalog-facing:

    * ``catalog_sync`` takes it as ``skip_columns`` (no FieldDefinition is created)
    * ``generate_events`` takes it as ``reserved_columns`` and stays quiet about a
      reserved column having no FieldDefinition — of course it has none, that is
      this function's doing, and reporting it as a plan gap sent a fresh demo's
      very first scan out with six of seven detail lines wrong

    It deliberately does NOT feed ``check_scalar_columns_unreserved`` — a project
    that already selected a group-rule column as a breakdown keeps working.

    Columns named by ``event_name_format`` are subtracted last and win over every
    rule above. Reserving one is not a cosmetic mistake: ``catalog_sync`` forwards
    this set as ``skip_columns``, so the column gets no FieldDefinition, and
    ``generate_events`` builds its format arguments only from columns that have
    one. The name format is then evaluated with the placeholder missing and the
    whole collection dies on ``the event name format references unknown keys``. That
    is what took production's 'Old events (iOS)' scan down for 200 consecutive
    runs: its group rules match ``action`` and its name format is
    ``{action}``, so tripl-jfm3.90 reserved away the one column the event's
    identity was built from.

    A DOTTED placeholder is subtracted by its BASE column as well as by its full
    key, which is why this subtracts ``name_format_base_columns`` and not just
    the placeholder keys.
    ``{event.category}`` is walked out of the ``event`` column's JSON, and
    ``generate_events`` assembles ``col.path`` keys only for columns that reached
    ``col_meta`` — i.e. that have a FieldDefinition. Subtracting the full key
    ``event.category`` from a set of top-level column names removes nothing, so a
    config whose ``platform_column`` is ``event`` kept ``event`` reserved and
    reproduced tripl-lpin from the other direction: same outage, same message,
    reached through a placeholder shape the subtraction could not see.

    That base reduction belongs to the SUBTRACTION and nowhere else. A group-rule
    condition the config declares to be a JSON value path is dropped WHOLE rather
    than reduced, because reducing on the additive side reserves a column nobody
    asked to reserve; the argument is at the guard in ``_event_group_rule_columns``.
    What both halves buy is one invariant: this function never invents a name the
    config did not write. Every key it returns is either a column the config names
    or a rule's condition field verbatim — a dotted one included, since a dotted
    warehouse column is real — and never a base column derived from a longer key.
    """
    reserved = (
        config.event_type_column,
        config.time_column,
        config.app_version_column,
        config.platform_column,
    )
    named = {column for column in reserved if column} | _event_group_rule_columns(config)
    return named - name_format_base_columns(config.event_name_format)
