"""Cross-surface demo contract.

Creates one demo and asserts every advertised Plan / Observe / Govern surface is
non-empty and internally coherent (ids resolve, coverage reconciles, deliveries
reference real rules). This is the single fixture that fails if a builder stops
populating a surface.

``DEMO_COVERED_SURFACES`` is the CANONICAL list of surfaces the demo promises to
populate. It is deliberately explicit: when a new core product surface is added,
a maintainer must consciously decide whether the demo covers it and extend this
list (and the assertions below) — the demo is not silently allowed to go stale.
"""

import pytest
from httpx import AsyncClient

DEMO_COVERED_SURFACES: frozenset[str] = frozenset(
    {
        # Plan
        "events",
        "event_types",
        "variables",
        "meta_fields",
        "relations",
        # Observe
        "event_metrics_total",
        "metrics_catalog",
        "fact_tables",
        "distribution_drifts",
        "anomaly_signals",
        "monitors_summary",
        "top_events",
        # Govern
        "scan_jobs",
        "coverage",
        "shadow_events",
        "alert_destinations",
        "alert_deliveries",
    }
)

_PLAN = {"events", "event_types", "variables", "meta_fields", "relations"}
_OBSERVE = {
    "event_metrics_total",
    "metrics_catalog",
    "fact_tables",
    "distribution_drifts",
    "anomaly_signals",
    "monitors_summary",
    "top_events",
}
_GOVERN = {"scan_jobs", "coverage", "shadow_events", "alert_destinations", "alert_deliveries"}


def test_coverage_guard_partitions_plan_observe_govern() -> None:
    # Every covered surface belongs to exactly one journey, and all three journeys
    # are represented — a structural guard so the list can't silently lose a whole
    # product area.
    assert _PLAN | _OBSERVE | _GOVERN == DEMO_COVERED_SURFACES
    assert _PLAN and _OBSERVE and _GOVERN
    assert not (_PLAN & _OBSERVE) and not (_OBSERVE & _GOVERN) and not (_PLAN & _GOVERN)


def _items(payload: object) -> list:
    if isinstance(payload, dict):
        for key in ("items", "data", "signals"):
            if key in payload and isinstance(payload[key], list):
                return payload[key]
    return payload if isinstance(payload, list) else []


@pytest.mark.asyncio
async def test_demo_populates_every_advertised_surface(client: AsyncClient) -> None:
    slug = (await client.post("/api/v1/projects/demo")).json()["slug"]

    async def get(path: str) -> object:
        resp = await client.get(f"/api/v1/projects/{slug}{path}")
        assert resp.status_code == 200, (path, resp.status_code, resp.text[:200])
        return resp.json()

    covered: set[str] = set()

    # ── Plan ─────────────────────────────────────────────────────────────
    events = _items(await get("/events"))
    assert events
    assert any(ev.get("field_values") for ev in events)
    assert any(ev.get("meta_values") for ev in events)
    covered.add("events")

    event_types = _items(await get("/event-types"))
    assert event_types
    screen_view = next(et for et in event_types if et["name"] == "screen_view")
    covered.add("event_types")
    assert _items(await get("/variables"))
    covered.add("variables")
    assert _items(await get("/meta-fields"))
    covered.add("meta_fields")
    assert _items(await get("/relations"))
    covered.add("relations")

    # ── Observe ──────────────────────────────────────────────────────────
    assert _items(await get("/metrics/total"))
    covered.add("event_metrics_total")

    metrics = _items(await get("/metrics"))
    assert len(metrics) >= 4
    assert {"sql", "event_composition", "fact"} <= {m["kind"] for m in metrics}
    covered.add("metrics_catalog")

    assert _items(await get("/fact-tables"))
    covered.add("fact_tables")
    # Distribution drift is seeded on the screen_view event type's platform mix.
    drifts = _items(
        await get(f"/distribution-drifts?scope_type=event_type&scope_ref={screen_view['id']}")
    )
    assert drifts
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

    # ── Govern ───────────────────────────────────────────────────────────
    scan_configs = _items(await get("/scans"))
    assert scan_configs
    jobs = _items(await get(f"/scans/{scan_configs[0]['id']}/jobs"))
    assert jobs
    assert any(j["status"] == "completed" for j in jobs)
    covered.add("scan_jobs")

    coverage = await get("/reconciliation/coverage")
    assert coverage["items"]
    assert coverage["summary"]["matched_count"] <= coverage["summary"]["total_count"]
    covered.add("coverage")

    shadow = await get("/reconciliation/shadow-events")
    assert shadow["total"] >= 1
    covered.add("shadow_events")

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
