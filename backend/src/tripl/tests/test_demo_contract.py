"""Cross-surface demo contract, over the full story.

Creates one demo with the default history (``DEMO_HISTORY_DAYS``, three weekly
promo sends and a release train) and asserts every advertised Plan / Observe /
Govern surface is non-empty and internally coherent: ids resolve, coverage
reconciles, deliveries reference real rules, and what the pipeline observed is
what the warehouse calendar put there. This is the one test that fails if a
builder stops authoring a surface or the replay stops observing one.

``DEMO_COVERED_SURFACES`` is the CANONICAL list of surfaces the demo promises to
populate. It is deliberately explicit: when a new core product surface is added,
a maintainer must consciously decide whether the demo covers it and extend this
list (and the assertions below) — the demo is not silently allowed to go stale.

Invariants, never counts copied from a run: the calendar names WHAT happened and
WHEN, the assertions check that tripl's own pipeline saw it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.config import settings
from tripl.core.analyzers.distribution_drift import PSI_BAND_MINOR, PSI_BAND_SIGNIFICANT
from tripl.core.bucketing import to_utc
from tripl.core.demo_warehouse import incidents_between, releases_between, roster
from tripl.core.demo_warehouse.calendar import (
    INCIDENT_KINDS,
    PROMO_EVENT,
    PROMO_START,
    PROMO_WEEKDAY,
    REGRESSION_ROTATION,
    Incident,
)
from tripl.core.demo_warehouse.roster import COUNTRY_MIX, PLATFORMS
from tripl.models.chart_annotation import ChartAnnotation
from tripl.models.distribution_drift import DistributionDrift
from tripl.models.domain_enums import ChartAnnotationSource
from tripl.models.event import Event
from tripl.models.event_metric import EventMetric
from tripl.models.event_type import EventType
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.project import Project
from tripl.services.demo import noise, pipeline
from tripl.services.demo.builders.branches import MERGED_BRANCH_NAME
from tripl.services.demo.builders.catalog import REVENUE_BREAKDOWN_COLUMNS, REVENUE_METRIC_NAME
from tripl.services.demo.builders.collaboration import DOCS, WATCHED
from tripl.services.release_annotations import release_annotation_label
from tripl.tests.conftest import TestSessionLocal

DEMO_COVERED_SURFACES: frozenset[str] = frozenset(
    {
        # Plan
        "events",
        "event_types",
        "variables",
        "meta_fields",
        "relations",
        "event_comments",
        "docs",
        "branches",
        "watches",
        # Observe
        "event_metrics_total",
        "metrics_catalog",
        "metric_breakdowns",
        "fact_tables",
        "distribution_drifts",
        "anomaly_signals",
        "monitors_summary",
        "top_events",
        "release_regressions",
        "release_annotations",
        "platform_presence",
        "planned_events",
        "planned_window_suggestions",
        "alert_inbox",
        # Govern
        "scan_jobs",
        "coverage",
        "shadow_events",
        "lifecycle_findings",
        "alert_destinations",
        "alert_deliveries",
    }
)

_PLAN = {
    "events",
    "event_types",
    "variables",
    "meta_fields",
    "relations",
    "event_comments",
    "docs",
    "branches",
    "watches",
}
_OBSERVE = {
    "event_metrics_total",
    "metrics_catalog",
    "metric_breakdowns",
    "fact_tables",
    "distribution_drifts",
    "anomaly_signals",
    "monitors_summary",
    "top_events",
    "release_regressions",
    "release_annotations",
    "platform_presence",
    "planned_events",
    "planned_window_suggestions",
    "alert_inbox",
}
_GOVERN = {
    "scan_jobs",
    "coverage",
    "shadow_events",
    "lifecycle_findings",
    "alert_destinations",
    "alert_deliveries",
}

#: The direction each incident kind moves its target.
_INCIDENT_DIRECTION = {
    "spike": "spike",
    "drop": "drop",
    "error_burst": "spike",
    "type_dip": "drop",
    "platform_shift": "spike",
}
#: Values a distribution drift's movers may name, per drift field.
_FIELD_DOMAIN = {
    "platform": set(PLATFORMS),
    "country": {country for country, _share in COUNTRY_MIX},
}


def test_coverage_guard_partitions_plan_observe_govern() -> None:
    # Every covered surface belongs to exactly one journey, and all three journeys
    # are represented — a structural guard so the list can't silently lose a whole
    # product area.
    assert _PLAN | _OBSERVE | _GOVERN == DEMO_COVERED_SURFACES
    assert _PLAN and _OBSERVE and _GOVERN
    assert not (_PLAN & _OBSERVE) and not (_OBSERVE & _GOVERN) and not (_PLAN & _GOVERN)


def test_every_incident_kind_has_a_direction() -> None:
    assert set(_INCIDENT_DIRECTION) == set(INCIDENT_KINDS)


def _items(payload: object) -> list:
    if isinstance(payload, dict):
        for key in ("items", "data", "signals"):
            if key in payload and isinstance(payload[key], list):
                return payload[key]
    return payload if isinstance(payload, list) else []


def _parsed(value: str) -> datetime:
    # SQLite drops the offset of an aware column; treat a naive stamp as UTC.
    stamp = datetime.fromisoformat(value)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


async def _detected(
    session: AsyncSession, scan_config_id: uuid.UUID
) -> set[tuple[str, str, datetime, str]]:
    """``(scope, target name, bucket, direction)`` of every event and type anomaly."""
    by_event = (
        await session.execute(
            select(Event.name, MetricAnomaly.bucket, MetricAnomaly.direction)
            .join(Event, Event.id == MetricAnomaly.event_id)
            .where(
                MetricAnomaly.scan_config_id == scan_config_id,
                MetricAnomaly.scope_type == "event",
            )
        )
    ).all()
    by_type = (
        await session.execute(
            select(EventType.name, MetricAnomaly.bucket, MetricAnomaly.direction)
            .join(EventType, EventType.id == MetricAnomaly.event_type_id)
            .where(
                MetricAnomaly.scan_config_id == scan_config_id,
                MetricAnomaly.scope_type == "event_type",
            )
        )
    ).all()
    return {
        (scope, name, to_utc(bucket), str(direction))
        for scope, rows in (("event", by_event), ("event_type", by_type))
        for name, bucket, direction in rows
    }


def _seen(detected: set[tuple[str, str, datetime, str]], incident: Incident) -> bool:
    direction = _INCIDENT_DIRECTION[incident.kind]
    return any(
        scope == incident.scope
        and name == incident.target
        and incident.start <= bucket < incident.end
        and moved == direction
        for scope, name, bucket, moved in detected
    )


async def _assert_the_replay_saw_the_calendar(project_id: uuid.UUID) -> None:
    """The observed half against the warehouse calendar and its own series."""
    async with TestSessionLocal() as session:
        seeded_at = await session.scalar(
            select(Project.demo_seeded_at).where(Project.id == project_id)
        )
        assert seeded_at is not None
        now = to_utc(seeded_at)
        scan_config_id = await pipeline.demo_scan_config_id(session, project_id)
        assert scan_config_id is not None
        detected = await _detected(session, scan_config_id)

        # Daily incidents: every kind in the rotation is caught at the scope the
        # calendar puts it on, in its own hours, moving the way it moves.
        scored_from, scored_to = pipeline.scoring_window(now)
        scored = [
            incident
            for incident in incidents_between(scored_from, scored_to)
            if scored_from <= incident.start and incident.end <= scored_to
        ]
        assert {incident.kind for incident in scored} == set(INCIDENT_KINDS)
        missed = {
            kind
            for kind in INCIDENT_KINDS
            if not any(_seen(detected, incident) for incident in scored if incident.kind == kind)
        }
        assert not missed, f"incident kinds the scoring pass never caught: {sorted(missed)}"
        # ...and the scoring pass ends on an incident hour, which is detected: it
        # is the newest bucket of its scope, the one the dispatcher alerts on.
        head = scored_to - timedelta(hours=1)
        [newest] = [i for i in incidents_between(head, scored_to) if i.start <= head < i.end]
        assert (
            newest.scope,
            newest.target,
            head,
            _INCIDENT_DIRECTION[newest.kind],
        ) in detected

        # Every event-scope anomaly reports its stored count and moves away from
        # what it expected in the direction it names.
        anomalies = (
            (
                await session.execute(
                    select(MetricAnomaly).where(
                        MetricAnomaly.scan_config_id == scan_config_id,
                        MetricAnomaly.scope_type == "event",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert anomalies
        for anomaly in anomalies:
            stored = (
                await session.execute(
                    select(EventMetric.count).where(
                        EventMetric.scan_config_id == scan_config_id,
                        EventMetric.event_id == anomaly.event_id,
                        EventMetric.bucket == anomaly.bucket,
                    )
                )
            ).scalar_one_or_none()
            assert anomaly.actual_count == float(stored or 0), anomaly.bucket
            if anomaly.direction == "spike":
                assert anomaly.actual_count > anomaly.expected_count
            else:
                assert anomaly.actual_count < anomaly.expected_count

        # Distribution drift: the real PSI over the warehouse's mix shifts, so it
        # varies, flags at least once, and every band is what the score implies.
        drifts = (
            (
                await session.execute(
                    select(DistributionDrift).where(
                        DistributionDrift.scan_config_id == scan_config_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len({round(drift.psi, 4) for drift in drifts}) > 1
        assert any(drift.band != "stable" for drift in drifts)
        for drift in drifts:
            if drift.psi < PSI_BAND_MINOR:
                assert drift.band == "stable", (drift.psi, drift.band)
            elif drift.psi < PSI_BAND_SIGNIFICANT:
                assert drift.band == "minor", (drift.psi, drift.band)
            else:
                assert drift.band == "significant", (drift.psi, drift.band)
            assert drift.top_movers
            domain = _FIELD_DOMAIN.get(drift.field_name)
            if domain is not None:
                assert {mover["value"] for mover in drift.top_movers} <= domain

        # Release markers: one per calendar release that shipped inside the
        # history, at its activation (a day or so into the rollout), project-wide.
        releases = (
            (
                await session.execute(
                    select(ChartAnnotation).where(
                        ChartAnnotation.project_id == project_id,
                        ChartAnnotation.source == ChartAnnotationSource.release.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        published = {
            release_annotation_label(version): at
            for version, at in releases_between(noise.history_start(now), now)
        }
        assert releases
        assert len({row.label for row in releases}) == len(releases)
        for row in releases:
            assert row.scope_type is None
            assert row.label in published, (row.label, sorted(published))
            assert (
                published[row.label]
                <= to_utc(row.bucket)
                < published[row.label] + timedelta(days=2)
            )


@pytest.mark.asyncio
async def test_demo_populates_every_advertised_surface(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The full story: the suite's default is a two-day history.
    monkeypatch.setattr(settings, "demo_history_days", noise.DEMO_HISTORY_DAYS)
    created = await client.post("/api/v1/projects/demo")
    assert created.status_code == 202, created.text
    assert created.json()["generation_status"] == "ready", created.json()
    slug = created.json()["slug"]
    project_id = uuid.UUID(created.json()["id"])

    async def get(path: str) -> object:
        resp = await client.get(f"/api/v1/projects/{slug}{path}")
        assert resp.status_code == 200, (path, resp.status_code, resp.text[:200])
        return resp.json()

    covered: set[str] = set()

    # ── Plan ─────────────────────────────────────────────────────────────
    events = _items(await get("/events?limit=500"))
    assert events
    assert any(ev.get("field_values") for ev in events)
    assert any(ev.get("meta_values") for ev in events)
    event_ids = {ev["name"]: ev["id"] for ev in events}
    # The list hides archived events unless asked for them.
    assert {event.name for event in roster.events() if event.status != "archived"} <= set(event_ids)
    covered.add("events")

    event_types = _items(await get("/event-types"))
    assert {et.name for et in roster.event_types()} <= {et["name"] for et in event_types}
    event_type_ids = {et["name"]: et["id"] for et in event_types}
    covered.add("event_types")
    assert _items(await get("/variables"))
    covered.add("variables")
    assert _items(await get("/meta-fields"))
    covered.add("meta_fields")
    assert _items(await get("/relations"))
    covered.add("relations")

    comments = _items(await get(f"/events/{event_ids['Purchase Completed']}/comments"))
    assert comments
    covered.add("event_comments")

    docs = await get("/docs")
    assert {path for path, _content in DOCS} <= {doc["path"] for doc in docs["project_docs"]}
    covered.add("docs")

    branches = _items(await get("/branches"))
    by_name = {branch["name"]: branch for branch in branches}
    assert by_name[MERGED_BRANCH_NAME]["merged_at"] is not None
    open_branch = by_name["feature/checkout-funnel"]
    assert _items(await get(f"/branches/{open_branch['id']}/comments"))
    covered.add("branches")

    metrics = _items(await get("/metrics"))
    metric_ids = {metric["name"]: metric["id"] for metric in metrics}
    watched_ids = {"event": event_ids, "event_type": event_type_ids, "metric": metric_ids}
    for entity_type, name in WATCHED:
        entity_id = watched_ids[entity_type.value][name]
        state = await get(f"/subscriptions/{entity_type.value}/{entity_id}")
        assert state["watching"] is True, (entity_type, name)
    covered.add("watches")

    # ── Observe ──────────────────────────────────────────────────────────
    assert _items(await get("/metrics/total"))
    covered.add("event_metrics_total")

    assert len(metrics) >= 4
    assert {"sql", "event_composition", "fact"} <= {m["kind"] for m in metrics}
    # Every catalog metric was collected over the history.
    assert all(m["spark"] for m in metrics), [m["name"] for m in metrics if not m["spark"]]
    covered.add("metrics_catalog")

    breakdowns = await get(f"/metrics/{metric_ids[REVENUE_METRIC_NAME]}/breakdowns")
    assert set(REVENUE_BREAKDOWN_COLUMNS) <= set(breakdowns["columns"])
    assert breakdowns["series"]
    covered.add("metric_breakdowns")

    assert _items(await get("/fact-tables"))
    covered.add("fact_tables")

    async with TestSessionLocal() as session:
        drifted_type = await session.scalar(
            select(DistributionDrift.event_type_id)
            .join(EventType, EventType.id == DistributionDrift.event_type_id)
            .where(EventType.project_id == project_id)
            .limit(1)
        )
    assert drifted_type is not None
    assert _items(await get(f"/distribution-drifts?scope_type=event_type&scope_ref={drifted_type}"))
    covered.add("distribution_drifts")

    signals = _items(await get("/anomalies/signals"))
    assert signals
    # Referential coherence: each active signal carries a scope and real numbers.
    assert all(s.get("scope_type") and s.get("bucket") for s in signals)
    assert all(isinstance(s.get("actual_count"), (int, float)) for s in signals)
    covered.add("anomaly_signals")

    monitors = await get("/monitors-summary")
    assert isinstance(monitors, dict) and monitors
    covered.add("monitors_summary")

    assert _items(await get("/overview/top-events"))
    covered.add("top_events")

    scan_configs = _items(await get("/scans"))
    assert scan_configs
    scan_id = scan_configs[0]["id"]

    # A release breaks the event the calendar's rotation names for it.
    regressions = _items(await get(f"/scans/{scan_id}/release-regressions"))
    assert regressions
    broken = {(event, kind) for event, kind, _kept in REGRESSION_ROTATION}
    assert any((item["scope_name"], item["kind"]) in broken for item in regressions), regressions
    covered.add("release_regressions")

    annotations = _items(await get("/annotations"))
    assert any(row["source"] == "release" for row in annotations)
    covered.add("release_annotations")

    presence = await get(f"/scans/{scan_id}/platform-presence")
    assert presence["items"]
    assert set(presence["platforms"]) <= set(PLATFORMS)
    covered.add("platform_presence")

    planned = _items(await get("/planned-events"))
    assert any(row["label"] == pipeline.DEMO_PLANNED_EVENT_LABEL for row in planned)
    covered.add("planned_events")

    # The weekly promo, marked expected every week it ran, comes back as a
    # suggested recurring window on its own weekday and hour.
    suggestions = _items(await get("/planned-events/suggestions"))
    [promo] = [s for s in suggestions if s["note"] == pipeline.DEMO_WEEKLY_PROMO_NOTE]
    assert promo["scope_type"] == "event"
    assert promo["scope_ref"] == event_ids[PROMO_EVENT]
    assert (promo["weekday"], promo["hour"]) == (PROMO_WEEKDAY, PROMO_START.seconds // 3600)
    assert promo["verdict_count"] >= 3
    assert promo["direction"] == "spike"
    assert all(_parsed(w["starts_at"]) > datetime.now(UTC) for w in promo["windows"])
    covered.add("planned_window_suggestions")

    inbox = await get("/alert-inbox")
    assert inbox["items"]
    covered.add("alert_inbox")

    # ── Govern ───────────────────────────────────────────────────────────
    jobs = _items(await get(f"/scans/{scan_id}/jobs"))
    assert jobs
    assert all(j["status"] == "completed" for j in jobs)
    covered.add("scan_jobs")

    coverage = await get("/reconciliation/coverage")
    assert coverage["items"]
    assert coverage["summary"]["matched_count"] <= coverage["summary"]["total_count"]
    covered.add("coverage")

    # Shadow candidates are names the warehouse sends and the plan lacks, and
    # one can be accepted into the plan.
    shadow = await get("/reconciliation/shadow-events?status=new")
    assert shadow["total"] >= 1
    unplanned = {event.name for event in roster.unplanned_events()}
    assert {item["event_name"] for item in shadow["items"]} <= unplanned
    candidate = shadow["items"][0]
    accepted = await client.post(
        f"/api/v1/projects/{slug}/reconciliation/shadow-events/{candidate['id']}/accept",
        json={"event_type_id": candidate["event_type_id"] or event_type_ids["app_lifecycle"]},
    )
    assert accepted.status_code == 200, accepted.text
    after = _items(await get("/events?limit=500"))
    assert candidate["event_name"] in {ev["name"] for ev in after}
    covered.add("shadow_events")

    # The deprecated events with a successor are the lifecycle pass's findings.
    findings = _items(await get("/lifecycle-findings"))
    assert findings
    deprecated = {event.name for event in roster.events() if event.status == "deprecated"}
    assert {item["event_name"] for item in findings} <= deprecated
    covered.add("lifecycle_findings")

    destinations = _items(await get("/alert-destinations"))
    assert destinations
    covered.add("alert_destinations")

    deliveries = _items(await get("/alert-deliveries"))
    assert deliveries
    # Referential coherence: a delivery references a real rule + destination.
    assert all(d.get("rule_name") and d.get("destination_name") for d in deliveries)
    covered.add("alert_deliveries")

    # Every canonical surface was exercised.
    assert covered == DEMO_COVERED_SURFACES, DEMO_COVERED_SURFACES - covered

    await _assert_the_replay_saw_the_calendar(project_id)
