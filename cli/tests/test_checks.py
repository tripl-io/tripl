"""The check rules, as arithmetic. No HTTP, no respx, no clock.

Every test here builds a Snapshot by hand and asserts on finding CODES and
EVIDENCE. Nothing asserts on prose — ``title``, ``summary`` and ``message`` are
explicitly outside the stability contract.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

import pytest

from tests.conftest import (
    make_data_source,
    make_drift,
    make_event_type,
    make_job,
    make_project,
    make_scan_config,
)
from tripl_cli.diagnostics.checks import (
    check_data_sources,
    check_drifts,
    check_projects,
    run_checks,
)
from tripl_cli.diagnostics.scan_checks import (
    GENERIC_SCAN_ERROR,
    backoff_delay_seconds,
    check_scans,
    streaks_by_config,
)
from tripl_cli.model import (
    JOBS_WINDOW,
    SCOPE_PROJECT,
    Check,
    DriftCoverage,
    Fetched,
    Instance,
    ProjectSelection,
    Report,
    Severity,
    Snapshot,
    exit_code_for,
)

SLUG = "prod"
HOUR = 3600


def build_snapshot(
    now: datetime,
    *,
    configs: Sequence[dict[str, Any]] = (),
    jobs: Mapping[str, Any] | None = None,
    data_sources: Fetched[Any] | None = None,
    event_types: Sequence[dict[str, Any]] = (),
    drifts: Mapping[tuple[str, str], Fetched[Any]] | None = None,
    projects: Sequence[dict[str, Any]] | None = None,
    requested_slugs: Sequence[str] = (),
    **kwargs: Any,
) -> Snapshot:
    project_rows = tuple(projects) if projects is not None else (make_project(),)
    job_map = {}
    for config_id, rows in (jobs or {}).items():
        job_map[(SLUG, config_id)] = (
            rows if isinstance(rows, Fetched) else Fetched(value=list(rows), status_code=200)
        )
    return Snapshot(
        now=now,
        selection=ProjectSelection(
            projects=project_rows,
            requested_slugs=tuple(requested_slugs),
            by_slug={
                slug: Fetched(value=row, status_code=200)
                for slug, row in zip(requested_slugs, project_rows, strict=False)
            },
        ),
        scans={SLUG: Fetched(value=list(configs), status_code=200)},
        jobs=job_map,
        data_sources=data_sources,
        event_types={SLUG: Fetched(value=list(event_types), status_code=200)},
        drifts=dict(drifts or {}),
        drift_coverage=kwargs.pop(
            "drift_coverage",
            {SLUG: DriftCoverage(examined=len(drifts or {}), total=len(event_types))},
        ),
        **kwargs,
    )


def scan_check(snapshot: Snapshot) -> Check:
    return check_scans(snapshot, streaks_by_config(snapshot))


def codes(check: Check) -> list[str]:
    return [finding.code for finding in check.findings]


def finding_for(check: Check, code: str) -> Any:
    matches = [finding for finding in check.findings if finding.code == code]
    assert matches, f"expected a {code} finding, got {codes(check)}"
    return matches[0]


def failing_jobs(now: datetime, count: int, *, error: str | None = None) -> list[dict[str, Any]]:
    """``count`` failed dispatcher jobs, newest first, one hour apart."""
    return [
        make_job(
            job_id=f"job-{index}",
            status="failed",
            at=now - timedelta(hours=index + 1),
            error_message=error,
        )
        for index in range(count)
    ]


# --------------------------------------------------------------------------
# scans
# --------------------------------------------------------------------------


def test_failing_scan_config_reports_its_streak_and_the_job_id_to_grep(now: datetime) -> None:
    check = scan_check(
        build_snapshot(
            now,
            configs=[make_scan_config()],
            jobs={"scan-1": failing_jobs(now, 7, error=GENERIC_SCAN_ERROR)},
        )
    )

    assert check.status is Severity.FAIL
    finding = finding_for(check, "scan_config_failing")
    assert finding.evidence["consecutive_failures"] == 7
    assert finding.evidence["last_failed_job_id"] == "job-0"
    assert finding.evidence["interval"] == "1h"
    assert finding.evidence["last_success_at"] is None


def test_manual_replay_and_demo_jobs_neither_count_nor_clear_a_streak(now: datetime) -> None:
    """The single most likely bug in the port of _consecutive_failure_streak.

    `scan_jobs` is shared with manual scans, replays and the demo runtime tick.
    A row without the dispatcher's `mode` stamp must be skipped entirely — not
    counted as a failure, and not treated as the success that ends the streak.
    """
    interleaved = [
        make_job(
            job_id="d-0", status="failed", at=now - timedelta(minutes=10), result_summary=None
        ),
        make_job(job_id="f-0", status="failed", at=now - timedelta(hours=1)),
        make_job(
            job_id="d-1",
            status="completed",
            at=now - timedelta(hours=2),
            result_summary={"demo_runtime_tick": True},
        ),
        make_job(job_id="f-1", status="failed", at=now - timedelta(hours=3)),
        make_job(
            job_id="r-0",
            status="completed",
            at=now - timedelta(hours=4),
            result_summary={"mode": "metrics_replay"},
        ),
        make_job(job_id="f-2", status="failed", at=now - timedelta(hours=5)),
    ]

    check = scan_check(
        build_snapshot(now, configs=[make_scan_config()], jobs={"scan-1": interleaved})
    )

    finding = finding_for(check, "scan_config_failing")
    assert finding.evidence["consecutive_failures"] == 3
    assert finding.evidence["last_failed_job_id"] == "f-0"


def test_a_successful_dispatcher_job_ends_the_streak(now: datetime) -> None:
    jobs = [
        make_job(job_id="ok", status="completed", at=now, time_to=now),
        *failing_jobs(now, 5),
    ]

    check = scan_check(build_snapshot(now, configs=[make_scan_config()], jobs={"scan-1": jobs}))

    assert "scan_config_failing" not in codes(check)
    assert check.status is Severity.PASS


@pytest.mark.parametrize(
    ("interval_seconds", "streak", "expected"),
    [
        (HOUR, 2, None),  # below FAILURE_BACKOFF_AFTER
        (HOUR, 3, HOUR),  # first backoff is one interval
        (HOUR, 5, 4 * HOUR),
        (900, 6, 7200),  # 15m tops out at 2h (8-interval cap)
        (604800, 6, 604800),  # 1w: the 24h ceiling is floored back to one interval
        (86400, 4, 86400),  # 1d: 2 x 1d clipped by the 24h ceiling
    ],
)
def test_backoff_delay_matches_the_backend_formula(
    interval_seconds: int, streak: int, expected: int | None
) -> None:
    """Pins the SECOND copy of schedule.py's FAILURE_BACKOFF_* constants."""
    assert backoff_delay_seconds(streak, interval_seconds) == expected


