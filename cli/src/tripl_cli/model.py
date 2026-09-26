"""The vocabulary the commands speak: severities, findings, checks, snapshots.

Pure data plus the handful of total functions over it. No IO, no clock reads,
no import of ``collect`` — everything here can be built in a test without a
network, which is what lets the check rules be tested as arithmetic.

At the package ROOT rather than inside ``diagnostics``, because ``api``,
``install``, ``watch`` and every command group import it while only ``doctor``
reaches a verdict: the JSON helpers, ``JOBS_WINDOW``, ``Instance``, ``Run``,
``SectionError`` and the ``scans``/``drifts`` snapshots are facts about the wire
and about one CLI run, not about doctor's verdict machinery (tripl-azhh).
Splitting this file in two — a shared half here, a doctor half left in
``diagnostics`` — was rejected: ``Fetched``, ``Target``, ``Instance`` and
``SectionError`` are read from both sides, so the split line would fall
somewhere a contributor has to re-guess for every type added after it.

It also imports nothing from ``tripl_cli.api``, and must not: the request layer
imports the JSON helpers below, so a reference back would close the cycle. The
one consequence is that a snapshot carries an already-projected request DOCUMENT
rather than an ``ApiRequest`` — which is the right shape anyway, because that
document is the thing ``--dry-run`` prints and it must never be able to carry a
header or a credential (tripl-ey6j.5).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from tripl_cli.errors import EXIT_CHECKS_FAILED, EXIT_OK

JsonDict = dict[str, Any]
JsonList = list[JsonDict]


class Severity(StrEnum):
    """What a check or finding says about the instance.

    Four levels, and ``SKIP`` is a first-class one rather than a silent pass:
    "I could not look" and "I looked and it was fine" are the two answers the
    incident kept confusing. ``PASS`` spells its member name out because ``pass``
    is a keyword.
    """

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


# Fixed ids, fixed order, each appearing exactly once per run. Per-project
# results are `findings` INSIDE a check, never repeated checks — a consumer
# selects by id and gets one row.
CHECK_IDS: tuple[str, ...] = (
    "connectivity",
    "auth",
    "projects",
    "data_sources",
    "scans",
    "drifts",
)

SCOPE_INSTANCE = "instance"
SCOPE_PROJECT = "project"
SCOPE_UNKNOWN = "unknown"

# NOT under /api/v1, and unauthenticated: backend/src/tripl/main.py:263 declares
# `@app.get("/health", include_in_schema=False)`, so it is absent from the
# OpenAPI document too. It answers 200 {"status":"ok"} or 503
# {"status":"error","component":"database"}. Lives here rather than in collect so
# the pure check layer can name it in a message without importing the HTTP layer.
HEALTH_PATH = "/health"

# How many scan jobs one history read asks for. Set by collect (it is a query
# parameter) and needed by the checks (a streak that fills the whole window is a
# lower bound, not a measurement), so it belongs to neither of them alone.
JOBS_WINDOW = 200

# The five values of the backend's ScanInterval enum, in seconds. The codes come
# from backend/src/tripl/models/domain_enums.py::ScanInterval; the DURATIONS are
# a second spelling of backend/src/tripl/core/intervals.py::INTERVALS, which is
# the map schedule.py actually feeds to the backoff via get_interval(). Needed
# because every staleness and backoff rule here is expressed in multiples of a
# config's own interval.
INTERVAL_SECONDS: Mapping[str, int] = {
    "15m": 900,
    "1h": 3600,
    "6h": 21600,
    "1d": 86400,
    "1w": 604800,
}


@dataclass(frozen=True)
class Fetched[T]:
    """One read that either produced a payload or produced a reason it did not.

    This type is how TRAP 3 stops being a matter of discipline: a caller cannot
    reach the payload without passing a value that is ``None`` when the call
    failed, so "the endpoint 404'd" can never be silently read as "the list was
    empty". That misreading is what invalidated the 2026-07-30 audit.
    """

    value: T | None = None
    status_code: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.value is not None


@dataclass(frozen=True)
class Target:
    """The object a finding - or a watch event - is about, and how to find it.

    The ``kind`` enumeration below is canonical: it is the complete set a
    consumer can switch on, so anything that builds a Target has to appear in it.
    ``scan_job`` and ``alert_delivery`` are watch's, and exist because a follow
    mode points at the individual RUN and the individual DELIVERY, where doctor
    points at the config that owns them.
    """

    kind: str  # "scan_config" | "event_type" | "data_source" | "project"
    #        | "scan_job" | "alert_delivery"
    id: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class Finding:
    """One problem, in the operator's words plus machine-readable evidence.

    ``code`` and ``evidence`` are the contract; ``message`` is prose and may be
    reworded in any release. Severity is only ever WARN or FAIL — a finding is
    by definition something to look at.
    """

    code: str
    severity: Severity
    message: str
    project: str | None = None
    target: Target | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Check:
    id: str
    title: str
    status: Severity
    summary: str
    skip_reason: str | None = None
    findings: tuple[Finding, ...] = ()


@dataclass(frozen=True)
class Instance:
    base_url: str
    base_url_source: str
    api_key_source: str
    api_key_scope: str


@dataclass(frozen=True)
class ProjectSelection:
    """Which projects this run looked at, and how it decided.

    Shared by doctor and status so the two commands cannot drift on *which*
    projects they report about.
    """

    requested_slugs: tuple[str, ...] = ()
    include_demo: bool = False
    # None means the listing was never attempted — either --project was given or
    # the key is project-scoped and the listing 403s by design.
    listing: Fetched[JsonList] | None = None
    by_slug: Mapping[str, Fetched[JsonDict]] = field(default_factory=dict)
    projects: tuple[JsonDict, ...] = ()
    excluded_demo_count: int = 0


@dataclass(frozen=True)
class DriftCoverage:
    """How much of ONE project's event-type list the drift budget reached.

    Per project rather than per run: ``--max-event-types`` is spent round-robin
    across projects (collect), so an instance-wide "40 of 90 examined" cannot say
    WHICH project was only partly looked at — and naming it is the whole value of
    reporting truncation at all (tripl-ey6j.9).
    """

    examined: int = 0
    total: int = 0

    @property
    def truncated(self) -> bool:
        return self.total > self.examined


def _healthy() -> Fetched[JsonDict]:
    return Fetched(value={"status": "ok"}, status_code=200)


@dataclass(frozen=True)
class Snapshot:
    """Everything doctor read, before any of it was judged.

    Defaults describe a reachable instance with nothing in it, so a rule test
    sets only the two or three fields its rule actually reads.
    """

    now: datetime
    base_url: str = "https://tripl.example.com"
    base_url_source: str = "$TRIPL_BASE_URL"
    api_key_source: str = "$TRIPL_API_KEY"
    api_key_scope: str = SCOPE_INSTANCE
    requests: int = 0
    health: Fetched[JsonDict] = field(default_factory=_healthy)
    # None means "not attempted" — the connectivity check short-circuited.
    auth: Fetched[JsonDict] | None = field(
        default_factory=lambda: Fetched(value={}, status_code=200)
    )
    selection: ProjectSelection | None = None
    data_sources: Fetched[JsonList] | None = None
    scans: Mapping[str, Fetched[JsonList]] = field(default_factory=dict)
    jobs: Mapping[tuple[str, str], Fetched[JsonList]] = field(default_factory=dict)
    event_types: Mapping[str, Fetched[JsonList]] = field(default_factory=dict)
    drifts: Mapping[tuple[str, str], Fetched[JsonDict]] = field(default_factory=dict)
    # Keyed by project slug. The instance-wide totals below are DERIVED from it
    # rather than carried alongside it, so the per-project and the whole-run
    # numbers cannot disagree.
    drift_coverage: Mapping[str, DriftCoverage] = field(default_factory=dict)

    @property
    def selected_projects(self) -> tuple[JsonDict, ...]:
        return self.selection.projects if self.selection is not None else ()

    @property
    def event_types_examined(self) -> int:
        return sum(coverage.examined for coverage in self.drift_coverage.values())

    @property
    def event_types_seen(self) -> int:
        return sum(coverage.total for coverage in self.drift_coverage.values())


@dataclass(frozen=True)
class Report:
    command: str
    instance: Instance
    generated_at: datetime
    duration_ms: int
    requests: int
    checks: tuple[Check, ...]

    @property
    def status(self) -> Severity:
        return overall_status(self.checks)

    @property
    def counts(self) -> dict[str, int]:
        # All four keys always present, including the zeroes: a consumer that
        # has to test for key existence before reading a count will eventually
        # forget to.
        tally = {level.value: 0 for level in Severity}
        for check in self.checks:
            tally[check.status.value] += 1
        return tally


@dataclass(frozen=True)
class CoverageStatus:
    days: int
    pct: float
    matched: int
    total: int


@dataclass(frozen=True)
class SectionError:
    section: str
    status_code: int | None
    message: str
    # The CONCRETE path that failed. Optional because `status` names its section
    # and nothing else (one coverage read per project, so the section IS the
    # address); the scans/drifts fan-outs need it, because "one of this project's
    # 40 event types could not be read" is unactionable without saying which.
    endpoint: str | None = None


@dataclass(frozen=True)
class ProjectStatus:
    slug: str
    name: str
    is_demo: bool
    event_count: int
    active_event_count: int
    event_type_count: int
    scan_count: int
    failing_scan_count: int
    significant_open_signals: int
    firing_monitors: int
    coverage: CoverageStatus | None = None
    errors: tuple[SectionError, ...] = ()


@dataclass(frozen=True)
class StatusSnapshot:
    instance: Instance
    generated_at: datetime
    duration_ms: int
    requests: int
    projects: tuple[ProjectStatus, ...]


@dataclass(frozen=True)
class Run:
    """What every ``tripl <group> <verb>`` invocation carries in common.

    Composed into the snapshots below rather than repeated across them, so the
    envelope ``report._envelope`` builds cannot differ between two commands.
    """

    instance: Instance
    generated_at: datetime
    duration_ms: int = 0
    requests: int = 0


@dataclass(frozen=True)
class ProjectScans:
    """One project's scan configs, already trimmed by ``api.scans.scan_config_summary``."""

    slug: str
    name: str
    is_demo: bool = False
    scans: tuple[JsonDict, ...] = ()
    errors: tuple[SectionError, ...] = ()


