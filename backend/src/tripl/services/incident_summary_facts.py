"""The numbered facts an incident summary is written from (F14, #267).

Pure database reads, no LLM. Every fact is a short sentence of already-redacted
text plus an in-app link, numbered 1..N after a deterministic ordering so the
model can cite ``[n]`` and the UI can resolve it. Texts use absolute UTC
timestamps and pre-rounded numbers only, so ``facts_hash`` is stable between
reads and changes exactly when something the summary would mention changes: a
new delivery, a verdict, a comment, a status change or a release.

Two things never reach a fact:

* ``AlertDeliveryItem.sample_value`` (or any other drift sample value);
* the value of a field marked sensitive. Attribution payloads are copied and
  every value of a column naming a sensitive field is replaced before the
  headline is phrased; the stored row is never touched.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.core.analyzers.attribution import attribution_headline, release_line
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.chart_annotation import ChartAnnotation
from tripl.models.domain_enums import ChartAnnotationSource, MetricScopeType
from tripl.models.event_photo_comment import EventPhotoComment
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.meta_field_definition import MetaFieldDefinition
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.project import Project
from tripl.models.signal_triage import SignalTriage
from tripl.schemas.alerting import AlertInboxGroupResponse
from tripl.services import alerting_service, anomaly_attribution_service, signal_triage_service
from tripl.services._signal_verdict_read import VERDICT_ACTIONS, resolve_verdict
from tripl.services.mentions import excerpt
from tripl.services.project_links import project_org_slugs, qualify_project_path
from tripl.services.project_lookup import resolve_project
from tripl.services.signal_triage_service import SignalKey, TriageIndex

# Part of the hash: a change to how facts are phrased or to the prompt makes
# every cached summary stale, which is what should happen.
PROMPT_VERSION = "incident-summary-v2"

MAX_FACTS = 20
MAX_SCOPES = 5
MAX_RELEASE_MARKERS = 3
MAX_SIMILAR = 5
MAX_COMMENTS = 5
SIMILAR_LOOKBACK = timedelta(days=90)
RELEASE_LOOKBACK = timedelta(hours=48)
COMMENT_LOOKBACK = timedelta(days=7)
REDACTED = "(redacted)"

# Rows read for one group. A long-running incident has no ceiling on its
# history; the facts only need its newest buckets per scope.
_MAX_GROUP_ITEMS = 1000
_NOTE_MAX = 500
_VERDICT_NOTE_MAX = 200
_COMMENT_EXCERPT = 280

# The order kinds are numbered in. ``MAX_FACTS`` truncates from the end, so the
# least essential kinds go last.
_KIND_ORDER = ("incident", "note", "scope", "attribution", "release", "similar", "comment")

_IDENTIFIER_SEPARATORS = re.compile(r"[._]+")
_ANNOTATION_SCOPES = frozenset(
    {
        MetricScopeType.project_total.value,
        MetricScopeType.event_type.value,
        MetricScopeType.event.value,
        MetricScopeType.metric.value,
    }
)


@dataclass(frozen=True)
class SummaryFact:
    id: int
    kind: str
    text: str
    href: str | None

    def to_payload(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "text": self.text, "href": self.href}


@dataclass(frozen=True)
class IncidentFacts:
    project: Project
    group: AlertInboxGroupResponse
    facts: tuple[SummaryFact, ...]
    facts_hash: str
    # The project's organization slug: completes the org-less hrefs of a stored
    # summary written before F20 PR8 when it is read back.
    org_slug: str | None = None


@dataclass(frozen=True)
class _Draft:
    kind: str
    text: str
    href: str | None


@dataclass(frozen=True)
class _Scope:
    """One distinct scope of the group, as its newest item describes it."""

    scan_config_id: uuid.UUID
    scope_type: str
    scope_ref: str
    scope_name: str
    event_id: uuid.UUID | None
    event_type_id: uuid.UUID | None
    bucket: datetime
    direction: str
    actual_count: float
    expected_count: float

    @property
    def partition(self) -> uuid.UUID | None:
        # A catalog metric is project-global: its anomalies and verdicts carry
        # no scan, whatever config the delivery was filed under.
        return None if self.scope_type == MetricScopeType.metric.value else self.scan_config_id


# --- formatting ------------------------------------------------------------


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def fmt_time(value: datetime) -> str:
    return as_utc(value).strftime("%Y-%m-%d %H:%M UTC")


def fmt_number(value: float) -> str:
    rounded = round(float(value), 2)
    if rounded == int(rounded):
        return f"{int(rounded):,}"
    return f"{rounded:,.2f}"


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def incident_href(slug: str, correlation_group_id: uuid.UUID) -> str:
    return f"/p/{slug}/alerting?incident={correlation_group_id}"


def scope_href(
    slug: str,
    scope_type: str,
    scope_ref: str,
    *,
    scan_config_id: uuid.UUID | None,
    event_id: uuid.UUID | None,
    event_type_id: uuid.UUID | None,
) -> str | None:
    """The scope's drilldown page, by the rule ``activity_service._monitoring_path``
    uses (FK columns, never ``scope_ref``), plus catalog metrics.

    The stored ``AlertDeliveryItem.monitoring_path`` is not used: it is empty for
    every item routed into an incident and absolute when set.
    """
    scope = str(scope_type)
    if scope == MetricScopeType.project_total.value and scan_config_id is not None:
        return f"/p/{slug}/monitoring/project-total/{scan_config_id}"
    if scope == MetricScopeType.event_type.value and event_type_id is not None:
        return f"/p/{slug}/monitoring/event-type/{event_type_id}"
    if scope == MetricScopeType.event.value and event_id is not None:
        return f"/p/{slug}/monitoring/event/{event_id}"
    if scope == MetricScopeType.metric.value and scope_ref:
        return f"/p/{slug}/monitoring/metric/{scope_ref}"
    return None


def _scope_href(slug: str, scope: _Scope) -> str | None:
    return scope_href(
        slug,
        scope.scope_type,
        scope.scope_ref,
        scan_config_id=scope.scan_config_id,
        event_id=scope.event_id,
        event_type_id=scope.event_type_id,
    )


def compute_facts_hash(facts: Iterable[SummaryFact]) -> str:
    """The staleness key of a summary: the prompt version and each fact's kind
    and text, in order.

    ``href`` is deliberately NOT part of it (critique #21, F20 PR8). A link is
    how the UI resolves a citation, not something the summary says, so a change
    to link SHAPE — ``/p/{slug}/...`` becoming ``/o/{org}/p/{slug}/...`` — must
    not mark every summary stale and regenerate it through the LLM. Migration
    ``d4e8f1a2b3c5`` rewrote the stored hashes to this definition. Any change
    here owes the stored rows the same rewrite.
    """
    canonical = json.dumps(
        {
            "version": PROMPT_VERSION,
            "facts": [[fact.kind, fact.text] for fact in facts],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def number_facts(drafts: Iterable[_Draft]) -> tuple[SummaryFact, ...]:
    """Order by kind (stable within a kind), cap, and number from 1.

    Whitespace is collapsed, so no fact text spans lines: a scope or rule name
    carrying a newline cannot forge a numbered fact line in the prompt.
    """
    rank = {kind: index for index, kind in enumerate(_KIND_ORDER)}
    ordered = sorted(drafts, key=lambda draft: rank[draft.kind])[:MAX_FACTS]
    return tuple(
        SummaryFact(id=index, kind=draft.kind, text=" ".join(draft.text.split()), href=draft.href)
        for index, draft in enumerate(ordered, start=1)
    )


# --- redaction -------------------------------------------------------------


def _is_sensitive(sensitivity: str | None) -> bool:
    return (sensitivity or "none") != "none"


def column_is_sensitive(column: str, sensitive_names: frozenset[str]) -> bool:
    """Whether an attribution column names a sensitive field.

    Matches, case-insensitively, the whole column, any dotted-path suffix of it,
    and its last dotted/underscore segment: ``properties.user.email`` matches a
    sensitive ``user.email`` field and one named ``email``;
    ``properties.user_email`` matches ``user_email`` and ``email``.
    """
    if not sensitive_names:
        return False
    name = column.strip().lower()
    segments = name.split(".")
    candidates = {".".join(segments[index:]) for index in range(len(segments))}
    parts = [part for part in _IDENTIFIER_SEPARATORS.split(name) if part]
    if parts:
        candidates.add(parts[-1])
    return bool(candidates & sensitive_names)


def redact_attribution_payload(
    payload: Mapping[str, Any], sensitive_names: frozenset[str]
) -> dict[str, Any]:
    """A deep copy of ``{delta, columns, release}`` with sensitive values replaced."""
    redacted = copy.deepcopy(dict(payload))
    columns = redacted.get("columns")
    if not isinstance(columns, list):
        return redacted
    for column in columns:
        if not isinstance(column, dict):
            continue
        name = column.get("column")
        if not isinstance(name, str) or not column_is_sensitive(name, sensitive_names):
            continue
        values = column.get("values")
        if not isinstance(values, list):
            continue
        for entry in values:
            if isinstance(entry, dict) and "value" in entry:
                entry["value"] = REDACTED
    return redacted


async def load_sensitive_field_names(
    session: AsyncSession, project_id: uuid.UUID
) -> frozenset[str]:
    """Names of the project's sensitive event fields and meta fields, lowercased."""
    field_rows = await session.execute(
        select(FieldDefinition.name, FieldDefinition.sensitivity)
        .join(EventType, EventType.id == FieldDefinition.event_type_id)
        .where(EventType.project_id == project_id)
    )
    meta_rows = await session.execute(
        select(MetaFieldDefinition.name, MetaFieldDefinition.sensitivity).where(
            MetaFieldDefinition.project_id == project_id
        )
    )
    return frozenset(
        str(name).strip().lower()
        for name, sensitivity in [*field_rows.all(), *meta_rows.all()]
        if name and _is_sensitive(sensitivity)
    )


# --- sources ---------------------------------------------------------------


async def _load_scopes(
    session: AsyncSession, project_id: uuid.UUID, correlation_group_id: uuid.UUID
) -> tuple[list[_Scope], datetime | None]:
    """The group's distinct scopes, newest first (cap ``MAX_SCOPES``), and its
    oldest bucket."""
    rows = (
        await session.execute(
            select(AlertDeliveryItem, AlertDelivery.scan_config_id)
            .join(AlertDelivery, AlertDelivery.id == AlertDeliveryItem.delivery_id)
            .where(
                AlertDelivery.project_id == project_id,
                AlertDeliveryItem.correlation_group_id == correlation_group_id,
            )
            .order_by(
                AlertDeliveryItem.bucket.desc(),
                AlertDeliveryItem.scope_type.asc(),
                AlertDeliveryItem.scope_ref.asc(),
                AlertDelivery.created_at.desc(),
                AlertDeliveryItem.id.asc(),
            )
            .limit(_MAX_GROUP_ITEMS)
        )
    ).all()
    scopes: dict[tuple[str, str], _Scope] = {}
    first_bucket: datetime | None = None
    for item, scan_config_id in rows:
        bucket = as_utc(item.bucket)
        first_bucket = bucket if first_bucket is None else min(first_bucket, bucket)
        key = (str(item.scope_type), item.scope_ref)
        if key in scopes or len(scopes) >= MAX_SCOPES:
            continue
        scopes[key] = _Scope(
            scan_config_id=scan_config_id,
            scope_type=str(item.scope_type),
            scope_ref=item.scope_ref,
            scope_name=item.scope_name,
            event_id=item.event_id,
            event_type_id=item.event_type_id,
            bucket=bucket,
            direction=str(item.direction),
            actual_count=item.actual_count,
            expected_count=item.expected_count,
        )
    return list(scopes.values()), first_bucket


def _incident_draft(slug: str, group: AlertInboxGroupResponse) -> _Draft:
    scopes = ", ".join(group.scope_names) or "an unnamed scope"
    measure = (
        f"newest value {fmt_number(group.actual_count)} vs expected "
        f"{fmt_number(group.expected_count)}"
    )
    if group.percent_delta is not None:
        measure += f" ({round(abs(group.percent_delta), 1)}% {group.direction})"
    else:
        measure += " (no baseline)"
    rules = ", ".join(sorted(group.rule_names)) or "none"
    text = (
        f"Incident: a {group.direction} on {scopes}; {measure}. "
        f"First delivered {fmt_time(group.first_delivery_at)}, "
        f"latest delivered {fmt_time(group.latest_delivery_at)}. "
        f"Rules: {rules}. Status: {group.status}."
    )
    return _Draft("incident", text, incident_href(slug, group.correlation_group_id))


def _scope_draft(slug: str, scope: _Scope) -> _Draft:
    text = (
        f"Scope {scope.scope_name} ({scope.scope_type}), bucket {fmt_time(scope.bucket)}: "
        f"{scope.direction}, actual {fmt_number(scope.actual_count)} vs expected "
        f"{fmt_number(scope.expected_count)}."
    )
    return _Draft("scope", text, _scope_href(slug, scope))


async def _anomaly_for(session: AsyncSession, scope: _Scope) -> MetricAnomaly | None:
    partition = scope.partition
    scan_match = (
        MetricAnomaly.scan_config_id.is_(None)
        if partition is None
        else MetricAnomaly.scan_config_id == partition
    )
    anomaly: MetricAnomaly | None = await session.scalar(
        select(MetricAnomaly)
        .where(
            scan_match,
            MetricAnomaly.scope_type == scope.scope_type,
            MetricAnomaly.scope_ref == scope.scope_ref,
            MetricAnomaly.bucket == scope.bucket,
        )
        .limit(1)
    )
    return anomaly


def _normalise_version(label: str) -> str:
    text = label.strip().lower()
    for prefix in ("release ", "version ", "v"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    return text


async def _attribution_and_release_drafts(
    session: AsyncSession,
    slug: str,
    project_id: uuid.UUID,
    scopes: Sequence[_Scope],
) -> tuple[list[_Draft], list[_Draft], set[str]]:
    anomalies: list[tuple[_Scope, MetricAnomaly]] = []
    for scope in scopes:
        anomaly = await _anomaly_for(session, scope)
        if anomaly is not None:
            anomalies.append((scope, anomaly))
    if not anomalies:
        return [], [], set()
    rows = await anomaly_attribution_service.load_attribution_rows(
        session, [anomaly.id for _, anomaly in anomalies]
    )
    if not rows:
        return [], [], set()
    sensitive = await load_sensitive_field_names(session, project_id)
    attribution: list[_Draft] = []
    releases: list[_Draft] = []
    versions: set[str] = set()
    for scope, anomaly in anomalies:
        row = rows.get(anomaly.id)
        if row is None:
            continue
        payload = redact_attribution_payload(
            {"delta": row.delta, "columns": row.columns or [], "release": row.release},
            sensitive,
        )
        href = _scope_href(slug, scope)
        headline = attribution_headline(payload, scope.direction)
        if headline:
            attribution.append(
                _Draft(
                    "attribution",
                    f"Breakdown of {scope.scope_name} at {fmt_time(scope.bucket)}: {headline}.",
                    href,
                )
            )
        line = release_line(payload, anomaly_bucket=scope.bucket, direction=scope.direction)
        release = payload.get("release")
        version = release.get("version") if isinstance(release, Mapping) else None
        if line and version:
            key = _normalise_version(str(version))
            if key in versions:
                continue
            versions.add(key)
            releases.append(_Draft("release", f"{line} ({scope.scope_name}).", href))
    return attribution, releases, versions


async def _release_marker_drafts(
    session: AsyncSession,
    slug: str,
    project_id: uuid.UUID,
    scopes: Sequence[_Scope],
    *,
    first_bucket: datetime,
    latest_bucket: datetime,
    seen_versions: set[str],
) -> list[_Draft]:
    scope_matches = [
        and_(
            ChartAnnotation.scope_type == scope.scope_type,
            ChartAnnotation.scope_ref == scope.scope_ref,
        )
        for scope in scopes
        if scope.scope_type in _ANNOTATION_SCOPES
    ]
    rows = (
        await session.execute(
            select(ChartAnnotation)
            .where(
                ChartAnnotation.project_id == project_id,
                ChartAnnotation.source.in_(
                    [ChartAnnotationSource.release.value, ChartAnnotationSource.api.value]
                ),
                ChartAnnotation.bucket >= first_bucket - RELEASE_LOOKBACK,
                ChartAnnotation.bucket <= latest_bucket,
                or_(ChartAnnotation.scope_type.is_(None), *scope_matches),
            )
            .order_by(ChartAnnotation.bucket.desc(), ChartAnnotation.id.asc())
            .limit(MAX_RELEASE_MARKERS * 4)
        )
    ).scalars()
    hrefs = {(scope.scope_type, scope.scope_ref): _scope_href(slug, scope) for scope in scopes}
    default_href = next(iter(hrefs.values()), None)
    drafts: list[_Draft] = []
    for row in rows:
        key = _normalise_version(row.label)
        if key in seen_versions:
            continue
        seen_versions.add(key)
        kind = "Release" if str(row.source) == ChartAnnotationSource.release.value else "Deploy"
        href = (
            hrefs.get((str(row.scope_type), row.scope_ref or ""), default_href)
            if row.scope_type is not None
            else default_href
        )
        drafts.append(
            _Draft(
                "release",
                f"{kind} marker {_truncate(row.label, 200)} at {fmt_time(row.bucket)}.",
                href,
            )
        )
        if len(drafts) >= MAX_RELEASE_MARKERS:
            break
    return drafts


async def _candidate_keys(
    session: AsyncSession,
    project_id: uuid.UUID,
    correlation_group_id: uuid.UUID,
    scope: _Scope,
    *,
    since: datetime,
    before: datetime,
) -> set[SignalKey]:
    """Past buckets on the scope that may carry a verdict: its verdict rows and
    its deliveries into other incidents."""
    wanted_scope = signal_triage_service.scope_key(
        scope.partition, scope.scope_type, scope.scope_ref
    )
    keys: set[SignalKey] = set()
    triage_rows = await session.execute(
        select(SignalTriage.scan_config_id, SignalTriage.bucket).where(
            SignalTriage.project_id == project_id,
            SignalTriage.scope_type == scope.scope_type,
            SignalTriage.scope_ref == scope.scope_ref,
            SignalTriage.action.in_(sorted(VERDICT_ACTIONS)),
            SignalTriage.bucket >= since,
            SignalTriage.bucket < before,
        )
    )
    for scan_config_id, bucket in triage_rows.all():
        if bucket is None:
            continue
        key = signal_triage_service.signal_key(
            scan_config_id, scope.scope_type, scope.scope_ref, bucket
        )
        if key[:3] == wanted_scope:
            keys.add(key)
    item_rows = await session.execute(
        select(AlertDelivery.scan_config_id, AlertDeliveryItem.bucket)
        .join(AlertDelivery, AlertDelivery.id == AlertDeliveryItem.delivery_id)
        .where(
            AlertDelivery.project_id == project_id,
            AlertDeliveryItem.scope_type == scope.scope_type,
            AlertDeliveryItem.scope_ref == scope.scope_ref,
            AlertDeliveryItem.bucket >= since,
            AlertDeliveryItem.bucket < before,
            AlertDeliveryItem.correlation_group_id.is_not(None),
            AlertDeliveryItem.correlation_group_id != correlation_group_id,
        )
    )
    for scan_config_id, bucket in item_rows.all():
        key = signal_triage_service.signal_key(
            scan_config_id, scope.scope_type, scope.scope_ref, bucket
        )
        if key[:3] == wanted_scope:
            keys.add(key)
    return keys


async def _similar_drafts(
    session: AsyncSession,
    slug: str,
    project_id: uuid.UUID,
    group: AlertInboxGroupResponse,
    scopes: Sequence[_Scope],
    *,
    first_bucket: datetime,
) -> list[_Draft]:
    since = first_bucket - SIMILAR_LOOKBACK
    found: list[tuple[datetime, str, str, _Draft]] = []
    for scope in scopes:
        keys = await _candidate_keys(
            session,
            project_id,
            group.correlation_group_id,
            scope,
            since=since,
            before=first_bucket,
        )
        if not keys:
            continue
        # The same resolution ``signal_verdict_service._verdicts_for_scope`` does:
        # the signal's own verdict row, else the status of the incident it was
        # routed into. Author names are deliberately not loaded.
        index = (
            await signal_triage_service.load_triage_indexes(
                session, [project_id], min_bucket=min(key[3] for key in keys)
            )
        ).get(project_id) or TriageIndex()
        refs = await alerting_service.incident_refs_for_signals(session, project_id, keys)
        fallback_href = _scope_href(slug, scope)
        for key in keys:
            ref = refs.get(key)
            info = resolve_verdict(index.verdicts.get(key), ref, {})
            if info is None:
                continue
            text = (
                f"Past signal on {scope.scope_name} at {fmt_time(key[3])}: "
                f"verdict {str(info.verdict).replace('_', ' ')}"
            )
            if info.expected_reason is not None:
                text += f", reason {info.expected_reason}"
            text += "."
            if info.note:
                text += f" Note: {_truncate(info.note, _VERDICT_NOTE_MAX)}"
            href = (
                incident_href(slug, ref.correlation_group_id) if ref is not None else fallback_href
            )
            found.append((key[3], scope.scope_type, scope.scope_ref, _Draft("similar", text, href)))
    found.sort(key=lambda entry: (-entry[0].timestamp(), entry[1], entry[2]))
    drafts = [entry[3] for entry in found[:MAX_SIMILAR]]
    if group.false_positive_count > 0:
        times = "time" if group.false_positive_count == 1 else "times"
        drafts.append(
            _Draft(
                "similar",
                f"This incident has been marked a false positive "
                f"{group.false_positive_count} {times} before.",
                incident_href(slug, group.correlation_group_id),
            )
        )
    return drafts


async def _comment_drafts(
    session: AsyncSession, slug: str, group: AlertInboxGroupResponse
) -> list[_Draft]:
    if group.event_id is None or str(group.scope_type) != MetricScopeType.event.value:
        return []
    since = as_utc(group.first_delivery_at) - COMMENT_LOOKBACK
    rows = (
        await session.execute(
            select(EventPhotoComment.body, EventPhotoComment.created_at)
            .where(
                EventPhotoComment.event_id == group.event_id,
                EventPhotoComment.parent_id.is_(None),
                EventPhotoComment.created_at >= since,
            )
            .order_by(EventPhotoComment.created_at.desc(), EventPhotoComment.id.asc())
            .limit(MAX_COMMENTS)
        )
    ).all()
    href = f"/p/{slug}/events/detail/{group.event_id}"
    return [
        _Draft(
            "comment",
            f"Comment on the event at {fmt_time(created_at)}: {excerpt(body, _COMMENT_EXCERPT)}",
            href,
        )
        for body, created_at in rows
    ]


async def gather_incident_facts(
    session: AsyncSession, slug: str, correlation_group_id: uuid.UUID
) -> IncidentFacts:
    """Every fact the summary may use, numbered, with the hash over them.

    404s (from ``get_alert_inbox_group``) when the group has no items in the
    project.
    """
    group = await alerting_service.get_alert_inbox_group(session, slug, correlation_group_id)
    project = await resolve_project(session, slug)
    scopes, first_bucket = await _load_scopes(session, project.id, correlation_group_id)
    first_bucket = first_bucket or as_utc(group.latest_bucket)
    latest_bucket = as_utc(group.latest_bucket)

    drafts: list[_Draft] = [_incident_draft(slug, group)]
    if group.note:
        drafts.append(
            _Draft(
                "note",
                f"Operator note on the incident: {_truncate(group.note, _NOTE_MAX)}",
                incident_href(slug, correlation_group_id),
            )
        )
    drafts.extend(_scope_draft(slug, scope) for scope in scopes)
    attribution, releases, versions = await _attribution_and_release_drafts(
        session, slug, project.id, scopes
    )
    drafts.extend(attribution)
    drafts.extend(releases)
    drafts.extend(
        await _release_marker_drafts(
            session,
            slug,
            project.id,
            scopes,
            first_bucket=first_bucket,
            latest_bucket=latest_bucket,
            seen_versions=versions,
        )
    )
    drafts.extend(
        await _similar_drafts(session, slug, project.id, group, scopes, first_bucket=first_bucket)
    )
    drafts.extend(await _comment_drafts(session, slug, group))

    # The builders above write org-less ``/p/{slug}/...`` links; complete them
    # with the project's organization (F20 PR8). The hash ignores hrefs, so this
    # never makes a stored summary stale.
    org_slug = (await project_org_slugs(session, [project.id])).get(project.id)
    if org_slug is not None:
        drafts = [
            replace(draft, href=qualify_project_path(org_slug, draft.href))
            if draft.href is not None
            else draft
            for draft in drafts
        ]
    facts = number_facts(drafts)
    return IncidentFacts(
        project=project,
        group=group,
        facts=facts,
        facts_hash=compute_facts_hash(facts),
        org_slug=org_slug,
    )
