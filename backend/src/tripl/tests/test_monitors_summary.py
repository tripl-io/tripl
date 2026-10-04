from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from httpx import AsyncClient

from tripl.services.monitoring_utils import (
    RECENT_SIGNAL_WINDOW,
    classify_signal_state,
    recent_signal_window_from_hours,
    scan_interval_to_timedelta,
    summarize_monitor_states,
)

_HorizonCase = tuple[timedelta | None, timedelta | None, datetime, datetime, datetime]


def _horizon_grid() -> list[_HorizonCase]:
    """``(interval, recent_window, anomaly_bucket, metric_head, now)`` over every boundary.

    Every grid the product supports, every configured open-signal window, and
    ages straddling each boundary either branch of ``classify_signal_state`` can
    have: the head itself, the 24h window, three days (the daily floor), a week
    and three weeks (the weekly floor).
    """
    head = datetime(2026, 8, 6, tzinfo=UTC)
    now = head + timedelta(minutes=30)
    offset_hours = (-1, 0, 1, 5, 23, 24, 25, 47, 48, 71, 72, 73, 167, 168, 169, 503, 504, 505)
    return [
        (
            scan_interval_to_timedelta(interval),
            recent_signal_window_from_hours(window_hours),
            head - timedelta(hours=hours),
            head,
            now,
        )
        for interval in (None, "15m", "1h", "6h", "1d", "1w")
        for window_hours in (None, 6, 24, 72)
        for hours in offset_hours
    ]