def test_backoff_is_warn_not_fail(now: datetime) -> None:
    """Correct scheduler behaviour must never be indistinguishable from a fault."""
    check = scan_check(
        build_snapshot(now, configs=[make_scan_config()], jobs={"scan-1": failing_jobs(now, 6)})
    )

    backoff = finding_for(check, "scan_backoff_active")
    assert backoff.severity is Severity.WARN
    assert backoff.evidence["backoff_after"] == 3
    assert backoff.evidence["deferred_by_seconds_estimate"] == 8 * HOUR
    assert backoff.evidence["next_attempt_not_before_estimate"] is not None


def test_streak_of_two_reports_failing_without_a_backoff_finding(now: datetime) -> None:
    check = scan_check(
        build_snapshot(now, configs=[make_scan_config()], jobs={"scan-1": failing_jobs(now, 2)})
    )

    assert "scan_config_failing" in codes(check)
    assert "scan_backoff_active" not in codes(check)


def test_generic_error_message_is_labelled_as_a_fallback_not_a_cause(now: datetime) -> None:
    check = scan_check(
        build_snapshot(
            now,
            configs=[make_scan_config()],
            jobs={"scan-1": failing_jobs(now, 4, error=GENERIC_SCAN_ERROR)},
        )
    )

    finding = finding_for(check, "scan_config_failing")
    assert finding.evidence["last_error_is_generic"] is True
    assert finding.evidence["last_error"] == GENERIC_SCAN_ERROR
    # The job id is the only handle onto the real cause, so it has to be quoted.
    assert finding.evidence["last_failed_job_id"] in finding.message


def test_curated_error_message_is_reported_verbatim(now: datetime) -> None:
    curated = "Scan failed: could not connect to the data source."
    check = scan_check(
        build_snapshot(
            now, configs=[make_scan_config()], jobs={"scan-1": failing_jobs(now, 3, error=curated)}
        )
    )

    finding = finding_for(check, "scan_config_failing")
    assert finding.evidence["last_error"] == curated
    assert finding.evidence["last_error_is_generic"] is False


