"""Warehouse scan configs and their job history.

Carries the two pure facts about scan configs that both surfaces need — is the
dispatcher's own query going to select this one, and which config does a
``--scan`` selector mean — because each was defined once already and a second
copy is this repository's signature defect.

No projection for ``ScanJobResponse``. Its nine fields are all load-bearing:
``result_summary`` carries the replay chunk progress ``tripl watch`` reads, and
``error_message`` is the whole answer to "why did it fail". Pass a job through
verbatim; do not add a trim here for symmetry with the config summary below.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from tripl_cli.api.request import ApiRequest
from tripl_cli.model import JOBS_WINDOW, JsonDict, JsonList, names_by_id

CONFIGS = "/projects/{slug}/scans"
CONFIG = "/projects/{slug}/scans/{scan_id}"
RUN = "/projects/{slug}/scans/{scan_id}/run"
METRICS_REPLAY = "/projects/{slug}/scans/{scan_id}/metrics/replay"
JOBS = "/projects/{slug}/scans/{scan_id}/jobs"
JOB = "/projects/{slug}/scans/{scan_id}/jobs/{job_id}"
JOB_CANCEL = "/projects/{slug}/scans/{scan_id}/jobs/{job_id}/cancel"

ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("get", CONFIGS),
    ("get", CONFIG),
    ("post", RUN),
    ("post", METRICS_REPLAY),
    ("get", JOBS),
    ("get", JOB),
    ("post", JOB_CANCEL),
)

# The route's own ceiling (Query(ge=1, le=200)), reused from the constant doctor
# and the checks already share rather than spelled a third time.
JOBS_LIMIT_MAX = JOBS_WINDOW

# What a listing keeps. ScanConfigResponse carries `base_query` — free-text SQL
# that can run to kilobytes — plus ~20 tuning knobs that answer no operational
# question. Neither an agent budgeting context nor an operator reading a table
# wants them, and the MCP `list_scans` description already promises "(id, name,
# schedule, governed event types)", so this aligns the payload with the
# description rather than changing a contract anybody stated.
SCAN_CONFIG_SUMMARY_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "interval",
    "time_column",
    "data_source_id",
    "event_type_id",
    "event_type_column",
    "event_name_format",
    "replay_chunk_interval",
    "scan_lookback_hours",
    "created_at",
    "updated_at",
)


def list_configs(slug: str) -> ApiRequest:
    return ApiRequest("GET", CONFIGS.format(slug=slug))


def get_config(slug: str, scan_id: str) -> ApiRequest:
    return ApiRequest("GET", CONFIG.format(slug=slug, scan_id=scan_id))


def run(slug: str, scan_id: str) -> ApiRequest:
    """Trigger a run of a stored config. Editor-gated; a ``tk_w_`` key suffices."""
    return ApiRequest("POST", RUN.format(slug=slug, scan_id=scan_id))


def replay_metrics(slug: str, scan_id: str, body: JsonDict) -> ApiRequest:
    """Bounded metrics replay. Declared, and deliberately not reachable from the CLI.

    ``scans.py`` guards this route with ``deps.get_owner_user``, which rejects
    ANY request carrying ``request.state.api_key_scope`` — and every valid API
    key sets it, read or write. So a Bearer-token client cannot reach it at all
    and a ``tripl scans replay`` would 403 every time. The builder
    exists so the contract test still watches the route; see the follow-up bead
    on whether an owner-role ``tk_w_`` key should be allowed through.
    """
    return ApiRequest("POST", METRICS_REPLAY.format(slug=slug, scan_id=scan_id), json_body=body)


def list_jobs(slug: str, scan_id: str, *, limit: int | None = None) -> ApiRequest:
    """Newest jobs first. ``limit=None`` leaves the parameter off entirely.

    That is not the same as passing the default: the MCP takes whatever the
    server's default is (50 today), doctor asks for the maximum because it is
    measuring how long a streak has run, and watch asks for a smaller window
    because it repeats. Three deliberate answers, one builder.
    """
    return ApiRequest("GET", JOBS.format(slug=slug, scan_id=scan_id), params={"limit": limit})


def get_job(slug: str, scan_id: str, job_id: str) -> ApiRequest:
    return ApiRequest("GET", JOB.format(slug=slug, scan_id=scan_id, job_id=job_id))


def cancel_job(slug: str, scan_id: str, job_id: str) -> ApiRequest:
    """Cancel an active job. 409 when it is neither pending nor running."""
    return ApiRequest("POST", JOB_CANCEL.format(slug=slug, scan_id=scan_id, job_id=job_id))


def is_dispatchable(config: JsonDict) -> bool:
    """Whether the dispatcher's own query would ever select this config.

    ``schedule.py`` selects on ``ScanConfig.interval.isnot(None)`` AND
    ``ScanConfig.time_column.isnot(None)``. A config missing the time column is
    never dispatched, so asking for its job history is a request that can only
    return the empty list.
    """
    return bool(config.get("interval")) and bool(config.get("time_column"))


def scan_config_summary(config: JsonDict) -> JsonDict:
    """``SCAN_CONFIG_SUMMARY_FIELDS`` plus the one derived operational fact.

    ``dispatchable`` is the single most useful thing to know about a config and
    it must not be computed twice — the CLI table and the MCP tool read the same
    key rather than each applying the predicate.
    """
    summary = {key: config[key] for key in SCAN_CONFIG_SUMMARY_FIELDS if key in config}
    summary["dispatchable"] = is_dispatchable(config)
    return summary


def config_names(scans: JsonList) -> dict[str, str]:
    """``{scan_config_id: name}``, in the order the listing returned them.

    NOT filtered through ``is_dispatchable``. That predicate requires an interval
    AND a time column because doctor asks "is the scheduler working", and a
    config with no interval has no dispatch history worth reading. Watch — and
    ``tripl scans run`` — ask "what is running right now", and a manual replay on
    an unscheduled config is exactly what tripl-ey6j.4 was filed for.

    Kept as a name of its own because that exclusion is the fact worth writing
    down; the extraction itself is ``model.names_by_id``, which the event-type
    and branch selectors read through too.
    """
    return names_by_id(scans)


def resolve_selectors(
    named: Mapping[str, str], selectors: Sequence[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Resolve ``--scan``/``<scan>`` to config ids. Returns (matched, unmatched).

    Exact match on name first, then on id. Never substring, never
    case-insensitive: a follow mode that silently followed the wrong config
    because two names share a prefix is worse than one that refuses to start,
    and a ``scans run`` that triggered the wrong SQL for the same reason is
    worse still. Shared with watch so the two cannot disagree about what
    ``'prod events'`` names.
    """
    if not selectors:
        return tuple(named), ()
    by_name: dict[str, list[str]] = {}
    for config_id, name in named.items():
        by_name.setdefault(name, []).append(config_id)
    matched: list[str] = []
    unmatched: list[str] = []
    for selector in selectors:
        hits = by_name.get(selector) or ([selector] if selector in named else [])
        if not hits:
            unmatched.append(selector)
            continue
        matched.extend(config_id for config_id in hits if config_id not in matched)
    return tuple(matched), tuple(unmatched)