class TestFreshnessHorizon:
    """The window an open signal is measured against, and the copies of it."""

    @pytest.mark.parametrize(
        ("interval", "settling_minutes"),
        [
            (iv, minutes)
            for iv in ("15m", "1h", "6h", "1d", "1w")
            # Every allowance short of a full day. 1440 is covered separately
            # below: it is the one value this floor does NOT rescue.
            for minutes in (0, 1, 59, 120, 121, 360)
        ],
    )
    def test_a_live_scope_signal_stays_open_on_every_grid(
        self,
        interval: str,
        settling_minutes: int,
    ) -> None:
        """The newest anomaly the detector may emit must classify as open.

        Ingestion settling withholds the newest ``ceil(delay / interval)``
        buckets from emission, so a scope that is still emitting carries its
        newest possible anomaly that far behind its metric head. Measured
        against a bare 24h window a daily grid put it a full bucket outside and
        a weekly grid six days outside, so every signal on such a scan read as
        closed however large it was.
        """
        from tripl.core.analyzers.anomaly_detector import settling_buckets_for

        delta = scan_interval_to_timedelta(interval)
        assert delta is not None
        withheld = settling_buckets_for(delta, timedelta(minutes=settling_minutes))
        # A head on the grid, and "now" a fraction of a bucket past it — the
        # ordinary case, a scan that has just collected.
        head = datetime(2026, 8, 6, tzinfo=UTC)
        now = head + delta // 2
        newest_emittable = head - withheld * delta

        assert (
            classify_signal_state(
                anomaly_bucket=newest_emittable,
                latest_metric_bucket=head,
                now=now,
                interval=delta,
            )
            is not None
        )

    @pytest.mark.parametrize("interval", ["15m", "1h", "6h"])
    def test_a_full_day_of_settling_still_closes_a_sub_daily_signal(
        self,
        interval: str,
    ) -> None:
        """The residual this floor does not reach — and why settings now refuse it.

        ``anomaly_ingestion_settling_minutes`` accepts up to 1440. At exactly
        that, the newest emittable anomaly on any sub-daily grid is a full 24
        hours behind the head, which the 24h window cannot contain — the same
        shape of bug as the daily grid, but driven by the allowance rather than
        by the bucket size, so flooring on the interval cannot see it. Fixing it
        *here* means the freshness horizon knowing the allowance, which is a
        per-project value the display path does not load.

        The API instead REFUSES a settling allowance that reaches the open-signal window
        (``schemas.project_anomaly_settings.settling_window_conflict``, enforced
        on the merged settings in ``project_anomaly_settings_service``), since
        asking to score a day late and to close signals after a day is
        incoherent. So this case is no longer reachable by configuring a project
        — but ``classify_signal_state`` is unchanged and a row written before
        that guard existed still holds the pair, so the behaviour stays pinned
        here: this is exactly the display blackout the guard exists to prevent.
        ``test_project_anomaly_settings`` pins the refusal itself.
        """
        from tripl.core.analyzers.anomaly_detector import settling_buckets_for

        delta = scan_interval_to_timedelta(interval)
        assert delta is not None
        withheld = settling_buckets_for(delta, timedelta(minutes=1440))
        head = datetime(2026, 8, 6, tzinfo=UTC)

        assert (
            classify_signal_state(
                anomaly_bucket=head - withheld * delta,
                latest_metric_bucket=head,
                now=head + delta // 2,
                interval=delta,
            )
            is None
        )

    def test_the_horizon_is_unchanged_on_grids_finer_than_the_window(self) -> None:
        """Inert where it should be: the floor only bites past 8h buckets."""
        now = datetime(2026, 8, 6, 12, tzinfo=UTC)
        head = datetime(2026, 8, 6, 12, tzinfo=UTC)
        # 25 hours old on an hourly grid: outside the 24h window before and after.
        stale = head - timedelta(hours=25)
        assert (
            classify_signal_state(
                anomaly_bucket=stale,
                latest_metric_bucket=head,
                now=now,
                interval=timedelta(hours=1),
            )
            is None
        )

    def test_a_stopped_scan_still_ages_out(self) -> None:
        """The cap half of the constant survives: a dead daily scan closes."""
        now = datetime(2026, 8, 6, 12, tzinfo=UTC)
        # Head and anomaly both far in the past — the scan stopped collecting.
        head = now - timedelta(days=30)
        assert (
            classify_signal_state(
                anomaly_bucket=head,
                latest_metric_bucket=head,
                now=now,
                interval=timedelta(days=1),
            )
            is None
        )

    def test_the_worker_uses_the_shared_objects_rather_than_copies(self) -> None:
        """The worker's signal helpers must BE the display ones, not equal them.

        ``signals.py`` used to keep its own copy of the constant, the interval
        table, the two predicates and the classifier, on the stated grounds that
        the worker must not import the request-path services layer. That reason
        was false (see ``test_monitoring_utils_is_a_pure_leaf`` below) and the
        copies drifted twice inside one PR — one fix widened only the
        display copy's freshness horizon, another only its recent branch —
        each time making the UI show a signal open while the alerting path acted
        as though it were closed.

        Equality was what the previous version of this test asserted, and
        equality is exactly what a fresh copy also satisfies on the day it is
        written. Identity is the property that cannot be re-broken by copying:
        re-introducing a local definition fails here immediately.
        """
        from tripl.services import monitoring_utils
        from tripl.worker.tasks.metrics import _helpers as worker_helpers
        from tripl.worker.tasks.metrics import signals as worker_signals

        assert worker_signals.classify_signal_state is monitoring_utils.classify_signal_state, (
            "the worker re-declared the classifier instead of importing it"
        )
        assert (
            worker_signals.scan_interval_to_timedelta is monitoring_utils.scan_interval_to_timedelta
        )
        # ``LATEST_SCAN_STALE_INTERVALS`` is deliberately NOT re-imported: the
        # only thing in the worker that ever read it was the duplicated horizon
        # function, so a live reference here would be a re-export kept alive by
        # its own test rather than by a caller.
        assert not hasattr(worker_signals, "LATEST_SCAN_STALE_INTERVALS")
        # The third copy of the 24h window: nothing pinned this one at all.
        assert worker_helpers.RECENT_SIGNAL_WINDOW is monitoring_utils.RECENT_SIGNAL_WINDOW

    def test_monitoring_utils_is_a_pure_leaf(self) -> None:
        """The precondition that makes the import above legal, enforced not asserted.

        ``signals.py`` justified its copies with "the worker must not import the
        async request-path services layer". ``monitoring_utils`` imports no
        ``tripl`` module at all — no models, no SQLAlchemy, no FastAPI — so
        importing it from the worker costs an empty ``services/__init__.py`` and
        nothing else, and the worker already imports ``tripl.services`` in a
        dozen other task modules.

        Read with ``ast`` rather than by importing, the way
        ``test_core_does_not_import_worker`` reads its rule: this must fail on
        the line that WOULD create the coupling, not only once something at
        import time happens to be slow or circular.
        """
        import ast
        from pathlib import Path

        from tripl.services import monitoring_utils

        assert monitoring_utils.__file__ is not None
        source = Path(monitoring_utils.__file__).read_text(encoding="utf-8")
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                # A relative import inside services/ is a services import.
                imported.add(node.module or "tripl.services")

        offenders = sorted(
            name for name in imported if name == "tripl" or name.startswith("tripl.")
        )
        assert offenders == [], (
            f"monitoring_utils must stay importable from the worker; it now pulls in {offenders}"
        )

    def test_the_alert_paths_extra_argument_is_inert_for_the_display_path(self) -> None:
        """``emission_lag`` is what the worker's copy carried that the page's did not.

        Folding the two copies into one function is only safe if the parameter
        that came with the worker's version changes nothing when it is absent.
        Asserted over every grid, every configured window and every age
        straddling a boundary either branch can have — the exhaustive table the
        two-implementation parity test used to run, repointed at the surviving
        function now that there is only one.

        This is the proof that the merge did not move the API: a default of zero
        makes the latest-scan test ``anomaly_bucket >= latest_metric_bucket``
        exactly as it read before.
        """
        for interval, window, bucket, head, now in _horizon_grid():
            assert classify_signal_state(
                anomaly_bucket=bucket,
                latest_metric_bucket=head,
                now=now,
                interval=interval,
                recent_window=window,
            ) == classify_signal_state(
                anomaly_bucket=bucket,
                latest_metric_bucket=head,
                now=now,
                interval=interval,
                recent_window=window,
                emission_lag=timedelta(0),
            ), f"emission_lag=0 moved the answer at interval={interval}, bucket={bucket}"

    def test_the_emission_lag_is_exactly_a_shift_of_the_metric_head(self) -> None:
        """And nothing else — it must not touch the wall-clock freshness tests.

        The alert path measures against the SETTLED head because detection
        withholds the newest ``settling_buckets`` from emission, so a still-alive
        scope can never carry an anomaly at or after its raw head. That is a
        statement about which bucket counts as "the head", not about how old a
        signal may be: a lag that also relaxed the recency check would keep a
        dead scan's final anomaly firing past the horizon the cap exists to
        enforce.

        Pinned as an equivalence rather than a table of expected strings so it
        cannot be satisfied by copying whatever the implementation happens to
        return.
        """
        for interval, window, bucket, head, now in _horizon_grid():
            for lag in (timedelta(hours=1), timedelta(hours=2), timedelta(days=1)):
                assert classify_signal_state(
                    anomaly_bucket=bucket,
                    latest_metric_bucket=head,
                    now=now,
                    interval=interval,
                    recent_window=window,
                    emission_lag=lag,
                ) == classify_signal_state(
                    anomaly_bucket=bucket,
                    latest_metric_bucket=head - lag,
                    now=now,
                    interval=interval,
                    recent_window=window,
                ), f"emission_lag={lag} is not a head shift at interval={interval}, {bucket=}"