@pytest.mark.parametrize(
    ("behind", "expected"),
    [(timedelta(hours=62), True), (timedelta(minutes=40), False)],
)
def test_watermark_stale_fires_only_when_time_to_is_more_than_three_intervals_old(
    now: datetime, behind: timedelta, expected: bool
) -> None:
    """The green/red-blind case the incident actually hit: succeeding, but empty."""
    jobs = [make_job(job_id="ok", status="completed", at=now, time_to=now - behind)]

    check = scan_check(build_snapshot(now, configs=[make_scan_config()], jobs={"scan-1": jobs}))

    assert ("scan_watermark_stale" in codes(check)) is expected


def test_config_with_no_interval_is_never_judged(now: datetime) -> None:
    """Never dispatched BY DESIGN — a manufactured fault is worse than none."""
    ancient = [make_job(job_id="old", status="failed", at=now - timedelta(days=90))]

    check = scan_check(
        build_snapshot(
            now,
            configs=[make_scan_config(interval=None, created_at=now - timedelta(days=90))],
            jobs={"scan-1": ancient},
        )
    )

    assert check.findings == ()
    assert check.status is Severity.PASS


def test_config_with_an_interval_but_no_time_column_is_reported_as_never_dispatchable(
    now: datetime,
) -> None:
    """Mirrors schedule.py's `ScanConfig.time_column.isnot(None)` predicate.

    The dispatcher's query never selects such a config, so it produces no jobs,
    no error and no signal anywhere else in the product.
    """
    check = scan_check(build_snapshot(now, configs=[make_scan_config(time_column=None)], jobs={}))

    finding = finding_for(check, "scan_config_not_dispatchable")
    assert finding.severity is Severity.FAIL
    assert finding.evidence["interval"] == "1h"


def test_an_interval_this_cli_cannot_convert_is_reported_instead_of_judged(now: datetime) -> None:
    """A backend that grows a sixth ScanInterval must not be judged with a wrong
    denominator: every staleness and backoff rule here is a multiple of the config's
    own interval, so an unknown one has to yield this warning and no verdict at all.
    """
    idle = [make_job(at=now - timedelta(hours=9), time_to=now - timedelta(hours=9))]

    check = scan_check(
        build_snapshot(now, configs=[make_scan_config(interval="30m")], jobs={"scan-1": idle})
    )

    # A history that a known 1h interval would call stale, and nothing says so.
    assert codes(check) == ["scan_interval_unknown"]
    finding = finding_for(check, "scan_interval_unknown")
    assert finding.severity is Severity.WARN
    assert finding.evidence == {"interval": "30m"}
    assert check.status is Severity.WARN


@pytest.mark.parametrize(
    ("age", "expected"),
    [(timedelta(minutes=10), False), (timedelta(days=3), True)],
)
def test_never_collected_waits_two_intervals_before_firing(
    now: datetime, age: timedelta, expected: bool
) -> None:
    check = scan_check(
        build_snapshot(now, configs=[make_scan_config(created_at=now - age)], jobs={"scan-1": []})
    )

    assert ("scan_never_collected" in codes(check)) is expected


def test_not_dispatched_fires_on_an_idle_config(now: datetime) -> None:
    jobs = [make_job(job_id="old", status="completed", at=now - timedelta(hours=9), time_to=now)]

    check = scan_check(build_snapshot(now, configs=[make_scan_config()], jobs={"scan-1": jobs}))

    assert "scan_not_dispatched" in codes(check)


def test_not_dispatched_is_suppressed_when_the_config_is_already_failing(now: datetime) -> None:
    """One root cause, one finding. A failing config is trivially also idle."""
    jobs = [
        make_job(job_id=f"f-{i}", status="failed", at=now - timedelta(hours=9 + i))
        for i in range(4)
    ]

    check = scan_check(build_snapshot(now, configs=[make_scan_config()], jobs={"scan-1": jobs}))

    assert "scan_config_failing" in codes(check)
    assert "scan_not_dispatched" not in codes(check)


