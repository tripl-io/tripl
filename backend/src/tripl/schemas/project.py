import uuid
from datetime import datetime
from typing import Literal, cast

from pydantic import BaseModel, Field, field_validator, model_validator

from tripl.core.alert_schedule import validate_timezone
from tripl.models.domain_enums import (
    AnomalyDirection,
    MetricScopeType,
    ProjectGenerationStatus,
)
from tripl.models.scan_job import ScanJobStatus
from tripl.schemas.not_null_update import reject_explicit_nulls
from tripl.semver import (
    DEFAULT_APP_VERSION_KEEP_RELEASES,
    MAX_APP_VERSION_KEEP_RELEASES,
)

#: Project slugs no project may take (F20 PR5). Each is a path segment that
#: routes, or will route, as something other than a project: ``/projects/demo``
#: is the demo API, ``/orgs/{org}`` the organization prefix, ``/p/{slug}`` and
#: ``/o/{org}`` the web app's project and organization routes, and the rest are
#: top-level web or API sections a project slug would shadow. Exact matches only:
#: ``demo-3f2a1c`` (a generated demo) and ``newsletter`` are fine.
RESERVED_PROJECT_SLUGS: frozenset[str] = frozenset(
    {
        "account",
        "admin",
        "api",
        "auth",
        "data-sources",
        "demo",
        "invite",
        "me",
        "new",
        "o",
        "orgs",
        "p",
        "project-templates",
        "projects",
        "settings",
        "users",
        "workspace",
    }
)


def reject_reserved_slug(value: str | None) -> str | None:
    """422 for a reserved project slug; see :data:`RESERVED_PROJECT_SLUGS`."""
    if value is not None and value in RESERVED_PROJECT_SLUGS:
        raise ValueError(f"'{value}' is reserved and cannot be used as a project slug")
    return value


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=255, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    description: str = ""
    app_version_keep_releases: int = Field(
        default=DEFAULT_APP_VERSION_KEEP_RELEASES,
        ge=1,
        le=MAX_APP_VERSION_KEEP_RELEASES,
    )
    # IANA zone every wall-clock schedule in this project is read in —
    # today, alert digest cadences. 'UTC' is what every project had
    # implicitly before the column existed. max_length mirrors the
    # String(64) column; the longest real zone is 32 characters
    # (America/Argentina/ComodRivadavia), so it constrains nothing valid.
    timezone: str = Field("UTC", max_length=64)
    # A built-in template (``GET /project-templates``) whose starter plan is
    # seeded onto a draft branch for review; main stays empty (F21, GH #274).
    # Not a Project column: ``create_project`` excludes it from the ORM kwargs.
    template_id: str | None = Field(None, min_length=1, max_length=64)

    @field_validator("slug")
    @classmethod
    def check_slug_not_reserved(cls, value: str) -> str:
        reject_reserved_slug(value)
        return value

    @field_validator("timezone")
    @classmethod
    def check_timezone(cls, value: str | None) -> str | None:
        """Normalise, then reject a zone ``zoneinfo`` cannot resolve.

        The alert flusher degrades to UTC on an unresolvable zone so one bad
        project cannot stop every other project's digest — which is precisely
        why an unresolvable one must never be storable.

        Stripped first because a timezone is something people paste, and
        ``ZoneInfo`` does not forgive the surrounding space: " Europe/Moscow "
        is a perfectly ordinary copy from a docs page and was answered with
        "Unknown timezone". Stripping also means one spelling reaches the
        column, so two projects cannot hold the same zone under two values.
        """
        if value is None:
            return None
        return validate_timezone(value.strip())


# The update fields whose Project column is NOT NULL, so an explicit ``null``
# from a client is a 422 naming the field and not a DB-level 500 — see
# ``schemas/not_null_update``. ``timezone`` and ``app_version_keep_releases``
# are absent because they refuse a null a second way, through the
# ``Field(cast(T, None))`` spelling below; every other field here reaches
# ``update_project``'s generic ``setattr`` loop unfiltered.
_PROJECT_NOT_NULL_UPDATE_FIELDS = frozenset({"name", "slug", "description"})


class ProjectUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    slug: str | None = Field(
        None, min_length=1, max_length=255, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
    )
    description: str | None = None
    # ``int`` with a default of None makes the PATCH field optional while still
    # rejecting an explicitly supplied JSON null (and keeps OpenAPI non-nullable).
    app_version_keep_releases: int = Field(
        cast(int, None),
        ge=1,
        le=MAX_APP_VERSION_KEEP_RELEASES,
    )
    # ``str`` with a None default makes the PATCH field optional while still
    # rejecting an explicitly supplied JSON null — the column is NOT NULL, and
    # the generic setattr loop in ``update_project`` would otherwise write it.
    timezone: str = Field(cast(str, None), max_length=64)

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_nulls(cls, data: object) -> object:
        return reject_explicit_nulls(data, _PROJECT_NOT_NULL_UPDATE_FIELDS)

    # No reserved-slug validator here, unlike ``ProjectCreate``: the schema cannot
    # see the project's current slug, and the settings form resends it unchanged
    # on every save, so a project that already holds a now-reserved slug could
    # never be edited again. ``project_service.update_project`` refuses only a
    # CHANGE to a reserved slug.

    @field_validator("timezone")
    @classmethod
    def check_timezone(cls, value: str | None) -> str | None:
        """Normalise, then reject a zone ``zoneinfo`` cannot resolve.

        The alert flusher degrades to UTC on an unresolvable zone so one bad
        project cannot stop every other project's digest — which is precisely
        why an unresolvable one must never be storable.

        Stripped first because a timezone is something people paste, and
        ``ZoneInfo`` does not forgive the surrounding space: " Europe/Moscow "
        is a perfectly ordinary copy from a docs page and was answered with
        "Unknown timezone". Stripping also means one spelling reaches the
        column, so two projects cannot hold the same zone under two values.
        """
        if value is None:
            return None
        return validate_timezone(value.strip())


class DetectionResetPeriod(BaseModel):
    """Optional half-open window (``after <= t < before``) for a danger-zone reset.

    Both bounds are optional; omitting both clears the whole project.

    ``dry_run`` returns the counts the reset WOULD delete and deletes nothing, so
    the confirm dialog can name them (ST-39). It defaults to false, unlike
    ``VariableRetirementRequest``: this body predates it, and a client that
    omits it has always meant "delete".
    """

    before: datetime | None = None
    after: datetime | None = None
    dry_run: bool = False


class DemoCancelResponse(BaseModel):
    """Outcome of asking an in-flight demo provision to abandon itself.

    ``cancelled`` is only true when a still-seeding shell was found and flagged;
    the provision then deletes itself instead of promoting.

    When it is false there was nothing to stop, and ``state`` says which of two
    very different things that means (DEMO-28): ``finished`` — a demo of this
    user's became ready moments ago, so it WILL be in their projects list and
    the caller should say so — or ``none`` — no recent demo exists, the create
    never got far enough (or failed on its own), and the caller must not promise
    anything will appear. ``stopped`` always pairs with ``cancelled=true``.
    """

    cancelled: bool
    slug: str | None = None
    state: Literal["stopped", "finished", "none"]


class AnomalyResetCounts(BaseModel):
    metric_anomalies: int
    metric_breakdown_anomalies: int
    metric_baselines: int


class DriftResetCounts(BaseModel):
    schema_drifts: int
    distribution_drifts: int


class VariableRetirementRequest(BaseModel):
    """How to retire the variables nothing references, and whether to commit.

    ``dry_run`` defaults to true so the destructive verb takes a second,
    explicit call: the counts come back broken down by reason first, and only
    then does an operator decide.
    """

    mode: Literal["delete", "exclude"] = "delete"
    dry_run: bool = True


class VariableRetirementCounts(BaseModel):
    """What a retirement pass did, and why it spared everything it spared.

    The ``kept_*`` fields mirror ``core.variable_retirement.KeptReason`` one for
    one, and a test pins that agreement: a reason the core can emit but this
    schema cannot name would be dropped silently from the operator's preview,
    which is the one number they are being asked to trust.
    """

    scanned: int
    retirable: int
    retired: int
    kept_referenced: int = 0
    kept_observed: int = 0
    kept_documented: int = 0
    kept_user_edited: int = 0
    kept_excluded: int = 0


class ProjectLatestScanJob(BaseModel):
    id: uuid.UUID
    scan_config_id: uuid.UUID
    scan_name: str
    status: ScanJobStatus
    started_at: datetime | None
    completed_at: datetime | None
    result_summary: dict[str, object] | None
    error_message: str | None
    created_at: datetime


class ProjectLatestSignal(BaseModel):
    scan_config_id: uuid.UUID
    scan_name: str
    scope_type: MetricScopeType
    scope_ref: str
    scope_name: str
    state: str
    bucket: datetime
    actual_count: float
    expected_count: float
    z_score: float
    direction: AnomalyDirection