def _state(*, is_active: bool, last_anomaly_bucket=None, last_notified_at=None) -> SimpleNamespace:
    return SimpleNamespace(
        is_active=is_active,
        last_anomaly_bucket=last_anomaly_bucket,
        last_notified_at=last_notified_at,
    )


class TestSummarizeMonitorStates:
    def test_no_states_is_healthy(self) -> None:
        now = datetime.now(UTC)
        rollup = summarize_monitor_states([], now=now)
        assert rollup.status == "healthy"
        assert rollup.active_scope_count == 0
        assert rollup.firing_scope_count == 0
        assert rollup.last_anomaly_at is None

    def test_active_recent_anomaly_is_firing(self) -> None:
        now = datetime.now(UTC)
        states = [_state(is_active=True, last_anomaly_bucket=now - timedelta(hours=1))]
        rollup = summarize_monitor_states(states, now=now)
        assert rollup.status == "firing"
        assert rollup.active_scope_count == 1
        assert rollup.firing_scope_count == 1

    def test_active_stale_anomaly_is_warning(self) -> None:
        now = datetime.now(UTC)
        states = [_state(is_active=True, last_anomaly_bucket=now - timedelta(hours=48))]
        rollup = summarize_monitor_states(states, now=now)
        assert rollup.status == "warning"
        assert rollup.active_scope_count == 1
        assert rollup.firing_scope_count == 0

    def test_active_without_anomaly_is_warning(self) -> None:
        now = datetime.now(UTC)
        states = [_state(is_active=True, last_anomaly_bucket=None)]
        rollup = summarize_monitor_states(states, now=now)
        assert rollup.status == "warning"

    def test_inactive_states_are_healthy(self) -> None:
        now = datetime.now(UTC)
        states = [_state(is_active=False, last_anomaly_bucket=now - timedelta(hours=1))]
        rollup = summarize_monitor_states(states, now=now)
        assert rollup.status == "healthy"
        assert rollup.active_scope_count == 0

    def test_mixed_scopes_count_only_active_and_report_latest(self) -> None:
        now = datetime.now(UTC)
        states = [
            _state(is_active=True, last_anomaly_bucket=now - timedelta(hours=1)),
            _state(is_active=True, last_anomaly_bucket=now - timedelta(hours=72)),
            _state(is_active=False, last_anomaly_bucket=now - timedelta(minutes=10)),
        ]
        rollup = summarize_monitor_states(states, now=now)
        assert rollup.status == "firing"
        assert rollup.active_scope_count == 2
        assert rollup.firing_scope_count == 1
        # latest anomaly across ALL states (incl. inactive) is the 10-minutes-ago one
        assert rollup.last_anomaly_at == now - timedelta(minutes=10)


