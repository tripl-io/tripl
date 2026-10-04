"""Batch 4: the rule simulator's cooldown partition (``tripl-0zpq.42``).

``tripl.alerting_matching`` exists so the in-UI replay cannot answer a different
question from the live send path, and its module docstring promises exactly
that. The replay's cooldown key was the one place the promise was false: it
keyed on ``(scope_type, scope_ref)`` while the live clock, ``AlertRuleState``,
is unique on ``(rule_id, scan_config_id, scope_type, scope_ref)`` and
``dispatch._prepare_alert_deliveries`` reads only the running config's states.
A project runs several scan configs (the live iOS project runs three) and a rule
covers the whole project unless deliberately bound to one, so the same event can
be anomalous in two configs on the same day: live sends twice, the replay
reported once, and the what-if under-counted the rule it was asked about.

Everything here is in-memory — the matcher never touches a session — but it uses
real ``AlertRule`` / ``MetricAnomaly`` instances so the attribute names cannot
drift away from the columns the live path reads.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from tripl.alerting_matching import SCOPE_METRIC, simulate_rule_firings
from tripl.models.alert_rule import AlertRule
from tripl.models.alert_rule_state import AlertRuleState
from tripl.models.metric_anomaly import MetricAnomaly

FIRST = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
SECOND = FIRST + timedelta(minutes=30)
THIRD = FIRST + timedelta(minutes=90)


def _rule(**overrides: object) -> AlertRule:
    """An enabled, project-wide rule with every numeric gate wide open.

    ``scan_config_id=None`` is the default in production and is the case that
    diverged: a scan-BOUND rule was never affected, because
    ``rule_matches_anomaly`` already drops every other scan's candidates.
    """
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "destination_id": uuid.uuid4(),
        "scan_config_id": None,
        "name": "Batch 4 matching rule",
        "enabled": True,
        "include_project_total": True,
        "include_event_types": True,
        "include_events": True,
        "include_schema_drifts": False,
        "include_distribution_drifts": False,
        "include_variable_value_drifts": False,
        "include_release_regressions": False,
        "include_metrics": False,
        "notify_on_spike": True,
        "notify_on_drop": True,
        "min_percent_delta": 0.0,
        "min_absolute_delta": 0.0,
        "min_expected_count": 0.0,
        "cooldown_minutes": 60,
        "message_template": None,
        "items_template": None,
        "message_format": "plain",
    }
    defaults.update(overrides)
    rule = AlertRule(**defaults)
    rule.filters = []
    return rule


def _anomaly(
    bucket: datetime,
    *,
    scope_ref: str,
    scan_config_id: uuid.UUID | None,
    scope_type: str = "event",
) -> MetricAnomaly:
    """One anomalous bucket, far enough past every threshold to always match."""
    return MetricAnomaly(
        id=uuid.uuid4(),
        scan_config_id=scan_config_id,
        scope_type=scope_type,
        scope_ref=scope_ref,
        event_id=None,
        event_type_id=None,
        bucket=bucket,
        actual_count=100.0,
        expected_count=10.0,
        stddev=1.0,
        z_score=5.0,
        direction="spike",
    )


def test_the_same_scope_in_two_scans_replays_as_two_firings() -> None:
    """Two scans observing one event are two live clocks, so two firings.

    ``event`` scope_refs are the bare event id (``signals.py`` stamps the entity
    id, not a per-config key), so this pair is one scope_ref under two configs —
    the exact shape that used to collapse.
    """
    rule = _rule(cooldown_minutes=60)
    scan_a = uuid.uuid4()
    scan_b = uuid.uuid4()
    scope_ref = str(uuid.uuid4())

    fired = simulate_rule_firings(
        rule,
        [
            _anomaly(FIRST, scope_ref=scope_ref, scan_config_id=scan_a),
            _anomaly(SECOND, scope_ref=scope_ref, scan_config_id=scan_b),
        ],
    )

    # Live, this is two AlertRuleState rows and two sends. Revert the scan
    # partition in ``simulate_rule_firings`` and the 09:30 row sits 30 minutes
    # inside a 60-minute cooldown it never shared, so this list loses its second
    # entry and the replay under-reports the rule by half.
    assert [(a.scan_config_id, a.bucket) for a in fired] == [
        (scan_a, FIRST),
        (scan_b, SECOND),
    ]


def test_one_scan_repeating_a_scope_is_still_gated_by_the_cooldown() -> None:
    """The partition widened; the clock inside a partition did not go away.

    Without this, the test above would also pass with the cooldown deleted
    outright — which is a different, much worse bug.
    """
    rule = _rule(cooldown_minutes=60)
    scan = uuid.uuid4()
    scope_ref = str(uuid.uuid4())

    fired = simulate_rule_firings(
        rule,
        [
            _anomaly(FIRST, scope_ref=scope_ref, scan_config_id=scan),
            _anomaly(SECOND, scope_ref=scope_ref, scan_config_id=scan),
            _anomaly(THIRD, scope_ref=scope_ref, scan_config_id=scan),
        ],
    )

    # 09:00 fires, 09:30 is swallowed, 10:30 clears the hour.
    assert [a.bucket for a in fired] == [FIRST, THIRD]


def test_a_metric_scope_keeps_one_project_wide_cooldown_clock() -> None:
    """Catalog metrics are project-global and must NOT be split by scan.

    Live dispatch stores NO scan config on a ``metric``-scope state —
    ``AlertRuleState.scan_config_id`` is NULL — and the partial unique index
    ``uq_alert_rule_state_metric_scope`` over that NULL space is what gives the
    series one clock per (rule, scope) for the whole project.
    Dispatch USED TO anchor it on the project's lowest config id instead; that
    anchor is gone, so there is no canonical config left for the replay to
    mirror. The replay therefore keys its cooldown on the SCOPE rather than on
    the candidate's ``scan_config_id``, which says what the partition IS rather
    than what one candidate happens to hold — the off-spec pair below pins
    exactly that.
    """
    rule = _rule(cooldown_minutes=60, include_metrics=True)
    scope_ref = str(uuid.uuid4())

    production_shape = simulate_rule_firings(
        rule,
        [
            _anomaly(FIRST, scope_ref=scope_ref, scan_config_id=None, scope_type=SCOPE_METRIC),
            _anomaly(SECOND, scope_ref=scope_ref, scan_config_id=None, scope_type=SCOPE_METRIC),
        ],
    )
    assert [a.bucket for a in production_shape] == [FIRST]

    # Off-spec on purpose: a metric candidate carries NULL today, so this pair
    # cannot arrive from the loader. It pins WHY the partition is derived from
    # the scope rather than read off ``anomaly.scan_config_id`` — a future
    # change to how metric state is partitioned must not be able to silently
    # split this clock. Rewrite the branch as a bare ``anomaly.scan_config_id``
    # and this goes to two.
    if_a_scan_ever_leaks_in = simulate_rule_firings(
        rule,
        [
            _anomaly(
                FIRST, scope_ref=scope_ref, scan_config_id=uuid.uuid4(), scope_type=SCOPE_METRIC
            ),
            _anomaly(
                SECOND, scope_ref=scope_ref, scan_config_id=uuid.uuid4(), scope_type=SCOPE_METRIC
            ),
        ],
    )
    assert [a.bucket for a in if_a_scan_ever_leaks_in] == [FIRST]


def test_the_live_partition_the_replay_mirrors_still_contains_the_scan() -> None:
    """The replay key is only correct while the live key has these four columns.

    ``simulate_rule_firings`` says it mirrors ``uq_alert_rule_state_scope``. If
    that constraint is ever re-cut — dropping ``scan_config_id``, or adding a
    fifth column — the docstring becomes a lie and the replay silently starts
    answering a question production no longer asks, with nothing else in the
    suite to notice. Fail here and go re-decide the key, deliberately.
    """
    constraint = next(
        c for c in AlertRuleState.__table__.constraints if c.name == "uq_alert_rule_state_scope"
    )
    assert {column.name for column in constraint.columns} == {
        "rule_id",
        "scan_config_id",
        "scope_type",
        "scope_ref",
    }
