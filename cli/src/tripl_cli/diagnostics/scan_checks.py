"""The scans check: why scheduled metrics collection is not producing data.

This is the centre of gravity of ``tripl doctor``. The 2026-07-28..31 incident
was a scan config failing for four days behind a generic "Scan failed due to an
internal error", with the retry backoff reading to the operator as a hang. Every
rule here exists to make one of those four days unnecessary.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from tripl_cli.api import scans as scans_api
from tripl_cli.model import (
    INTERVAL_SECONDS,
    JOBS_WINDOW,
    Check,
    Finding,
    JsonDict,
    JsonList,
    Severity,
    Snapshot,
    Target,
    as_dict,
    format_duration,
    parse_time,
    text_of,
    to_rfc3339,
    worst,
)

CHECK_ID = "scans"
CHECK_TITLE = "Scheduled metrics collection"

# backend/src/tripl/worker/tasks/metrics/tasks.py::METRICS_COLLECTION_MODE. The
# dispatcher stamps this into result_summary AT JOB CREATION (schedule.py:360),
# so a failure that died before it could write anything else still carries the
# mark. `scan_jobs` is shared with manual catalog scans, metrics replays
# ("metrics_replay"), event-group applies and the demo runtime tick
# ({"demo_runtime_tick": true}) — identify positively, never by exclusion.
DISPATCHER_MODE = "metrics_collection"

# Mirrors backend/src/tripl/worker/tasks/metrics/schedule.py:
#   FAILURE_BACKOFF_AFTER = 3, FAILURE_BACKOFF_MAX_INTERVALS = 8,
#   FAILURE_BACKOFF_CEILING = timedelta(hours=24), _failure_backoff_delay().
# This is a SECOND copy of those constants and it will eventually drift from the
# backend's. It is defused three ways rather than pretended away: the JSON key
# says _estimate, the printed sentence says "about", and backoff_after ships as
# evidence so the reader can see the rule the number came from.
FAILURE_BACKOFF_AFTER = 3
FAILURE_BACKOFF_MAX_INTERVALS = 8
FAILURE_BACKOFF_CEILING_SECONDS = 24 * 3600

# The backend's own generic fallback (worker/tasks/_errors.py::user_facing_error).
# Anything that is not an author-curated ScanError collapses to this string, so
# quoting it as "the cause" is precisely the wall the incident hit for four days.
GENERIC_SCAN_ERROR = "Scan failed due to an internal error."

# The dispatcher beat runs every 300 s and buckets settle, so one missed tick is
# noise. Three intervals is the point at which a human should look; a check that
# cries wolf gets muted, and then it is not there for the run that mattered.
STALE_INTERVALS = 3
NEVER_COLLECTED_INTERVALS = 2

# A DEMO project's scheduled collection is NOT judged for staleness at all.
# The scheduler throttles a demo to one collection per
# cooldown and stops collecting entirely while nobody has opened the demo
# (backend/src/tripl/worker/tasks/_demo_pause.py), and nothing the API exposes
# tells a deliberately paused demo from a dead beat scheduler. A doctor that
# reported FAIL on every idle demo - the project every new operator opens
# first - would be a doctor nobody keeps running. A demo's FAILING runs are
# still reported: a paused demo creates no jobs, so it cannot fail.


@dataclass(frozen=True)
class FailureStreak:
    """The run of failed dispatcher jobs at the head of a config's history."""

    length: int
    last_failed_at: datetime | None = None
    last_failed_job_id: str | None = None
    last_error: str | None = None
    # CLI-only, not part of the backend's port: the OLDEST failure in the streak,
    # which is what the data-sources check compares a green connection probe
    # against to decide whether that probe is evidence of anything.
    first_failed_at: datetime | None = None
    # The walk reached the end of the fetched window without meeting a job that
    # ENDS the streak, so `length` and `first_failed_at` are lower bounds, not
    # measurements. Also CLI-only: the backend never needs this because it only
    # wants the delay, which saturates long before the window does.
    saturated: bool = False


def dispatcher_jobs(jobs: Sequence[JsonDict]) -> list[JsonDict]:
    """Only the jobs this instance's metrics dispatcher created."""
    return [
        job for job in jobs if as_dict(job.get("result_summary")).get("mode") == DISPATCHER_MODE
    ]