def test_a_non_200_jobs_response_is_a_failure_not_an_empty_history(now: datetime) -> None:
    check = scan_check(
        build_snapshot(
            now,
            configs=[make_scan_config()],
            jobs={"scan-1": Fetched(value=None, status_code=500, error="tripl API error (500)")},
        )
    )

    finding = finding_for(check, "endpoint_unexpected_status")
    assert finding.evidence["status_code"] == 500
    assert check.status is Severity.FAIL


# --------------------------------------------------------------------------
# drifts
# --------------------------------------------------------------------------


def drift_snapshot(now: datetime, drifts: Sequence[dict[str, Any]], **kwargs: Any) -> Snapshot:
    return build_snapshot(
        now,
        event_types=[make_event_type()],
        drifts={(SLUG, "et-1"): Fetched(value={"items": list(drifts)}, status_code=200)},
        **kwargs,
    )


def test_a_non_200_drifts_response_is_a_failure_not_an_empty_list(now: datetime) -> None:
    """TRAP 3. /projects/{slug}/schema-drifts does not exist; a 404 is not "none"."""
    snapshot = build_snapshot(
        now,
        event_types=[make_event_type()],
        drifts={(SLUG, "et-1"): Fetched(value=None, status_code=404, error="Not found (404)")},
    )

    check = check_drifts(snapshot)

    assert check.status is Severity.FAIL
    assert finding_for(check, "endpoint_unexpected_status").evidence["status_code"] == 404


def test_accepted_missing_field_drift_is_reported_as_a_deleted_field(now: datetime) -> None:
    """Accepting a missing_field drift calls session.delete(field) server-side."""
    resolved = now - timedelta(days=1)
    check = check_drifts(
        drift_snapshot(
            now,
            [
                make_drift(
                    status="accepted",
                    drift_type="missing_field",
                    resolved_at=resolved,
                    resolved_by="3f2a0000-0000-0000-0000-000000000001",
                )
            ],
        )
    )

    finding = finding_for(check, "schema_field_deleted_by_accept")
    assert finding.severity is Severity.WARN
    assert finding.evidence["field_name"] == "user_id"
    assert finding.evidence["resolved_by"] == "3f2a0000-0000-0000-0000-000000000001"
    assert finding.evidence["resolved_at"] is not None


def test_an_accepted_drift_with_no_resolver_drops_the_actor_clause(now: datetime) -> None:
    """resolved_by is a UUID and may be null; it must never be invented."""
    check = check_drifts(
        drift_snapshot(
            now, [make_drift(status="accepted", drift_type="missing_field", resolved_by=None)]
        )
    )

    finding = finding_for(check, "schema_field_deleted_by_accept")
    assert finding.evidence["resolved_by"] is None
    assert "None" not in finding.message


@pytest.mark.parametrize(
    ("offset", "expected"),
    [(timedelta(days=-1), True), (timedelta(days=1), False)],
)
def test_snoozed_drift_past_its_snooze_counts_as_open_and_a_future_one_does_not(
    now: datetime, offset: timedelta, expected: bool
) -> None:
    check = check_drifts(
        drift_snapshot(now, [make_drift(status="snoozed", snoozed_until=now + offset)])
    )

    assert ("schema_drift_open" in codes(check)) is expected


def test_a_truncated_drift_scan_can_never_report_pass(now: datetime) -> None:
    """A truncated PASS is the lie that invalidated the 2026-07-30 audit."""
    snapshot = build_snapshot(
        now,
        event_types=[make_event_type()],
        drifts={(SLUG, "et-1"): Fetched(value={"items": []}, status_code=200)},
        drift_coverage={SLUG: DriftCoverage(examined=200, total=340)},
    )

    check = check_drifts(snapshot)

    assert check.status is Severity.WARN
    finding = finding_for(check, "drift_scan_truncated")
    assert finding.project == SLUG
    assert finding.evidence == {"project": SLUG, "examined": 200, "total": 340}


# --------------------------------------------------------------------------
# data sources
# --------------------------------------------------------------------------


def test_project_scoped_key_skips_data_sources_and_the_skip_is_not_a_pass(now: datetime) -> None:
    """TRAP 5: GET /data-sources 403s for a project-scoped key by design."""
    snapshot = build_snapshot(
        now,
        configs=[make_scan_config()],
        jobs={"scan-1": [make_job(at=now, time_to=now)]},
        api_key_scope=SCOPE_PROJECT,
        auth=Fetched(value=None, status_code=403, error="Forbidden (403)"),
        requested_slugs=[SLUG],
    )

    checks = run_checks(snapshot)
    data_sources = next(check for check in checks if check.id == "data_sources")
    report = Report(
        command="doctor",
        instance=Instance("http://x", "$TRIPL_BASE_URL", "$TRIPL_API_KEY", SCOPE_PROJECT),
        generated_at=now,
        duration_ms=0,
        requests=0,
        checks=tuple(checks),
    )

    assert data_sources.status is Severity.SKIP
    assert data_sources.skip_reason is not None
    assert "403" in data_sources.skip_reason
    assert report.counts["skip"] == 1
    assert report.counts["pass"] == 5


