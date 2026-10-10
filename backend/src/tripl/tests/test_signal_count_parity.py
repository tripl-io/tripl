"""Parity between the sidebar/Overview badge count and the AnomaliesPage signal list.

Regression guard (supersedes the earlier contract).
The AnomaliesPage now lists EVERY open signal as a flat, magnitude-
filtered list (``get_active_signals(expanded=True)`` -> ``_flag_incident_children``):
project_total + event_type + per-event, incident children TAGGED but kept, then the
page hides everything below the "Significant" magnitude threshold. The badge count
(``project_service._populate_monitoring_signals`` -> ``monitoring_signal_count``) must
count the same population — open signals across all scopes with relative effect >= 0.5,
children included, NO incident dedup — so the badge equals the page's headline open
count. (Pre-fix the badge counted only deduped project_total+event_type incidents and
so undercounted a busy project: 3 on the badge vs 270 on the page.)

Both code paths now stringify the native UUID in Python, so the seeded anomaly
references use the detector's hyphenated UUID format on SQLite and PostgreSQL.
We seed structurally identical projects for the page and sidebar paths.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from tripl.models.domain_enums import (
    MetricAggregation,
    MetricComposition,
    MetricKind,
    MetricStatus,
)
from tripl.models.event_metric import EventMetric
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.models.scan_config import ScanConfig
from tripl.schemas.event_metric import MetricSignalResponse
from tripl.services.metrics_insights_service import get_active_signals, is_significant_signal
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_project_lookup_perf import captured_sql

# Recent, hour-aligned bucket so classify_signal_state keeps each scope's latest
# anomaly open (a latest_scan signal stays fresh only while its bucket is newer
# than now - 24h).
_BUCKET = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)


def _scope_ref(value: str) -> str:
    """Use the detector-written, hyphenated UUID representation."""
    return str(uuid.UUID(value))


def _anomaly(
    *,
    scan_config_id: str,
    scope_type: str,
    scope_ref: str,
    event_type_id: str | None,
) -> MetricAnomaly:
    return MetricAnomaly(
        id=uuid.uuid4(),
        scan_config_id=uuid.UUID(scan_config_id),
        scope_type=scope_type,
        scope_ref=scope_ref,
        event_id=None,
        event_type_id=uuid.UUID(event_type_id) if event_type_id else None,
        bucket=_BUCKET,
        actual_count=99,
        expected_count=40,
        stddev=5,
        z_score=8,
        direction="spike",
    )


async def _event_type_metric(scan_config_id: str, event_type_id: str) -> EventMetric:
    # event_id NULL + event_type_id set anchors recency for BOTH the event_type
    # scope (keyed by event_type_id) and the project_total scope (keyed by
    # scan_config_id) in the summary/insights metric-bucket unions.
    return EventMetric(
        id=uuid.uuid4(),
        scan_config_id=uuid.UUID(scan_config_id),
        event_id=None,
        event_type_id=uuid.UUID(event_type_id),
        bucket=_BUCKET,
        count=42,
    )


async def _create_data_source(client: AsyncClient, name: str) -> str:
    resp = await client.post(
        "/api/v1/data-sources",
        json={
            "name": name,
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "analytics",
            "username": "default",
            "password": "",
        },
    )
    assert resp.status_code == 201
    return resp.json()["id"]


async def _create_event_type(client: AsyncClient, slug: str, name: str) -> str:
    resp = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": name, "display_name": name.replace("_", " ").title()},
    )
    assert resp.status_code == 201
    return resp.json()["id"]


async def _create_scan(client: AsyncClient, slug: str, data_source_id: str, name: str) -> str:
    resp = await client.post(
        f"/api/v1/projects/{slug}/scans",
        json={"data_source_id": data_source_id, "name": name, "base_query": "SELECT 1"},
    )
    assert resp.status_code == 201
    return resp.json()["id"]


async def _seed_project(client: AsyncClient, slug: str) -> None:
    """Seed a project with three open signals across two scans:

    * Scan A: a project_total parent AND an event_type child on the same
      (scan_config_id, bucket, direction) -> ONE incident, TWO rows (the child is
      tagged incident_child but kept, not collapsed).
    * Scan B: an event_type with NO project_total parent -> one more row.

    All three clear the "Significant" magnitude gate (actual 99 vs expected 40 ->
    relative effect ~1.5), so both the expanded page list and the badge count must
    report 3 — every open significant signal, children included.
    """
    project_resp = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert project_resp.status_code == 201

    data_source_id = await _create_data_source(client, f"Warehouse {slug}")
    event_type_a = await _create_event_type(client, slug, "scan_a_type")
    event_type_b = await _create_event_type(client, slug, "scan_b_type")
    scan_a = await _create_scan(client, slug, data_source_id, "Scan A")
    scan_b = await _create_scan(client, slug, data_source_id, "Scan B")

    async with TestSessionLocal() as session:
        # Scan A: dual-scope incident (project_total parent + event_type child).
        session.add(await _event_type_metric(scan_a, event_type_a))
        session.add(
            _anomaly(
                scan_config_id=scan_a,
                scope_type="project_total",
                scope_ref=_scope_ref(scan_a),
                event_type_id=None,
            )
        )
        session.add(
            _anomaly(
                scan_config_id=scan_a,
                scope_type="event_type",
                scope_ref=_scope_ref(event_type_a),
                event_type_id=event_type_a,
            )
        )
        # Scan B: unparented event_type incident (no project_total on this scan).
        session.add(await _event_type_metric(scan_b, event_type_b))
        session.add(
            _anomaly(
                scan_config_id=scan_b,
                scope_type="event_type",
                scope_ref=_scope_ref(event_type_b),
                event_type_id=event_type_b,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_sidebar_count_matches_page_signal_list(client: AsyncClient) -> None:
    # Page path: get_active_signals stringifies scope_ref as str(uuid) (hyphenated).
    page_slug = "signal-parity-page"
    await _seed_project(client, page_slug)
    async with TestSessionLocal() as session:
        page_signals = await get_active_signals(session, page_slug, expanded=True)

    # Expanded list keeps every scope: scan A's project_total + its tagged
    # event_type child, plus scan B's unparented event_type -> three rows.
    assert sorted(signal.scope_type for signal in page_signals) == [
        "event_type",
        "event_type",
        "project_total",
    ]
    # The badge mirrors the page's DEFAULT "Significant" view, so compare against the
    # magnitude-filtered count (here all three clear the gate).
    page_significant_count = sum(
        1
        for signal in page_signals
        if is_significant_signal(signal.actual_count, signal.expected_count)
    )
    assert page_significant_count == 3

    # Sidebar path: monitoring_signal_count uses the same UUID encoding.
    sidebar_slug = "signal-parity-sidebar"
    await _seed_project(client, sidebar_slug)
    resp = await client.get(f"/api/v1/projects/{sidebar_slug}")
    assert resp.status_code == 200
    sidebar_count = resp.json()["summary"]["monitoring_signal_count"]

    # Parity: the badge counts the same significant open signals the expanded page
    # lists — children included, no dedup. Pre-fix the badge counted only deduped
    # project_total+event_type incidents and read 2.
    assert sidebar_count == page_significant_count
    assert sidebar_count == 3


# ---------------------------------------------------------------------------
# An outage that is still running
# ---------------------------------------------------------------------------

# Five days back: far outside the 24h open-signal window on every grid this scan
# can use, so bucket age alone classifies the anchor closed.
_OUTAGE_ANCHOR_BUCKET = _BUCKET - timedelta(days=5)


async def _seed_ongoing_outage(
    client: AsyncClient,
    slug: str,
    *,
    scan_alive: bool = True,
    anchor: datetime = _OUTAGE_ANCHOR_BUCKET,
    last_stored_bucket: datetime | None = None,
    actual_count: float = 0,
    expected_count: float = 40,
) -> None:
    """Seed one event type that went silent at ``anchor`` (five days ago by default).

    ``dead_type`` has metric rows up to the anchor and nothing after — the shape
    ``_collapse_outage_runs`` leaves behind once it has announced an outage once
    (anchor row, ``actual_count`` 0, never re-emitted). ``live_type`` keeps
    collecting on the SAME scan when ``scan_alive``, so the collector is
    demonstrably alive and the silence belongs to the scope; with
    ``scan_alive=False`` nothing on the scan has collected since the anchor and
    the scan is indistinguishable from one that was switched off.

    ``last_stored_bucket`` places the silent scope's final metric row, defaulting
    to the anchor itself. Production stores no row for an empty bucket, so a real
    outage anchored on the first zero has its last row STRICTLY BEFORE the anchor;
    both shapes must classify the same way.

    ``actual_count``/``expected_count`` describe the anchor row itself. The
    defaults are the ordinary outage (nothing observed against a baseline of 40);
    the interesting departures are ``expected_count=0`` (a scope that was never
    expected to emit — no incident) and a non-zero ``actual_count``
    off a zero baseline (a real spike from nothing, which must keep working).
    """
    project_resp = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert project_resp.status_code == 201

    data_source_id = await _create_data_source(client, f"Warehouse {slug}")
    dead_type = await _create_event_type(client, slug, "dead_type")
    live_type = await _create_event_type(client, slug, "live_type")
    scan_id = await _create_scan(client, slug, data_source_id, "Scan")

    async with TestSessionLocal() as session:
        # Last thing the silent scope ever emitted.
        session.add(
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=uuid.UUID(scan_id),
                event_id=None,
                event_type_id=uuid.UUID(dead_type),
                bucket=anchor if last_stored_bucket is None else last_stored_bucket,
                count=42,
            )
        )
        # The rest of the scan: still collecting right up to now, or frozen at
        # the anchor alongside the silent scope.
        live_metric = await _event_type_metric(scan_id, live_type)
        if not scan_alive:
            live_metric.bucket = anchor
        session.add(live_metric)
        # The persisted anchor: dropped to zero, announced once, never refreshed.
        outage = _anomaly(
            scan_config_id=scan_id,
            scope_type="event_type",
            scope_ref=_scope_ref(dead_type),
            event_type_id=dead_type,
        )
        outage.bucket = anchor
        outage.actual_count = actual_count
        outage.expected_count = expected_count
        outage.z_score = -20
        outage.direction = "drop"
        session.add(outage)
        await session.commit()


@pytest.mark.asyncio
async def test_ongoing_outage_stays_open_on_both_surfaces(client: AsyncClient) -> None:
    """A five-day-old outage that never recovered is still an open signal.

    The anchor row is the only announcement the scope will ever get, so ageing it
    out on wall-clock bucket age deleted a running incident from the Anomalies
    page, the sidebar/top-bar badge and the Overview stat at once. All of them
    must keep reporting it, and report the SAME number.
    """
    page_slug = "outage-open-page"
    await _seed_ongoing_outage(client, page_slug)
    async with TestSessionLocal() as session:
        page_signals = await get_active_signals(session, page_slug, expanded=True)

    assert len(page_signals) == 1, page_signals
    signal = page_signals[0]
    assert signal.scope_type == "event_type"
    assert signal.actual_count == 0
    # The scope's newest data point IS the anomaly, which is what "latest_scan"
    # has always meant — the UI renders it "Live".
    assert signal.state == "latest_scan"

    page_significant_count = sum(
        1
        for candidate in page_signals
        if is_significant_signal(candidate.actual_count, candidate.expected_count)
    )
    assert page_significant_count == 1

    sidebar_slug = "outage-open-sidebar"
    await _seed_ongoing_outage(client, sidebar_slug)
    resp = await client.get(f"/api/v1/projects/{sidebar_slug}")
    assert resp.status_code == 200
    sidebar_count = resp.json()["summary"]["monitoring_signal_count"]

    assert sidebar_count == page_significant_count
    assert sidebar_count == 1


@pytest.mark.asyncio
async def test_ongoing_outage_is_still_a_signal_on_the_drilldown(client: AsyncClient) -> None:
    """The page the Anomalies list links to must agree with the list.

    The list row is a link: clicking it opens the monitoring detail, which reads
    the per-scope metrics endpoint. That endpoint classified the anchor by bucket
    age alone, so it answered ``latest_signal: null`` for the very incident the
    list had just reported as live — three surfaces saying "open" and the page
    they open saying nothing at all.
    """
    slug = "outage-open-drilldown"
    await _seed_ongoing_outage(client, slug)

    async with TestSessionLocal() as session:
        listed = await get_active_signals(session, slug, expanded=True)
    assert len(listed) == 1, listed

    resp = await client.get(f"/api/v1/projects/{slug}/event-types/{listed[0].scope_ref}/metrics")
    assert resp.status_code == 200
    drilldown = resp.json()["latest_signal"]

    assert drilldown is not None, "the drilldown dropped a signal the list reports as open"
    assert drilldown["state"] == listed[0].state == "latest_scan"


@pytest.mark.asyncio
async def test_outage_on_a_stopped_scan_closes_on_the_drilldown(client: AsyncClient) -> None:
    """And the stopped-collector cap holds on the drilldown too.

    The pairing matters: a drilldown that simply always reported a signal would
    pass the test above while breaking the cap that keeps a switched-off scan
    from pinning its final anomaly red forever.
    """
    slug = "outage-stopped-drilldown"
    await _seed_ongoing_outage(client, slug, scan_alive=False)

    async with TestSessionLocal() as session:
        event_type_ids = [
            row.scope_ref for row in await get_active_signals(session, slug, expanded=True)
        ]
    assert event_type_ids == []

    listing = (await client.get(f"/api/v1/projects/{slug}/event-types")).json()
    dead_id = next(row["id"] for row in listing if row["name"] == "dead_type")
    resp = await client.get(f"/api/v1/projects/{slug}/event-types/{dead_id}/metrics")
    assert resp.status_code == 200
    assert resp.json()["latest_signal"] is None


# Ten days back: OUTSIDE the drilldown chart's default range. The monitoring
# detail page opens on the last 7 days and sends that window as ``from``/``to``
# on every load, so an incident older than a week has no row of any kind inside
# the range the page asks for.
_LONG_OUTAGE_ANCHOR_BUCKET = _BUCKET - timedelta(days=10)

# What the page sends for its default 7-day range.
_DEFAULT_RANGE_FROM = _BUCKET - timedelta(days=7)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "last_stored_bucket"),
    [
        # The scope's final stored row sits ON the anchor bucket...
        ("row-at-anchor", None),
        # ...and, the production shape, strictly BEFORE it: an empty bucket
        # stores no row at all, so the anchor's own bucket is absent from
        # event_metrics and the widening probe finds no bucket at or after it.
        ("no-row-at-or-after-anchor", _LONG_OUTAGE_ANCHOR_BUCKET - timedelta(hours=1)),
    ],
)
async def test_ongoing_outage_reaches_a_drilldown_range_that_starts_after_it(
    client: AsyncClient,
    case: str,
    last_stored_bucket: datetime | None,
) -> None:
    """The drilldown must show the incident the user clicked, however old it is.

    An outage is announced ONCE, at onset, and never re-emitted, so a scope that
    has been down for ten days has exactly one row describing it and that row is
    ten days old. The Anomalies list, the sidebar badge and the bell all keep
    reporting it (``_outage_is_still_running`` re-checks it against the series
    rather than ageing it out), but the chart the list links to loads only its
    selected range — 7 days by default — so the anchor was outside every query
    the drilldown ran and the page for the incident answered ``latest_signal:
    null``.
    """
    slug = f"outage-open-before-range-{case}"
    await _seed_ongoing_outage(
        client,
        slug,
        anchor=_LONG_OUTAGE_ANCHOR_BUCKET,
        last_stored_bucket=last_stored_bucket,
    )

    async with TestSessionLocal() as session:
        listed = await get_active_signals(session, slug, expanded=True)
    assert len(listed) == 1, listed

    with captured_sql() as statements:
        resp = await client.get(
            f"/api/v1/projects/{slug}/event-types/{listed[0].scope_ref}/metrics",
            params={
                "from": _DEFAULT_RANGE_FROM.isoformat(),
                "to": datetime.now(UTC).isoformat(),
            },
        )
    assert resp.status_code == 200
    body = resp.json()

    drilldown = body["latest_signal"]
    assert drilldown is not None, "the drilldown dropped an open signal older than its range"
    assert drilldown["state"] == listed[0].state == "latest_scan"
    anchor_iso = _LONG_OUTAGE_ANCHOR_BUCKET.isoformat().replace("+00:00", "Z")
    assert drilldown["bucket"] == anchor_iso

    # Widening is what makes the signal reachable, so the chart shows the anchor
    # too — the user sees the bucket they clicked, not an empty range above it.
    flagged = [point["bucket"] for point in body["data"] if point["is_anomaly"]]
    assert flagged == [anchor_iso], body["data"]

    # The widening probe must stay off an unbounded event_metrics scan, same
    # rule as every other read of that table (see test_metrics_api.py).
    unbounded = [
        statement
        for statement in statements
        if "FROM event_metrics" in statement
        and "LIMIT" not in statement.upper()
        and "event_metrics.bucket >=" not in statement
    ]
    assert unbounded == [], f"unbounded event_metrics scan: {unbounded}"


@pytest.mark.asyncio
async def test_a_closed_anomaly_before_the_range_leaves_the_drilldown_range_alone(
    client: AsyncClient,
) -> None:
    """And a CLOSED anchor does not drag the chart back to it.

    The pairing matters: reaching back to any older anomaly would silently serve
    a different range than the user picked, on every scope that has ever fired.
    Only an anchor that is still OPEN — the one the list is reporting right now —
    earns the widening. Here the scan stopped collecting with the scope, so the
    stopped-collector cap closes the anchor and nothing may be widened for it.
    """
    slug = "outage-closed-before-range"
    await _seed_ongoing_outage(
        client,
        slug,
        scan_alive=False,
        anchor=_LONG_OUTAGE_ANCHOR_BUCKET,
    )

    async with TestSessionLocal() as session:
        assert await get_active_signals(session, slug, expanded=True) == []

    listing = (await client.get(f"/api/v1/projects/{slug}/event-types")).json()
    dead_id = next(row["id"] for row in listing if row["name"] == "dead_type")
    resp = await client.get(
        f"/api/v1/projects/{slug}/event-types/{dead_id}/metrics",
        params={
            "from": _DEFAULT_RANGE_FROM.isoformat(),
            "to": datetime.now(UTC).isoformat(),
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["latest_signal"] is None
    assert body["data"] == [], "a closed anchor pulled the chart outside the requested range"


@pytest.mark.asyncio
async def test_outage_on_a_stopped_scan_closes_on_both_surfaces(client: AsyncClient) -> None:
    """The stopped-collector cap survives: no live series, no open signal.

    Same anchor, but nothing on the scan has collected since — indistinguishable
    from a scan that was switched off, and pinning that red forever is exactly
    what the latest-scan freshness cap exists to prevent.
    """
    page_slug = "outage-stopped-page"
    await _seed_ongoing_outage(client, page_slug, scan_alive=False)
    sidebar_slug = "outage-stopped-sidebar"
    await _seed_ongoing_outage(client, sidebar_slug, scan_alive=False)

    async with TestSessionLocal() as session:
        page_signals = await get_active_signals(session, page_slug, expanded=True)
    assert page_signals == []

    resp = await client.get(f"/api/v1/projects/{sidebar_slug}")
    assert resp.status_code == 200
    assert resp.json()["summary"]["monitoring_signal_count"] == 0


# ---------------------------------------------------------------------------
# A zero baseline is not an outage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_zero_baseline_anchor_closes_on_every_surface(client: AsyncClient) -> None:
    """Zero expected, zero actual: nothing was lost, so there is nothing to hold open.

    The anchor is shaped exactly like the ongoing outage above and differs in one
    field — the scope was never expected to emit anything. Nothing has been lost,
    so the re-check must not hold it open; and it is the one shape the re-check
    ALONE can end, because an empty scope stores no metric rows and its own head
    stays frozen at the anchor, keeping ``classify_signal_state``'s latest-scan
    branch true forever. Production carried 14 of these, open since the day they
    were written and impossible to close.

    They sit below the magnitude gate, so the badge and the page's "Significant"
    view never showed them — but the page's own headline denominator ("52 of 214
    open") is the LENGTH of this list, so a number the operator uses to judge
    whether detection is noisy could only ever grow.
    """
    page_slug = "zero-baseline-page"
    await _seed_ongoing_outage(client, page_slug, expected_count=0)
    async with TestSessionLocal() as session:
        page_signals = await get_active_signals(session, page_slug, expanded=True)

    # THE assertion of this test: pre-fix this list had one row, forever.
    assert page_signals == []

    # The badge read 0 before the fix too (the magnitude gate hid the row), so
    # this pins that the two surfaces still agree rather than that the badge
    # moved — parity is what this module exists to hold.
    sidebar_slug = "zero-baseline-sidebar"
    await _seed_ongoing_outage(client, sidebar_slug, expected_count=0)
    resp = await client.get(f"/api/v1/projects/{sidebar_slug}")
    assert resp.status_code == 200
    assert resp.json()["summary"]["monitoring_signal_count"] == 0

    # And the drilldown the list links to, which classifies the same anchor on
    # its own path and reported it live while the list no longer lists it.
    listing = (await client.get(f"/api/v1/projects/{page_slug}/event-types")).json()
    dead_id = next(row["id"] for row in listing if row["name"] == "dead_type")
    drilldown = await client.get(f"/api/v1/projects/{page_slug}/event-types/{dead_id}/metrics")
    assert drilldown.status_code == 200
    assert drilldown.json()["latest_signal"] is None


@pytest.mark.asyncio
async def test_a_zero_baseline_anchor_does_not_widen_the_drilldown_range(
    client: AsyncClient,
) -> None:
    """And a closed zero-baseline anchor does not drag the chart back to itself.

    The conditional range widening (``metrics_service._load_scope_anomalies``)
    runs the same classification the response will, so it has to answer the same
    way: only an anchor that is still OPEN earns a range the user did not pick.
    Without the expectation in that call the widening kept reaching ten days back
    for a row that is no longer a signal.
    """
    slug = "zero-baseline-before-range"
    await _seed_ongoing_outage(
        client,
        slug,
        anchor=_LONG_OUTAGE_ANCHOR_BUCKET,
        expected_count=0,
    )

    async with TestSessionLocal() as session:
        assert await get_active_signals(session, slug, expanded=True) == []

    listing = (await client.get(f"/api/v1/projects/{slug}/event-types")).json()
    dead_id = next(row["id"] for row in listing if row["name"] == "dead_type")
    resp = await client.get(
        f"/api/v1/projects/{slug}/event-types/{dead_id}/metrics",
        params={
            "from": _DEFAULT_RANGE_FROM.isoformat(),
            "to": datetime.now(UTC).isoformat(),
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["latest_signal"] is None
    assert body["data"] == [], "a closed anchor pulled the chart outside the requested range"


@pytest.mark.asyncio
async def test_a_spike_from_a_zero_baseline_is_still_a_signal(client: AsyncClient) -> None:
    """The class the fix must NOT touch: a real spike out of nothing.

    Expected 0 with a non-zero actual is a scope that started emitting where it
    never had — a genuine observation, and one the magnitude gate rates highly
    (|5 - 0| / max(0, 1) = 5.0). It never reaches the outage re-check at all: the
    anchor row does not say the scope was at zero, so the predicate rejects it on
    ``actual_count`` before the expectation is looked at. Its behaviour is
    identical either side of the fix, fresh and aged, and this test says so.
    """
    fresh_slug = "zero-baseline-spike-fresh"
    await _seed_ongoing_outage(
        client,
        fresh_slug,
        anchor=_BUCKET,
        actual_count=5,
        expected_count=0,
    )
    async with TestSessionLocal() as session:
        fresh = await get_active_signals(session, fresh_slug, expanded=True)

    assert len(fresh) == 1, fresh
    assert fresh[0].state == "latest_scan"
    assert (fresh[0].actual_count, fresh[0].expected_count) == (5, 0)
    assert is_significant_signal(fresh[0].actual_count, fresh[0].expected_count)

    # Aged five days past the horizon it closes on its own age — as it always
    # did, and for the ordinary reason, not the new one.
    aged_slug = "zero-baseline-spike-aged"
    await _seed_ongoing_outage(
        client,
        aged_slug,
        actual_count=5,
        expected_count=0,
    )
    async with TestSessionLocal() as session:
        assert await get_active_signals(session, aged_slug, expanded=True) == []


# ---------------------------------------------------------------------------
# Catalog metrics are classified on THEIR OWN grid, everywhere
# ---------------------------------------------------------------------------

# 30 hours back: outside a bare 24h freshness window, inside the daily grid's own
# max(24h, 3 * 1d) = 72h horizon. Every surface that judges a daily catalog metric
# on the right grid keeps this signal open; every surface that falls back to 24h
# closes it. Metric-scope scope_refs are built with ``str(uuid)`` on BOTH the page
# and the badge path (no SQL cast is involved), so unlike the event scopes above a
# single seeded project can be read by every surface at once.
_DAILY_BUCKET = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
    hours=30
)


def _metric_anomaly(metric_id: str, *, bucket: datetime = _DAILY_BUCKET) -> MetricAnomaly:
    """A metric-scope anomaly: project-global, NULL scan_config_id, keyed by metric id."""
    return MetricAnomaly(
        id=uuid.uuid4(),
        scan_config_id=None,
        scope_type="metric",
        scope_ref=str(uuid.UUID(metric_id)),
        event_id=None,
        event_type_id=None,
        bucket=bucket,
        actual_count=99,
        expected_count=40,
        stddev=5,
        z_score=8,
        direction="spike",
    )


async def _seed_daily_catalog_metric(
    client: AsyncClient,
    slug: str,
    *,
    inherits_scan_grid: bool,
) -> str:
    """Seed one anomalous catalog metric whose grid is DAILY, and return its id.

    ``inherits_scan_grid=False`` is a ``fact`` metric carrying ``interval='1d'``
    itself. ``inherits_scan_grid=True`` is an ``event_composition`` metric, which
    leaves ``interval`` NULL and rides the grid of the scan its stored values are
    stamped with — here a daily scan. Both are the same daily series; only where
    the grid is recorded differs, and no surface may care which.
    """
    project_resp = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert project_resp.status_code == 201
    project_id = uuid.UUID(project_resp.json()["id"])

    data_source_id = await _create_data_source(client, f"Warehouse {slug}")
    scan_id = await _create_scan(client, slug, data_source_id, "Daily Scan")

    metric_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        if inherits_scan_grid:
            # The source scan collects daily; the metric has no grid of its own.
            scan = await session.get(ScanConfig, uuid.UUID(scan_id))
            assert scan is not None
            scan.interval = "1d"
        session.add(
            MetricDefinition(
                id=metric_id,
                project_id=project_id,
                name=f"metric_{metric_id.hex[:8]}",
                display_name="Daily Metric",
                kind=(
                    MetricKind.event_composition.value
                    if inherits_scan_grid
                    else MetricKind.fact.value
                ),
                aggregation=None if inherits_scan_grid else MetricAggregation.count.value,
                composition=MetricComposition.single.value,
                config={},
                data_source_id=None if inherits_scan_grid else uuid.UUID(data_source_id),
                interval=None if inherits_scan_grid else "1d",
                status=MetricStatus.active.value,
                anomaly_detection_enabled=True,
            )
        )
        session.add(
            MetricValue(
                id=uuid.uuid4(),
                metric_definition_id=metric_id,
                # Only an ``event_composition`` metric stamps its values with the
                # source scan; that stamp IS where its grid is recorded.
                scan_config_id=uuid.UUID(scan_id) if inherits_scan_grid else None,
                bucket=_DAILY_BUCKET,
                value=99.0,
            )
        )
        session.add(_metric_anomaly(str(metric_id)))
        await session.commit()
    return str(metric_id)


async def _metric_signal_surfaces(
    client: AsyncClient,
    slug: str,
    metric_id: str,
    *,
    series_time_from: datetime | None = None,
) -> dict[str, object]:
    """Read the same catalog-metric signal from every surface that reports it.

    ``series_time_from`` is the drilldown's range picker: the other three
    surfaces are project-wide and have no range to pick, so it is the only knob
    that can make them disagree by construction.
    """
    async with TestSessionLocal() as session:
        page_signals = [
            signal
            for signal in await get_active_signals(session, slug, expanded=True)
            if signal.scope_type == "metric"
        ]

    summary = await client.get(f"/api/v1/projects/{slug}")
    assert summary.status_code == 200

    listing = await client.get(f"/api/v1/projects/{slug}/metrics")
    assert listing.status_code == 200
    listed = next(item for item in listing.json()["items"] if item["id"] == metric_id)

    series = await client.get(
        f"/api/v1/projects/{slug}/metrics/{metric_id}/series",
        # The range picker's query alias is ``from``, exactly as the frontend
        # sends it; ``params`` so the offset's ``+`` survives URL encoding.
        params={} if series_time_from is None else {"from": series_time_from.isoformat()},
    )
    assert series.status_code == 200

    return {
        "page": len(page_signals),
        "page_state": page_signals[0].state if page_signals else None,
        "badge": summary.json()["summary"]["monitoring_signal_count"],
        "list": listed["latest_signal"] is not None,
        "drilldown": series.json()["latest_signal"] is not None,
    }


@pytest.mark.asyncio
async def test_daily_catalog_metric_is_open_on_every_surface(client: AsyncClient) -> None:
    """A daily metric's signal must not depend on which surface is asking.

    The badge's batched query (``_active_metric_signals_by_project``) once never
    selected ``MetricDefinition.interval`` nor passed it to ``classify_signal_state``,
    so it judged every catalog metric against a bare 24h window while the
    Anomalies page, the metrics list and the drilldown all used the metric's own
    grid. A daily metric therefore read OPEN on the page and ZERO on the badge.
    """
    slug = "metric-grid-own"
    metric_id = await _seed_daily_catalog_metric(client, slug, inherits_scan_grid=False)

    surfaces = await _metric_signal_surfaces(client, slug, metric_id)

    assert surfaces == {
        "page": 1,
        "page_state": "latest_scan",
        "badge": 1,
        "list": True,
        "drilldown": True,
    }


@pytest.mark.asyncio
async def test_event_composition_metric_is_open_on_every_surface(client: AsyncClient) -> None:
    """Same claim for a metric whose daily grid is INHERITED from its source scan.

    An ``event_composition`` metric leaves ``MetricDefinition.interval`` NULL, so
    the list paths handed ``classify_signal_state`` no interval at all and fell
    back to 24h, while the drilldown resolved the inherited scan grid. On a daily
    scan the list closed a signal the detail page showed as open.
    """
    slug = "metric-grid-inherited"
    metric_id = await _seed_daily_catalog_metric(client, slug, inherits_scan_grid=True)

    surfaces = await _metric_signal_surfaces(client, slug, metric_id)

    assert surfaces == {
        "page": 1,
        "page_state": "latest_scan",
        "badge": 1,
        "list": True,
        "drilldown": True,
    }


# ---------------------------------------------------------------------------
# The remaining two ways the four catalog-metric surfaces could still disagree
# ---------------------------------------------------------------------------

# Eight days back, hour-aligned. Open under a 240-hour (10-day)
# ``recent_signal_window_hours``, closed under the 24h default, and outside any
# range picker narrower than eight days on either count.
_OLD_BUCKET = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
    days=8
)
_WIDE_WINDOW_HOURS = 240
# The range a user can pick on the drilldown while the signal above is open.
_NARROW_RANGE_FROM = datetime.now(UTC) - timedelta(days=2)


async def _seed_catalog_metric_signal(
    client: AsyncClient,
    slug: str,
    *,
    bucket: datetime,
    detection_enabled: bool = True,
    status: MetricStatus = MetricStatus.active,
    recent_signal_window_hours: int | None = None,
) -> str:
    """Seed one anomalous DAILY ``fact`` metric and return its id.

    Deliberately a ``fact`` metric with its own ``interval``: the grid-resolution
    variants are already covered above, and these two cases are about the flag
    and the range rather than about where the grid is recorded.
    """
    project_resp = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert project_resp.status_code == 201
    project_id = uuid.UUID(project_resp.json()["id"])

    data_source_id = await _create_data_source(client, f"Warehouse {slug}")

    metric_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        if recent_signal_window_hours is not None:
            session.add(
                ProjectAnomalySettings(
                    project_id=project_id,
                    anomaly_detection_enabled=True,
                    recent_signal_window_hours=recent_signal_window_hours,
                )
            )
        session.add(
            MetricDefinition(
                id=metric_id,
                project_id=project_id,
                name=f"metric_{metric_id.hex[:8]}",
                display_name="Daily Metric",
                kind=MetricKind.fact.value,
                aggregation=MetricAggregation.count.value,
                composition=MetricComposition.single.value,
                config={},
                data_source_id=uuid.UUID(data_source_id),
                interval="1d",
                status=status.value,
                anomaly_detection_enabled=detection_enabled,
            )
        )
        session.add(
            MetricValue(
                id=uuid.uuid4(),
                metric_definition_id=metric_id,
                scan_config_id=None,
                bucket=bucket,
                value=99.0,
            )
        )
        session.add(_metric_anomaly(str(metric_id), bucket=bucket))
        await session.commit()
    return str(metric_id)


@pytest.mark.asyncio
async def test_open_metric_signal_older_than_the_selected_range_is_still_shown(
    client: AsyncClient,
) -> None:
    """A signal the list linked you to must not vanish on the page it links to.

    ``recent_signal_window_hours`` is a project-wide dial and reaches up to 720
    (30 days), while the drilldown's range picker is per-visit. Raise the window
    to ten days and an eight-day-old metric signal is open on the Anomalies page,
    the sidebar badge and the metrics list — but the drilldown loaded anomalies
    only inside the requested window, so picking anything narrower than the
    signal's own age left the detail page reporting no signal at all. The event
    scopes were given the reach-back for exactly this shape; the catalog metric
    was left out.
    """
    slug = "metric-window-wider-than-range"
    metric_id = await _seed_catalog_metric_signal(
        client,
        slug,
        bucket=_OLD_BUCKET,
        recent_signal_window_hours=_WIDE_WINDOW_HOURS,
    )

    surfaces = await _metric_signal_surfaces(
        client,
        slug,
        metric_id,
        series_time_from=_NARROW_RANGE_FROM,
    )

    assert surfaces == {
        "page": 1,
        "page_state": "latest_scan",
        "badge": 1,
        "list": True,
        "drilldown": True,
    }


@pytest.mark.asyncio
async def test_closed_metric_signal_does_not_widen_the_selected_range(
    client: AsyncClient,
) -> None:
    """The reach-back is CONDITIONAL, and this is the other half of it.

    Same eight-day-old anomaly on the same daily grid, but under the default 24h
    window its horizon is max(24h, 3 x 1d) = 72h, so it is closed on every
    project-wide surface. Reaching back to it anyway would serve a different
    range than the user picked on every metric that has ever been flagged.
    """
    slug = "metric-closed-anchor"
    metric_id = await _seed_catalog_metric_signal(client, slug, bucket=_OLD_BUCKET)

    surfaces = await _metric_signal_surfaces(
        client,
        slug,
        metric_id,
        series_time_from=_NARROW_RANGE_FROM,
    )

    assert surfaces == {
        "page": 0,
        "page_state": None,
        "badge": 0,
        "list": False,
        "drilldown": False,
    }


@pytest.mark.asyncio
async def test_metric_with_detection_disabled_is_closed_on_every_surface(
    client: AsyncClient,
) -> None:
    """Off means off, on all four surfaces rather than on two of them.

    Turning a metric's anomaly detection off stops new rows being scored but
    leaves the existing ones in place. The Anomalies page and the sidebar badge
    both filter on ``MetricDefinition.anomaly_detection_enabled``; the metrics
    list and the drilldown did not, so flipping the switch immediately produced a
    metric that read "no open signals" on the page and the badge while its own
    row and its own detail page still showed one.
    """
    slug = "metric-detection-off"
    metric_id = await _seed_catalog_metric_signal(
        client,
        slug,
        bucket=_DAILY_BUCKET,
        detection_enabled=False,
    )

    surfaces = await _metric_signal_surfaces(client, slug, metric_id)

    assert surfaces == {
        "page": 0,
        "page_state": None,
        "badge": 0,
        "list": False,
        "drilldown": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("parked", [MetricStatus.archived, MetricStatus.draft])
async def test_a_metric_that_left_active_is_closed_on_every_surface(
    client: AsyncClient,
    parked: MetricStatus,
) -> None:
    """Leaving ``active`` is as final as the toggle, on all four surfaces.

    Detection scores only ``active`` metrics, but all four display surfaces
    filtered on ``anomaly_detection_enabled`` alone, so an archived metric kept
    reporting the open signal it froze with — collection stops, the newest value
    bucket stops advancing, and the last anomaly stays on the settled head until
    the wall-clock horizon max(24h, 3 x interval) closes it (three days on this
    daily grid, three weeks on a weekly one).

    Same seed and same daily bucket as the detection-off case above, so the only
    thing under test is the status half of the predicate.
    """
    slug = f"metric-{parked.value}"
    metric_id = await _seed_catalog_metric_signal(
        client,
        slug,
        bucket=_DAILY_BUCKET,
        status=parked,
    )

    surfaces = await _metric_signal_surfaces(client, slug, metric_id)

    assert surfaces == {
        "page": 0,
        "page_state": None,
        "badge": 0,
        "list": False,
        "drilldown": False,
    }


def test_relative_effect_floors_a_count_but_not_a_fractional_series() -> None:
    """The floor protects counts and is a category error on a ratio.

    A count expected below 1 means essentially no traffic, so dividing by it
    would let a dead scope outrank a real move — the floor stays. A conversion
    ratio expected at 0.12 and observed at 0.04 is a two-thirds collapse; the
    same floor scored it 0.08, below the 0.5 bar, so it never reached the
    default Anomalies view, the bell or the sidebar badge.
    """
    from tripl.services.metrics_insights_service import (
        SIGNIFICANT_MIN_REL_EFFECT,
        is_significant_signal,
        relative_effect,
    )

    # Count: floored, and deliberately so.
    assert relative_effect(2.0, 0.5) == pytest.approx(1.5)
    assert relative_effect(150.0, 100.0) == pytest.approx(0.5)

    # Fractional: divided by its real baseline.
    assert relative_effect(0.04, 0.12, count_shaped=False) == pytest.approx(2 / 3)
    assert relative_effect(0.04, 0.12) == pytest.approx(0.08)

    # ...which is exactly the difference between invisible and significant.
    assert not is_significant_signal(0.04, 0.12)
    assert is_significant_signal(0.04, 0.12, count_shaped=False)
    assert SIGNIFICANT_MIN_REL_EFFECT == 0.5


def test_relative_effect_states_a_zero_baseline_rather_than_dividing_by_it() -> None:
    """Unbounded growth off a zero baseline, reported as a FINITE ceiling.

    Finite because the number is serialized: ``inf`` leaves
    ``MetricSignalResponse`` as JSON null, the client reads null as "the server
    did not compute this" and falls back to its count-shaped estimate, which for
    a sub-unit actual scores below the 0.5 bar. The badge gates in-process, where
    ``inf`` compares fine, so such a signal was counted on the sidebar and hidden
    on every page the badge links to.
    """
    from tripl.services.metrics_insights_service import (
        MAX_RELATIVE_EFFECT,
        SIGNIFICANT_MIN_REL_EFFECT,
        relative_effect,
    )

    assert relative_effect(0.0, 0.0, count_shaped=False) == 0.0

    effect = relative_effect(0.5, 0.0, count_shaped=False)
    assert math.isfinite(effect)
    assert effect == MAX_RELATIVE_EFFECT
    assert effect >= SIGNIFICANT_MIN_REL_EFFECT
    assert is_significant_signal(0.5, 0.0, count_shaped=False)

    # A baseline small enough to overflow the division on its own lands on the
    # same ceiling rather than on inf.
    assert math.isfinite(relative_effect(1e300, 1e-300, count_shaped=False))


def test_a_zero_baseline_signal_reaches_the_client_as_a_number() -> None:
    """The magnitude has to survive the wire, not just the process.

    Serialization is where the badge and the page parted: the count is taken in
    Python and the filter is applied in the browser, so a value that only
    compares correctly in Python is a parity bug.
    """
    from tripl.services.metrics_insights_service import (
        SIGNIFICANT_MIN_REL_EFFECT,
        relative_effect,
    )

    # Sub-unit actual on purpose: it is the shape where the client's own fallback
    # (|actual - expected| / max(expected, 1) = 0.3) lands below the bar.
    payload = json.loads(
        MetricSignalResponse(
            scope_type="metric",
            scope_ref=str(uuid.uuid4()),
            state="latest_scan",
            bucket=_BUCKET,
            actual_count=0.3,
            expected_count=0.0,
            stddev=1e-9,
            z_score=9.0,
            direction="spike",
            relative_effect=relative_effect(0.3, 0.0, count_shaped=False),
        ).model_dump_json()
    )

    assert payload["relative_effect"] is not None
    assert payload["relative_effect"] >= SIGNIFICANT_MIN_REL_EFFECT
