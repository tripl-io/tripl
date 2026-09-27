"""``tripl watch`` end to end, through ``main([...])`` against a mocked instance.

Virtual time throughout: the ``fake_clock`` fixture makes every ``sleep`` return
instantly, so a twenty-tick run costs nothing. Nothing here sleeps, and nothing
here asserts on prose - the contract is the exit code, the event tokens, the
request URLs and the JSON keys.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.conftest import (
    FakeClock,
    FakeInstance,
    make_delivery,
    make_job,
    make_project,
    make_replay_job,
    make_scan_config,
    make_signal,
    make_summary,
)
from tripl_cli.cli import main
from tripl_cli.watch.model import EVENT_TOKENS

pytestmark = pytest.mark.usefixtures("configured_env", "fake_clock")

TIMESTAMPED = re.compile(r"^\d{4}-\d{2}-\d{2}T")


def watch(*argv: str, duration: str = "30") -> int:
    return main(["watch", "--duration", duration, *argv])


def lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line]


def stream_lines(text: str) -> list[str]:
    return [line for line in lines(text) if TIMESTAMPED.match(line)]


def tokens_of(text: str) -> list[str]:
    return [line.split()[1] for line in stream_lines(text)]


def documents(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def calls_to(api: FakeInstance, url: str) -> list[Any]:
    """Requests this run actually made, off the router the fixture owns."""
    return [call for call in api.router.calls if str(call.request.url).split("?")[0] == url]


def query_of(call: Any) -> dict[str, list[str]]:
    return parse_qs(urlsplit(str(call.request.url)).query)


# --- the acceptance criteria ----------------------------------------------


def test_a_running_replay_shows_live_chunk_progress(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance criterion 1 of tripl-ey6j.4, end to end.

    The chunk counter is the field that made "is this scan hung or just slow?"
    answerable during the 2026-07-28..31 incident.
    """
    tripl_api.each(
        tripl_api.jobs_url("prod", "scan-1"),
        [
            (200, [make_replay_job(chunks_completed=chunk, current_chunk_index=chunk)])
            for chunk in (3, 4, 5, 6)
        ],
    )

    code = watch()
    captured = capsys.readouterr()

    assert code == 0
    progress = [line for line in stream_lines(captured.out) if " job.progress " in line]
    assert len(progress) == 3, captured.out
    assert "chunk 4 of 18 (22.2%) collecting" in progress[0]
    assert "chunk 5 of 18 (27.8%) collecting" in progress[1]
    assert "chunk 6 of 18 (33.3%) collecting" in progress[2]