def test_redacted_probe_message_is_reported_as_owner_only_not_as_none(now: datetime) -> None:
    """`data_sources._redact_connection` blanks last_test_message below owner."""
    snapshot = build_snapshot(
        now,
        configs=[make_scan_config()],
        data_sources=Fetched(
            value=[
                make_data_source(
                    last_test_status="failed", last_test_at=now, last_test_message=None
                )
            ],
            status_code=200,
        ),
    )

    check = check_data_sources(snapshot, streaks_by_config(snapshot))

    finding = finding_for(check, "data_source_probe_failed")
    assert finding.evidence["message_redacted"] is True
    assert finding.evidence["last_test_message"] is None
    assert "None" not in finding.message
    assert "owner" in finding.message


def test_failed_probe_quotes_its_message_when_the_key_can_see_it(now: datetime) -> None:
    text = "Scan failed: could not connect to the data source."
    snapshot = build_snapshot(
        now,
        configs=[make_scan_config()],
        data_sources=Fetched(
            value=[
                make_data_source(
                    last_test_status="failed", last_test_at=now, last_test_message=text
                )
            ],
            status_code=200,
        ),
    )

    check = check_data_sources(snapshot, streaks_by_config(snapshot))

    finding = finding_for(check, "data_source_probe_failed")
    assert finding.evidence["last_test_message"] == text
    assert finding.evidence["message_redacted"] is False
    assert finding.evidence["scan_config_ids"] == ["scan-1"]


def test_stale_success_probe_is_a_warning_that_denies_being_evidence_of_health(
    now: datetime,
) -> None:
    """A green probe that predates the failures is not evidence of anything."""
    snapshot = build_snapshot(
        now,
        configs=[make_scan_config()],
        jobs={"scan-1": failing_jobs(now, 3)},
        data_sources=Fetched(
            value=[make_data_source(last_test_at=now - timedelta(days=6))], status_code=200
        ),
    )

    check = check_data_sources(snapshot, streaks_by_config(snapshot))

    finding = finding_for(check, "data_source_probe_stale")
    assert finding.severity is Severity.WARN
    assert finding.evidence["last_test_status"] == "success"
    assert finding.evidence["streak_started_at"] is not None


def test_a_green_probe_taken_after_the_failures_started_is_not_flagged(now: datetime) -> None:
    snapshot = build_snapshot(
        now,
        configs=[make_scan_config()],
        jobs={"scan-1": failing_jobs(now, 3)},
        data_sources=Fetched(value=[make_data_source(last_test_at=now)], status_code=200),
    )

    check = check_data_sources(snapshot, streaks_by_config(snapshot))

    assert check.findings == ()
    assert check.status is Severity.PASS


def test_a_non_200_data_sources_response_skips_rather_than_passing(now: datetime) -> None:
    snapshot = build_snapshot(
        now,
        configs=[make_scan_config()],
        data_sources=Fetched(value=None, status_code=500, error="tripl API error (500)"),
    )

    check = check_data_sources(snapshot, streaks_by_config(snapshot))

    assert check.status is Severity.SKIP
    assert check.skip_reason is not None
    assert "500" in check.skip_reason


# --------------------------------------------------------------------------
# projects
# --------------------------------------------------------------------------


def test_a_project_that_failed_to_provision_is_a_failure_that_carries_the_reason(
    now: datetime,
) -> None:
    """Every count doctor reports about a half-provisioned project is unreliable.

    The stage it stopped at and the recorded error are the whole diagnosis, so
    they have to survive into the evidence rather than only into the prose.
    """
    check = check_projects(
        build_snapshot(
            now,
            projects=[
                make_project(
                    generation_status="failed",
                    generation_stage="events",
                    generation_error="seeding the event table raised",
                )
            ],
        )
    )

    assert check.status is Severity.FAIL
    finding = finding_for(check, "project_generation_failed")
    assert finding.severity is Severity.FAIL
    assert finding.project == SLUG
    assert finding.target is not None and finding.target.kind == "project"
    assert finding.evidence == {
        "generation_status": "failed",
        "generation_stage": "events",
        "generation_error": "seeding the event table raised",
    }


