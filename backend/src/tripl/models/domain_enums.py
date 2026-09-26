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
    """What a user did about an open signal that no rule routed to an incident.

    ``acknowledged`` and ``expected`` pin ONE bucket (one signal); ``muted``
    covers the whole scope until ``muted_until`` (NULL = until unmuted).
    """

    acknowledged = "acknowledged"
    muted = "muted"
    expected = "expected"


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
    owner = "owner"
    editor = "editor"
    viewer = "viewer"


class ApiKeyScope(enum.StrEnum):
    read = "read"
    write = "write"