@dataclass(frozen=True)
class ScansSnapshot:
    run: Run
    projects: tuple[ProjectScans, ...] = ()

    @property
    def scan_count(self) -> int:
        return sum(len(project.scans) for project in self.projects)

    @property
    def error_count(self) -> int:
        """How many reads did not arrive. Same property name as ``DriftsSnapshot``
        because the two footers say the same sentence, from the same helper."""
        return sum(len(project.errors) for project in self.projects)

    @property
    def failed(self) -> bool:
        """Any project whose scan listing did not arrive.

        A partial listing that exits 0 is the trap this whole tool exists to
        close: "3 scan configs" when a fourth project 403'd is a lie the operator
        cannot see (TRAP 3).
        """
        return self.error_count > 0


@dataclass(frozen=True)
class ScanJobsSnapshot:
    run: Run
    project: str
    scan_id: str
    scan_name: str
    limit: int
    # ScanJobResponse rows verbatim, newest first — the order the API returns.
    jobs: tuple[JsonDict, ...] = ()


@dataclass(frozen=True)
class DriftRow:
    """One SchemaDriftResponse, verbatim, plus the two facts it cannot carry.

    ``event_type_name`` comes from the event-type listing (a drift knows only its
    ``event_type_id``) and ``untriaged`` is ``api.event_types.is_untriaged``
    evaluated against this run's clock.
    """

    drift: JsonDict
    event_type_name: str
    untriaged: bool