def test_a_signal_that_appears_on_the_second_poll_is_reported_without_a_restart(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance criterion 2 of tripl-ey6j.4, end to end."""
    tripl_api.each(tripl_api.signals_url("prod"), [(200, []), (200, [make_signal()])])

    code = watch(duration="60")
    captured = capsys.readouterr()

    assert code == 0
    assert tokens_of(captured.out).count("signal.opened") == 1


def test_a_replay_that_finishes_while_watching_ends_with_job_finished(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    tripl_api.each(
        tripl_api.jobs_url("prod", "scan-1"),
        [
            (200, [make_replay_job(chunks_completed=16, current_chunk_index=16)]),
            (200, [make_replay_job(chunks_completed=17, current_chunk_index=17)]),
            (200, [make_replay_job(status="completed", chunks_completed=18, phase="completed")]),
        ],
    )

    watch()
    captured = capsys.readouterr()

    assert tokens_of(captured.out) == [
        "watch.started",
        "job.progress",
        "job.finished",
        "watch.stopped",
    ]
    finished = next(line for line in stream_lines(captured.out) if "job.finished" in line)
    assert "18 of 18 chunks" in finished


# --- the error policy (TRAP B) --------------------------------------------


def test_a_signals_poll_that_500s_does_not_emit_cleared_lines_and_prints_degraded(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """The central negative case: a failed read must NOT read as an empty list.

    Diffing against [] would print signal.cleared for the open signal and then
    reprint it as signal.opened on recovery - lying twice during the incident.
    """
    signal = make_signal()
    tripl_api.each(
        tripl_api.signals_url("prod"),
        [(200, [signal]), (500, {"detail": "boom"}), (200, [signal])],
    )

    watch(duration="120")
    captured = capsys.readouterr()

    emitted = tokens_of(captured.out)
    assert "signal.cleared" not in emitted
    assert "signal.opened" not in emitted, "the still-open signal was reprinted as new"
    degraded = [line for line in stream_lines(captured.out) if "poll.degraded" in line]
    assert len(degraded) == 1
    # The RESOLVED path, not the template: an operator following six projects
    # cannot act on "/projects/{slug}/..." - it does not say which one failed.
    assert "/projects/prod/anomalies/signals" in degraded[0]
    assert "{slug}" not in degraded[0]
    assert "HTTP 500" in degraded[0]
    assert emitted.count("poll.recovered") == 1


def test_a_transition_that_happened_during_a_blind_window_is_reported_on_recovery(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed poll is a reporting DELAY, not a coverage gap."""
    opened = make_signal()
    tripl_api.each(
        tripl_api.signals_url("prod"),
        [(200, []), (503, {"detail": "down"}), (200, [opened])],
    )

    watch(duration="120")
    captured = capsys.readouterr()

    emitted = tokens_of(captured.out)
    assert emitted.count("signal.opened") == 1
    recovered = next(line for line in stream_lines(captured.out) if "poll.recovered" in line)
    assert "1 event reported from the gap" in recovered


def test_a_failure_on_one_stream_does_not_suspend_the_others(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    tripl_api.signals("prod", {"detail": "boom"}, status=500)
    tripl_api.each(
        tripl_api.jobs_url("prod", "scan-1"),
        [(200, [make_replay_job(chunks_completed=chunk)]) for chunk in (3, 4)],
    )

    watch()
    captured = capsys.readouterr()

    emitted = tokens_of(captured.out)
    assert "poll.degraded" in emitted
    assert "job.progress" in emitted


def test_repeated_failures_report_on_powers_of_two_and_never_end_the_run(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """A follow tool that exits when the thing it follows goes down is backwards."""
    tripl_api.jobs("prod", "scan-1", {"detail": "boom"}, status=502)

    code = watch(duration="90")
    captured = capsys.readouterr()

    assert code == 0
    # 10 ticks of failure -> the 1st, 2nd, 4th and 8th are reported.
    assert tokens_of(captured.out).count("poll.degraded") == 4


def test_a_revoked_key_mid_run_ends_the_run_with_exit_one_and_a_stopped_line(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only a 401 ends the run: the key is gone and waiting cannot fix it."""
    jobs = tripl_api.each(
        tripl_api.jobs_url("prod", "scan-1"),
        [(200, [make_job(at=tripl_api.now)]), (401, {"detail": "Invalid API key"})],
    )

    code = watch(duration="600")
    captured = capsys.readouterr()

    assert code == 1
    assert jobs.call_count == 2, "watch kept polling after the 401"
    stopped = next(line for line in stream_lines(captured.out) if "watch.stopped" in line)
    assert "authentication_failed" in stopped


def test_a_403_on_one_project_does_not_end_the_run(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    tripl_api.deliveries("prod", {"detail": "forbidden"}, status=403)

    code = watch(duration="60")
    captured = capsys.readouterr()

    assert code == 0
    assert "poll.degraded" in tokens_of(captured.out)
    assert "watch.stopped" in tokens_of(captured.out)


# --- request shape (TRAP A, TRAP E) ---------------------------------------


def test_jobs_are_requested_with_the_watch_limit_not_doctors(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """200 rows per config every 10 seconds is tripl-jfm3.107 rebuilt."""
    watch()
    capsys.readouterr()

    made = calls_to(tripl_api, tripl_api.jobs_url("prod", "scan-1"))
    assert made
    assert all(query_of(call)["limit"] == ["10"] for call in made)


def test_signals_are_requested_expanded(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """The collapsed list drops event-scope incidents entirely (TRAP A)."""
    watch()
    capsys.readouterr()

    made = calls_to(tripl_api, tripl_api.signals_url("prod"))
    assert made
    assert all(query_of(call)["expanded"] == ["true"] for call in made)


def test_deliveries_are_requested_failed_only_and_without_a_date_floor(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """A date_from floor filters created_at, so a delivery created before it that
    FAILS during the run would be invisible."""
    watch()
    capsys.readouterr()

    made = calls_to(tripl_api, tripl_api.deliveries_url("prod"))
    assert made
    for call in made:
        query = query_of(call)
        assert query["status"] == ["failed"]
        assert query["limit"] == ["20"]
        assert "date_from" not in query
        assert "date_to" not in query


def test_slow_streams_are_not_polled_on_every_tick(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """get_active_signals caches at ttl_seconds=30; polling faster is wasted work.

    The EXACT counts, not an upper bound: an 18-second run at --interval 2 is ten
    ticks, and every slow stream is read exactly once - on the seeding tick. A
    `<= 2` assertion would still pass if the cadence silently doubled, which is
    the whole quantity this test is named after.
    """
    watch("--interval", "2", duration="18")
    capsys.readouterr()

    assert len(calls_to(tripl_api, tripl_api.jobs_url("prod", "scan-1"))) == 10
    assert len(calls_to(tripl_api, tripl_api.signals_url("prod"))) == 1
    assert len(calls_to(tripl_api, tripl_api.deliveries_url("prod"))) == 1
    # `prepare` read the listing once; the slow clock starts there rather than
    # firing a second identical read on the seeding tick.
    assert len(calls_to(tripl_api, tripl_api.scans_url("prod"))) == 1


def test_a_slow_stream_is_re_read_on_the_first_tick_that_reaches_the_ttl(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """The boundary itself: the due test is `>=`, so the 30s tick re-reads.

    Ticks land at 0, 10, 20 and 30 seconds. Only the last one is a full
    SLOW_STREAM_MIN_SECONDS after the seeding read, so each slow stream is read
    exactly twice - which is also what makes the previous test's `== 1` a
    statement about the cadence rather than about the run being too short.
    """
    watch(duration="30")
    capsys.readouterr()

    assert len(calls_to(tripl_api, tripl_api.jobs_url("prod", "scan-1"))) == 4
    assert len(calls_to(tripl_api, tripl_api.signals_url("prod"))) == 2
    assert len(calls_to(tripl_api, tripl_api.deliveries_url("prod"))) == 2
    assert len(calls_to(tripl_api, tripl_api.scans_url("prod"))) == 2


def test_a_run_that_stops_one_tick_short_of_the_ttl_reads_a_slow_stream_once(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other side of the boundary: ticks at 0, 10 and 20 never reach 30s."""
    watch(duration="20")
    capsys.readouterr()

    assert len(calls_to(tripl_api, tripl_api.jobs_url("prod", "scan-1"))) == 3
    assert len(calls_to(tripl_api, tripl_api.signals_url("prod"))) == 1


def test_the_next_tick_is_scheduled_after_the_previous_one_completed(
    tripl_api: FakeInstance, fake_clock: FakeClock, capsys: pytest.CaptureFixture[str]
) -> None:
    """No fixed wall clock and no catch-up: a slow instance is polled LESS."""
    import httpx

    def slow(request: httpx.Request) -> httpx.Response:
        fake_clock.advance(5.0)
        return httpx.Response(200, json=[make_job(at=tripl_api.now)])

    tripl_api.handler(tripl_api.jobs_url("prod", "scan-1"), slow)

    watch()
    capsys.readouterr()

    # Every recorded sleep is the FULL interval even though each tick burned 5
    # virtual seconds - the alternative would queue ticks behind a stall.
    assert fake_clock.sleeps == [10.0, 10.0]
    assert len(calls_to(tripl_api, tripl_api.jobs_url("prod", "scan-1"))) == 3


def test_watch_opens_exactly_one_connection_pool_over_twenty_ticks(
    tripl_api: FakeInstance,
    tracking_pool: list[Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Blocks the "simplify to a sync outer loop" regression."""
    watch(duration="190")
    capsys.readouterr()

    assert len(tracking_pool) == 1
    assert len(calls_to(tripl_api, tripl_api.jobs_url("prod", "scan-1"))) == 20


# --- the first screen (TRAP D) --------------------------------------------


def test_an_instance_with_no_running_jobs_prints_a_preamble_and_exits_zero(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    code = watch()
    captured = capsys.readouterr()

    assert code == 0
    assert "running    none" in captured.out
    assert "pending    none" in captured.out
    assert "watch.started" in tokens_of(captured.out)
    assert not [token for token in tokens_of(captured.out) if token.startswith("job.")]


def test_the_preamble_shows_a_running_replay_at_its_current_chunk(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """An operator who attached 30 seconds late must not be told nothing is up."""
    tripl_api.jobs("prod", "scan-1", [make_replay_job(chunks_completed=3, at=tripl_api.now)])

    watch()
    captured = capsys.readouterr()

    running = next(line for line in captured.out.splitlines() if line.startswith("  running"))
    assert "'prod events'" in running
    assert "chunk 3 of 18 (16.7%) collecting" in running
    assert "(metrics_replay)" in running


def test_the_preamble_labels_both_signal_counts_because_they_are_different_sets(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """TRAP A: watch counts every scope, the server counts the significant ones.

    A labelled mismatch is honest; an unexplained one becomes a bug report.
    """
    tripl_api.projects([make_project(summary=make_summary(monitoring_signal_count=1))])
    tripl_api.signals(
        "prod",
        [make_signal(), make_signal(scope_type="event", scope_ref="checkout.completed")],
    )

    watch()
    captured = capsys.readouterr()

    signals = next(line for line in captured.out.splitlines() if line.startswith("  signals"))
    assert "2 open across all scopes (baseline)" in signals
    assert "counts 1 as significant" in signals


def test_a_pre_existing_open_signal_does_not_print_as_newly_opened(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    tripl_api.signals("prod", [make_signal()])

    watch(duration="60")
    captured = capsys.readouterr()

    assert "signal.opened" not in tokens_of(captured.out)


def test_a_config_discovered_mid_run_seeds_silently(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """A config created mid-incident must not dump its job history as events."""
    later = make_scan_config("scan-2", name="checkout replay")
    tripl_api.each(
        tripl_api.scans_url("prod"),
        [(200, [make_scan_config()]), (200, [make_scan_config(), later])],
    )
    tripl_api.jobs("prod", "scan-2", [make_job(job_id="job-old", at=tripl_api.now)])

    watch(duration="60")
    captured = capsys.readouterr()

    assert len(calls_to(tripl_api, tripl_api.jobs_url("prod", "scan-2"))) >= 1
    assert not [token for token in tokens_of(captured.out) if token.startswith("job.")]


def test_no_preamble_line_starts_with_a_timestamp_and_every_stream_line_does(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """`tripl watch | grep -E '^[0-9]{4}-'` is the stream alone, by contract."""
    watch()
    captured = capsys.readouterr()

    printed = lines(captured.out)
    first_stream = next(index for index, line in enumerate(printed) if TIMESTAMPED.match(line))
    assert first_stream > 0, "the preamble was empty"
    assert all(not TIMESTAMPED.match(line) for line in printed[:first_stream])
    assert all(TIMESTAMPED.match(line) for line in printed[first_stream:])
    assert printed[first_stream].split()[1] == "watch.started"


# --- output contracts (TRAP C) --------------------------------------------


def test_human_output_is_ascii_only(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """`tripl watch | tee incident.log` must produce what the operator saw."""
    tripl_api.jobs("prod", "scan-1", [make_replay_job(chunks_completed=3)])
    tripl_api.signals("prod", [make_signal()])
    tripl_api.deliveries("prod", [make_delivery()])

    watch()
    captured = capsys.readouterr()

    assert captured.out.isascii(), "non-ASCII in the watch stream"
    assert "\x1b" not in captured.out, "an escape sequence reached the log"


def test_every_project_bearing_line_names_its_project_at_the_same_column(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """`[prod] ` at column 39 on every line that belongs to a project.

    Asserted POSITIVELY, one line at a time. The set-comprehension this replaced
    substituted 39 for any line that had no bracket at all, so a build that
    dropped the project prefix from every stream line passed it unchanged - and
    the prefix is the only thing that says which of six followed projects a line
    is about.
    """
    tripl_api.each(
        tripl_api.jobs_url("prod", "scan-1"),
        [(200, [make_replay_job(chunks_completed=chunk)]) for chunk in (3, 4)],
    )
    tripl_api.each(
        tripl_api.deliveries_url("prod"),
        [(200, {"items": [], "total": 0}), (200, {"items": [make_delivery()], "total": 1})],
    )
    tripl_api.each(tripl_api.signals_url("prod"), [(200, []), (200, [make_signal()])])

    watch(duration="60")
    captured = capsys.readouterr()

    printed = stream_lines(captured.out)
    of_a_project = [line for line in printed if not line.split()[1].startswith("watch.")]
    assert {line.split()[1] for line in of_a_project} >= {
        "job.progress",
        "signal.opened",
        "delivery.failed",
    }, captured.out
    for line in of_a_project:
        # 20 (timestamp) + 2 + 15 (widest token) + 2.
        assert "[" in line, f"no project prefix at all: {line}"
        assert line.index("[") == 39, line
        assert line[39:].startswith("[prod] "), line


def test_the_meta_lines_carry_no_project_bracket(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """watch.started and watch.stopped are about the RUN, not about a project.

    A bracket on them would make `grep '\\[prod\\]'` - the obvious way to pull one
    project out of a multi-project capture - silently include the run's own
    framing, and would claim the footer's tallies were that project's alone.
    """
    watch()
    captured = capsys.readouterr()

    printed = stream_lines(captured.out)
    assert [line.split()[1] for line in printed] == ["watch.started", "watch.stopped"]
    for line in printed:
        assert "[" not in line, line
        # Content still starts at column 39, bracket or no bracket.
        assert line[38] == " " and line[39] != " ", line


def test_json_mode_emits_one_object_per_line_with_a_monotonic_seq_on_stdout(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    tripl_api.each(
        tripl_api.jobs_url("prod", "scan-1"),
        [(200, [make_replay_job(chunks_completed=chunk)]) for chunk in (3, 4)],
    )

    watch("--json")
    captured = capsys.readouterr()

    records = documents(captured.out)
    assert [record["seq"] for record in records] == list(range(1, len(records) + 1))
    assert {record["schema_version"] for record in records} == {1}
    assert {record["command"] for record in records} == {"watch"}
    assert records[0]["event"] == "watch.started"
    assert records[0]["stream"] == "meta"
    assert records[-1]["event"] == "watch.stopped"
    assert all(set(record) == _ENVELOPE_KEYS for record in records)
    # The human report still happened - on stderr.
    assert "Following" in captured.err


_ENVELOPE_KEYS = {
    "schema_version",
    "tool",
    "tool_version",
    "command",
    "stream",
    "seq",
    "time",
    "event",
    "project",
    "target",
    "message",
    "data",
}


def test_the_json_progress_line_carries_the_workers_own_key_names(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    tripl_api.each(
        tripl_api.jobs_url("prod", "scan-1"),
        [
            (200, [make_replay_job(chunks_completed=3, current_chunk_index=3)]),
            (200, [make_replay_job(chunks_completed=4, current_chunk_index=4)]),
        ],
    )

    watch("--json")
    captured = capsys.readouterr()

    progress = next(r for r in documents(captured.out) if r["event"] == "job.progress")
    assert progress["stream"] == "event"
    assert progress["project"] == "prod"
    assert progress["target"] == {"kind": "scan_job", "id": "job-91c2", "name": "prod events"}
    assert progress["data"]["replay_chunks_completed"] == 4
    assert progress["data"]["replay_chunks_total"] == 18
    assert progress["data"]["replay_progress_phase"] == "collecting"
    assert progress["data"]["scan_name"] == "prod events"


def test_the_started_line_frames_the_run_and_the_stopped_line_tallies_it(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    watch("--json")
    captured = capsys.readouterr()

    records = documents(captured.out)
    started = records[0]["data"]
    assert started["projects"] == ["prod"]
    assert started["interval_seconds"] == 10.0
    assert started["duration_seconds"] == 30.0
    assert started["jobs_limit"] == 10
    assert started["instance"]["api_key_scope"] == "unknown"
    assert set(started["baseline"]) == {
        "running_jobs",
        "pending_jobs",
        "open_signals",
        "significant_open_signals",
        "failed_deliveries",
    }
    stopped = records[-1]["data"]
    assert stopped["reason"] == "duration_elapsed"
    assert stopped["ticks"] == 4
    assert stopped["requests"] > 0
    assert stopped["counts"]["watch.started"] == 1
    assert "watch.stopped" not in stopped["counts"], "the footer counted itself"


def test_json_and_human_carry_the_same_events_in_the_same_order(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    tripl_api.each(
        tripl_api.jobs_url("prod", "scan-1"),
        [(200, [make_replay_job(chunks_completed=chunk)]) for chunk in (3, 4, 5)],
    )
    tripl_api.deliveries("prod", [make_delivery()])

    watch("--json")
    captured = capsys.readouterr()

    assert [record["event"] for record in documents(captured.out)] == tokens_of(captured.err)


def test_every_documented_token_is_one_the_renderer_can_lay_out() -> None:
    """The column width is derived from the vocabulary, so a longer token would
    silently shift every line. Pins the arithmetic in render.py's docstring."""
    assert max(len(token) for token in EVENT_TOKENS) == 15


# --- refusals -------------------------------------------------------------


def test_more_than_the_config_cap_refuses_to_start(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refuse, never truncate: a repeating command cannot warn honestly."""
    tripl_api.scans(
        "prod", [make_scan_config(f"scan-{index}", name=f"c{index}") for index in range(25)]
    )

    code = watch()
    captured = capsys.readouterr()

    assert code == 2
    assert "--project" in captured.err
    assert not calls_to(tripl_api, tripl_api.jobs_url("prod", "scan-0")), (
        "it polled before refusing"
    )


def test_an_unmatched_scan_selector_is_a_usage_error_listing_the_candidates(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    code = watch("--scan", "nope")
    captured = capsys.readouterr()

    assert code == 2
    assert "nope" in captured.err
    assert "prod events (scan-1)" in captured.err
    assert not calls_to(tripl_api, tripl_api.jobs_url("prod", "scan-1"))


def test_a_named_scan_narrows_only_the_job_stream(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """Signals are never filtered by --scan: metric-scope signals carry a null
    scan_config_id and would be silently dropped."""
    tripl_api.scans("prod", [make_scan_config(), make_scan_config("scan-2", name="other")])
    tripl_api.jobs("prod", "scan-2", [])

    watch("--scan", "prod events")
    capsys.readouterr()

    assert calls_to(tripl_api, tripl_api.jobs_url("prod", "scan-1"))
    assert not calls_to(tripl_api, tripl_api.jobs_url("prod", "scan-2"))
    assert calls_to(tripl_api, tripl_api.signals_url("prod"))


def test_out_of_range_interval_is_a_usage_error(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["watch", "--interval", "0.5"])

    assert exit_info.value.code == 2
    assert not tripl_api.router.calls, "argparse let a socket open"


def test_a_project_scoped_key_without_project_gets_the_shared_selection_advice(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """Proves raise_selection_failure is reused rather than re-worded."""
    tripl_api.projects({"detail": "Forbidden"}, status=403)

    code = watch()
    captured = capsys.readouterr()

    assert code == 1
    assert "--project <slug>" in captured.err


def test_watch_exits_zero_even_when_a_followed_job_fails(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """watch never exits 3, whatever it observes."""
    tripl_api.each(
        tripl_api.jobs_url("prod", "scan-1"),
        [
            (200, [make_job(job_id="job-1", status="running", at=tripl_api.now)]),
            (
                200,
                [make_job(job_id="job-1", status="failed", at=tripl_api.now, error_message="boom")],
            ),
        ],
    )

    code = watch()
    captured = capsys.readouterr()

    assert code == 0
    assert "job.failed" in tokens_of(captured.out)


def test_ctrl_c_prints_the_stopped_line_and_returns_130(
    tripl_api: FakeInstance, fake_clock: FakeClock, capsys: pytest.CaptureFixture[str]
) -> None:
    """The most error-prone path in the command.

    Catching the interrupt to print a footer and then forgetting to re-raise
    would silently turn every Ctrl-C into an exit 0.
    """
    fake_clock.script = [None, None, KeyboardInterrupt()]

    code = main(["watch"])
    captured = capsys.readouterr()

    assert code == 130
    stopped = next(line for line in stream_lines(captured.out) if "watch.stopped" in line)
    assert "interrupted" in stopped


def test_a_delivery_failure_is_reported_with_the_channel_and_the_error(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """The highest-value line in the set: a failed page makes every other line
    in the stream useless to everyone not in this terminal."""
    tripl_api.each(
        tripl_api.deliveries_url("prod"),
        [(200, {"items": [], "total": 0}), (200, {"items": [make_delivery()], "total": 1})],
    )

    watch("--json", duration="60")
    captured = capsys.readouterr()

    failure = next(r for r in documents(captured.out) if r["event"] == "delivery.failed")
    assert failure["data"]["channel"] == "slack"
    assert failure["data"]["error_message"] == "channel_not_found"
    assert failure["data"]["destination_name"] == "oncall"
    assert failure["target"]["kind"] == "alert_delivery"


def test_a_stalled_replay_is_reported_as_an_observation_not_a_verdict(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """The 'is it hung?' question, answered without claiming to know."""
    tripl_api.jobs("prod", "scan-1", [make_replay_job(chunks_completed=8, at=tripl_api.now)])

    code = watch("--stall-after", "30", duration="90")
    captured = capsys.readouterr()

    assert code == 0
    stalled = [line for line in stream_lines(captured.out) if "job.stalled" in line]
    assert stalled, captured.out
    assert "watch has seen no progress since" in stalled[0]
    assert "chunk 8 of 18 (44.4%) collecting" in stalled[0]


def test_a_demo_project_is_excluded_unless_asked_for(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    tripl_api.projects([make_project(), make_project(slug="demo", is_demo=True)])
    tripl_api.scans("demo", [])

    watch("--json")
    captured = capsys.readouterr()

    assert documents(captured.out)[0]["data"]["projects"] == ["prod"]


def test_the_watch_command_is_registered_next_to_doctor_and_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])

    assert "watch" in capsys.readouterr().out


# --- the fatal startup read -----------------------------------------------


def test_a_scan_listing_that_fails_refuses_to_start_and_never_polls(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one read whose failure is fatal, and it is fatal BEFORE the first line.

    Without the listing watch does not know what to follow, and a feed that
    started anyway would be a feed silently missing every scan config - the exact
    shape of blindness the whole error policy exists to prevent. It has to fail
    at startup rather than halfway through, and it has to name the project: on a
    six-project run "could not list the scan configs" alone is unactionable.
    """
    tripl_api.scans("prod", {"detail": "boom"}, status=500)

    code = watch()
    captured = capsys.readouterr()

    assert code == 1
    assert "could not list the scan configs of 'prod'" in captured.err
    # It failed before polling: not one stream request was issued.
    assert not calls_to(tripl_api, tripl_api.jobs_url("prod", "scan-1"))
    assert not calls_to(tripl_api, tripl_api.signals_url("prod"))
    assert not calls_to(tripl_api, tripl_api.deliveries_url("prod"))
    # ...and not one line was printed, so nothing has to be walked back.
    assert stream_lines(captured.out) == []
    assert "watch.started" not in captured.out


def test_a_scan_listing_that_fails_for_one_of_two_projects_still_refuses(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refusing only for the broken project would start a run that is a lie.

    The operator asked to follow both; a feed that quietly covers one of them
    reads exactly like a feed that covers both and has nothing to report.
    """
    tripl_api.projects([make_project(), make_project(slug="eu")])
    tripl_api.scans("eu", {"detail": "nope"}, status=503)

    code = watch()
    captured = capsys.readouterr()

    assert code == 1
    assert "could not list the scan configs of 'eu'" in captured.err
    assert not calls_to(tripl_api, tripl_api.jobs_url("prod", "scan-1"))


# --- the full-window diagnostic -------------------------------------------


def window_of(prefix: str, api: FakeInstance) -> list[dict[str, Any]]:
    """Exactly WATCH_JOBS_LIMIT rows, so the response comes back at its limit."""
    return [make_job(job_id=f"{prefix}-{index}", at=api.now) for index in range(10)]


def test_a_full_job_window_of_entirely_new_rows_says_older_rows_may_be_hidden(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """The only shape from which unseen rows could have been pushed out.

    A full window whose every row is new means the jobs that were there last poll
    are now off the end of it, so watch cannot know what it missed. Saying so is
    doctor's scan_history_window_full precedent: state the coverage gap rather than
    let silence imply there was nothing to see.
    """
    tripl_api.each(
        tripl_api.jobs_url("prod", "scan-1"),
        [(200, window_of("a", tripl_api)), (200, window_of("b", tripl_api))],
    )

    watch("--json")
    captured = capsys.readouterr()

    degraded = [r for r in documents(captured.out) if r["event"] == "poll.degraded"]
    assert len(degraded) == 1, [r["message"] for r in degraded]
    data = degraded[0]["data"]
    assert data["window_full"] is True
    assert data["window"] == 10
    assert data["section"] == "jobs"
    assert data["target"] == "prod/scan-1"
    assert data["path"] == "/projects/prod/scans/scan-1/jobs"
    # Not a failed read: this variant carries no status and no error.
    assert data["status_code"] is None
    assert data["error"] is None
    assert data["consecutive_failures"] == 0
    assert degraded[0]["project"] == "prod"
    assert "Lower --interval" in degraded[0]["message"]


def test_a_full_job_window_sharing_one_row_with_the_last_one_is_not_reported(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """The companion, and the one that stops this being a per-tick nag.

    A healthy config with ten jobs of history returns a full window on EVERY
    poll. One surviving row proves nothing was pushed past the end, so a warning
    there would fire forever and teach the operator to ignore the line that
    matters.
    """
    first = window_of("a", tripl_api)
    overlapping = [first[-1], *window_of("b", tripl_api)[:9]]
    assert len(overlapping) == 10
    tripl_api.each(
        tripl_api.jobs_url("prod", "scan-1"),
        [(200, first), (200, overlapping)],
    )

    watch("--json")
    captured = capsys.readouterr()

    assert not [r for r in documents(captured.out) if r["event"] == "poll.degraded"]


# --- selection surfaces ----------------------------------------------------


def test_include_demo_follows_the_demo_project_too(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """The flag is published, so the behaviour behind it is a contract.

    A demo project is where an operator reproduces an incident safely, and it is
    excluded by DEFAULT - which means the only thing standing between this flag
    and silently doing nothing is a test.
    """
    tripl_api.projects([make_project(), make_project(slug="demo", is_demo=True)])
    tripl_api.scans("demo", [make_scan_config("scan-d", name="demo events")])
    tripl_api.jobs("demo", "scan-d", [])
    tripl_api.signals("demo", [])
    tripl_api.deliveries("demo", [])

    watch("--json", "--include-demo")
    captured = capsys.readouterr()

    started = documents(captured.out)[0]["data"]
    assert started["projects"] == ["prod", "demo"]
    assert calls_to(tripl_api, tripl_api.jobs_url("demo", "scan-d"))
    assert "demo (Demo)" in captured.err


def test_two_projects_are_followed_in_one_run_and_each_line_says_which(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """Multi-project is the case the whole `[slug]` prefix exists for.

    Every earlier test follows exactly one project, so nothing pinned that the
    fan-out reaches both, that the request accounting on watch.started adds them
    up, or that a line from one is attributed to it rather than to whichever
    project happened to be first.
    """
    tripl_api.projects([make_project(), make_project(slug="eu")])
    tripl_api.scans("eu", [make_scan_config("scan-eu", name="eu events")])
    tripl_api.jobs("eu", "scan-eu", [])
    tripl_api.deliveries("eu", [])
    tripl_api.each(
        tripl_api.jobs_url("prod", "scan-1"),
        [(200, [make_replay_job(chunks_completed=chunk)]) for chunk in (3, 4)],
    )
    tripl_api.each(
        tripl_api.signals_url("eu"),
        [(200, []), (200, [make_signal(scope_ref="eu")])],
    )

    watch("--json")
    captured = capsys.readouterr()

    records = documents(captured.out)
    started = records[0]["data"]
    assert started["projects"] == ["prod", "eu"]
    # One jobs read per config, and three slow streams per project on top.
    assert started["requests_per_fast_tick"] == 2
    assert started["requests_per_slow_tick"] == 8
    progress = next(r for r in records if r["event"] == "job.progress")
    opened = next(r for r in records if r["event"] == "signal.opened")
    assert progress["project"] == "prod"
    assert opened["project"] == "eu"
    assert calls_to(tripl_api, tripl_api.jobs_url("eu", "scan-eu"))
    assert "prod (Prod)" in captured.err
    assert "eu (Eu)" in captured.err


def test_a_job_cancelled_while_watching_is_reported_as_cancelled(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """The third terminal token, and the only one with no other coverage.

    A cancellation is not a failure and not a success; reporting it as either
    would send an operator hunting a cause that does not exist, or let a scan
    somebody killed pass for one that finished.
    """
    tripl_api.each(
        tripl_api.jobs_url("prod", "scan-1"),
        [
            (200, [make_job(job_id="job-1", status="running", at=tripl_api.now)]),
            (200, [make_job(job_id="job-1", status="cancelled", at=tripl_api.now)]),
        ],
    )

    code = watch("--json")
    captured = capsys.readouterr()

    assert code == 0
    cancelled = next(r for r in documents(captured.out) if r["event"] == "job.cancelled")
    assert cancelled["stream"] == "event"
    assert cancelled["data"]["status"] == "cancelled"
    assert cancelled["target"]["id"] == "job-1"
    assert "cancelled" in cancelled["message"]


# --- H1: a failed seeding read is never a zero on the first screen ---------


def test_a_seeding_read_that_fails_is_unknown_on_the_first_screen_never_zero(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """The preamble is the one place an operator reads a number as fact.

    "0 open across all scopes" and "the signals endpoint 500'd" are the two
    answers this tool exists to keep apart, and a seeding failure used to arrive
    as the first one. The JSON baseline has to agree: a null, never a 0, because
    a consumer summing these cannot tell a real zero from a fabricated one.
    """
    tripl_api.each(
        tripl_api.signals_url("prod"),
        [(500, {"detail": "boom"}), (200, [make_signal()])],
    )
    tripl_api.each(
        tripl_api.deliveries_url("prod"),
        [(500, {"detail": "boom"}), (200, {"items": [], "total": 0})],
    )

    watch("--json")
    captured = capsys.readouterr()

    signals = next(line for line in captured.err.splitlines() if line.startswith("  signals"))
    deliveries = next(line for line in captured.err.splitlines() if line.startswith("  deliveries"))
    assert "unknown - the seeding signals read failed (HTTP 500)" in signals
    assert "unknown - the seeding deliveries read failed (HTTP 500)" in deliveries
    assert "0 open across all scopes" not in captured.err
    assert "0 failed in the newest" not in captured.err

    records = documents(captured.out)
    started = next(record for record in records if record["event"] == "watch.started")
    baseline = started["data"]["baseline"]
    assert baseline["open_signals"] is None
    assert baseline["failed_deliveries"] is None
    # The jobs read succeeded, so ITS count is a real number, not a null.
    assert baseline["running_jobs"] == 0
    # The reason ships before the screen it qualifies on the JSON stream too, so
    # a consumer reading the baseline has already seen why two of it are null.
    assert [record["event"] for record in records[:3]] == [
        "poll.degraded",
        "poll.degraded",
        "watch.started",
    ]


def test_the_degraded_lines_are_printed_above_the_screen_they_qualify(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """The preamble's own pointer, `see the poll.degraded line above`, must be true.

    The preamble says exactly that, and if the diagnostics were flushed after the
    screen the operator would read `unknown` with no reason on the terminal and
    the reason three lines later, under the events.
    """
    tripl_api.signals("prod", {"detail": "boom"}, status=500)

    watch()
    captured = capsys.readouterr()

    printed = captured.out.splitlines()
    # Matched on the token COLUMN, not as a substring: the preamble line itself
    # says "see the poll.degraded line above", which is the claim under test.
    degraded = [
        index
        for index, line in enumerate(printed)
        if TIMESTAMPED.match(line) and line.split()[1] == "poll.degraded"
    ]
    screen = next(index for index, line in enumerate(printed) if line.startswith("  signals"))
    started = next(index for index, line in enumerate(printed) if " watch.started " in line)
    assert degraded, captured.out
    assert "see the poll.degraded line above" in printed[screen]
    assert degraded[0] < screen < started, captured.out
    # The later ticks' repeats belong under the screen, where the stream is.
    assert all(index > started for index in degraded[1:]), captured.out


def test_a_seeding_jobs_read_that_fails_does_not_print_running_none(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """`running    none` for a jobs read that never happened is the same lie.

    It is the worse one, in fact: "no jobs are running" is what an operator
    checks before restarting a worker.
    """
    tripl_api.each(
        tripl_api.jobs_url("prod", "scan-1"),
        [(500, {"detail": "boom"}), (200, [make_replay_job(chunks_completed=3)])],
    )

    watch("--json")
    captured = capsys.readouterr()

    assert "running    none" not in captured.err
    assert "pending    none" not in captured.err
    running = next(line for line in captured.err.splitlines() if line.startswith("  running"))
    assert "unknown - the seeding jobs read failed (HTTP 500)" in running

    started = next(r for r in documents(captured.out) if r["event"] == "watch.started")
    baseline = started["data"]["baseline"]
    assert baseline["running_jobs"] is None
    assert baseline["pending_jobs"] is None


# --- H2: no stall is fabricated for a stream watch cannot see --------------


def test_no_stall_is_reported_while_the_jobs_read_is_failing(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two contradictory lines, the second of them invented.

    The failed read holds the LAST GOOD rows in the snapshot - that is what turns
    an outage into a reporting delay - so a stall tracker that treats them as a
    fresh observation accrues seconds nobody watched, and then prints "unchanged
    for 2m" in the same run as "jobs read failed: HTTP 502". Every second this
    tracker reports must be a second watch was actually looking.
    """
    running = make_replay_job(status="running", chunks_completed=8, at=tripl_api.now)
    tripl_api.each(
        tripl_api.jobs_url("prod", "scan-1"),
        [(200, [running]), (502, {"detail": "gone"})],
    )

    code = watch("--stall-after", "30", duration="120")
    captured = capsys.readouterr()

    emitted = tokens_of(captured.out)
    assert code == 0
    assert "job.stalled" not in emitted, captured.out
    assert "job.unchanged" not in emitted, captured.out
    # The run kept going and kept saying so: 12 failed polls, reported on the
    # 1st, 2nd, 4th and 8th.
    assert emitted.count("poll.degraded") == 4


def test_the_stall_clock_restarts_after_the_stream_comes_back(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """A full threshold AFTER recovery, not after the last sighting before it.

    Resuming the clock would fire the moment the stream returned and attribute
    the blind stretch to the job. Restarting it costs one late report and buys a
    line that is true.
    """
    running = make_replay_job(status="running", chunks_completed=8, at=tripl_api.now)
    tripl_api.each(
        tripl_api.jobs_url("prod", "scan-1"),
        [
            (200, [running]),
            (502, {"detail": "gone"}),
            (502, {"detail": "gone"}),
            (200, [running]),
        ],
    )

    watch("--json", "--stall-after", "30", duration="60")
    captured = capsys.readouterr()

    records = documents(captured.out)
    stalled = [r for r in records if r["event"] == "job.stalled"]
    assert len(stalled) == 1, [r["message"] for r in stalled]
    # 30s of ACTUAL watching after the read recovered at t=30, not the 60s that
    # elapsed since the job was first seen.
    assert stalled[0]["data"]["unchanged_seconds"] == 30
    assert [r["event"] for r in records].index("poll.recovered") < [
        r["event"] for r in records
    ].index("job.stalled")
