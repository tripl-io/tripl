from __future__ import annotations

import enum


class FieldDefinitionType(enum.StrEnum):
    string = "string"
    number = "number"
    boolean = "boolean"
    json = "json"
    enum = "enum"
    url = "url"


class MetaFieldType(enum.StrEnum):
    string = "string"
    url = "url"
    boolean = "boolean"
    enum = "enum"
    date = "date"


class Sensitivity(enum.StrEnum):
    none = "none"
    pii = "pii"
    phi = "phi"
    financial = "financial"
    secret = "secret"


class SchemaDriftType(enum.StrEnum):
    new_field = "new_field"
    missing_field = "missing_field"
    type_changed = "type_changed"
    enum_violation = "enum_violation"
    required_null_violation = "required_null_violation"
    regex_violation = "regex_violation"
    range_violation = "range_violation"


class SchemaDriftStatus(enum.StrEnum):
    open = "open"
    accepted = "accepted"
    snoozed = "snoozed"
    false_positive = "false_positive"


class EventCommentStatus(enum.StrEnum):
    """Resolution state of one discussion thread on an event.

    The five columns come from ``SchemaDrift``, but not its vocabulary:
    ``accepted`` and ``false_positive`` are verdicts a detector's finding earns,
    and a question someone typed is neither accepted nor false. A thread is
    open, answered, or deliberately parked (tripl-h2sx.26).
    """

    open = "open"
    resolved = "resolved"
    snoozed = "snoozed"


class AlertInboxStatus(enum.StrEnum):
    open = "open"
    acknowledged = "acknowledged"
    resolved = "resolved"
    muted = "muted"
    false_positive = "false_positive"


class ShadowEventStatus(enum.StrEnum):
    new = "new"
    accepted = "accepted"
    dismissed = "dismissed"


class EventPhotoKind(enum.StrEnum):
    photo = "photo"
    figma = "figma"


class EventPhotoStorageBackend(enum.StrEnum):
    local = "local"
    gcs = "gcs"


class ScanInterval(enum.StrEnum):
    m15 = "15m"
    h1 = "1h"
    h6 = "6h"
    d1 = "1d"
    w1 = "1w"


class MergeResolutionChoice(enum.StrEnum):
    ours = "ours"
    theirs = "theirs"


class MetricScopeType(enum.StrEnum):
    project_total = "project_total"
    event_type = "event_type"
    event = "event"
    schema = "schema"
    distribution = "distribution"
    release_regression = "release_regression"
    # ``metric`` (user-defined MetricDefinition series). Added by the metrics
    # epic's anomaly-scope ticket (tripl-dxhp.6) via an ALTER TYPE migration.
    metric = "metric"
    # Observed variable values outside the documented list (epic tripl-j94c,
    # S13). Added via ALTER TYPE migration d1c2b3a4f5e6.
    variable_value_drift = "variable_value_drift"
    # A late or overdue scan source (#269). Added via ALTER TYPE migration
    # c4e8a2f6b1d3.
    source_freshness = "source_freshness"
    # An open lifecycle finding (#258): sunset overdue or successor silent.
    # Added via ALTER TYPE migration d5f7b9c1e3a8.
    lifecycle = "lifecycle"


class MetricKind(enum.StrEnum):
    """How a MetricDefinition produces its per-bucket value."""

    sql = "sql"
    event_composition = "event_composition"
    # ``fact``: aggregation over a separately-defined FactTable (fact-table model).
    fact = "fact"


class MetricStatus(enum.StrEnum):
    """Simple catalog lifecycle for metrics (no dev-implementation states)."""

    draft = "draft"
    active = "active"
    archived = "archived"


class MetricAggregation(enum.StrEnum):
    """Aggregation applied by a ``fact`` metric over a FactTable column."""

    count = "count"  # type: ignore[assignment]  # StrEnum member shadows str.count
    sum = "sum"
    avg = "avg"
    min = "min"
    max = "max"
    count_distinct = "count_distinct"