class TestClassifySignalState:
    def test_none_latest_metric_bucket_is_closed(self) -> None:
        # No stored metric values -> nothing to keep open (previously "latest_scan").
        now = datetime.now(UTC)
        assert (
            classify_signal_state(
                anomaly_bucket=now,
                latest_metric_bucket=None,
                now=now,
            )
            is None
        )

    def test_fresh_latest_scan_is_open(self) -> None:
        now = datetime.now(UTC)
        bucket = now - timedelta(hours=1)
        assert (
            classify_signal_state(
                anomaly_bucket=bucket,
                latest_metric_bucket=bucket,
                now=now,
            )
            == "latest_scan"
        )

    def test_stale_latest_scan_falls_through_to_closed(self) -> None:
        # Anomaly still tops max(bucket) but is 2 days old with no interval hint,
        # so it is stale against the 24h default horizon and no longer "latest_scan".
        now = datetime.now(UTC)
        bucket = now - timedelta(hours=48)
        assert (
            classify_signal_state(
                anomaly_bucket=bucket,
                latest_metric_bucket=bucket,
                now=now,
            )
            is None
        )

    def test_interval_extends_latest_scan_horizon(self) -> None:
        # A daily scan's 48h-old final anomaly is still fresh: horizon = 3 * 1d.
        now = datetime.now(UTC)
        bucket = now - timedelta(hours=48)
        assert (
            classify_signal_state(
                anomaly_bucket=bucket,
                latest_metric_bucket=bucket,
                now=now,
                interval=scan_interval_to_timedelta("1d"),
            )
            == "latest_scan"
        )

    def test_recent_branch_preserved(self) -> None:
        # Anomaly older than the latest scan but inside the 24h recent window.
        now = datetime.now(UTC)
        assert (
            classify_signal_state(
                anomaly_bucket=now - timedelta(hours=2),
                latest_metric_bucket=now - timedelta(hours=1),
                now=now,
            )
            == "recent"
        )

    def test_old_anomaly_below_latest_is_closed(self) -> None:
        now = datetime.now(UTC)
        assert (
            classify_signal_state(
                anomaly_bucket=now - timedelta(hours=48),
                latest_metric_bucket=now - timedelta(hours=1),
                now=now,
            )
            is None
        )

    def test_tz_naive_buckets_do_not_raise(self) -> None:
        now = datetime.now(UTC).replace(tzinfo=None)
        bucket = now - timedelta(hours=1)
        assert (
            classify_signal_state(
                anomaly_bucket=bucket,
                latest_metric_bucket=bucket,
                now=now,
            )
            == "latest_scan"
        )

    def test_scan_interval_to_timedelta_unknown_is_none(self) -> None:
        assert scan_interval_to_timedelta(None) is None
        assert scan_interval_to_timedelta("nope") is None
        assert scan_interval_to_timedelta("6h") == timedelta(hours=6)