def consecutive_failure_streak(jobs: Sequence[JsonDict]) -> FailureStreak:
    """Port of ``schedule.py::_consecutive_failure_streak``.

    ``jobs`` arrive newest-first, as ``GET .../jobs`` returns them
    ("Newest jobs first, capped."). A manual scan, a replay or a demo tick is
    neither counted as a failure nor treated as the success that ends a streak,
    so a user pressing "Run now" can neither trigger the backoff nor clear it.
    """
    length = 0
    last_failed_at: datetime | None = None
    last_failed_job_id: str | None = None
    last_error: str | None = None
    first_failed_at: datetime | None = None
    # Set unless the loop is STOPPED by a dispatcher job that is not a failure.
    # Running off the end of the window proves nothing about what precedes it, so
    # a streak that was never terminated is a lower bound (see the field's note).
    saturated = True
    for job in jobs:
        if as_dict(job.get("result_summary")).get("mode") != DISPATCHER_MODE:
            continue
        if job.get("status") != "failed":
            saturated = False
            break
        length += 1
        # completed_at is when the failure was recorded; fall back to created_at
        # so a row that never got stamped cannot read as "failed infinitely long
        # ago" and defeat the wait.
        stamped = parse_time(job.get("completed_at")) or parse_time(job.get("created_at"))
        if length == 1:
            last_failed_at = stamped
            last_failed_job_id = text_of(job, "id")
            last_error = text_of(job, "error_message")
        if stamped is not None:
            first_failed_at = stamped
    return FailureStreak(
        length=length,
        last_failed_at=last_failed_at,
        last_failed_job_id=last_failed_job_id,
        last_error=last_error,
        first_failed_at=first_failed_at,
        # A streak of zero is terminated by definition, and a window holding no
        # dispatcher job at all is handled by the caller's scan_never_collected
        # branch rather than reported as an infinite streak.
        saturated=saturated and length > 0,
    )


def backoff_delay_seconds(streak: int, interval_seconds: int) -> int | None:
    """Port of ``schedule.py::_failure_backoff_delay``. None means no wait.

    Two ceilings, because one is not enough across a 15m..1w interval range:
    MAX_INTERVALS keeps a 15m config at 2h rather than days, CEILING keeps a 1w
    config off an 8-week retry, and the outer max() floors the whole thing at one
    interval so 1d/1w configs simply retry at their own cadence.
    """
    if streak < FAILURE_BACKOFF_AFTER:
        return None
    ceiling = max(
        interval_seconds,
        min(interval_seconds * FAILURE_BACKOFF_MAX_INTERVALS, FAILURE_BACKOFF_CEILING_SECONDS),
    )
    # Pinned to int because ``int.__pow__`` is typed as returning Any (it yields a
    # float for a negative exponent); this exponent is >= 0 by the guard above.
    multiplier: int = 2 ** (streak - FAILURE_BACKOFF_AFTER)
    return min(interval_seconds * multiplier, ceiling)


def streaks_by_config(snapshot: Snapshot) -> dict[tuple[str, str], FailureStreak]:
    """Every config's failure streak, keyed by (slug, config id).

    Computed once and shared with the data-sources check, which needs to know
    when a config STARTED failing to decide whether a green connection probe
    predates it.
    """
    result: dict[tuple[str, str], FailureStreak] = {}
    for key, fetched in snapshot.jobs.items():
        if fetched.ok and fetched.value is not None:
            result[key] = consecutive_failure_streak(fetched.value)
    return result


def _interval_seconds(interval: str | None) -> int | None:
    return INTERVAL_SECONDS.get(interval) if interval else None


def _newest_completed(jobs: Sequence[JsonDict]) -> JsonDict | None:
    """The newest COMPLETED dispatcher job.

    The watermark lives in ``result_summary["time_to"]``, which the worker writes
    on completion (tasks.py:820). A FAILED dispatcher job carries only its mode
    stamp, so reading the watermark off "the newest job" would read a missing key
    as a missing watermark.
    """
    for job in jobs:
        if job.get("status") == "completed":
            return job
    return None