@dataclass(frozen=True)
class ProjectDrifts:
    slug: str
    name: str
    is_demo: bool = False
    event_types_total: int = 0
    event_types_examined: int = 0
    drifts: tuple[DriftRow, ...] = ()
    errors: tuple[SectionError, ...] = ()

    @property
    def truncated(self) -> bool:
        return self.event_types_total > self.event_types_examined


@dataclass(frozen=True)
class DriftsSnapshot:
    run: Run
    status_filter: str
    projects: tuple[ProjectDrifts, ...] = ()

    @property
    def drift_count(self) -> int:
        return sum(len(project.drifts) for project in self.projects)

    @property
    def untriaged_count(self) -> int:
        return sum(1 for project in self.projects for row in project.drifts if row.untriaged)

    @property
    def error_count(self) -> int:
        return sum(len(project.errors) for project in self.projects)

    @property
    def failed(self) -> bool:
        return self.error_count > 0


@dataclass(frozen=True)
class PlanRead:
    """One read of the plan or the event catalog, from any ``events``/``plan`` verb.

    ONE snapshot for all seven verbs rather than seven near-identical ones, and
    the same for the document ``report.plan_read_document`` builds from it. Each
    verb reads a different resource, but every one of them answers the same four
    questions — which project, which plan revision, how much of the resource
    came back, and what the rows are — so seven types would be seven places for
    the envelope to drift and seven ``jq`` idioms for a consumer to learn
    (tripl-3ixs). The rows themselves stay verbatim; ``kind`` says what one of
    them IS.

    NO ``errors`` tuple, unlike ``ScansSnapshot`` and ``DriftsSnapshot``. Those
    two fan out — one request per project, per event type — so a failed read
    there is one row of many and has to be reported beside the rows that
    arrived. Every verb here reads ONE resource of ONE project, so a failed read
    means there is no answer at all: it raises, prints the client's message and
    exits 1, the way ``scans jobs`` does. A partial-truth path that cannot exist
    needs no representation.
    """

    run: Run
    # "events list", "plan types", ... — the same string the envelope carries.
    command: str
    # What ONE item is: "event", "event_type", "field", "variable", "branch",
    # "search_result". Published in the document so a consumer can tell which
    # shape `items` holds without parsing `command`.
    kind: str
    project: str
    items: tuple[JsonDict, ...] = ()
    # None for both means main. The pair rather than an id alone because the id
    # is a UUID nobody recognises and the operator asked for a name.
    branch_id: str | None = None
    branch_name: str | None = None
    # The API's own total, where the route reports one; None where it answers a
    # bare array and the rows ARE the whole answer.
    total: int | None = None
    offset: int | None = None
    limit: int | None = None
    # The route's OWN answer to "were rows left behind", where it gives one, and
    # None where it does not (tripl-wkwv.3). Not published as its own key: it
    # feeds ``truncated`` below, which is the one thing a consumer reads.
    route_truncated: bool | None = None
    # Facts the ROUTE reported about the answer itself rather than about any
    # row. Exactly one today — search's ``semantic_used``, which says whether
    # the hits are semantic or substring matches and therefore how much a low
    # score means. Deliberately narrow: row data never goes in here, or this
    # becomes the second document shape the one-shape rule above exists to
    # prevent. Always present in the JSON, ``{}`` where a route reports none.
    meta: Mapping[str, Any] = field(default_factory=dict)

    @property
    def noun(self) -> str:
        """``kind`` as the footer says it: ``event_type`` -> ``event type``.

        Derived rather than a second field, so a new kind cannot ship with a
        noun that disagrees with it.
        """
        return self.kind.replace("_", " ")

    @property
    def truncated(self) -> bool:
        """More rows exist than this page carried.

        Reported rather than left to the reader, and for the same reason
        ``DriftCoverage.truncated`` is: a list that stops at the limit looks
        exactly like a list that ended, and the second reading is the dangerous
        one.

        A ladder of three rungs, best evidence first.

        THE ROUTE'S OWN ANSWER, where it gives one (tripl-wkwv.3). ``/search``
        does: it retrieves one row past its candidate window, so it knows
        whether a hit was dropped rather than inferring it, and a page that
        happens to be exactly full is not a dropped hit. Preferring the guess
        over that answer is how ``plan search`` came to print "more may have
        matched" on the same HTTP body that said nothing was dropped — while
        ``tripl-mcp``'s ``search_plan``, reading the same key through the same
        shared reader, answered ``false``.

        ``total``, which is what the API counted BEFORE paging: where a route
        reports one it can tell a full page from a finished list.

        A FULL PAGE, last, because it cannot tell the two apart at all — it is
        only ever "the page filled, and nothing better was on offer". It stays
        for the instance that predates ``truncated``: silence from an old route
        is not that route saying nothing was dropped.

        The last two require ``limit`` and no ``offset``: a verb that pages by
        offset has a real count and must not fall back to guessing from page
        fullness, and a verb that sent no limit cannot have filled one.
        """
        if self.route_truncated is not None:
            return self.route_truncated
        if self.total is not None:
            return self.total > (self.offset or 0) + len(self.items)
        return self.offset is None and self.limit is not None and len(self.items) >= self.limit