class MetricComposition(enum.StrEnum):
    """How an ``event_composition`` or ``fact`` metric combines its series.

    ``event_composition`` uses ``single`` / ``ratio`` / ``per_distinct_user``;
    ``fact`` uses ``single`` (one operand) and ``ratio`` (numerator / denominator
    operands, each over a — possibly different — FactTable).
    """

    single = "single"
    ratio = "ratio"
    per_distinct_user = "per_distinct_user"


class ChartAnnotationScopeType(enum.StrEnum):
    project_total = "project_total"
    event_type = "event_type"
    event = "event"
    metric = "metric"


class ChartAnnotationSource(enum.StrEnum):
    """Who put a chart annotation there.

    ``manual`` is a person in the UI, ``api`` is a CI/CLI client posting a
    deploy marker, and ``release`` is the metrics worker marking the bucket an
    app version activated in. ``release`` is reserved to the worker: the create
    API refuses it, so every release marker is one the activation gate drew.
    """

    manual = "manual"
    release = "release"
    api = "api"


class AnomalyDirection(enum.StrEnum):
    spike = "spike"
    drop = "drop"


class SignalTriageAction(enum.StrEnum):
    """What a user did about an open signal.

    ``acknowledged`` and the four verdicts (``expected``, ``tracking_bug``,
    ``false_positive``, ``real_issue``) pin ONE bucket (one signal); ``muted``
    covers the whole scope until ``muted_until`` (NULL = until unmuted). A
    signal carries at most one verdict at a time (F01, #254).
    """

    acknowledged = "acknowledged"
    muted = "muted"
    expected = "expected"
    tracking_bug = "tracking_bug"
    false_positive = "false_positive"
    real_issue = "real_issue"


class SignalVerdict(enum.StrEnum):
    """The verdict half of ``SignalTriageAction``: what a signal turned out to be."""

    expected = "expected"
    tracking_bug = "tracking_bug"
    false_positive = "false_positive"
    real_issue = "real_issue"


class SignalExpectedReason(enum.StrEnum):
    """Why an ``expected`` signal was expected. Documents only; it does not
    suppress later buckets."""

    campaign = "campaign"
    release = "release"
    seasonality = "seasonality"
    other = "other"


class MetricBreakdownAnomalyKind(enum.StrEnum):
    volume = "volume"
    parity = "parity"


class AlertDriftType(enum.StrEnum):
    new_field = "new_field"
    missing_field = "missing_field"
    type_changed = "type_changed"
    enum_violation = "enum_violation"
    required_null_violation = "required_null_violation"
    regex_violation = "regex_violation"
    range_violation = "range_violation"
    distribution_shift = "distribution_shift"
    missing = "missing"
    volume_drop = "volume_drop"
    # Written by the variable-value-drift candidate builder. The scope shipped
    # in d1c2b3a4f5e6 without this member, so the delivery INSERT failed on the
    # Postgres enum and took the whole collection transaction with it
    # (tripl-jfm3.97). Added to the type by e2f3a4b5c6d7.
    value_drift = "value_drift"
    # Written by the source-freshness candidate builder (#269): the scan's data
    # is late (``source_late``) or the scan itself stopped collecting
    # (``source_overdue``). Prefixed rather than the bare ``late``/``overdue``
    # freshness statuses so the shared column reads unambiguously. Added to the
    # type by c4e8a2f6b1d3.
    source_late = "source_late"
    source_overdue = "source_overdue"
    # Written by the lifecycle candidate builder (#258): the finding kind of a
    # ``lifecycle`` scope (``LifecycleFindingKind``). Added to the type by
    # d5f7b9c1e3a8.
    sunset_overdue = "sunset_overdue"
    successor_silent = "successor_silent"


class ReleaseRegressionKind(enum.StrEnum):
    missing = "missing"
    volume_drop = "volume_drop"