def _failing_finding(
    slug: str,
    name: str,
    interval: str | None,
    target: Target,
    streak: FailureStreak,
    *,
    last_success_at: str | None = None,
    data_source_id: str | None = None,
) -> Finding:
    """The `scan_config_failing` finding, which needs no interval arithmetic.

    Extracted so the unknown-interval path can still report it: a backend that
    grows a sixth ScanInterval member must not blind doctor to a dead config.
    """
    generic = streak.last_error == GENERIC_SCAN_ERROR
    since = to_rfc3339(streak.first_failed_at) if streak.first_failed_at else "an unknown time"
    if generic:
        cause = (
            f" Last error: {streak.last_error!r} - that is the backend's generic fallback, "
            f"not the real cause, so the cause is in the worker log for job "
            f"{streak.last_failed_job_id}."
        )
    elif streak.last_error:
        cause = f" Last error: {streak.last_error!r}."
    else:
        cause = " The failed job recorded no error message."
    cadence = (
        ""
        if streak.length >= FAILURE_BACKOFF_AFTER
        else " It is still retrying at its normal cadence."
    )
    # The whole fetched window is one unbroken run of failures, so the true streak
    # starts somewhere older than we asked for. Saying "200 failures since 02:00"
    # when the truth is "384 since Monday" misdates the incident by days, which is
    # exactly the question this finding exists to answer.
    if streak.saturated:
        run = (
            f"has failed at least {streak.length} consecutive scheduled runs, "
            f"at least since {since} (every job in the {JOBS_WINDOW}-row history "
            "window failed, so the run started earlier than this)"
        )
    else:
        run = f"has failed {streak.length} consecutive scheduled runs since {since}"
    return Finding(
        code="scan_config_failing",
        severity=Severity.FAIL,
        project=slug,
        target=target,
        message=f"Scan config {name!r} ({interval}) {run}.{cause}{cadence}",
        evidence={
            "consecutive_failures": streak.length,
            "consecutive_failures_truncated": streak.saturated,
            "jobs_window": JOBS_WINDOW,
            "last_error": streak.last_error,
            "last_error_is_generic": generic,
            "last_failed_job_id": streak.last_failed_job_id,
            "last_failed_at": (
                to_rfc3339(streak.last_failed_at) if streak.last_failed_at else None
            ),
            "last_success_at": last_success_at,
            "interval": interval,
            "data_source_id": data_source_id,
        },
    )