@pytest.mark.parametrize("status", ["pending", "seeding"])
def test_a_project_still_provisioning_warns_rather_than_failing(now: datetime, status: str) -> None:
    """Both in-progress statuses, because the rule is a membership test.

    Dropping either one reports a project whose data is still arriving as healthy,
    and provisioning is not a fault - so it must not turn a default run red.
    """
    check = check_projects(
        build_snapshot(
            now, projects=[make_project(generation_status=status, generation_stage="events")]
        )
    )

    assert check.status is Severity.WARN
    finding = finding_for(check, "project_generating")
    assert finding.severity is Severity.WARN
    assert finding.evidence == {"generation_status": status, "generation_stage": "events"}


# --------------------------------------------------------------------------
# verdict and exit codes
# --------------------------------------------------------------------------


def make_report(now: datetime, *statuses: Severity) -> Report:
    checks = tuple(
        Check(id=f"c{index}", title="t", status=status, summary="s")
        for index, status in enumerate(statuses)
    )
    return Report(
        command="doctor",
        instance=Instance("http://x", "$TRIPL_BASE_URL", "$TRIPL_API_KEY", "instance"),
        generated_at=now,
        duration_ms=1,
        requests=1,
        checks=checks,
    )


def test_exit_code_is_three_on_any_fail_and_zero_when_only_warnings(now: datetime) -> None:
    """The default cron contract: warnings must not turn the job red."""
    assert exit_code_for(make_report(now, Severity.PASS, Severity.FAIL)) == 3
    assert exit_code_for(make_report(now, Severity.PASS, Severity.WARN)) == 0
    assert exit_code_for(make_report(now, Severity.PASS, Severity.PASS)) == 0


def test_strict_promotes_warn_to_three_but_never_promotes_skip(now: datetime) -> None:
    warned = make_report(now, Severity.PASS, Severity.WARN)
    skipped = make_report(now, Severity.PASS, Severity.SKIP)

    assert exit_code_for(warned, strict=True) == 3
    # A project-scoped key would otherwise fail every strict run forever, for a
    # reason the operator cannot fix.
    assert exit_code_for(skipped, strict=True) == 0


def test_a_run_in_which_no_check_reached_a_verdict_is_a_failure(now: datetime) -> None:
    """ "0 failures" out of zero verdicts is the most dangerous green in the design."""
    report = make_report(now, Severity.SKIP, Severity.SKIP, Severity.SKIP)

    assert report.status is Severity.FAIL
    assert exit_code_for(report) == 3


# --------------------------------------------------------------------------
# window saturation and the demo cooldown
# --------------------------------------------------------------------------


def test_a_streak_filling_the_whole_window_is_reported_as_a_lower_bound(
    now: datetime,
) -> None:
    """The window is history we asked for, not history that exists.

    A 15m config failing for days fills all JOBS_WINDOW rows. Reporting that as
    "200 failures since this morning" misdates the incident by however long the
    real run is, and "when did this start" is the question doctor exists for.
    """
    check = scan_check(
        build_snapshot(
            now,
            configs=[make_scan_config()],
            jobs={"scan-1": failing_jobs(now, JOBS_WINDOW)},
        )
    )

    finding = finding_for(check, "scan_config_failing")
    assert finding.evidence["consecutive_failures_truncated"] is True
    assert finding.evidence["jobs_window"] == JOBS_WINDOW
    assert "at least" in finding.message


def test_a_terminated_streak_is_not_reported_as_truncated(now: datetime) -> None:
    """The counterpart: a run that a success ends is a measurement, not a bound."""
    jobs = [
        *failing_jobs(now, 4),
        make_job(job_id="job-ok", status="completed", at=now - timedelta(hours=9)),
    ]

    check = scan_check(build_snapshot(now, configs=[make_scan_config()], jobs={"scan-1": jobs}))

    finding = finding_for(check, "scan_config_failing")
    assert finding.evidence["consecutive_failures"] == 4
    assert finding.evidence["consecutive_failures_truncated"] is False
    assert "at least" not in finding.message