class TestOngoingOutageStaysOpen:
    """An outage anchor is re-checked against the CURRENT series, not its own age.

    ``_collapse_outage_runs`` gives an outage exactly one anchor row, announced
    at onset and never re-emitted, so a scope that has been silent for five days
    carries a five-day-old anomaly and nothing newer. Judged on bucket age alone
    that reads as closed, which is how a still-running incident disappeared from
    every signal surface. The re-check asks the persisted row instead: the anchor
    says the scope was at zero, ``anomaly_bucket >= latest_metric_bucket`` says
    it has emitted nothing since, and a fresh ``scan_latest_bucket`` says the
    scan is still collecting — so the silence is real and the signal is open.
    """

    def test_ongoing_outage_on_a_live_scan_stays_open(self) -> None:
        now = datetime.now(UTC)
        anchor = now - timedelta(days=5)
        assert (
            classify_signal_state(
                anomaly_bucket=anchor,
                # The scope's own series stops at the anchor: silent ever since.
                latest_metric_bucket=anchor,
                now=now,
                anomaly_actual_count=0.0,
                # Other scopes on the same scan are still collecting.
                scan_latest_bucket=now - timedelta(hours=1),
            )
            == "latest_scan"
        )

    def test_stopped_scan_still_closes_its_final_outage(self) -> None:
        # The cap the latest-scan branch exists for: a collector that stopped
        # must not pin its last anomaly red forever, outage-shaped or not.
        now = datetime.now(UTC)
        anchor = now - timedelta(days=5)
        assert (
            classify_signal_state(
                anomaly_bucket=anchor,
                latest_metric_bucket=anchor,
                now=now,
                anomaly_actual_count=0.0,
                scan_latest_bucket=now - timedelta(days=5),
            )
            is None
        )

    def test_a_stale_spike_is_not_reopened(self) -> None:
        # Only SILENCE is re-checkable from the anchor row. A burned-out spike
        # ages out exactly as before, whatever the scan is doing.
        now = datetime.now(UTC)
        anchor = now - timedelta(days=5)
        assert (
            classify_signal_state(
                anomaly_bucket=anchor,
                latest_metric_bucket=anchor,
                now=now,
                anomaly_actual_count=99.0,
                scan_latest_bucket=now - timedelta(hours=1),
            )
            is None
        )

    def test_a_recovered_scope_closes(self) -> None:
        # The scope emitted again after the anchor, so the series itself says the
        # outage is over: the latest-scan branch is not even entered.
        now = datetime.now(UTC)
        assert (
            classify_signal_state(
                anomaly_bucket=now - timedelta(days=5),
                latest_metric_bucket=now - timedelta(hours=1),
                now=now,
                anomaly_actual_count=0.0,
                scan_latest_bucket=now - timedelta(hours=1),
            )
            is None
        )

    def test_omitting_the_probe_reproduces_the_old_answer(self) -> None:
        # Callers that cannot supply the pair (catalog metrics have no scan to
        # ask) must land exactly where they did before.
        now = datetime.now(UTC)
        anchor = now - timedelta(days=5)
        assert (
            classify_signal_state(
                anomaly_bucket=anchor,
                latest_metric_bucket=anchor,
                now=now,
            )
            is None
        )
        assert (
            classify_signal_state(
                anomaly_bucket=anchor,
                latest_metric_bucket=anchor,
                now=now,
                anomaly_actual_count=0.0,
            )
            is None
        )