def _config_findings(
    slug: str,
    config: JsonDict,
    jobs: JsonList | None,
    jobs_error: str | None,
    jobs_status: int | None,
    now: datetime,
    streak: FailureStreak | None,
    is_demo: bool = False,
) -> list[Finding]:
    name = text_of(config, "name") or "(unnamed)"
    config_id = text_of(config, "id") or ""
    interval = text_of(config, "interval")
    target = Target(kind="scan_config", id=config_id or None, name=name)

    # A config with no interval is never dispatched BY DESIGN. Judging it would
    # manufacture a fault, and a manufactured fault is worse than no check.
    if interval is None:
        return []

    if not config.get("time_column"):
        return [
            Finding(
                code="scan_config_not_dispatchable",
                severity=Severity.FAIL,
                project=slug,
                target=target,
                message=(
                    f"Scan config {name!r} has an interval of {interval} but no time column, "
                    "so the scheduler never dispatches it at all - it does not appear in the "
                    "dispatcher's query. Set a time column on the scan."
                ),
                evidence={"interval": interval, "time_column": None},
            )
        ]

    if jobs is None:
        return [
            Finding(
                code="endpoint_unexpected_status",
                severity=Severity.FAIL,
                project=slug,
                target=target,
                message=(
                    f"GET /projects/{slug}/scans/{config_id}/jobs answered "
                    f"HTTP {jobs_status}; the collection state of this scan config is unknown, "
                    "not healthy."
                ),
                evidence={
                    "path": scans_api.JOBS.format(slug=slug, scan_id=config_id),
                    "status_code": jobs_status,
                    "error": jobs_error,
                },
            )
        ]

    seconds = _interval_seconds(interval)
    if seconds is None:
        # An interval the CLI does not know is a backend that grew a new
        # ScanInterval member. Say so instead of silently judging with a wrong
        # denominator. But "is it failing" needs NO denominator - only staleness
        # and the backoff estimate do - so it is still answered here. Returning
        # early on the whole config would mean a backend that grew a sixth
        # ScanInterval member blinds doctor to outright failures, and reports
        # "upgrade the CLI" while collection is dead.
        unknown = Finding(
            code="scan_interval_unknown",
            severity=Severity.WARN,
            project=slug,
            target=target,
            message=(
                f"Scan config {name!r} runs on interval {interval!r}, which this version of "
                "the CLI does not know how to convert to a duration, so its staleness and "
                "backoff could not be judged. Upgrade the CLI."
            ),
            evidence={"interval": interval},
        )
        blind_streak = streak if streak is not None else consecutive_failure_streak(jobs)
        if blind_streak.length == 0:
            return [unknown]
        return [unknown, _failing_finding(slug, name, interval, target, blind_streak)]

    findings: list[Finding] = []
    dispatched = dispatcher_jobs(jobs)
    streak = streak if streak is not None else consecutive_failure_streak(jobs)
    data_source_id = text_of(config, "data_source_id")
    stale_after = STALE_INTERVALS * seconds
    never_after = NEVER_COLLECTED_INTERVALS * seconds

    if not dispatched:
        # "Ever" is a claim about all history, and we hold one window of it. When
        # that window is FULL, its lack of a dispatcher job proves only that the
        # newest JOBS_WINDOW jobs were something else — manual runs (run_scan),
        # replays and event-group applies all write ScanJobs without the mode
        # stamp, so a busy config can bury its own scheduled history. Claiming
        # "the beat scheduler may not be running" there sends the operator after
        # a healthy scheduler.
        if len(jobs) >= JOBS_WINDOW:
            return [
                Finding(
                    code="scan_history_window_full",
                    severity=Severity.WARN,
                    project=slug,
                    target=target,
                    message=(
                        f"None of the {JOBS_WINDOW} most recent jobs for {name!r} ({interval}) "
                        "was created by the scheduler; they are manual scans, replays or demo "
                        "ticks. Whether scheduled collection is running could not be determined "
                        "from this window."
                    ),
                    evidence={"interval": interval, "jobs_window": JOBS_WINDOW},
                )
            ]
        created = parse_time(config.get("created_at"))
        age = (now - created).total_seconds() if created is not None else None
        # Not for a demo: one nobody opened may never have been collected, and
        # that is the pause working, not a dead scheduler.
        if not is_demo and age is not None and age > never_after:
            findings.append(
                Finding(
                    code="scan_never_collected",
                    severity=Severity.FAIL,
                    project=slug,
                    target=target,
                    message=(
                        f"No scheduled collection job has ever run for {name!r} ({interval}), "
                        f"created {format_duration(age)} ago - the beat scheduler may not be "
                        "running."
                    ),
                    evidence={
                        "interval": interval,
                        "created_at": to_rfc3339(created) if created else None,
                        "age_seconds": int(age),
                        "jobs_seen": len(jobs),
                    },
                )
            )
        return findings

    newest = dispatched[0]
    failing = streak.length > 0

    if failing:
        findings.append(
            _failing_finding(
                slug,
                name,
                interval,
                target,
                streak,
                last_success_at=_last_success_at(dispatched),
                data_source_id=data_source_id,
            )
        )
        delay = backoff_delay_seconds(streak.length, seconds)
        if delay is not None:
            not_before = (
                to_rfc3339(streak.last_failed_at + timedelta(seconds=delay))
                if streak.last_failed_at is not None
                else None
            )
            findings.append(
                Finding(
                    code="scan_backoff_active",
                    severity=Severity.WARN,
                    project=slug,
                    target=target,
                    message=(
                        "The scheduler has deliberately deferred the next attempt to not before "
                        f"{not_before} (about {format_duration(delay)} after the last failure): "
                        f"{FAILURE_BACKOFF_AFTER} or more consecutive failures trigger a backoff, "
                        "so the worker is not stuck."
                    ),
                    evidence={
                        "consecutive_failures": streak.length,
                        "backoff_after": FAILURE_BACKOFF_AFTER,
                        "interval_seconds": seconds,
                        "deferred_by_seconds_estimate": delay,
                        "next_attempt_not_before_estimate": not_before,
                    },
                )
            )
    else:
        dispatched_at = parse_time(newest.get("created_at"))
        idle = (now - dispatched_at).total_seconds() if dispatched_at is not None else 0.0
        # Never for a demo: its silence is the pause gate
        # working, and nothing exposed can tell it from a dead scheduler.
        if not is_demo and dispatched_at is not None and idle > stale_after:
            # Suppressed above when the config is already failing: one root
            # cause, one finding. A failing config is trivially also "not
            # recently dispatched", and printing both invites the operator to
            # chase the symptom.
            findings.append(
                Finding(
                    code="scan_not_dispatched",
                    severity=Severity.FAIL,
                    project=slug,
                    target=target,
                    message=(
                        f"No scheduled run for {name!r} ({interval}) in {format_duration(idle)}."
                    ),
                    evidence={
                        "interval": interval,
                        "last_dispatched_at": to_rfc3339(dispatched_at),
                        "idle_seconds": int(idle),
                    },
                )
            )

    if newest.get("status") == "completed":
        completed = _newest_completed(dispatched)
        watermark = parse_time(as_dict((completed or {}).get("result_summary")).get("time_to"))
        if watermark is not None:
            behind = (now - watermark).total_seconds()
            # Not for a demo: a paused demo's watermark freezes with its
            # collection, and its synthetic source is never "producing no rows".
            if not is_demo and behind > stale_after:
                findings.append(
                    Finding(
                        code="scan_watermark_stale",
                        severity=Severity.WARN,
                        project=slug,
                        target=target,
                        message=(
                            f"Collection for {name!r} is succeeding, but the last run only wrote "
                            f"data through {to_rfc3339(watermark)} - {format_duration(behind)} "
                            "behind now. The source table is producing no rows in the scan window."
                        ),
                        evidence={
                            "interval": interval,
                            "time_to": to_rfc3339(watermark),
                            "behind_seconds": int(behind),
                        },
                    )
                )
    return findings