@dataclass(frozen=True)
class MutationOutcome:
    """One write the CLI performed, or would have performed under ``--dry-run``.

    ``request`` is the projected document — method, path, params, body and
    NOTHING else. No headers, no Authorization value, no API key: this object is
    printed, and a credential that can be printed eventually is.

    Every optional key is carried on every mutation and serialised even when
    null. A consumer that has to test for key existence before reading one will
    eventually forget to, which is the same rule ``Report.counts`` follows.
    """

    command: str
    run: Run
    request: JsonDict
    project: str
    dry_run: bool = False
    scan_id: str | None = None
    scan_name: str | None = None
    job_id: str | None = None
    drift_id: str | None = None
    action: str | None = None
    # `annotate` only: True when the API answered 200 with an annotation that
    # already existed, False on a 201. None for every other verb and under
    # --dry-run, where nothing was asked.
    deduplicated: bool | None = None
    # None under --dry-run, always. The absence IS the statement.
    result: JsonDict | None = None


def overall_status(checks: Iterable[Check]) -> Severity:
    """Worst verdict across the checks that actually reached one.

    ``SKIP`` does not participate — except that a run where NOTHING reached a
    verdict is a FAIL. "0 failures" out of zero verdicts is the most dangerous
    green in the design, and it is exactly what an unreachable instance would
    otherwise print.
    """
    verdicts = [check.status for check in checks if check.status is not Severity.SKIP]
    if not verdicts:
        return Severity.FAIL
    if Severity.FAIL in verdicts:
        return Severity.FAIL
    if Severity.WARN in verdicts:
        return Severity.WARN
    return Severity.PASS