class TestConfiguredRecentWindow:
    """The per-project ``recent_signal_window_hours`` override.

    The compatibility contract: omitting ``recent_window`` must reproduce the
    24h default exactly, and passing it must move BOTH freshness branches (the
    latest-scan horizon and the recent-window fallback).
    """

    def test_default_window_is_24_hours(self) -> None:
        assert timedelta(hours=24) == RECENT_SIGNAL_WINDOW

    def test_recent_signal_window_from_hours(self) -> None:
        assert recent_signal_window_from_hours(None) is None
        assert recent_signal_window_from_hours(6) == timedelta(hours=6)
        assert recent_signal_window_from_hours(24) == RECENT_SIGNAL_WINDOW

    def test_explicit_24h_matches_the_default_path(self) -> None:
        # Passing the default value must not change any outcome.
        now = datetime.now(UTC)
        for offset in (timedelta(hours=1), timedelta(hours=23), timedelta(hours=48)):
            bucket = now - offset
            assert classify_signal_state(
                anomaly_bucket=bucket,
                latest_metric_bucket=bucket,
                now=now,
                recent_window=timedelta(hours=24),
            ) == classify_signal_state(
                anomaly_bucket=bucket,
                latest_metric_bucket=bucket,
                now=now,
            )

    def test_shorter_window_closes_a_bucket_open_under_24h(self) -> None:
        # A 6h-old anomaly on the latest scan: open by default, closed once the
        # project narrows the window to 4h.
        now = datetime.now(UTC)
        bucket = now - timedelta(hours=6)
        assert (
            classify_signal_state(
                anomaly_bucket=bucket,
                latest_metric_bucket=bucket,
                now=now,
            )
            == "latest_scan"
        )
        assert (
            classify_signal_state(
                anomaly_bucket=bucket,
                latest_metric_bucket=bucket,
                now=now,
                recent_window=timedelta(hours=4),
            )
            is None
        )

    def test_shorter_window_closes_the_recent_branch(self) -> None:
        # Anomaly below the latest bucket: "recent" by default, closed under a
        # window shorter than its age.
        now = datetime.now(UTC)
        assert (
            classify_signal_state(
                anomaly_bucket=now - timedelta(hours=6),
                latest_metric_bucket=now - timedelta(hours=1),
                now=now,
            )
            == "recent"
        )
        assert (
            classify_signal_state(
                anomaly_bucket=now - timedelta(hours=6),
                latest_metric_bucket=now - timedelta(hours=1),
                now=now,
                recent_window=timedelta(hours=4),
            )
            is None
        )

    def test_longer_window_keeps_a_bucket_open_past_24h(self) -> None:
        now = datetime.now(UTC)
        bucket = now - timedelta(hours=48)
        assert (
            classify_signal_state(
                anomaly_bucket=bucket,
                latest_metric_bucket=bucket,
                now=now,
            )
            is None
        )
        assert (
            classify_signal_state(
                anomaly_bucket=bucket,
                latest_metric_bucket=bucket,
                now=now,
                recent_window=timedelta(hours=72),
            )
            == "latest_scan"
        )

    def test_scan_interval_floor_still_wins_over_a_short_window(self) -> None:
        # The latest-scan horizon is max(window, 3 x interval): a stopped daily
        # scan must not be closed early just because the window was narrowed.
        now = datetime.now(UTC)
        bucket = now - timedelta(hours=48)
        assert (
            classify_signal_state(
                anomaly_bucket=bucket,
                latest_metric_bucket=bucket,
                now=now,
                interval=scan_interval_to_timedelta("1d"),
                recent_window=timedelta(hours=4),
            )
            == "latest_scan"
        )

    def test_summarize_monitor_states_stays_on_the_fixed_window(self) -> None:
        # Monitors summarize ALERT state, which dispatch classifies against the
        # fixed window, so the per-project open-signal setting must not move it.
        now = datetime.now(UTC)
        states = [_state(is_active=True, last_anomaly_bucket=now - timedelta(hours=6))]
        assert summarize_monitor_states(states, now=now).status == "firing"


async def _make_project(client: AsyncClient, slug: str) -> None:
    resp = await client.post(
        "/api/v1/projects",
        json={"name": slug, "slug": slug, "description": ""},
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_monitors_summary_empty_project(client: AsyncClient) -> None:
    await _make_project(client, "monitors-empty")
    resp = await client.get("/api/v1/projects/monitors-empty/monitors-summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "monitors": [],
        "firing_count": 0,
        "warning_count": 0,
        "healthy_count": 0,
        "total": 0,
        # A project with nothing in it feeds neither drift scope, so both read
        # False here. What the flags MEAN, and every shape that flips them, is
        # pinned in test_alerting_scope_readiness.py.
        "scope_readiness": {"variable_value_drift": False, "distribution_drift": False},
    }


@pytest.mark.asyncio
async def test_monitors_summary_lists_rule_as_healthy(client: AsyncClient) -> None:
    await _make_project(client, "monitors-demo")
    dest_resp = await client.post(
        "/api/v1/projects/monitors-demo/alert-destinations",
        json={
            "type": "slack",
            "name": "Main Slack",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T000/B000/XXX",
        },
    )
    assert dest_resp.status_code == 201
    destination_id = dest_resp.json()["id"]

    rule_resp = await client.post(
        f"/api/v1/projects/monitors-demo/alert-destinations/{destination_id}/rules",
        json={"name": "Spike watch", "enabled": True},
    )
    assert rule_resp.status_code == 201

    resp = await client.get("/api/v1/projects/monitors-demo/monitors-summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["healthy_count"] == 1
    assert body["firing_count"] == 0
    monitor = body["monitors"][0]
    assert monitor["rule_name"] == "Spike watch"
    assert monitor["destination_type"] == "slack"
    assert monitor["destination_name"] == "Main Slack"
    assert monitor["status"] == "healthy"
    assert monitor["active_scope_count"] == 0
    assert monitor["notify_on_spike"] is True
