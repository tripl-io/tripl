"""What to do with a reference to an event that is going away.

Two paths remove an event and they want opposite things. A group-rule MERGE has
a survivor, so a reference is re-pointed
(``core.analyzers._event_generator_merge_refs``). A DELETE has none, so the
reference is dropped (``services._event_reference_cleanup``). This module holds
the parts that are the same either way, so the two executors cannot drift into
two policies.

The list helpers return a NEW list rather than mutating in place, and that is
load-bearing rather than stylistic: these columns are plain ``JSON`` and this
repository maps no ``MutableList`` anywhere, so an in-place edit leaves the
instance unflagged, the write is never flushed, and every assertion made
against the in-memory object still passes. A test that cannot fail is how
a past bug survived as long as it did.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

__all__ = [
    "AlertFilterChange",
    "drop_preserving_order",
    "plan_alert_filter_change",
    "replace_preserving_order",
]

# ``AlertRuleFilter.operator`` values meaning "only these", as opposed to
# "anything but these". The distinction decides what an EMPTY list means.
_INCLUSIVE_OPERATORS = frozenset({"eq", "in"})


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def replace_preserving_order(values: Iterable[str], old: str, new: str) -> list[str]:
    """Swap *old* for *new*, de-duplicating, order preserved.

    De-duplicating matters because both events can already be listed: a ticket
    would otherwise name the survivor twice in its Jira summary, and a filter
    would render a duplicate chip.
    """
    return _unique([new if value == old else value for value in values])


def drop_preserving_order(values: Iterable[str], removed: set[str]) -> list[str]:
    """Remove every id in *removed*, de-duplicating, order preserved."""
    return _unique([value for value in values if value not in removed])


@dataclass(frozen=True)
class AlertFilterChange:
    """What a delete should do to one ``event``-field alert rule filter.

    ``remaining`` is the id list to store when the filter survives. When
    ``delete_filter`` is set the row goes instead, and ``disable_rule`` says
    whether the rule it belonged to has to be switched off with it.
    """

    remaining: list[str]
    delete_filter: bool = False
    disable_rule: bool = False


def plan_alert_filter_change(
    *, operator: str, values: Iterable[str], removed: set[str]
) -> AlertFilterChange | None:
    """Decide one filter's fate when the events in *removed* are deleted.

    ``None`` means the filter mentions none of them and must not be touched.

    The empty case is the whole reason this is a function rather than a line of
    inline code, because getting it wrong is the difference between a narrowed
    rule and a rule that pages on everything:

    * an emptied **inclusive** filter (``in`` / ``eq``) matches NOTHING, so
      leaving the row would silence the rule invisibly — and deleting the row
      alone would WIDEN it, because a rule narrowed to three events would then
      fire on every event its destination watches. Neither is what the author
      asked for, so the row goes and the rule is disabled: visibly off, and
      repairable from the UI.
    * an emptied **exclusive** filter (``not_in`` / ``ne``) matches EVERYTHING,
      which is exactly what "exclude these three" degrades to once the three are
      gone. The author's intent survives intact, so the row goes and the rule
      stays enabled.

    An empty ``values`` list must never be persisted either way:
    ``AlertRuleFilterResponse`` inherits the "must have at least one value"
    validator, so one stored empty list makes the destinations endpoint 500 for
    the whole project — including the screen an operator would use to fix it.
    """
    current = [str(value) for value in values]
    if not removed.intersection(current):
        return None

    remaining = drop_preserving_order(current, removed)
    if remaining:
        return AlertFilterChange(remaining=remaining)
    return AlertFilterChange(
        remaining=[],
        delete_filter=True,
        disable_rule=operator in _INCLUSIVE_OPERATORS,
    )
