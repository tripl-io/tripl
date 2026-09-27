"""The pure event health scorer (F15, #268): no database, facts in, score out."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from tripl.services import health_weights as hw
from tripl.services.health_score import EventHealthFacts, grade_for, score_event

_NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def _perfect(**overrides: object) -> EventHealthFacts:
    facts = EventHealthFacts(
        event_id=uuid.UUID(int=1),
        event_type_id=uuid.UUID(int=2),
        name="checkout_completed",
        status="live",
        last_seen_at=_NOW - timedelta(days=1),
        has_description=True,
        has_owner=True,
        contract_total=4,
        contract_violated=0,
        covered=True,
        detection_enabled=True,
        open_signals=0,
        freshness=(("checkout events", "fresh", 600),),
    )
    return replace(facts, **overrides)  # type: ignore[arg-type]


def _component(health: object, key: str) -> dict[str, object]:
    components = health.components  # type: ignore[attr-defined]
    return next(c.model_dump() for c in components if c.key == key)


def test_weights_sum_to_100_and_cover_every_component() -> None:
    assert sum(hw.COMPONENT_WEIGHTS.values()) == 100
    assert set(hw.COMPONENT_WEIGHTS) == set(hw.COMPONENT_ORDER) == set(hw.COMPONENT_LABELS)
    assert hw.COMPONENT_WEIGHTS["implemented_seen"] == hw.WEIGHT_IMPLEMENTED_SEEN == 25


def test_perfect_event_scores_100_with_nothing_excluded() -> None:
    health = score_event(_perfect(), _NOW)
    assert health.score == 100
    assert health.grade == "healthy"
    assert health.excluded == []
    assert health.renormalized is False
    assert health.top_issue is None
    assert [c.key for c in health.components] == list(hw.COMPONENT_ORDER)
    assert [c.weight for c in health.components] == [25, 20, 15, 15, 10, 15]


def test_scoring_is_deterministic() -> None:
    facts = _perfect(contract_violated=1, contract_violations=(("amount", "range_violation"),))
    first = score_event(facts, _NOW)
    second = score_event(facts, _NOW)
    assert first == second


@pytest.mark.parametrize(
    ("overrides", "key"),
    [
        ({"last_seen_at": _NOW - timedelta(days=12)}, "implemented_seen"),
        ({"last_seen_at": None}, "implemented_seen"),
        (
            {"contract_violated": 2, "contract_violations": (("a", "enum_violation"),) * 2},
            "contract",
        ),
        ({"schema_drifts": 1}, "drifts"),
        ({"value_drifts": 2}, "drifts"),
        ({"distribution_drifts": 1}, "drifts"),
        ({"open_signals": 1}, "signals"),
        ({"freshness": (("checkout events", "late", 7 * 3600),)}, "freshness"),
        ({"has_description": False}, "documentation"),
        ({"has_owner": False}, "documentation"),
    ],
)
def test_each_component_moves_the_score(overrides: dict[str, object], key: str) -> None:
    health = score_event(_perfect(**overrides), _NOW)
    assert health.score < 100
    component = _component(health, key)
    assert component["applies"] is True
    assert isinstance(component["value"], float)
    assert component["value"] < 1.0
    # The component that lost points names the top issue.
    assert health.top_issue == component["detail"]


def test_implemented_seen_bands() -> None:
    recent = score_event(_perfect(last_seen_at=_NOW - timedelta(days=3)), _NOW)
    stale = score_event(_perfect(last_seen_at=_NOW - timedelta(days=12)), _NOW)
    dead = score_event(_perfect(last_seen_at=_NOW - timedelta(days=45)), _NOW)
    never = score_event(_perfect(last_seen_at=None), _NOW)
    assert _component(recent, "implemented_seen")["value"] == 1.0
    assert _component(recent, "implemented_seen")["detail"] == "Last seen 3d ago"
    assert _component(stale, "implemented_seen")["value"] == 0.5
    assert _component(dead, "implemented_seen")["value"] == 0.0
    assert _component(never, "implemented_seen")["detail"] == "Never seen in data"
    # 25 points lost entirely: 100 - 25 = 75.
    assert never.score == 75


def test_deprecated_reads_lifecycle_findings() -> None:
    clean = score_event(_perfect(status="deprecated"), _NOW)
    overdue = score_event(
        _perfect(status="deprecated", lifecycle_kinds=frozenset({"sunset_overdue"})), _NOW
    )
    silent = score_event(
        _perfect(status="deprecated", lifecycle_kinds=frozenset({"successor_silent"})), _NOW
    )
    assert _component(clean, "implemented_seen")["value"] == 1.0
    assert _component(overdue, "implemented_seen")["value"] == 0.0
    assert _component(overdue, "implemented_seen")["detail"] == "Still receiving data after sunset"
    assert _component(silent, "implemented_seen")["value"] == 0.5


def test_contract_detail_names_the_failing_rules() -> None:
    health = score_event(
        _perfect(
            contract_total=9,
            contract_violated=2,
            contract_violations=(("amount", "range_violation"), ("plan", "enum_violation")),
        ),
        _NOW,
    )
    contract = _component(health, "contract")
    assert contract["detail"] == "2 of 9 contract rules failing: amount (range), plan (enum)"
    assert contract["counts"] == {"violated": 2, "total": 9}
    assert contract["value"] == pytest.approx(1 - 2 / 9, abs=1e-4)


def test_drift_and_signal_penalties_floor_at_zero() -> None:
    health = score_event(_perfect(schema_drifts=3, value_drifts=3, open_signals=5), _NOW)
    assert _component(health, "drifts")["value"] == 0.0
    assert _component(health, "drifts")["detail"] == "3 schema drifts, 3 value drifts"
    assert _component(health, "signals")["value"] == 0.0
    one = score_event(_perfect(open_signals=1), _NOW)
    assert _component(one, "signals")["value"] == 0.5
    assert _component(one, "signals")["detail"] == "1 signal needs a verdict"


def test_freshness_takes_the_worst_source_and_ignores_unknown() -> None:
    health = score_event(
        _perfect(
            freshness=(
                ("a source", "fresh", 60),
                ("checkout events", "late", 7 * 3600),
                ("manual", "unknown", None),
            )
        ),
        _NOW,
    )
    freshness = _component(health, "freshness")
    assert freshness["value"] == 0.5
    assert freshness["detail"] == "Source 'checkout events' is late (lag 7h)"
    only_unknown = score_event(_perfect(freshness=(("manual", "unknown", None),)), _NOW)
    assert "freshness" in only_unknown.excluded
    assert _component(only_unknown, "freshness")["excluded_reason"] == "No scheduled source"


def test_planned_event_scores_on_documentation_only() -> None:
    documented = score_event(
        _perfect(status="draft", last_seen_at=None, covered=False, freshness=()), _NOW
    )
    assert documented.excluded == [
        "implemented_seen",
        "contract",
        "drifts",
        "signals",
        "freshness",
    ]
    assert documented.renormalized is True
    assert documented.score == 100
    doc = _component(documented, "documentation")
    assert doc["effective_weight"] == 100.0
    seen = _component(documented, "implemented_seen")
    assert seen["value"] is None
    assert seen["effective_weight"] is None
    assert seen["excluded_reason"] == "Not implemented yet (status: draft)"

    half = score_event(_perfect(status="in_review", has_owner=False), _NOW)
    assert half.score == 50
    assert half.grade == "warning"
    assert half.top_issue == "No owner"


def test_no_contract_rules_excludes_contract_and_renormalizes() -> None:
    health = score_event(_perfect(contract_total=0, has_description=False), _NOW)
    assert health.excluded == ["contract"]
    assert _component(health, "contract")["excluded_reason"] == (
        "No contract rules on this event type"
    )
    # 80 applicable weight; documentation (15) at 0.5 loses 7.5 -> 72.5/80.
    assert health.score == round(100 * 72.5 / 80)
    assert _component(health, "implemented_seen")["effective_weight"] == pytest.approx(31.2)
    assert _component(health, "documentation")["effective_weight"] == pytest.approx(18.8)


def test_uncovered_and_detection_off_exclusions() -> None:
    uncovered = score_event(_perfect(covered=False, detection_enabled=False, freshness=()), _NOW)
    assert _component(uncovered, "drifts")["excluded_reason"] == "Not covered by any scan"
    assert _component(uncovered, "contract")["excluded_reason"] == "Not covered by any scan"
    assert _component(uncovered, "signals")["excluded_reason"] == (
        "Anomaly detection is off for its scans"
    )
    off = score_event(_perfect(detection_enabled=False), _NOW)
    assert off.excluded == ["signals"]
    assert _component(off, "drifts")["applies"] is True


def test_points_sum_to_the_score() -> None:
    cases = [
        _perfect(),
        _perfect(contract_total=7, contract_violated=3, schema_drifts=1, has_owner=False),
        _perfect(status="deprecated", lifecycle_kinds=frozenset({"successor_silent"})),
        _perfect(contract_total=0, detection_enabled=False, last_seen_at=None),
        _perfect(status="ready_for_dev", has_description=False),
    ]
    for facts in cases:
        health = score_event(facts, _NOW)
        points = sum(c.points or 0.0 for c in health.components)
        assert abs(points - health.score) <= 1.0, (facts, health)
        applicable = [c for c in health.components if c.applies]
        assert sum(c.effective_weight or 0 for c in applicable) == pytest.approx(100, abs=0.5)


def test_score_stays_within_bounds() -> None:
    worst = score_event(
        _perfect(
            last_seen_at=None,
            contract_violated=4,
            schema_drifts=10,
            open_signals=10,
            freshness=(("s", "overdue", 999999),),
            has_description=False,
            has_owner=False,
        ),
        _NOW,
    )
    assert worst.score == 0
    assert worst.grade == "unhealthy"


def test_grade_thresholds() -> None:
    assert grade_for(100) == "healthy"
    assert grade_for(hw.GRADE_HEALTHY_MIN) == "healthy"
    assert grade_for(hw.GRADE_HEALTHY_MIN - 1) == "warning"
    assert grade_for(hw.GRADE_WARNING_MIN) == "warning"
    assert grade_for(hw.GRADE_WARNING_MIN - 1) == "unhealthy"
    assert grade_for(0) == "unhealthy"


def test_top_issue_is_the_largest_loss() -> None:
    # Documentation loses 7.5 points (no owner); signals lose 7.5 x 2 = 15.
    health = score_event(_perfect(has_owner=False, open_signals=2), _NOW)
    assert health.top_issue == "2 signals need a verdict"


def test_uncovered_live_event_with_rules_excludes_contract() -> None:
    health = score_event(_perfect(status="live", covered=False, contract_total=4), _NOW)
    contract = _component(health, "contract")
    assert contract["applies"] is False
    assert contract["excluded_reason"] == "Not covered by any scan"
    assert "contract" in health.excluded
