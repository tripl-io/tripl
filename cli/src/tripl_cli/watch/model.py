"""The vocabulary watch speaks: constants, rows, and the frozen event tokens.

Pure data plus total functions over it. No IO, no clock reads, no ``httpx`` — so
the whole diff layer, which is where the acceptance criterion lives, is testable
as arithmetic.

Rows keep the API payload VERBATIM in ``raw`` and expose typed accessors over it.
That is what lets ``data`` in the JSONL carry the backend's own key names with no
second spelling to drift: an operator greps ``replay_chunks_completed`` in
``tasks.py`` and in the CLI output and finds the same string.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tripl_cli.model import (
    JsonDict,
    Target,
    as_dict,
    int_of,
    parse_time,
    text_of,
)
from tripl_cli.report import (
    WATCH_STREAM_DIAGNOSTIC,
    WATCH_STREAM_EVENT,
    WATCH_STREAM_META,
)

# --- cadence and budget ---------------------------------------------------

# One interval, one loop. 10s is the cadence the API is already designed for:
# `list_scan_jobs`'s own docstring describes the Scans tab polling it every 10
# seconds and ScansTab.tsx:94 is activeMs: 10000. A two-tier active/idle schedule
# was cut - it is two state machines for something the operator gets by typing
# --interval 5.
WATCH_INTERVAL_SECONDS = 10.0
MIN_INTERVAL_SECONDS = 2.0
MAX_INTERVAL_SECONDS = 3600.0

# How many jobs one poll asks for. Deliberately NOT doctor's JOBS_WINDOW=200:
# doctor asks "how long has this been broken" and needs history, watch asks "what
# appeared since the last poll" and needs recency. 20x the payload on a loop that
# repeats every 10 seconds is tripl-jfm3.107 rebuilt - production configs hold
# 1,366-1,551 jobs each and an uncapped fan-out pulled ~4,400 rows per tick.
WATCH_JOBS_LIMIT = 10

# The newest failed deliveries, with no date floor. A floor on `date_from` would
# filter AlertDelivery.created_at (services/_alerting_deliveries.py), so a
# delivery created before the floor that FAILS during the run would be invisible.
WATCH_DELIVERIES_LIMIT = 20

# Streams that do not deserve every tick. 30s is not invented: get_active_signals
# caches its unfiltered variants at ttl_seconds=30, so polling faster is provably
# wasted work when Redis is on and pure extra load when it is off. The scan
# listing changes maybe twice a week and is re-read on the same clock, which is
# still often enough to discover a config created mid-incident.
SLOW_STREAM_MIN_SECONDS = 30.0

# Expressed in SECONDS, not polls. A threshold counted in polls would silently
# become a 10-minute threshold for anyone who typed --interval 60, and the
# meaning of a flag must not change when a different flag changes. 120s is the
# smallest value that cannot fire on a chunk cadence this repo's own examples
# call normal (~50s per chunk).
WATCH_STALL_SECONDS = 120.0
MIN_STALL_SECONDS = 10.0
MAX_STALL_SECONDS = 86400.0

MIN_DURATION_SECONDS = 1.0
MAX_DURATION_SECONDS = 86400.0

# Four gather_bounded batches of MAX_CONCURRENT_REQUESTS (6). Above it watch
# REFUSES to start rather than truncating: doctor can report what its budget
# could not reach in a one-shot document, but a command that repeats would have
# to reprint a truncation warning every tick (spam) or print it once at the top
# (where it scrolls away, leaving the operator reading a feed that is silently
# missing the config the incident is in).
WATCH_MAX_CONFIGS = 24

# backend/src/tripl/worker/tasks/metrics/tasks.py:90-91.
METRICS_REPLAY_MODE = "metrics_replay"

# backend ScanJobStatus.
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
TERMINAL_STATUSES = (STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED)

# backend AlertDeliveryStatus. Spelled separately from ScanJobStatus.STATUS_FAILED
# even though the two strings are equal today: they are different enums on the
# backend, and one of them changing must not silently retune the other. It is the
# query parameter watch/collect.py sends AND the value the diff selects on, so
# those two can never disagree.
DELIVERY_STATUS_FAILED = "failed"

# Why a run ended, as it appears in watch.stopped's data.
REASON_INTERRUPTED = "interrupted"
REASON_DURATION = "duration_elapsed"
REASON_AUTH_FAILED = "authentication_failed"
# stdout (or stderr) went away - `tripl watch | head -20`. The footer that would
# carry this reason can no longer be printed, by construction; it is here so the
# WatchOutcome an embedder reads is honest about why the loop ended.
REASON_OUTPUT_CLOSED = "output_closed"

# --- the event vocabulary -------------------------------------------------

# Token -> JSONL stream. EVENT_TOKENS is derived from this mapping so the two can
# never disagree, and the docs contract test iterates the tuple.
STREAM_OF: Mapping[str, str] = {
    "watch.started": WATCH_STREAM_META,
    "watch.stopped": WATCH_STREAM_META,
    "job.queued": WATCH_STREAM_EVENT,
    "job.started": WATCH_STREAM_EVENT,
    "job.progress": WATCH_STREAM_EVENT,
    "job.stalled": WATCH_STREAM_EVENT,
    "job.unchanged": WATCH_STREAM_EVENT,
    "job.finished": WATCH_STREAM_EVENT,
    "job.failed": WATCH_STREAM_EVENT,
    "job.cancelled": WATCH_STREAM_EVENT,
    "signal.opened": WATCH_STREAM_EVENT,
    "signal.updated": WATCH_STREAM_EVENT,
    "signal.cleared": WATCH_STREAM_EVENT,
    "delivery.failed": WATCH_STREAM_EVENT,
    "poll.degraded": WATCH_STREAM_DIAGNOSTIC,
    "poll.recovered": WATCH_STREAM_DIAGNOSTIC,
}
EVENT_TOKENS: tuple[str, ...] = tuple(STREAM_OF)

# The longest token, and therefore the width of the second column.
EVENT_COLUMN_WIDTH = max(len(token) for token in EVENT_TOKENS)

# What lands in `data`, by the backend's own key names. Presence-conditional for
# the replay block on purpose: a metrics_collection job has none of those keys
# and watch does not invent progress the backend does not publish.
JOB_TIME_KEYS: tuple[str, ...] = ("created_at", "started_at", "completed_at", "updated_at")
REPLAY_SUMMARY_KEYS: tuple[str, ...] = (
    "mode",
    "replay_progress_phase",
    "replay_chunks_total",
    "replay_chunks_completed",
    "replay_progress_percent",
    "replay_current_chunk_index",
    "replay_current_chunk_from",
    "replay_current_chunk_to",
    "replay_chunk_interval",
    "time_from",
    "time_to",
)
SIGNAL_DATA_KEYS: tuple[str, ...] = (
    "scan_config_id",
    "scope_type",
    "scope_ref",
    "bucket",
    "direction",
    "state",
    "actual_count",
    "expected_count",
    "z_score",
    "stddev",
    "incident_child",
    "event_id",
    "event_type_id",
)
DELIVERY_DATA_KEYS: tuple[str, ...] = (
    "id",
    "status",
    "channel",
    "destination_id",
    "destination_name",
    "rule_id",
    "rule_name",
    "scan_config_id",
    "scan_job_id",
    "scan_name",
    "error_message",
    "matched_count",
    "is_local",
    "is_simulated",
    "sent_at",
    "created_at",
    "updated_at",
)


def pick(source: Mapping[str, Any], keys: Sequence[str]) -> JsonDict:
    """The declared keys that are actually present. Never invents a null."""
    return {key: source[key] for key in keys if key in source}


def repeat_multiple(elapsed: float, threshold: float) -> int:
    """1, 2, 4, 8... - how often a persisting condition is allowed to re-report.

    ONE rule in the codebase, applied twice: to a stalled job (in seconds) and to
    a stream that keeps failing to poll (in consecutive failures). ~12 lines over
    four hours instead of one every tick, and the operator still learns that it
    is still going.
    """
    if threshold <= 0 or elapsed < threshold:
        return 0
    multiple = 1
    while elapsed >= threshold * multiple * 2:
        multiple *= 2
    return multiple


# --- options --------------------------------------------------------------


@dataclass(frozen=True)
class WatchOptions:
    project_slugs: tuple[str, ...] = ()
    include_demo: bool = False
    scan_selectors: tuple[str, ...] = ()
    interval: float = WATCH_INTERVAL_SECONDS
    duration: float | None = None
    stall_after: float = WATCH_STALL_SECONDS
    jobs_limit: int = WATCH_JOBS_LIMIT
    deliveries_limit: int = WATCH_DELIVERIES_LIMIT


@dataclass(frozen=True)
class ProjectRef:
    """One followed project, plus the count the server itself calls significant."""

    slug: str
    name: str
    # ProjectResponse.summary.monitoring_signal_count - the backend's own
    # significance-filtered count. Printed BESIDE watch's all-scopes count and
    # labelled, never instead of it: the two are different populations and an
    # unexplained mismatch becomes a bug report (TRAP A).
    significant_open_signals: int = 0


# --- rows -----------------------------------------------------------------


@dataclass(frozen=True)
class JobRow:
    """One ScanJobResponse, with the replay block read but not renamed."""

    id: str
    raw: JsonDict

    @property
    def summary(self) -> JsonDict:
        return as_dict(self.raw.get("result_summary"))

    @property
    def scan_config_id(self) -> str | None:
        return text_of(self.raw, "scan_config_id")

    @property
    def status(self) -> str:
        return text_of(self.raw, "status") or ""

    @property
    def error_message(self) -> str | None:
        return text_of(self.raw, "error_message")

    @property
    def mode(self) -> str | None:
        return text_of(self.summary, "mode")

    @property
    def is_replay(self) -> bool:
        return self.mode == METRICS_REPLAY_MODE

    @property
    def has_progress_channel(self) -> bool:
        """Whether this job publishes anything that can move without its status.

        Only a metrics_replay job does: ``_heartbeat_replay_progress`` writes the
        chunk counters into ``result_summary``. A metrics_collection job's
        fingerprint is its status and nothing else, which is why it can never be
        reported as having stopped making progress - it never had any to report.
        """
        return self.is_replay

    @property
    def phase(self) -> str | None:
        return text_of(self.summary, "replay_progress_phase")

    @property
    def chunks_total(self) -> int:
        return int_of(self.summary, "replay_chunks_total")

    @property
    def chunks_completed(self) -> int:
        return int_of(self.summary, "replay_chunks_completed")

    @property
    def chunk_index(self) -> int | None:
        value = self.summary.get("replay_current_chunk_index")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @property
    def percent(self) -> float | None:
        value = self.summary.get("replay_progress_percent")
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return float(value)

    @property
    def started(self) -> datetime | None:
        return parse_time(self.raw.get("started_at")) or parse_time(self.raw.get("created_at"))

    @property
    def fingerprint(self) -> tuple[Any, ...]:
        """What counts as an observable change.

        ``updated_at`` is deliberately ABSENT. ``_heartbeat_replay_progress``
        bumps it on every heartbeat, including the `preparing` and `finalizing`
        calls where no counter moves; including it here would print a line on
        every heartbeat and, worse, would make job.stalled unreachable - a
        heartbeating-but-wedged job would look alive, which is exactly backwards
        for the question watch exists to answer. ``updated_at`` still ships in
        ``data`` for the operator to read.

        For a metrics_collection job the replay members are all absent, so the
        fingerprint reduces to the status - which is the whole of its story.
        """
        return (self.status, self.phase, self.chunks_completed, self.chunk_index)

    def data(self, *, scan_name: str | None, observed_at: datetime) -> JsonDict:
        payload: JsonDict = {
            "scan_config_id": self.scan_config_id,
            "scan_name": scan_name,
            "status": self.status,
            "error_message": self.error_message,
        }
        payload.update(pick(self.summary, REPLAY_SUMMARY_KEYS))
        payload.update(pick(self.raw, JOB_TIME_KEYS))
        payload["elapsed_seconds"] = self.elapsed_seconds(observed_at)
        return payload

    def elapsed_seconds(self, observed_at: datetime) -> int | None:
        """How long the job has been going, never negative.

        CLAMPED at zero on purpose. A job whose ``started_at`` is in the future -
        clock skew between the worker host and this one, which is ordinary in a
        container fleet - would otherwise produce a negative elapsed, and
        ``format_duration`` takes the absolute value, so the prose would report a
        job started 40 seconds from now as having ALREADY RUN for 40 seconds. A
        zero is visibly odd; a plausible wrong number is not.
        """
        started = self.started
        if started is None:
            return None
        return max(0, int((observed_at - started).total_seconds()))

    def clock_skew_seconds(self, observed_at: datetime) -> int:
        """How far into the future this job claims to have started. 0 = none."""
        started = self.started
        if started is None:
            return 0
        return max(0, int((started - observed_at).total_seconds()))


SignalKey = tuple[str | None, str, str]


@dataclass(frozen=True)
class SignalRow:
    """One MetricSignalResponse. It has no id and no detection timestamp."""

    raw: JsonDict

    @property
    def scan_config_id(self) -> str | None:
        return text_of(self.raw, "scan_config_id")

    @property
    def scope_type(self) -> str:
        return text_of(self.raw, "scope_type") or ""

    @property
    def scope_ref(self) -> str:
        return text_of(self.raw, "scope_ref") or ""

    @property
    def direction(self) -> str:
        return text_of(self.raw, "direction") or ""

    @property
    def bucket(self) -> str | None:
        return text_of(self.raw, "bucket")

    @property
    def state(self) -> str:
        return text_of(self.raw, "state") or ""

    @property
    def key(self) -> SignalKey:
        """The backend's OWN key (metrics_insights_service.py:527).

        Identity means the same thing on both sides by construction. ``bucket``
        is deliberately NOT part of it: with bucket in the identity a signal that
        advances to a later bucket produces a cleared plus an opened - a
        fabricated resolution in the middle of an ongoing incident.
        """
        return (self.scan_config_id, self.scope_type, self.scope_ref)

    @property
    def fingerprint(self) -> tuple[Any, ...]:
        return (self.bucket, self.direction, self.state)

    @property
    def data(self) -> JsonDict:
        return pick(self.raw, SIGNAL_DATA_KEYS)


@dataclass(frozen=True)
class DeliveryRow:
    """One AlertDeliveryResponse."""

    id: str
    raw: JsonDict

    @property
    def status(self) -> str:
        return text_of(self.raw, "status") or ""

    @property
    def channel(self) -> str:
        return text_of(self.raw, "channel") or ""

    @property
    def fingerprint(self) -> tuple[Any, ...]:
        return (self.status,)

    @property
    def data(self) -> JsonDict:
        return pick(self.raw, DELIVERY_DATA_KEYS)


def job_row_of(raw: JsonDict) -> JobRow | None:
    job_id = text_of(raw, "id")
    return JobRow(id=job_id, raw=raw) if job_id else None


def signal_row_of(raw: JsonDict) -> SignalRow | None:
    # scope_type and scope_ref are required on the response; a row without them
    # has no identity and cannot be tracked, so it is dropped rather than keyed
    # under a blank that would collide with the next one.
    row = SignalRow(raw=raw)
    return row if row.scope_type and row.scope_ref else None


def delivery_row_of(raw: JsonDict) -> DeliveryRow | None:
    delivery_id = text_of(raw, "id")
    return DeliveryRow(id=delivery_id, raw=raw) if delivery_id else None


# --- snapshots ------------------------------------------------------------


@dataclass(frozen=True)
class StreamSnapshot[K, R]:
    """One stream's rows as of one successful read.

    There is no seen-set anywhere in watch: each tick builds a fresh snapshot and
    diffs it against the previous one, which is why memory is O(response size)
    and constant in run duration. An append-only "everything I ever printed" set
    would grow without bound over an eight-hour incident, and evicting from it
    would cause a re-print that reads as a duplicate event.
    """

    rows: Mapping[K, R] = field(default_factory=dict)
    # The response came back at exactly its limit, so older rows are outside the
    # window. Stated rather than hidden - doctor's scan_history_window_full
    # precedent.
    window_full: bool = False


JobStream = StreamSnapshot[str, JobRow]
SignalStream = StreamSnapshot[SignalKey, SignalRow]
DeliveryStream = StreamSnapshot[str, DeliveryRow]
JobTarget = tuple[str, str]


@dataclass(frozen=True)
class TickSnapshot:
    """Every stream as of the last SUCCESSFUL read of each.

    A stream whose read failed keeps its previous entry, which is what turns an
    outage into a reporting DELAY rather than a coverage gap: the next good poll
    fires every transition that happened while watch could not see, late but
    complete. A stream absent from the mapping has never been read - the diff
    seeds it silently instead of dumping its history as events.
    """

    jobs: Mapping[JobTarget, JobStream] = field(default_factory=dict)
    signals: Mapping[str, SignalStream] = field(default_factory=dict)
    deliveries: Mapping[str, DeliveryStream] = field(default_factory=dict)
    # slug -> {scan_config_id: name}, for the 'nightly replay' prefix.
    configs: Mapping[str, Mapping[str, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class WatchEvent:
    """One line. ``stream`` is derived, so it cannot disagree with the token."""

    event: str
    time: datetime
    message: str
    project: str | None = None
    target: Target | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    @property
    def stream(self) -> str:
        return STREAM_OF[self.event]


@dataclass(frozen=True)
class StallEntry:
    """How long one running job's fingerprint has been unchanged.

    The only genuinely accumulating state in watch, and it is pruned every tick
    to the job ids present in the current snapshot - so it inherits the
    jobs_limit x configs bound rather than needing an eviction policy.
    """

    fingerprint: tuple[Any, ...]
    since: datetime
    since_monotonic: float
    # Highest power-of-two multiple of the threshold already reported. 0 = none.
    reported: int = 0
    # True once a tick went by in which this job's scan config could NOT be read.
    # The rows the tracker sees during such a tick are the ones it saw last time,
    # so nothing was observed: the clock freezes, nothing is reported, and the
    # first successful read afterwards rebases it. Without this the tracker would
    # accrue unobserved seconds and then claim, in the same tick as "jobs read
    # failed: HTTP 502", that a job has been "unchanged for 4m" - two
    # contradictory lines, the second of them fabricated.
    unobserved: bool = False


@dataclass(frozen=True)
class FailureState:
    """One stream's run of consecutive failed reads."""

    consecutive: int = 0
    since: datetime | None = None
    # The monotonic reading of the first failure, mirroring StallEntry: the same
    # "how long has this been going" arithmetic must not be done on the wall
    # clock in one place and the monotonic clock in the other. An NTP step or a
    # DST jump would otherwise print a negative or wildly long gap on recovery.
    since_monotonic: float | None = None
    last_status_code: int | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class WatchOutcome:
    reason: str
    elapsed_seconds: float
    ticks: int
    requests: int
    counts: Mapping[str, int]