def exit_code_for(report: Report, *, strict: bool = False) -> int:
    """0, or 3 when the checks found something.

    Warnings exit 0 by default: a cron that goes red over a handful of untriaged
    schema drifts gets muted, and then nobody sees the failing scan either.
    ``--strict`` promotes WARN, and deliberately does NOT promote SKIP — skip
    means "I could not look", and a project-scoped key would otherwise fail every
    strict run forever for a reason the operator cannot fix.
    """
    status = report.status
    if status is Severity.FAIL:
        return EXIT_CHECKS_FAILED
    if strict and status is Severity.WARN:
        return EXIT_CHECKS_FAILED
    return EXIT_OK


def worst(severities: Iterable[Severity]) -> Severity:
    """Severity of a check, given its findings. Empty means nothing to report."""
    levels = list(severities)
    if Severity.FAIL in levels:
        return Severity.FAIL
    if Severity.WARN in levels:
        return Severity.WARN
    return Severity.PASS


def scope_from_auth(auth: Fetched[JsonDict] | None) -> str:
    """Read the key's reach off ``GET /auth/me``.

    ``deps.py::_enforce_project_scope`` rejects any route WITHOUT a ``slug`` path
    param for a project-bound key, so ``/auth/me`` is an unambiguous oracle:
    200 means instance-wide, 403 means fenced to one project. Detecting that
    positively and first is what turns TRAP 5 into a signal instead of an
    inference from some later 403 that could equally mean "your role is too low".

    The 403 body text is deliberately not matched — it is prose and can change;
    the route plus the status code is the whole signal.
    """
    if auth is None:
        return SCOPE_UNKNOWN
    if auth.ok:
        return SCOPE_INSTANCE
    if auth.status_code == 403:
        return SCOPE_PROJECT
    return SCOPE_UNKNOWN