def test_a_full_window_of_manual_jobs_does_not_claim_the_scheduler_is_dead(
    now: datetime,
) -> None:
    """`scan_never_collected` is a claim about ALL history; we hold one window.

    run_scan, replay_metrics and apply_event_groups all write ScanJobs with no
    mode stamp, so a busy config can bury its own scheduled history. Sending the
    operator after a healthy beat scheduler is worse than admitting the window
    was not enough.
    """
    manual = [
        make_job(job_id=f"manual-{index}", at=now - timedelta(minutes=index + 1), result_summary={})
        for index in range(JOBS_WINDOW)
    ]

    check = scan_check(build_snapshot(now, configs=[make_scan_config()], jobs={"scan-1": manual}))

    assert codes(check) == ["scan_history_window_full"]
    assert check.status is Severity.WARN
    assert finding_for(check, "scan_history_window_full").evidence["jobs_window"] == JOBS_WINDOW


def test_a_short_window_of_manual_jobs_still_reports_never_collected(now: datetime) -> None:
    """The FAIL survives where it is still provable: the window was not full."""
    manual = [make_job(job_id="manual-1", at=now - timedelta(minutes=5), result_summary={})]

    check = scan_check(
        build_snapshot(
            now,
            configs=[make_scan_config(created_at=now - timedelta(days=3))],
            jobs={"scan-1": manual},
        )
    )

    assert codes(check) == ["scan_never_collected"]
    assert check.status is Severity.FAIL


def test_a_demo_inside_its_collection_cooldown_is_not_reported_as_stale(now: datetime) -> None:
    """schedule.py skips a demo's dispatch for DEMO_COLLECTION_COOLDOWN_HOURS = 6.

    The demo recipe builds its scan config with interval "1h", so the plain
    3-interval staleness rule calls a perfectly healthy demo broken for half of
    every cooldown - on the one project every new operator opens first.
    """
    idle_but_normal = [make_job(at=now - timedelta(hours=4), time_to=now - timedelta(hours=4))]

    check = scan_check(
        build_snapshot(
            now,
            projects=[make_project(is_demo=True)],
            configs=[make_scan_config()],
            jobs={"scan-1": idle_but_normal},
        )
    )

    assert codes(check) == []
    assert check.status is Severity.PASS


def test_a_real_project_idle_for_four_hours_is_still_reported(now: datetime) -> None:
    """The allowance is for demos only; the same silence on prod is a fault."""
    idle = [make_job(at=now - timedelta(hours=4), time_to=now - timedelta(hours=4))]

    check = scan_check(build_snapshot(now, configs=[make_scan_config()], jobs={"scan-1": idle}))

    assert "scan_not_dispatched" in codes(check)
    assert check.status is Severity.FAIL


def test_a_demo_silent_past_its_cooldown_is_not_reported(now: datetime) -> None:
    """A demo nobody opens stops collecting on purpose.

    The scheduler's pause gate stops a demo's dispatcher entirely, and no field
    the API exposes tells that pause from a dead scheduler, so a demo's silence
    is not judged at all. Its failures still are.
    """
    long_idle = [make_job(at=now - timedelta(hours=72), time_to=now - timedelta(hours=72))]

    check = scan_check(
        build_snapshot(
            now,
            projects=[make_project(is_demo=True)],
            configs=[make_scan_config()],
            jobs={"scan-1": long_idle},
        )
    )

    assert codes(check) == []
    assert check.status is Severity.PASS


def test_an_unknown_interval_does_not_hide_a_config_that_is_failing(now: datetime) -> None:
    """ "Upgrade the CLI" must not be the only thing said about a dead config.

    Staleness and the backoff estimate need the interval as a denominator; "is it
    failing" does not. Returning early on the whole config would mean a backend
    that grows a sixth ScanInterval member blinds doctor to outright failures.
    """
    check = scan_check(
        build_snapshot(
            now,
            configs=[make_scan_config(interval="30m")],
            jobs={"scan-1": failing_jobs(now, 5, error=GENERIC_SCAN_ERROR)},
        )
    )

    assert sorted(codes(check)) == ["scan_config_failing", "scan_interval_unknown"]
    assert check.status is Severity.FAIL
    failing = finding_for(check, "scan_config_failing")
    assert failing.evidence["consecutive_failures"] == 5
    # The two the denominator is genuinely required for stay absent.
    assert "scan_backoff_active" not in codes(check)
    assert "scan_not_dispatched" not in codes(check)
