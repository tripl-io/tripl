"""The only layer that speaks HTTP. Async, impure, and it never raises upward.

Every read is wrapped into a ``Fetched`` so a 403, a 404 and a dead socket all
arrive at the check layer as data. That is what makes doctor total: an
unreachable instance still produces a complete, parseable document instead of a
traceback (tripl-ey6j.2).

The exceptions are ``raise_selection_failure`` and ``read_or_raise``, which the
commands with no verdict contract call deliberately — ``status`` and the
``scans`` / ``drifts`` verbs either print the instance or fail like any other
command, so "could not reach the instance" is an ordinary CLI failure there
rather than a finding. Both opt back OUT of the ``Fetched`` regime; neither
throws the failure away.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from tripl_cli.api import auth as auth_api
from tripl_cli.api import data_sources as data_sources_api
from tripl_cli.api import event_types as event_types_api
from tripl_cli.api import monitoring as monitoring_api
from tripl_cli.api import projects as projects_api
from tripl_cli.api import scans as scans_api
from tripl_cli.api.request import ApiRequest, send
from tripl_cli.client import API_PREFIX, DEFAULT_USER_AGENT, TriplClient
from tripl_cli.config import Config
from tripl_cli.errors import TriplAPIError, TriplConnectionError, TriplError
from tripl_cli.model import (
    HEALTH_PATH,
    JOBS_WINDOW,
    SCOPE_PROJECT,
    CoverageStatus,
    DriftCoverage,
    Fetched,
    Instance,
    JsonDict,
    JsonList,
    ProjectSelection,
    ProjectStatus,
    SectionError,
    Snapshot,
    StatusSnapshot,
    as_dict,
    as_list,
    float_of,
    int_of,
    scope_from_auth,
    text_of,
)
from tripl_cli.runner import REQUEST_TIMEOUT_SECONDS, gather_bounded

# The API's maximum (schema: minimum 1, default 50, maximum 200).
#
# Deliberately NOT schedule.py's _FAILURE_STREAK_SCAN_LIMIT (= 40). The backend
# reads 40 rows because all it needs is the DELAY, which saturates at
# FAILURE_BACKOFF_AFTER + 3 = 6 failures — truncating a longer streak there can
# only shorten the wait, which is the safe direction for a scheduler.
#
# doctor answers a different question: "how long has this been broken". At 40
# rows a 15m config failing for four days reports 40 failures since this morning
# instead of 384 since Monday, and that is the single fact the operator came for.
# Any window past 6 gives the same backoff estimate, so asking for the maximum
# costs one query parameter and buys 5x the history. Saturation is still possible
# at 200, and is reported rather than hidden - see FailureStreak.saturated.
JOBS_LIMIT = JOBS_WINDOW

DEFAULT_MAX_EVENT_TYPES = 200
DEFAULT_COVERAGE_DAYS = 7


@dataclass(frozen=True)
class DoctorOptions:
    project_slugs: tuple[str, ...] = ()
    include_demo: bool = False
    max_event_types: int = DEFAULT_MAX_EVENT_TYPES
    timeout: float = REQUEST_TIMEOUT_SECONDS


@dataclass(frozen=True)
class StatusOptions:
    project_slugs: tuple[str, ...] = ()
    include_demo: bool = False
    days: int = DEFAULT_COVERAGE_DAYS


class Reader:
    """One authenticated, request-counting view of the API.

    ``api_key=""`` is not an oversight: with ``http_client`` supplied,
    ``TriplClient`` borrows the caller's pool and never builds a transport of its
    own, so the key it was handed is unused — the Authorization header already
    lives on the pool ``runner.run_async`` opened. Passing the real key here
    would put a second copy of the credential in a second object for no reason.
    """

    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self._api = TriplClient(base_url=base_url, api_key="", http_client=client)
        self.requests = 0

    async def send(self, request: ApiRequest) -> Any:
        """Perform one request and RAISE on failure. The single primitive.

        Used directly by ``status`` (no verdict contract, so an unreachable
        instance is an ordinary command failure) and by the mutating commands,
        where a refusal must reach the operator as a non-zero exit rather than
        as a report section (tripl-ey6j.5). Everything doctor and watch read goes
        through ``try_read*`` instead.
        """
        self.requests += 1
        return await send(self._api, request)

    @property
    def last_status_code(self) -> int | None:
        """The HTTP status of the last ``send``, for the one caller that needs it.

        ``tripl annotate`` tells "created" (201) from "de-duplicated" (200) by
        this alone - the two bodies have the same shape.
        """
        return self._api.last_status_code

    async def try_read(self, request: ApiRequest) -> Fetched[Any]:
        try:
            value = await self.send(request)
        except TriplAPIError as exc:
            return Fetched(value=None, status_code=exc.status_code, error=str(exc))
        except TriplConnectionError as exc:
            return Fetched(value=None, status_code=None, error=str(exc))
        return Fetched(value=value, status_code=200)

    async def try_read_list(self, request: ApiRequest) -> Fetched[JsonList]:
        raw = await self.try_read(request)
        if not raw.ok:
            return Fetched(value=None, status_code=raw.status_code, error=raw.error)
        if not isinstance(raw.value, list):
            # A payload of the wrong shape is a failure, not an empty list —
            # the same rule as a non-200 (TRAP 3).
            return Fetched(
                value=None,
                status_code=raw.status_code,
                error=(
                    f"expected a JSON array from {request.path}, got {type(raw.value).__name__}"
                ),
            )
        return Fetched(value=as_list(raw.value), status_code=raw.status_code)

    async def try_read_dict(self, request: ApiRequest) -> Fetched[JsonDict]:
        raw = await self.try_read(request)
        if not raw.ok:
            return Fetched(value=None, status_code=raw.status_code, error=raw.error)
        if not isinstance(raw.value, dict):
            return Fetched(
                value=None,
                status_code=raw.status_code,
                error=(
                    f"expected a JSON object from {request.path}, got {type(raw.value).__name__}"
                ),
            )
        return Fetched(value=as_dict(raw.value), status_code=raw.status_code)


async def probe_health(base_url: str, timeout: float) -> Fetched[JsonDict]:
    """Probe ``/health`` on its own client, with NO Authorization header.

    Unauthenticated on purpose. It separates two diagnoses that looked identical
    during the incident — "is the instance up and is its database reachable"
    from "is your key good" — because a 401 on an authenticated probe is
    ambiguous between them. It also keeps the credential off an endpoint that by
    design requires none, and therefore out of whatever proxy or access log
    fronts it.

    A separate client rather than a bypass in ``client.py``: that module is
    shared with tripl-mcp and nothing consumer-specific belongs in it.
    """
    url = f"{base_url.rstrip('/')}{HEALTH_PATH}"
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        ) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        return Fetched(value=None, status_code=None, error=repr(exc))
    body: JsonDict | None = None
    try:
        parsed = response.json()
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        body = parsed
    if response.status_code == 200 and body is not None and body.get("status") == "ok":
        return Fetched(value=body, status_code=200)
    snippet = response.text[:120]
    return Fetched(
        value=body,
        status_code=response.status_code,
        error=f"HTTP {response.status_code}, body: {snippet!r}",
    )


async def probe_auth_status(base_url: str, timeout: float) -> Fetched[JsonDict]:
    """Read ``/auth/status`` with NO Authorization header. Same regime as ``probe_health``.

    Unauthenticated by NECESSITY here rather than by choice: ``tripl install``
    calls this against an instance it has just started, where the first account
    does not exist yet and therefore no API key can (tripl-ey6j.3). The endpoint
    is unauthenticated on the backend for the same reason — the sign-in screen
    reads it before anybody is signed in.

    The path and method come from ``api.auth.get_status()`` so there is no second
    spelling of the route anywhere; only the transport differs, because the
    shared ``TriplClient`` pool always carries a credential and this must not.
    """
    request = auth_api.get_status()
    url = f"{base_url.rstrip('/')}{API_PREFIX}{request.path}"
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        ) as client:
            response = await client.request(request.method, url)
    except httpx.HTTPError as exc:
        return Fetched(value=None, status_code=None, error=repr(exc))
    try:
        parsed = response.json()
    except ValueError:
        parsed = None
    if response.status_code == 200 and isinstance(parsed, dict):
        return Fetched(value=as_dict(parsed), status_code=200)
    return Fetched(
        value=None,
        status_code=response.status_code,
        error=f"HTTP {response.status_code}, body: {response.text[:120]!r}",
    )


def filter_projects(
    projects: Sequence[JsonDict], *, include_demo: bool
) -> tuple[tuple[JsonDict, ...], int]:
    """Drop demo projects unless asked for them; report how many were dropped.

    Demos are excluded by default because their ``demo_runtime_tick`` jobs and
    the demo collection cooldown are noise in exactly the check that matters. The
    exclusion is always PRINTED, never silent — a diagnostic that quietly ignores
    half the instance is worse than one that refuses to run.
    """
    if include_demo:
        return tuple(projects), 0
    kept = tuple(project for project in projects if not project.get("is_demo"))
    return kept, len(projects) - len(kept)


async def select_projects(
    reader: Reader,
    *,
    slugs: Sequence[str],
    include_demo: bool,
    scope: str,
) -> ProjectSelection:
    """Resolve which projects this run reports on. Shared by doctor and status.

    Named slugs are fetched one by one; otherwise the instance listing is used.
    A project-scoped key is not sent to the listing at all — ``deps.py`` refuses
    it by design, so spending the request only to translate a 403 into advice
    would be a request that can never succeed (TRAP 5).
    """
    if slugs:
        fetched = await gather_bounded(
            [reader.try_read_dict(projects_api.get_project(slug)) for slug in slugs]
        )
        by_slug = dict(zip(slugs, fetched, strict=True))
        found = [item.value for item in fetched if item.value is not None]
        # An explicitly named project is always included, demo or not: naming it
        # IS the intent, and --include-demo exists for the listing path.
        return ProjectSelection(
            requested_slugs=tuple(slugs),
            include_demo=include_demo,
            listing=None,
            by_slug=by_slug,
            projects=tuple(found),
        )
    if scope == SCOPE_PROJECT:
        return ProjectSelection(include_demo=include_demo, listing=None)
    listing = await reader.try_read_list(projects_api.list_projects())
    if not listing.ok or listing.value is None:
        return ProjectSelection(include_demo=include_demo, listing=listing)
    kept, excluded = filter_projects(listing.value, include_demo=include_demo)
    return ProjectSelection(
        include_demo=include_demo,
        listing=listing,
        projects=kept,
        excluded_demo_count=excluded,
    )


def raise_selection_failure(selection: ProjectSelection) -> None:
    """Turn a failed project read back into the error the client produced.

    ``status`` has no verdict contract — it either prints the instance or fails
    like any other command — so it opts back out of the Fetched regime here. The
    message is the client's own, verbatim, so what the operator reads is exactly
    what a non-wrapped call would have printed. Only the exception TYPE is
    reconstructed, and only far enough to carry the right exit code.
    """
    failures: list[Fetched[Any]] = [item for item in selection.by_slug.values() if not item.ok]
    if selection.listing is not None and not selection.listing.ok:
        failures.append(selection.listing)
    for failure in failures:
        message = failure.error or "the tripl API did not answer"
        if failure.status_code == 403 and selection.listing is failure:
            # doctor answers this one specifically (project_not_authorized); the
            # bare client message would send a status user hunting a broken
            # instance instead of typing one flag. A project-scoped key is fenced
            # to one project by design and instance-level listing is meant to 403.
            message = (
                f"{message} If this key is scoped to a single project, that 403 is "
                "expected - name the project with --project <slug>."
            )
        if failure.status_code is not None:
            raise TriplAPIError(failure.status_code, None, message)
        raise TriplError(message)


def read_or_raise[T](fetched: Fetched[T]) -> T:
    """The payload, or the failure that stopped it arriving. Never a stand-in.

    ``try_read_list`` / ``try_read_dict`` already treat a payload of the WRONG
    SHAPE as a failure, exactly as they treat a non-200 (TRAP 3). This is how a
    command with no verdict contract consumes that verdict: a 200 carrying an
    object where an array belongs becomes a printed message and exit 1, never a
    rendered empty table.

    Only the exception TYPE is reconstructed, and only far enough to carry the
    right exit code — the message is the client's own, verbatim. A wrong-shaped
    200 is deliberately NOT a ``TriplAPIError``: that class means "the instance
    answered 4xx/5xx", and a consumer branching on its ``status_code`` would be
    told the opposite of what happened.
    """
    if fetched.ok and fetched.value is not None:
        return fetched.value
    message = fetched.error or "the tripl API did not answer"
    status = fetched.status_code
    if status is not None and status >= 400:
        raise TriplAPIError(status, None, message)
    raise TriplError(message)


def instance_of(config: Config, base_url: str, scope: str) -> Instance:
    return Instance(
        base_url=base_url,
        base_url_source=config.sources.get("base_url", "unknown"),
        api_key_source=config.sources.get("api_key", "unknown"),
        api_key_scope=scope,
    )


async def collect_doctor(
    client: httpx.AsyncClient,
    config: Config,
    options: DoctorOptions,
    *,
    base_url: str,
    now: datetime,
) -> Snapshot:
    """Read everything doctor judges, in one pass, and judge none of it."""
    health = await probe_health(base_url, options.timeout)
    sources = dict(config.sources)
    base_source = sources.get("base_url", "unknown")
    key_source = sources.get("api_key", "unknown")
    if health.ok is False or health.value is None or health.value.get("status") != "ok":
        # Short-circuit: with the instance unreachable every other call would
        # spend its own full timeout to learn the same thing. This is what bounds
        # a dead-host run to roughly ONE timeout rather than one per endpoint.
        return Snapshot(
            now=now,
            base_url=base_url,
            base_url_source=base_source,
            api_key_source=key_source,
            api_key_scope="unknown",
            requests=1,
            health=health,
            auth=None,
        )

    reader = Reader(client, base_url)
    auth = await reader.try_read_dict(auth_api.get_me())
    scope = scope_from_auth(auth)
    selection = await select_projects(
        reader,
        slugs=options.project_slugs,
        include_demo=options.include_demo,
        scope=scope,
    )
    projects = selection.projects
    slugs = [slug for slug in (text_of(project, "slug") for project in projects) if slug]

    data_sources: Fetched[JsonList] | None = None
    if projects and scope != SCOPE_PROJECT:
        data_sources = await reader.try_read_list(data_sources_api.list_data_sources())

    scans: dict[str, Fetched[JsonList]] = {}
    if slugs:
        results = await gather_bounded(
            [reader.try_read_list(scans_api.list_configs(slug)) for slug in slugs]
        )
        scans = dict(zip(slugs, results, strict=True))

    job_targets: list[tuple[str, str]] = [
        (slug, config_id)
        for slug in slugs
        for scan_config in (scans[slug].value or [])
        if scans_api.is_dispatchable(scan_config)
        for config_id in (text_of(scan_config, "id"),)
        if config_id
    ]
    jobs: dict[tuple[str, str], Fetched[JsonList]] = {}
    if job_targets:
        results = await gather_bounded(
            [
                reader.try_read_list(scans_api.list_jobs(slug, scan_id, limit=JOBS_LIMIT))
                for slug, scan_id in job_targets
            ]
        )
        jobs = dict(zip(job_targets, results, strict=True))

    event_types: dict[str, Fetched[JsonList]] = {}
    if slugs:
        results = await gather_bounded(
            [reader.try_read_list(event_types_api.list_event_types(slug)) for slug in slugs]
        )
        event_types = dict(zip(slugs, results, strict=True))

    # The budgeted round-robin fan-out lives in api.event_types because
    # `tripl drifts list` needs exactly the same plan, and two implementations of
    # it is how "we did not look there" starts printing as "nothing there"
    # (tripl-ey6j.5). What the budget could not reach is still REPORTED here, as
    # drift_scan_truncated, and drift_coverage records what each project got.
    drift_targets, examined, totals = event_types_api.plan_drift_targets(
        {slug: event_types[slug].value or [] for slug in slugs},
        budget=options.max_event_types,
    )
    drifts: dict[tuple[str, str], Fetched[JsonDict]] = {}
    if drift_targets:
        dict_results = await gather_bounded(
            [
                reader.try_read_dict(event_types_api.list_drifts(slug, type_id))
                for slug, type_id in drift_targets
            ]
        )
        drifts = dict(zip(drift_targets, dict_results, strict=True))

    return Snapshot(
        now=now,
        base_url=base_url,
        base_url_source=base_source,
        api_key_source=key_source,
        api_key_scope=scope,
        requests=reader.requests + 1,  # + the unauthenticated /health probe
        health=health,
        auth=auth,
        selection=selection,
        data_sources=data_sources,
        scans=scans,
        jobs=jobs,
        event_types=event_types,
        drifts=drifts,
        drift_coverage={
            slug: DriftCoverage(examined=examined[slug], total=totals[slug]) for slug in slugs
        },
    )


def _coverage_of(payload: JsonDict) -> CoverageStatus:
    summary = as_dict(payload.get("summary"))
    return CoverageStatus(
        days=int_of(payload, "days"),
        pct=float_of(summary, "coverage_pct"),
        matched=int_of(summary, "matched_count"),
        total=int_of(summary, "total_count"),
    )


async def collect_status(
    client: httpx.AsyncClient,
    config: Config,
    options: StatusOptions,
    *,
    base_url: str,
    generated_at: datetime,
) -> StatusSnapshot:
    """One project read per project, plus one coverage read.

    Every count in the output comes from ``ProjectResponse.summary``, which the
    backend computes. That is deliberate and it is what keeps TRAP 2 shut:
    ``monitoring_signal_count`` is already filtered through
    ``metrics_insights_service.is_significant_signal``, so reading it means the
    CLI agrees with the app's own badge BY CONSTRUCTION and carries no third copy
    of SIGNIFICANT_MIN_REL_EFFECT. Same for ``firing_monitor_count``, which the
    backend keeps in lockstep with MonitorsSummaryResponse.firing_count.
    """
    reader = Reader(client, base_url)
    selection = await select_projects(
        reader,
        slugs=options.project_slugs,
        include_demo=options.include_demo,
        scope="unknown",
    )
    raise_selection_failure(selection)

    projects = selection.projects
    slugs = [slug for slug in (text_of(project, "slug") for project in projects) if slug]
    coverage: dict[str, Fetched[JsonDict]] = {}
    if slugs:
        results = await gather_bounded(
            [
                reader.try_read_dict(monitoring_api.get_coverage(slug, days=options.days))
                for slug in slugs
            ]
        )
        coverage = dict(zip(slugs, results, strict=True))

    rows: list[ProjectStatus] = []
    for project in projects:
        slug = text_of(project, "slug") or ""
        summary = as_dict(project.get("summary"))
        fetched = coverage.get(slug)
        errors: list[SectionError] = []
        row_coverage: CoverageStatus | None = None
        if fetched is not None and fetched.ok and fetched.value is not None:
            row_coverage = _coverage_of(fetched.value)
        elif fetched is not None:
            # A failed section leaves its own key null and says why, rather than
            # blanking the row: the counts that DID arrive are still the answer.
            errors.append(
                SectionError(
                    section="coverage",
                    status_code=fetched.status_code,
                    message=fetched.error or "the tripl API did not answer",
                )
            )
        rows.append(
            ProjectStatus(
                slug=slug,
                name=text_of(project, "name") or slug,
                is_demo=bool(project.get("is_demo")),
                event_count=int_of(summary, "event_count"),
                active_event_count=int_of(summary, "active_event_count"),
                event_type_count=int_of(summary, "event_type_count"),
                scan_count=int_of(summary, "scan_count"),
                failing_scan_count=int_of(summary, "failing_scan_config_count"),
                significant_open_signals=int_of(summary, "monitoring_signal_count"),
                firing_monitors=int_of(summary, "firing_monitor_count"),
                coverage=row_coverage,
                errors=tuple(errors),
            )
        )

    return StatusSnapshot(
        # status never probes /auth/me, so it cannot know the key's reach and
        # says so rather than guessing. `tripl doctor` is the command that
        # answers that question.
        instance=instance_of(config, base_url, "unknown"),
        generated_at=generated_at,
        duration_ms=0,
        requests=reader.requests,
        projects=tuple(rows),
    )