def is_healthy(health: Fetched[JsonDict]) -> bool:
    """200 from /health with the body a tripl instance actually returns."""
    return health.ok and isinstance(health.value, dict) and health.value.get("status") == "ok"


def as_dict(value: Any) -> JsonDict:
    """Coerce an untyped JSON member to a dict; anything else reads as empty."""
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> JsonList:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def names_by_id(items: JsonList) -> dict[str, str]:
    """``{id: name}`` for any listing, in the order the API returned it.

    Every response model in this API carries ``id`` and ``name``, and three
    arguments resolve a selector against exactly that pair — ``<scan>``,
    ``<event-type>`` and ``--branch``. One extractor, so a fourth cannot arrive
    with its own idea of what to do about a row missing a name (tripl-3ixs).

    A row with no usable ``id`` is DROPPED: nothing can select it, and keeping
    it would put an unaddressable entry in the candidate list a failed match
    prints. A row with no ``name`` is keyed to its own id, so it stays
    selectable by id — which is the whole point of matching on both.
    """
    named: dict[str, str] = {}
    for item in items:
        identifier = text_of(item, "id")
        if identifier:
            named[identifier] = text_of(item, "name") or identifier
    return named


def page_items(payload: Any) -> JsonList:
    """``{"items": [...], "total": n}`` -> the items. Anything else reads as empty.

    Seven routes answer this envelope — events, variables, branches, search,
    alert deliveries, one event type's drifts, and reconciliation's dead/shadow
    listings — and each of them used to unwrap it at its own call site or not at
    all. One definition, so "the API answered an object where a list belongs" is
    decided in one place.

    Re-exported through ``tripl_cli.api`` for ``tripl_mcp``, which read the same
    envelope by hand at three tool sites until tripl-i1dt. Both distributions
    answer the question here now: a CLI that dropped a non-dict row while the
    MCP kept it, reading the same route, is two readings of one wire format.

    Like ``as_list``, this cannot tell an empty page from a failed read: a caller
    must have established that the READ succeeded first (TRAP 3).
    """
    items = payload.get("items") if isinstance(payload, dict) else None
    return as_list(items)


def page_total(payload: Any) -> int | None:
    """``{"items": [...], "total": n}`` -> ``n``, or None when there is no total.

    None is load-bearing: it is what says "this route reports no count", which is
    the only thing that distinguishes "the page is the whole answer" from "there
    are more rows and nobody said so".
    """
    total = payload.get("total") if isinstance(payload, dict) else None
    return total if isinstance(total, int) and not isinstance(total, bool) else None


def text_of(source: Mapping[str, Any], key: str) -> str | None:
    value = source.get(key)
    return value if isinstance(value, str) and value else None


def int_of(source: Mapping[str, Any], key: str, default: int = 0) -> int:
    value = source.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def float_of(source: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    """A JSON number as a float. ``True`` is not 1.0 here, exactly as in ``int_of``.

    JSON has one number type, so a percentage can arrive as ``91`` or ``91.4``
    and both must read the same; ``bool`` is excluded because it is an ``int``
    subclass in Python and a flag rendered as ``1.00`` is a wrong answer that
    looks like a right one.
    """
    value = source.get(key)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return default


def parse_time(raw: Any) -> datetime | None:
    """Parse an API timestamp, or None. Naive values are read as UTC.

    The API serialises aware datetimes, but ``result_summary`` carries whatever
    the worker wrote, and a naive value read as local time would silently shift
    every staleness verdict by the operator's offset.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def to_rfc3339(value: datetime) -> str:
    """UTC, second precision, literal Z — one spelling everywhere in the JSON."""
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def format_duration(seconds: float) -> str:
    """A rough human duration. Prose only — never assert on this."""
    value = int(abs(seconds))
    if value < 90:
        return f"{value}s"
    if value < 90 * 60:
        return f"{value // 60}m"
    if value < 72 * 3600:
        return f"{value // 3600}h"
    return f"{value // 86400}d"
