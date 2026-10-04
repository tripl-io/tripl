"""The watch diff as arithmetic: no network, no loop, no wall clock.

The acceptance criterion of the watch work - "a running replay shows live chunk
progress" - is provable here, which is why this file exists separately from the
end-to-end one. Every test below pins a decision that could plausibly have been
made the other way.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from tests.conftest import (
    DISPATCHER_MODE,
    make_delivery,
    make_job,
    make_replay_job,
    make_signal,
)
from tripl_cli.watch.diff import DiffResult, diff_tick
from tripl_cli.watch.model import (
    WATCH_STALL_SECONDS,
    DeliveryRow,
    JobRow,
    JobStream,
    JobTarget,
    SignalKey,
    SignalRow,
    SignalStream,
    StallEntry,
    StreamSnapshot,
    TickSnapshot,
    WatchEvent,
    delivery_row_of,
    job_row_of,
    signal_row_of,
)

MOMENT = datetime(2026, 7, 31, 19, 10, 41, tzinfo=UTC)
TARGET = ("prod", "scan-1")
CONFIGS: Mapping[str, Mapping[str, str]] = {"prod": {"scan-1": "nightly replay"}}


def jobs_snapshot(rows: Sequence[dict[str, Any]], *, window_full: bool = False) -> TickSnapshot:
    parsed: dict[str, JobRow] = {}
    for raw in rows:
        row = job_row_of(raw)
        assert row is not None
        parsed[row.id] = row
    stream: JobStream = StreamSnapshot(rows=parsed, window_full=window_full)
    return TickSnapshot(jobs={TARGET: stream}, configs=CONFIGS)


def signals_snapshot(rows: Sequence[dict[str, Any]]) -> TickSnapshot:
    parsed: dict[SignalKey, SignalRow] = {}
    for raw in rows:
        row = signal_row_of(raw)
        assert row is not None
        parsed[row.key] = row
    stream: SignalStream = StreamSnapshot(rows=parsed)
    return TickSnapshot(signals={"prod": stream}, configs=CONFIGS)


def deliveries_snapshot(rows: Sequence[dict[str, Any]]) -> TickSnapshot:
    parsed: dict[str, DeliveryRow] = {}
    for raw in rows:
        row = delivery_row_of(raw)
        assert row is not None
        parsed[row.id] = row
    return TickSnapshot(deliveries={"prod": StreamSnapshot(rows=parsed)}, configs=CONFIGS)


def run(
    previous: TickSnapshot,
    current: TickSnapshot,
    *,
    at: datetime = MOMENT,
    monotonic: float = 0.0,
    stalls: Mapping[str, StallEntry] | None = None,
    stall_after: float = WATCH_STALL_SECONDS,
    refreshed: Collection[JobTarget] | None = None,
) -> DiffResult:
    """One tick of the diff.

    ``refreshed`` defaults to None, which is diff_tick's own "every target was
    freshly read" - so the tests that are about diffing rather than about
    observation say nothing about it and read exactly as they did.
    """
    return diff_tick(
        previous,
        current,
        observed_at=at,
        monotonic=monotonic,
        stalls=stalls or {},
        stall_after=stall_after,
        refreshed_jobs=refreshed,
    )


def tokens(result: DiffResult) -> list[str]:
    return [event.event for event in result.events]


def only(result: DiffResult, token: str) -> WatchEvent:
    matching = [event for event in result.events if event.event == token]
    assert len(matching) == 1, f"expected exactly one {token}, got {tokens(result)}"
    return matching[0]


# --- jobs -----------------------------------------------------------------


def test_a_replay_chunk_advance_emits_one_progress_line_with_the_backend_key_names() -> None:
    """The acceptance criterion, at the diff layer.

    Asserts on the worker's OWN key names. If anyone renames them on the way out,
    an operator can no longer grep the same string in tasks.py and in the output.
    """
    before = jobs_snapshot([make_replay_job(chunks_completed=4, current_chunk_index=4)])
    after = jobs_snapshot(
        [
            make_replay_job(
                chunks_completed=5,
                current_chunk_index=5,
                chunk_from=datetime(2026, 7, 4, tzinfo=UTC),
                chunk_to=datetime(2026, 7, 5, tzinfo=UTC),
            )
        ]
    )

    result = run(before, after)

    assert tokens(result) == ["job.progress"]
    event = result.events[0]
    assert event.data["replay_chunks_completed"] == 5
    assert event.data["replay_chunks_total"] == 18
    assert event.data["replay_progress_percent"] == 27.8
    assert event.data["replay_current_chunk_index"] == 5
    assert event.data["replay_progress_phase"] == "collecting"
    assert event.data["mode"] == "metrics_replay"
    assert event.target is not None
    assert event.target.kind == "scan_job"
    assert event.target.id == "job-91c2"
    assert "chunk 5 of 18 (27.8%) collecting" in event.message


def test_a_job_whose_updated_at_moved_but_whose_counters_did_not_emits_nothing() -> None:
    """_heartbeat_replay_progress bumps updated_at on every heartbeat.

    With updated_at in the fingerprint, watch would print a line on every
    heartbeat AND job.stalled would become unreachable - a heartbeating-but-
    wedged job would look alive, exactly backwards for the incident question.
    """
    before = jobs_snapshot([make_replay_job(at=MOMENT, updated_at=MOMENT)])
    after = jobs_snapshot([make_replay_job(at=MOMENT, updated_at=MOMENT + timedelta(seconds=30))])

    result = run(before, after)

    assert tokens(result) == []


def test_a_metrics_collection_job_emits_no_progress_lines() -> None:
    """Watch does not invent progress the backend does not publish."""
    running = make_job(job_id="job-c1", status="running", at=MOMENT)
    assert running["result_summary"]["mode"] == DISPATCHER_MODE
    before = jobs_snapshot([make_job(job_id="job-c1", status="pending", at=MOMENT)])
    after = jobs_snapshot([running])

    result = run(before, after)

    assert tokens(result) == ["job.started"]
    # ...and nothing invents a chunk count for it.
    assert "replay_chunks_total" not in result.events[0].data


def test_a_completed_replay_reports_all_chunks_done() -> None:
    """tasks.py merges phase="completed" with completed == len(chunks) into the
    final result_summary, so job.finished can honestly say 18 of 18."""
    before = jobs_snapshot([make_replay_job(chunks_completed=17, current_chunk_index=17)])
    after = jobs_snapshot(
        [
            make_replay_job(
                status="completed",
                chunks_completed=18,
                phase="completed",
                current_chunk_index=None,
            )
        ]
    )

    result = run(before, after)

    assert tokens(result) == ["job.finished"]
    event = result.events[0]
    assert event.data["replay_chunks_completed"] == 18
    assert event.data["replay_progress_percent"] == 100.0
    assert "18 of 18 chunks" in event.message


def test_a_new_pending_job_is_queued_and_a_new_running_job_is_started() -> None:
    before = jobs_snapshot([])
    queued = run(before, jobs_snapshot([make_job(job_id="job-2", status="pending", at=MOMENT)]))
    started = run(before, jobs_snapshot([make_job(job_id="job-2", status="running", at=MOMENT)]))

    assert tokens(queued) == ["job.queued"]
    assert tokens(started) == ["job.started"]


def test_a_job_that_appeared_and_failed_inside_one_poll_still_reports() -> None:
    """At a 10s interval a short job can be created and finish between polls.

    Reporting only transitions watch personally observed would drop exactly the
    failure that triggers the delivery lines.
    """
    result = run(
        jobs_snapshot([]),
        jobs_snapshot([make_job(job_id="job-9", status="failed", at=MOMENT, error_message="boom")]),
    )

    assert tokens(result) == ["job.failed"]
    assert result.events[0].data["error_message"] == "boom"


def test_a_job_missing_from_a_windowed_response_emits_no_cleared_line() -> None:
    """Jobs are windowed by created_at desc, so a disappearance is an artifact."""
    before = jobs_snapshot([make_job(job_id="job-old", status="completed", at=MOMENT)])
    after = jobs_snapshot([])

    result = run(before, after)

    assert tokens(result) == []


def test_a_scan_config_seen_for_the_first_time_seeds_silently() -> None:
    """previous has no entry for the target at all - a config discovered mid-run."""
    result = run(
        TickSnapshot(configs=CONFIGS),
        jobs_snapshot([make_replay_job(status="running"), make_job(job_id="job-old")]),
    )

    assert tokens(result) == []
    # ...but its running job is now on the stall clock.
    assert set(result.stalls) == {"job-91c2"}


# --- stalls ---------------------------------------------------------------


def advance_stalls(
    job: dict[str, Any], *, seconds: Sequence[float], stall_after: float = 120.0
) -> list[tuple[float, list[str]]]:
    """Feed the SAME job at a scripted sequence of monotonic times."""
    snapshot = jobs_snapshot([job])
    stalls: Mapping[str, StallEntry] = {}
    seen: list[tuple[float, list[str]]] = []
    previous = TickSnapshot(configs=CONFIGS)
    for elapsed in seconds:
        result = run(
            previous,
            snapshot,
            at=MOMENT + timedelta(seconds=elapsed),
            monotonic=elapsed,
            stalls=stalls,
            stall_after=stall_after,
        )
        stalls = result.stalls
        previous = snapshot
        seen.append((elapsed, tokens(result)))
    return seen


def test_a_stalled_job_reports_at_the_threshold_and_then_on_powers_of_two() -> None:
    """1x, 2x, 4x, 8x - ~12 lines over four hours, not one every tick."""
    job = make_replay_job(status="running", chunks_completed=8, current_chunk_index=8)
    seen = advance_stalls(job, seconds=[0, 60, 120, 180, 240, 300, 480, 600, 960])

    fired = [elapsed for elapsed, events in seen if "job.stalled" in events]
    assert fired == [120, 240, 480, 960]


def test_a_stall_line_names_the_frozen_progress_and_when_watch_last_saw_it() -> None:
    job = make_replay_job(status="running", chunks_completed=8, current_chunk_index=8)
    snapshot = jobs_snapshot([job])
    seed = run(TickSnapshot(configs=CONFIGS), snapshot, monotonic=0.0)
    result = run(
        snapshot,
        snapshot,
        at=MOMENT + timedelta(seconds=300),
        monotonic=300.0,
        stalls=seed.stalls,
    )

    event = only(result, "job.stalled")
    assert event.data["unchanged_seconds"] == 300
    assert event.data["unchanged_since"] == "2026-07-31T19:10:41Z"
    assert event.data["replay_chunks_completed"] == 8
    assert "chunk 8 of 18 (44.4%) collecting" in event.message


def test_progress_after_a_stall_suppresses_the_next_stall_line() -> None:
    """A fingerprint change restarts the clock; there is no job.unstalled token."""
    stuck = jobs_snapshot([make_replay_job(status="running", chunks_completed=8)])
    moved = jobs_snapshot([make_replay_job(status="running", chunks_completed=9)])

    seed = run(TickSnapshot(configs=CONFIGS), stuck, monotonic=0.0)
    stalled = run(stuck, stuck, at=MOMENT, monotonic=200.0, stalls=seed.stalls)
    assert "job.stalled" in tokens(stalled)

    progressed = run(stuck, moved, at=MOMENT, monotonic=210.0, stalls=stalled.stalls)
    assert tokens(progressed) == ["job.progress"]

    quiet = run(moved, moved, at=MOMENT, monotonic=300.0, stalls=progressed.stalls)
    assert tokens(quiet) == []

    late = run(moved, moved, at=MOMENT, monotonic=340.0, stalls=quiet.stalls)
    assert "job.stalled" in tokens(late)


def test_the_stall_tracker_is_pruned_to_the_jobs_in_the_current_snapshot() -> None:
    """The one accumulating structure in watch. 500 job ids, 50 ticks, bound 10."""
    stalls: Mapping[str, StallEntry] = {}
    previous = TickSnapshot(configs=CONFIGS)
    for tick in range(50):
        rows = [
            make_replay_job(job_id=f"job-{tick}-{index}", status="running") for index in range(10)
        ]
        current = jobs_snapshot(rows)
        result = run(previous, current, monotonic=float(tick), stalls=stalls)
        stalls = result.stalls
        previous = current
        assert len(stalls) <= 10, f"tracker grew to {len(stalls)} on tick {tick}"
    assert set(stalls) == {f"job-49-{index}" for index in range(10)}


def test_a_pending_job_no_worker_ever_picked_up_is_reported_as_unchanged() -> None:
    """The most common way a scan hangs, and it used to be reported as nothing.

    A dead Celery worker leaves the job pending forever: one ``job.queued`` line
    and then total silence, which reads exactly like a healthy quiet instance.
    It is reported under its OWN token because "stalled" would claim watch saw
    progress stop, and a pending job never published any.
    """
    job = make_job(job_id="job-p1", status="pending", at=MOMENT)
    snapshot = jobs_snapshot([job])

    seed = run(TickSnapshot(configs=CONFIGS), snapshot, monotonic=0.0)
    result = run(
        snapshot,
        snapshot,
        at=MOMENT + timedelta(seconds=300),
        monotonic=300.0,
        stalls=seed.stalls,
    )

    event = only(result, "job.unchanged")
    assert "job.stalled" not in tokens(result)
    assert "still in status pending" in event.message
    assert "no worker has picked it up" in event.message
    assert event.data["unchanged_seconds"] == 300
    assert event.data["status"] == "pending"


def test_a_running_collection_job_is_never_reported_as_stalled() -> None:
    """The negative half, and the reason the second token exists at all.

    A metrics_collection job's fingerprint IS its status, so it cannot change
    while the job runs - which made every long collection job structurally
    guaranteed to be called stalled the moment it outlived --stall-after. Watch
    can honestly say only that the status has not moved.
    """
    job = make_job(job_id="job-c1", status="running", at=MOMENT)
    assert job["result_summary"]["mode"] == DISPATCHER_MODE
    snapshot = jobs_snapshot([job])

    seed = run(TickSnapshot(configs=CONFIGS), snapshot, monotonic=0.0)
    result = run(
        snapshot,
        snapshot,
        at=MOMENT + timedelta(seconds=300),
        monotonic=300.0,
        stalls=seed.stalls,
    )

    event = only(result, "job.unchanged")
    assert "job.stalled" not in tokens(result)
    assert "stalled" not in event.message
    assert "publishes no progress" in event.message
    # ...and nothing invents a chunk count for it, in the prose or in the data.
    assert "replay_chunks_total" not in event.data
    assert "in status running" in event.message


def test_a_replay_and_a_collection_job_frozen_together_get_different_tokens() -> None:
    """One tick, one threshold, two different claims - because they ARE different.

    The replay job published progress and stopped; the collection job never had
    any to publish. Collapsing them into one token would make the honest half
    unreadable.
    """
    snapshot = jobs_snapshot(
        [
            make_replay_job(status="running", chunks_completed=8, current_chunk_index=8),
            make_job(job_id="job-c1", status="running", at=MOMENT),
        ]
    )

    seed = run(TickSnapshot(configs=CONFIGS), snapshot, monotonic=0.0)
    result = run(
        snapshot,
        snapshot,
        at=MOMENT + timedelta(seconds=300),
        monotonic=300.0,
        stalls=seed.stalls,
    )

    assert sorted(tokens(result)) == ["job.stalled", "job.unchanged"]
    stalled = only(result, "job.stalled")
    unchanged = only(result, "job.unchanged")
    assert stalled.target is not None and stalled.target.id == "job-91c2"
    assert unchanged.target is not None and unchanged.target.id == "job-c1"
    assert "watch has seen no progress since" in stalled.message


# --- stalls: what was actually OBSERVED ------------------------------------


def test_a_target_whose_read_failed_this_tick_accrues_no_stall_time() -> None:
    """The rows in an unrefreshed stream are last tick's, so nothing was observed.

    A failed read holds the last GOOD snapshot - that is what turns an outage
    into a reporting delay rather than a coverage gap - and a tracker that counts
    those held rows as fresh sightings ends up printing "unchanged for 4m" in the
    same tick as "jobs read failed: HTTP 502".
    """
    snapshot = jobs_snapshot([make_replay_job(status="running", chunks_completed=8)])
    seed = run(TickSnapshot(configs=CONFIGS), snapshot, monotonic=0.0, refreshed={TARGET})

    stalls = seed.stalls
    for elapsed in (60.0, 120.0, 240.0, 480.0):
        result = run(
            snapshot,
            snapshot,
            at=MOMENT + timedelta(seconds=elapsed),
            monotonic=elapsed,
            stalls=stalls,
            refreshed=set(),
        )
        assert tokens(result) == [], f"reported at {elapsed}s with nothing observed"
        stalls = result.stalls

    assert stalls["job-91c2"].unobserved is True
    assert stalls["job-91c2"].reported == 0


def test_the_first_good_look_after_a_blind_stretch_restarts_the_stall_clock() -> None:
    """It RESTARTS rather than resumes, and the reported window says so.

    Resuming would fire the moment the stream came back and blame the job for the
    outage. Restarting costs one late report and buys a number - and an
    ``unchanged_since`` - that describe seconds watch was actually looking.
    """
    snapshot = jobs_snapshot([make_replay_job(status="running", chunks_completed=8)])
    stalls: Mapping[str, StallEntry] = run(
        TickSnapshot(configs=CONFIGS), snapshot, monotonic=0.0, refreshed={TARGET}
    ).stalls

    for elapsed in (60.0, 120.0):
        stalls = run(
            snapshot,
            snapshot,
            at=MOMENT + timedelta(seconds=elapsed),
            monotonic=elapsed,
            stalls=stalls,
            refreshed=set(),
        ).stalls

    recovered = run(
        snapshot,
        snapshot,
        at=MOMENT + timedelta(seconds=240),
        monotonic=240.0,
        stalls=stalls,
        refreshed={TARGET},
    )
    assert tokens(recovered) == [], "reported on the tick it regained sight"

    early = run(
        snapshot,
        snapshot,
        at=MOMENT + timedelta(seconds=300),
        monotonic=300.0,
        stalls=recovered.stalls,
        refreshed={TARGET},
    )
    assert tokens(early) == [], "fired 60s after recovery on a 120s threshold"

    due = run(
        snapshot,
        snapshot,
        at=MOMENT + timedelta(seconds=360),
        monotonic=360.0,
        stalls=early.stalls,
        refreshed={TARGET},
    )
    event = only(due, "job.stalled")
    assert event.data["unchanged_seconds"] == 120
    assert event.data["unchanged_since"] == "2026-07-31T19:14:41Z"


def test_a_finished_job_leaves_the_stall_tracker() -> None:
    running = jobs_snapshot([make_replay_job(status="running")])
    done = jobs_snapshot([make_replay_job(status="completed", phase="completed")])

    seeded = run(TickSnapshot(configs=CONFIGS), running)
    assert set(seeded.stalls) == {"job-91c2"}

    finished = run(running, done, stalls=seeded.stalls)
    assert finished.stalls == {}


# --- signals --------------------------------------------------------------


def test_a_new_scope_emits_opened_and_a_vanished_scope_emits_cleared() -> None:
    """Newness is set-difference: MetricSignalResponse has no detected_at."""
    empty = signals_snapshot([])
    one = signals_snapshot([make_signal()])

    opened = run(empty, one)
    cleared = run(one, empty)

    assert tokens(opened) == ["signal.opened"]
    assert tokens(cleared) == ["signal.cleared"]
    assert "0 signals still open" in cleared.events[0].message


def test_a_signal_that_advances_bucket_emits_updated_not_cleared_plus_opened() -> None:
    """bucket is in the FINGERPRINT, not the identity.

    With bucket in the identity this pair would print a cleared plus an opened -
    a fabricated resolution in the middle of an ongoing incident.
    """
    before = signals_snapshot([make_signal(bucket=datetime(2026, 7, 31, 19, 0, tzinfo=UTC))])
    after = signals_snapshot([make_signal(bucket=datetime(2026, 7, 31, 19, 30, tzinfo=UTC))])

    result = run(before, after)

    assert tokens(result) == ["signal.updated"]
    # Verbatim from MetricSignalResponse, which pydantic spells with a `Z`.
    assert result.events[0].data["bucket"] == "2026-07-31T19:30:00Z"
    assert "moved to bucket 2026-07-31T19:30:00Z" in result.events[0].message


def test_a_metric_scope_signal_with_a_null_scan_config_id_is_keyed_and_rendered() -> None:
    """metrics_insights_service appends metric-scope signals with no config.

    Keying on the triple has to survive the None, and the event has to render
    with a null target rather than a synthetic id.
    """
    signal = make_signal(scope_type="metric", scope_ref="checkout_rate", scan_config_id=None)
    result = run(signals_snapshot([]), signals_snapshot([signal]))

    assert tokens(result) == ["signal.opened"]
    event = result.events[0]
    assert event.target is None
    assert event.data["scan_config_id"] is None
    assert event.data["scope_type"] == "metric"
    assert event.data["scope_ref"] == "checkout_rate"


def test_two_scopes_under_one_config_are_two_signals() -> None:
    both = signals_snapshot(
        [
            make_signal(scope_type="project_total", scope_ref="prod"),
            make_signal(scope_type="event", scope_ref="checkout.completed"),
        ]
    )

    result = run(signals_snapshot([]), both)

    assert tokens(result) == ["signal.opened", "signal.opened"]


def test_a_signal_carries_every_field_the_response_has_including_incident_child() -> None:
    """Watch defines no threshold and drops no row: SIGNIFICANT_MIN_REL_EFFECT
    has drifted twice already and there is no third copy here (TRAP A)."""
    signal = make_signal(incident_child=True, z_score=-6.1, actual_count=412.0)
    result = run(signals_snapshot([]), signals_snapshot([signal]))

    data = result.events[0].data
    assert data["incident_child"] is True
    assert data["z_score"] == -6.1
    assert data["actual_count"] == 412.0
    assert data["stddev"] == 120.0
    assert data["state"] == "open"


def test_a_signals_stream_seen_for_the_first_time_seeds_silently() -> None:
    """TRAP D: a pre-existing open signal is not a newly opened one."""
    result = run(TickSnapshot(configs=CONFIGS), signals_snapshot([make_signal()]))

    assert tokens(result) == []


# --- deliveries -----------------------------------------------------------


def test_a_delivery_that_transitions_into_failed_emits_one_line() -> None:
    before = deliveries_snapshot([make_delivery(status="pending", error_message=None)])
    after = deliveries_snapshot([make_delivery(status="failed")])

    result = run(before, after)

    assert tokens(result) == ["delivery.failed"]
    event = result.events[0]
    assert event.data["error_message"] == "channel_not_found"
    assert event.data["destination_name"] == "oncall"
    assert event.target is not None
    assert event.target.kind == "alert_delivery"
    assert event.target.id == "del-4f21"
    assert "Nobody was paged" in event.message


def test_a_delivery_that_stays_failed_does_not_repeat() -> None:
    failed = deliveries_snapshot([make_delivery(status="failed")])

    assert tokens(run(failed, failed)) == []


def test_a_new_failed_delivery_emits_even_though_it_was_never_seen_pending() -> None:
    result = run(deliveries_snapshot([]), deliveries_snapshot([make_delivery(status="failed")]))

    assert tokens(result) == ["delivery.failed"]


def test_a_sent_delivery_is_not_reported() -> None:
    result = run(
        deliveries_snapshot([]),
        deliveries_snapshot([make_delivery(status="sent", error_message=None)]),
    )

    assert tokens(result) == []


# --- stream identity ------------------------------------------------------
#
# What the deleted `test_the_stream_aliases_describe_the_shapes_the_diff_consumes`
# meant to guard: the key each stream is stored under. It asserted on two values
# it had constructed two lines earlier, so no production change could fail it.
# These two make the same claim through diff_tick, where a narrower key shows up
# as a row that silently disappears.


def test_two_jobs_of_one_scan_config_are_two_rows_not_one() -> None:
    """The job stream is keyed by job id, and only the id will do.

    Keyed by scan_config_id - the other plausible choice, and the one every other
    mapping in watch uses - these two collapse into one, and the operator is told
    about whichever job the response happened to list second.
    """
    result = run(
        jobs_snapshot([]),
        jobs_snapshot(
            [
                make_job(job_id="job-a", status="running", at=MOMENT),
                make_job(job_id="job-b", status="running", at=MOMENT),
            ]
        ),
    )

    assert tokens(result) == ["job.started", "job.started"]
    assert {event.target.id for event in result.events if event.target} == {"job-a", "job-b"}
    # ...and both are on the stall clock, under their own ids.
    assert set(result.stalls) == {"job-a", "job-b"}


def test_two_configs_reporting_the_same_scope_are_two_signals() -> None:
    """The signal key is the backend's own triple, scan_config_id included.

    ``metrics_insights_service.py:527`` keys it that way, and two scan configs
    watching the same event type is ordinary. Keyed on the scope alone the second
    config's signal never opens and never clears - it simply is not there.
    """
    both = signals_snapshot(
        [
            make_signal(
                scan_config_id="scan-1", scope_type="event", scope_ref="checkout.completed"
            ),
            make_signal(
                scan_config_id="scan-2", scope_type="event", scope_ref="checkout.completed"
            ),
        ]
    )

    result = run(signals_snapshot([]), both)

    assert tokens(result) == ["signal.opened", "signal.opened"]
    assert {event.data["scan_config_id"] for event in result.events} == {"scan-1", "scan-2"}