def _last_success_at(dispatched: Sequence[JsonDict]) -> str | None:
    for job in dispatched:
        if job.get("status") == "completed":
            stamped = parse_time(job.get("completed_at")) or parse_time(job.get("created_at"))
            return to_rfc3339(stamped) if stamped else None
    return None


def check_scans(snapshot: Snapshot, streaks: Mapping[tuple[str, str], FailureStreak]) -> Check:
    """One check, one row, however many projects and configs it covered."""
    findings: list[Finding] = []
    judged = 0
    failing = 0
    for project in snapshot.selected_projects:
        slug = text_of(project, "slug") or ""
        fetched = snapshot.scans.get(slug)
        if fetched is None:
            continue
        if not fetched.ok or fetched.value is None:
            findings.append(
                Finding(
                    code="endpoint_unexpected_status",
                    severity=Severity.FAIL,
                    project=slug,
                    target=Target(kind="project", name=slug),
                    message=(
                        f"GET /projects/{slug}/scans answered HTTP {fetched.status_code}; the "
                        "scan configuration of this project is unknown, not empty."
                    ),
                    evidence={
                        "path": scans_api.CONFIGS.format(slug=slug),
                        "status_code": fetched.status_code,
                        "error": fetched.error,
                    },
                )
            )
            continue
        for config in fetched.value:
            if not config.get("interval"):
                continue
            judged += 1
            config_id = text_of(config, "id") or ""
            jobs_fetched = snapshot.jobs.get((slug, config_id))
            produced = _config_findings(
                slug,
                config,
                jobs_fetched.value if jobs_fetched is not None else None,
                jobs_fetched.error if jobs_fetched is not None else None,
                jobs_fetched.status_code if jobs_fetched is not None else None,
                snapshot.now,
                streaks.get((slug, config_id)),
                is_demo=bool(project.get("is_demo")),
            )
            if any(finding.severity is Severity.FAIL for finding in produced):
                failing += 1
            findings.extend(produced)

    status = worst(finding.severity for finding in findings)
    if judged == 0:
        summary = "No scan config has a collection interval, so nothing is scheduled to collect."
    elif failing:
        verb = "is" if failing == 1 else "are"
        summary = f"{failing} of {judged} scheduled scan configs {verb} not collecting."
    elif findings:
        summary = f"{judged} scheduled scan configs; none is failing, but see below."
    else:
        summary = f"{judged} scheduled scan configs are collecting on time."
    return Check(
        id=CHECK_ID,
        title=CHECK_TITLE,
        status=status,
        summary=summary,
        findings=tuple(findings),
    )
