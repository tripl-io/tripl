"""Two snapshots in, a tuple of events out. Pure: no network, no clock, no print.

This is where the acceptance criterion of the watch command lives, which is why it is
a function of two values rather than a method on the loop: "a running replay
shows live chunk progress" is provable here with no sockets and no wall clock.

Every message is prose and may be reworded in any release; ``event`` and ``data``
are the contract.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime

from tripl_cli.model import Target, format_duration, to_rfc3339
from tripl_cli.watch.model import (
    DELIVERY_STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    TERMINAL_STATUSES,
    DeliveryRow,
    JobRow,
    JobTarget,
    SignalRow,
    StallEntry,
    TickSnapshot,
    WatchEvent,
    repeat_multiple,
)
from tripl_cli.watch.render import named, number, plural, progress_phrase, stamp

_TERMINAL_TOKEN: Mapping[str, str] = {
    STATUS_COMPLETED: "job.finished",
    STATUS_FAILED: "job.failed",
    STATUS_CANCELLED: "job.cancelled",
}


@dataclass(frozen=True)
class DiffResult:
    """The events of one tick, and the stall tracker as it now stands.

    The tracker comes back rather than being mutated in place so this module
    stays a function: a test can replay a scripted sequence of ticks and assert
    the exact emission times.
    """

    events: tuple[WatchEvent, ...] = ()
    stalls: Mapping[str, StallEntry] = field(default_factory=dict)


def diff_tick(
    previous: TickSnapshot,
    current: TickSnapshot,
    *,
    observed_at: datetime,
    monotonic: float,
    stalls: Mapping[str, StallEntry],
    stall_after: float,
    refreshed_jobs: Collection[JobTarget] | None = None,
) -> DiffResult:
    """Diff two snapshots.

    ``refreshed_jobs`` names the scan configs whose jobs read SUCCEEDED this tick.
    ``current.jobs`` carries an entry for every config watch has ever read
    successfully, including the ones whose read just failed - that is what turns
    an outage into a reporting delay - so without this the stall tracker would
    read stale rows as fresh observations. ``None`` means the caller made no such
    distinction and every target is to be treated as freshly read.
    """
    events: list[WatchEvent] = []
    events.extend(_diff_jobs(previous, current, observed_at=observed_at))
    next_stalls = _track_stalls(
        current,
        observed_at=observed_at,
        monotonic=monotonic,
        stalls=stalls,
        stall_after=stall_after,
        events=events,
        refreshed=refreshed_jobs,
    )
    events.extend(_diff_signals(previous, current, observed_at=observed_at))
    events.extend(_diff_deliveries(previous, current, observed_at=observed_at))
    return DiffResult(events=tuple(events), stalls=next_stalls)


# --- jobs -----------------------------------------------------------------


def _diff_jobs(
    previous: TickSnapshot, current: TickSnapshot, *, observed_at: datetime
) -> list[WatchEvent]:
    events: list[WatchEvent] = []
    for target, stream in current.jobs.items():
        before = previous.jobs.get(target)
        if before is None:
            # First sight of this scan config: seed silently. A follow mode that
            # dumped a newly discovered config's whole job history as events
            # would read as a burst of things that just happened (TRAP D).
            continue
        slug, _scan_id = target
        for job_id, job in stream.rows.items():
            was = before.rows.get(job_id)
            if was is None:
                token = _new_job_token(job)
            elif was.fingerprint == job.fingerprint:
                continue
            else:
                token = _changed_job_token(was, job)
            if token is None:
                continue
            events.append(
                _job_event(token, job, slug=slug, current=current, observed_at=observed_at)
            )
        # Removals are NOT reported: the jobs list is windowed by created_at
        # desc, so a disappearance there is a window artifact, not an event.
    return events


def _new_job_token(job: JobRow) -> str | None:
    """A job identity watch has never seen before.

    A job that appears already terminal reports its terminal event rather than
    nothing: at a 10s interval a short collection job can be created and finish
    inside one poll, and dropping it would make watch blind to exactly the
    failures that trigger the delivery lines below it.
    """
    if job.status == STATUS_PENDING:
        return "job.queued"
    if job.status == STATUS_RUNNING:
        return "job.started"
    return _TERMINAL_TOKEN.get(job.status)


def _changed_job_token(was: JobRow, job: JobRow) -> str | None:
    if was.status != job.status:
        if job.status == STATUS_RUNNING:
            return "job.started"
        if job.status == STATUS_PENDING:
            return "job.queued"
        return _TERMINAL_TOKEN.get(job.status)
    if job.status == STATUS_RUNNING and job.is_replay:
        # The headline criterion. Only a metrics_replay job has progress to
        # report; a metrics_collection job's fingerprint cannot move without its
        # status moving, and watch does not invent progress the backend never
        # published.
        return "job.progress"
    return None


def _job_event(
    token: str,
    job: JobRow,
    *,
    slug: str,
    current: TickSnapshot,
    observed_at: datetime,
) -> WatchEvent:
    name = _scan_name(current, slug, job.scan_config_id)
    return WatchEvent(
        event=token,
        time=observed_at,
        message=_job_message(token, job, name=name, observed_at=observed_at),
        project=slug,
        target=Target(kind="scan_job", id=job.id, name=name),
        data=job.data(scan_name=name, observed_at=observed_at),
    )


def _job_message(token: str, job: JobRow, *, name: str | None, observed_at: datetime) -> str:
    head = f"{named(name)}job {job.id}"
    elapsed = job.elapsed_seconds(observed_at)
    took = f" after {format_duration(elapsed)}" if elapsed is not None else ""
    if token == "job.queued":
        return f"{head} queued{_mode_suffix(job)}."
    if token == "job.started":
        return f"{head} started{_mode_suffix(job)}."
    if token == "job.progress":
        window = _chunk_window(job)
        since = f", {format_duration(elapsed)} elapsed" if elapsed is not None else ""
        return f"{head} {progress_phrase(job)}{window}{since}."
    if token == "job.finished":
        chunks = (
            f" ({job.chunks_completed} of {job.chunks_total} chunks)"
            if job.is_replay and job.chunks_total
            else ""
        )
        return f"{head} completed{took}{chunks}."
    if token == "job.failed":
        detail = f": {job.error_message!r}" if job.error_message else " with no error message"
        return f"{head} failed{took}{detail}."
    return f"{head} cancelled{took}."


def _mode_suffix(job: JobRow) -> str:
    return f" ({job.mode})" if job.mode else ""


def _chunk_window(job: JobRow) -> str:
    start = stamp(job.summary.get("replay_current_chunk_from"))
    end = stamp(job.summary.get("replay_current_chunk_to"))
    return f" {start}..{end}" if start and end else ""


# --- stalls ---------------------------------------------------------------


def _track_stalls(
    current: TickSnapshot,
    *,
    observed_at: datetime,
    monotonic: float,
    stalls: Mapping[str, StallEntry],
    stall_after: float,
    events: list[WatchEvent],
    refreshed: Collection[JobTarget] | None = None,
) -> dict[str, StallEntry]:
    """Re-key the tracker to the jobs in this snapshot, and report the silences.

    A statement about OBSERVATION, never about health - which is why it does not
    touch the significance-gate class of problem. `tripl doctor` is the command
    with verdicts.

    Two shapes of unchanged-ness, told apart because they are not the same claim:

    * a job WITH a progress channel (metrics_replay) whose counters stopped ->
      ``job.stalled``: progress was being published and is not any more;
    * a job WITHOUT one -> ``job.unchanged``: watch can honestly say only that
      the status has not moved. That covers the pending job no worker ever picked
      up, which is the most common way a scan hangs and used to produce one
      ``job.queued`` line and then total silence, and it stops a
      metrics_collection job - whose fingerprint CANNOT change while it runs -
      being reported as stalled the moment it outlives --stall-after.

    A terminal job is dropped from the tracker entirely; there is nothing further
    to wait for.
    """
    tracked: dict[str, StallEntry] = {}
    for target, stream in current.jobs.items():
        slug, _scan_id = target
        # A stale entry is not an observation. See StallEntry.unobserved.
        fresh = refreshed is None or target in refreshed
        for job_id, job in stream.rows.items():
            if job.status in TERMINAL_STATUSES:
                continue
            entry = stalls.get(job_id)
            if entry is None or entry.fingerprint != job.fingerprint:
                # Progress implicitly clears a stall: the clock restarts, so the
                # next report needs another full threshold. There is no
                # job.unstalled token to keep in sync.
                tracked[job_id] = StallEntry(
                    fingerprint=job.fingerprint,
                    since=observed_at,
                    since_monotonic=monotonic,
                    unobserved=not fresh,
                )
                continue
            if not fresh:
                tracked[job_id] = replace(entry, unobserved=True)
                continue
            if entry.unobserved:
                # First good look after a blind stretch. The clock RESTARTS
                # rather than resuming, so every second this tracker ever reports
                # is a second watch was actually looking.
                tracked[job_id] = replace(
                    entry,
                    since=observed_at,
                    since_monotonic=monotonic,
                    reported=0,
                    unobserved=False,
                )
                continue
            unchanged = monotonic - entry.since_monotonic
            multiple = repeat_multiple(unchanged, stall_after)
            if multiple > entry.reported:
                events.append(
                    _stall_event(
                        job,
                        slug=slug,
                        current=current,
                        observed_at=observed_at,
                        entry=entry,
                        unchanged=unchanged,
                    )
                )
                entry = replace(entry, reported=multiple)
            tracked[job_id] = entry
    return tracked


def _stall_event(
    job: JobRow,
    *,
    slug: str,
    current: TickSnapshot,
    observed_at: datetime,
    entry: StallEntry,
    unchanged: float,
) -> WatchEvent:
    name = _scan_name(current, slug, job.scan_config_id)
    data = job.data(scan_name=name, observed_at=observed_at)
    data["unchanged_since"] = to_rfc3339(entry.since)
    data["unchanged_seconds"] = int(unchanged)
    if job.has_progress_channel:
        token = "job.stalled"
        message = (
            f"{named(name)}job {job.id} unchanged for {format_duration(unchanged)} at "
            f"{progress_phrase(job)}; watch has seen no progress since "
            f"{to_rfc3339(entry.since)}."
        )
    else:
        # Never "stalled": this job publishes nothing that COULD move short of its
        # status, so claiming stopped progress would be claiming an observation
        # that was never available to make.
        token = "job.unchanged"
        waiting = (
            "no worker has picked it up"
            if job.status == STATUS_PENDING
            else "it publishes no progress, so its status is all watch can see"
        )
        message = (
            f"{named(name)}job {job.id} still in status {job.status} after "
            f"{format_duration(unchanged)} of watching ({waiting}); "
            f"unchanged since {to_rfc3339(entry.since)}."
        )
    return WatchEvent(
        event=token,
        time=observed_at,
        message=message,
        project=slug,
        target=Target(kind="scan_job", id=job.id, name=name),
        data=data,
    )


# --- signals --------------------------------------------------------------


def _diff_signals(
    previous: TickSnapshot, current: TickSnapshot, *, observed_at: datetime
) -> list[WatchEvent]:
    events: list[WatchEvent] = []
    for slug, stream in current.signals.items():
        before = previous.signals.get(slug)
        if before is None:
            continue
        for key, signal in stream.rows.items():
            was = before.rows.get(key)
            if was is None:
                events.append(_signal_event("signal.opened", signal, slug, current, observed_at))
            elif was.fingerprint != signal.fingerprint:
                events.append(_signal_event("signal.updated", signal, slug, current, observed_at))
        # Removals ARE reported here, and only here: /anomalies/signals takes no
        # limit and returns the complete active set, so a disappearance is real
        # rather than a window artifact.
        for key, signal in before.rows.items():
            if key not in stream.rows:
                events.append(
                    _signal_event(
                        "signal.cleared",
                        signal,
                        slug,
                        current,
                        observed_at,
                        remaining=len(stream.rows),
                    )
                )
    return events


def _signal_event(
    token: str,
    signal: SignalRow,
    slug: str,
    current: TickSnapshot,
    observed_at: datetime,
    *,
    remaining: int = 0,
) -> WatchEvent:
    name = _scan_name(current, slug, signal.scan_config_id)
    # The scan config, or null for the `metric` scope, whose signals carry a null
    # scan_config_id. Never a synthetic composite id: the backend does not own
    # that string format, and the day it grows a real signal id there would be no
    # compatible way to switch. The identity lives in `data` as separate fields.
    target = (
        Target(kind="scan_config", id=signal.scan_config_id, name=name)
        if signal.scan_config_id
        else None
    )
    bucket = stamp(signal.bucket) or "an unknown bucket"
    head = f"{named(name)}{signal.scope_type} {signal.direction}"
    counts = (
        f"actual {number(signal.raw.get('actual_count'))} vs "
        f"expected {number(signal.raw.get('expected_count'))} "
        f"(z={number(signal.raw.get('z_score'), digits=1)})"
    )
    if token == "signal.opened":
        message = f"{head} at {bucket}: {counts}."
    elif token == "signal.updated":
        message = f"{head} moved to bucket {bucket}: {counts}."
    else:
        # "cleared", not "resolved": a disappearance may only mean the freshness
        # window closed. Cleared claims it left the active list and nothing more.
        message = (
            f"{head} at {bucket} is no longer in the active set; "
            f"{plural(remaining, 'signal')} still open in this project."
        )
    return WatchEvent(
        event=token,
        time=observed_at,
        message=message,
        project=slug,
        target=target,
        data=signal.data,
    )


# --- deliveries -----------------------------------------------------------


def _diff_deliveries(
    previous: TickSnapshot, current: TickSnapshot, *, observed_at: datetime
) -> list[WatchEvent]:
    events: list[WatchEvent] = []
    for slug, stream in current.deliveries.items():
        before = previous.deliveries.get(slug)
        if before is None:
            continue
        for delivery_id, delivery in stream.rows.items():
            if delivery.status != DELIVERY_STATUS_FAILED:
                continue
            was = before.rows.get(delivery_id)
            if was is not None and was.fingerprint == delivery.fingerprint:
                continue
            events.append(_delivery_event(delivery, slug, observed_at))
    return events


def _delivery_event(delivery: DeliveryRow, slug: str, observed_at: datetime) -> WatchEvent:
    raw = delivery.raw
    rule = raw.get("rule_name") or "an alert rule"
    destination = raw.get("destination_name") or "an unnamed destination"
    error = raw.get("error_message")
    detail = f": {error!r}" if isinstance(error, str) and error else " with no error recorded"
    message = (
        f"{rule!r} -> {delivery.channel} {destination!r} failed{detail}. "
        f"Nobody was paged; delivery {delivery.id}."
    )
    return WatchEvent(
        event="delivery.failed",
        time=observed_at,
        message=message,
        project=slug,
        target=Target(kind="alert_delivery", id=delivery.id, name=str(rule)),
        data=delivery.data,
    )


# --- shared helpers -------------------------------------------------------


def _scan_name(current: TickSnapshot, slug: str, scan_config_id: str | None) -> str | None:
    if scan_config_id is None:
        return None
    return current.configs.get(slug, {}).get(scan_config_id)
