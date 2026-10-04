"""What happens to a reference when the event it names goes away.

``core.event_references`` is the shared half of two opposite executors: a group
merge re-points a reference onto the surviving event, a delete drops it. These
tests are pure — no session, no database — because the decisions are pure, and
the one that matters is not obvious.

The alert-filter rule is the sharp edge. Getting it wrong does not lose a row,
it silently INVERTS a rule: an emptied ``in`` filter matches NOTHING and an
emptied ``not_in`` filter matches EVERYTHING. That truth table is asserted
against ``alerting_matching`` itself rather than restated here as a comment, so
the policy stays pinned to the matcher it reasons about.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from tripl.alerting_matching import filter_matches_anomaly
from tripl.core.event_references import (
    drop_preserving_order,
    plan_alert_filter_change,
    replace_preserving_order,
)
from tripl.models.alert_rule_filter import AlertRuleFilter


@dataclass
class _Candidate:
    """The subset of AlertMatchCandidate that ``filter_matches_anomaly`` reads."""

    event_id: uuid.UUID | None
    event_type_id: uuid.UUID | None = None
    direction: str = "spike"
    id: uuid.UUID = uuid.UUID(int=1)
    scan_config_id: uuid.UUID | None = None
    scope_type: str = "event"
    scope_ref: str = ""
    bucket: datetime = datetime(2026, 1, 1, tzinfo=UTC)
    actual_count: float = 1.0
    expected_count: float = 1.0


def _filter(operator: str, values: list[str]) -> AlertRuleFilter:
    return AlertRuleFilter(
        id=uuid.uuid4(), rule_id=uuid.uuid4(), field="event", operator=operator, values=values
    )


# --------------------------------------------------------------------------
# The list mechanics
# --------------------------------------------------------------------------


def test_replace_preserves_order_and_collapses_a_duplicate() -> None:
    # Both events already listed: the survivor must appear once, in place.
    assert replace_preserving_order(["a", "b", "c"], "a", "b") == ["b", "c"]


def test_drop_preserves_order() -> None:
    assert drop_preserving_order(["a", "b", "c"], {"b"}) == ["a", "c"]


def test_the_helpers_return_a_new_list_rather_than_mutating() -> None:
    """These columns are plain JSON and no MutableList is mapped anywhere.

    An in-place edit leaves the instance unflagged, is never flushed, and every
    assertion against the in-memory object still passes — a test that cannot
    fail, which is how a past bug lasted as long as it did.
    """
    original = ["a", "b"]
    assert replace_preserving_order(original, "a", "z") is not original
    assert drop_preserving_order(original, {"a"}) is not original
    assert original == ["a", "b"]


# --------------------------------------------------------------------------
# The truth table the policy rests on, asserted against the real matcher
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("operator", "expected"),
    [("in", False), ("eq", False), ("not_in", True), ("ne", True)],
)
def test_an_empty_filter_means_opposite_things_per_operator(operator: str, expected: bool) -> None:
    """Why an emptied filter cannot simply be left in place.

    An empty inclusive filter silences its rule; an empty exclusive one opens it
    to everything. Neither is what the author wrote, and neither is visible.
    """
    candidate = _Candidate(event_id=uuid.uuid4())
    assert filter_matches_anomaly(_filter(operator, []), candidate) is expected


def test_a_null_event_id_satisfies_every_event_filter() -> None:
    """The reason a deleted event's anomalies must be DELETED, not left NULLed.

    ``metric_anomalies.event_id`` is ON DELETE SET NULL, so the rows outlive the
    event — and a NULL actual value short-circuits to True here, so the orphan
    passes even a filter written specifically to exclude it. Archiving an event
    suppresses its alerts; deleting one would otherwise un-suppress them.
    """
    orphan = _Candidate(event_id=None)
    excluded = str(uuid.uuid4())
    assert filter_matches_anomaly(_filter("not_in", [excluded]), orphan) is True
    assert filter_matches_anomaly(_filter("in", [excluded]), orphan) is True


# --------------------------------------------------------------------------
# The policy itself
# --------------------------------------------------------------------------


def test_a_filter_that_names_none_of_the_dead_events_is_left_alone() -> None:
    assert plan_alert_filter_change(operator="in", values=["a", "b"], removed={"z"}) is None


def test_a_surviving_filter_keeps_its_remaining_ids_in_order() -> None:
    change = plan_alert_filter_change(operator="in", values=["a", "b", "c"], removed={"b"})
    assert change is not None
    assert change.remaining == ["a", "c"]
    assert change.delete_filter is False
    assert change.disable_rule is False


def test_an_emptied_inclusive_filter_takes_its_rule_out_of_service() -> None:
    """Deleting the row alone would WIDEN the rule to everything its destination watches."""
    change = plan_alert_filter_change(operator="in", values=["a"], removed={"a"})
    assert change is not None
    assert change.delete_filter is True
    assert change.disable_rule is True


def test_an_emptied_exclusive_filter_leaves_its_rule_running() -> None:
    """'Exclude these three' genuinely degrades to 'exclude nothing' once they are gone."""
    change = plan_alert_filter_change(operator="not_in", values=["a"], removed={"a"})
    assert change is not None
    assert change.delete_filter is True
    assert change.disable_rule is False


@pytest.mark.parametrize("operator", ["in", "eq", "not_in", "ne"])
def test_an_emptied_filter_never_reports_an_empty_list_to_store(operator: str) -> None:
    """An empty ``values`` must never be persisted.

    ``AlertRuleFilterResponse`` inherits the "must have at least one value"
    validator, so one stored empty list 500s the destinations endpoint for the
    whole project — including the screen an operator would use to repair it.
    """
    change = plan_alert_filter_change(operator=operator, values=["a"], removed={"a"})
    assert change is not None
    assert change.delete_filter is True, f"{operator} must delete the row, not store []"