class ReleaseComparabilityReason(enum.StrEnum):
    """Why one release-regression pass concluded what it concluded.

    Mirrors the ``REASON_*`` constants in
    ``tripl.core.analyzers.release_regression``, which is DB-free and so cannot
    import this; keep the two in step.
    """

    comparable = "comparable"
    no_baseline = "no_baseline"
    baseline_no_volume = "baseline_no_volume"
    population_mismatch = "population_mismatch"


class DistributionDriftBand(enum.StrEnum):
    stable = "stable"
    minor = "minor"
    significant = "significant"


class AlertRuleFilterField(enum.StrEnum):
    event_type = "event_type"
    event = "event"
    direction = "direction"
    # A catalog metric (MetricDefinition id). Only a ``metric``-scope signal
    # carries one; every other signal passes a metric filter through, the way a
    # project-total signal passes an ``event`` filter (JR-15).
    metric = "metric"


class AlertRuleFilterOperator(enum.StrEnum):
    eq = "eq"
    ne = "ne"
    in_ = "in"
    not_in = "not_in"


class AlertMessageFormat(enum.StrEnum):
    plain = "plain"
    slack_mrkdwn = "slack_mrkdwn"
    telegram_html = "telegram_html"
    telegram_markdownv2 = "telegram_markdownv2"


class ProjectGenerationStatus(enum.StrEnum):
    """Provisioning lifecycle for generated (demo) projects.

    Ordinary projects are created ``ready`` in a single step. Demo projects move
    ``seeding`` -> ``ready`` on success or ``seeding`` -> ``failed`` when
    provisioning raises; non-``ready`` demos are hidden from normal project lists
    so a partially built or failed demo never surfaces as a real workspace.
    """

    pending = "pending"
    seeding = "seeding"
    ready = "ready"
    failed = "failed"


class UserRole(enum.StrEnum):
    """The legacy instance role (``users.role``, ``invitations.role``).

    Not read by any permission check since F20 PR4: organization roles
    (:class:`OrganizationRole`) and project roles replaced it. The columns stay
    until a cleanup PR drops them.
    """

    owner = "owner"
    editor = "editor"
    viewer = "viewer"


class OrganizationRole(enum.StrEnum):
    """A user's role in one organization (``organization_members.role``).

    The source of truth for organization-level rights (F20 PR4). ``owner`` and
    ``admin`` administer the organization and are the implicit ``owner`` of
    every project in it; only an ``owner`` can make or unmake another owner.
    ``member`` holds the role of their ``project_members`` row in a project,
    or the organization's ``default_project_role`` where they hold none.
    """

    owner = "owner"
    admin = "admin"
    member = "member"


class OrganizationStatus(enum.StrEnum):
    """Lifecycle of an organization row (``organizations.status``, F20 PR6).

    ``deleting`` is set the moment an owner asks to delete the organization; a
    Celery job then purges it. From that moment every read of it answers 404
    (``services.org_resolution``), so nothing new lands in an organization that
    is on its way out.

    ``suspended`` is set by a platform admin from the platform console (F20
    PR14). The organization stays listed for its members, with its status, but
    every org-scoped request of theirs answers 403 "This organization is
    suspended", and the scheduled worker jobs skip its projects
    (``services.active_org_scope``). Unsuspending restores it untouched.
    """

    active = "active"
    deleting = "deleting"
    suspended = "suspended"


class ApiKeyScope(enum.StrEnum):
    read = "read"
    write = "write"


class ProjectMemberRole(enum.StrEnum):
    """A user's role inside one project (``project_members.role``).

    There is no per-project ``owner``: an owner or admin of the project's
    organization (:class:`OrganizationRole`) sees and manages every project of
    that organization without a membership row, as project role ``owner``. For
    everyone else the row is authoritative (``services.project_access``), and
    a member without a row gets the organization's ``default_project_role``.

    ``none`` is "no access": as a row it opts one organization member out of
    one project (the project is a 404 for them, whatever the organization
    default); as ``organizations.default_project_role`` it means members see
    only the projects they hold a row in.
    """

    none = "none"
    editor = "editor"
    viewer = "viewer"