class ProjectSummary(BaseModel):
    event_type_count: int = 0
    event_count: int = 0
    active_event_count: int = 0
    implemented_event_count: int = 0
    review_pending_event_count: int = 0
    archived_event_count: int = 0
    variable_count: int = 0
    scan_count: int = 0
    # Metric definitions in the project, any status. Drives the "Define a key
    # metric" onboarding step's done-state (JR-2). Metrics are not plan
    # entities, so a ``?branch=`` read does not change it.
    metric_count: int = 0
    alert_destination_count: int = 0
    # ENABLED alert rules across this project's destinations. A destination on
    # its own routes nothing — a rule is what binds a signal to a channel — so
    # "is alerting actually wired up?" needs both counters, not just the
    # destination one. Disabled rules are excluded because they
    # deliver nothing either.
    alert_rule_count: int = 0
    monitoring_signal_count: int = 0
    # Open property drifts (F23, #306): new properties, missing required ones
    # and type changes nobody has triaged. Kept apart from
    # ``monitoring_signal_count``, which must equal the Anomalies page; both
    # this and the health score read ``_open_signals.open_property_drift_counts``.
    open_property_drift_count: int = 0
    firing_monitor_count: int = 0
    # Incidents in the Alerting Inbox whose effective status is `open`. The
    # sidebar used to badge Alerting with ``alert_destination_count``, so it read
    # "Alerting 1" while 52 incidents sat open. A badge that
    # disagrees with the page it labels is worse than no badge — this one is
    # computed by ``_populate_open_incident_counts``, which shares the inbox's
    # own window and status rules rather than approximating them.
    open_incident_count: int = 0
    # Number of scan configs whose *latest* run failed. Distinct from
    # ``latest_scan_job`` (the single newest job across the whole project): a
    # config that fails every run is invisible there once a *different* config
    # logs a newer success, so this per-config rollup is what the workspace
    # "failed jobs" surface must count.
    failing_scan_config_count: int = 0
    # Enabled alert destinations whose *latest* delivery failed — the
    # per-channel twin of ``failing_scan_config_count``, so the Overview status
    # line can name a broken channel next to failing scans (MO-15).
    failing_alert_destination_count: int = 0
    latest_scan_job: ProjectLatestScanJob | None = None
    latest_signal: ProjectLatestSignal | None = None


class ProjectResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: str
    app_version_keep_releases: int = Field(
        default=DEFAULT_APP_VERSION_KEEP_RELEASES,
        ge=1,
        le=MAX_APP_VERSION_KEEP_RELEASES,
    )
    # Defaulted, NOT required. ``list_projects`` rehydrates this model from a
    # 60s Redis cache, so immediately after a deploy the cache still holds
    # entries serialized by the previous schema. A required field would make
    # every GET /projects 500 until the TTL expired.
    timezone: str = "UTC"
    created_at: datetime
    updated_at: datetime
    summary: ProjectSummary = Field(default_factory=ProjectSummary)
    # Demo identity & provisioning state (real projects: is_demo=False,
    # generation_status="ready", the rest null). Surfaces provisioning progress
    # and failure detail to the UI without leaking internals.
    is_demo: bool = False
    generation_status: ProjectGenerationStatus = ProjectGenerationStatus.ready
    generation_stage: str | None = None
    generation_error: str | None = None
    demo_recipe_version: str | None = None
    # When the demo was seeded. Floored to the hour, because the runtime tick
    # anchors its bucket grid to it — so it is NOT a usable "freshness" stamp:
    # a demo seeded at 10:59 carries 10:00. Use demo_last_tick_at for that.
    demo_seeded_at: datetime | None = None
    # When the runtime tick last advanced this demo's data. NULL until the first
    # tick. This is the only honest freshness signal the UI has.
    demo_last_tick_at: datetime | None = None
    created_by_user_id: uuid.UUID | None = None
    # Whether THIS caller may write inside the project: the answer the editor
    # gate (``api.deps.get_editor_user``) would give on the project's slug-scoped
    # routes — role, API-key scope and ``ProjectMutationScope`` together. Computed
    # per request by ``project_service.with_can_mutate``; False until then, and
    # never stored in the shared project-list cache.
    can_mutate: bool = False
    # THIS caller's role in the project: ``owner`` for an owner or admin of its
    # organization, else the membership role (``services.project_access``).
    # Per request like ``can_mutate`` — never stored in the shared project-list
    # cache, and defaulted so an entry cached by an older schema still validates.
    my_role: Literal["owner", "editor", "viewer"] = "viewer"

    model_config = {"from_attributes": True}


class ProjectCreateResponse(ProjectResponse):
    """``POST /projects``: the project plus the template branch it opened, if any.

    ``template_branch_id`` is the draft branch holding a template's starter plan
    (F21, GH #274); null when the project was created blank. Only the create
    route returns it, so the shared ``ProjectResponse`` and the cached project
    list stay untouched.
    """

    template_branch_id: uuid.UUID | None = None
