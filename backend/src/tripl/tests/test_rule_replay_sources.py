"""What the rule REPLAY can see.

Live dispatch merges FIVE candidate sources (``worker/tasks/metrics/dispatch``);
the in-UI replay merged three. Variable-value drifts and release regressions
were never loaded, so they never reached ``tripl.alerting_matching`` at all and
a rule carrying ``include_variable_value_drifts`` or
``include_release_regressions`` replayed SILENT in every field at once —
``anomalies_considered``, ``matched_before_cooldown``, ``firings``, ``noisy``
and ``rendered_message`` are all derived from that one list. The operator read
"quiet, not noisy" for a rule that pages thirty times a week, and
``website/docs/use/troubleshooting.md``'s "if it doesn't match in the simulator
it won't match live either" was false for exactly those two scopes.

The fix is additive, and an additive fix is the easiest kind to prove with a
test that would also pass without it, so every test below names the line it
would catch. Five of them assert an ABSENCE — two drifts no live scan can reach
(one value drift, one schema drift), a variable taken out of scanning, a scan
with no version column, a rule with the toggle off — because loading too much is
the same defect as loading too little pointed the other way: a replay that is
louder than the pipeline is just as unusable as one that is quieter.

One family here was never about loading. ``project_total`` always reached the
replay and was never NAMED in it: ``_build_scope_name_map`` resolved every other
scope and had no branch for the one nearly every rule carries, so the preview
labelled those rows with a raw scan-config uuid where the delivered message says
"All events". Its two tests sit between the loading tests and the renderer ones
because the defect belongs to neither group.

The last three tests are not about loading at all. They are about what the two
newly reachable families SAY, and they render one release regression through
both production renderers and compare the whole item. That is where the
preview's hard-coded ``"expected_basis": ""`` shows up, and with it the LAST of
``window_from``'s three hops, ``alerting_rendering._drift_facts`` — those were
the last two places the preview and the send disagreed about a firing
(a later fix closed the other two). They cannot show the first two hops:
they hand-build both objects, so that a firing the service ACTUALLY emits
carries the window is asserted over HTTP in
``test_a_release_regression_the_pipeline_delivers_now_reaches_the_replay``
instead.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient

# Imported first and for its side effect, exactly as ``test_alert_message_content``
# documents: the worker task package is import-order sensitive and celery_app's
# bottom-of-file registration is what pulls the task modules in an order they
# all survive. This file reaches into ``alerts_messages`` for the SEND-side
# renderer, so it must enter the same way rather than rely on an alphabetically
# earlier test file having done it.
import tripl.worker.celery_app  # noqa: F401
from tripl.alert_templates import (
    ALERT_MESSAGE_FORMAT_PLAIN,
    ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2,
    NO_BASELINE_LABEL,
    get_default_items_template,
)
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import (
    MetricScopeType,
    ReleaseRegressionKind,
    SchemaDriftStatus,
    SchemaDriftType,
)
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.release_regression import ReleaseRegression
from tripl.models.scan_config import ScanConfig
from tripl.models.schema_drift import SchemaDrift
from tripl.models.variable import Variable
from tripl.models.variable_value_drift import VariableValueDrift
from tripl.schemas.alerting import SimulatedRuleFiring
from tripl.services.alerting_rendering import render_firing_item
from tripl.services.alerting_service import _build_scope_name_map
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks.alerts_messages import _build_items_text
from tripl.worker.tasks.metrics.alert_payload import _build_alert_scope_names

_EVENT_NAME = "checkout:completed"
_VARIABLE_NAME = "tier"
_OBSERVED_VALUES = ["platinum", "diamond"]
# The schema drift's field. Named rather than inlined because the scope NAME the
# replay reports is ``"<event type display name>.<field>"``, so the same string
# has to appear in the seed and in the assertion.
_SCHEMA_DRIFT_FIELD = "checkout_total"
_VERSION = "15.7.5"
_PREVIOUS_VERSION = "15.7.4"
# The comparison window a release regression measures over. 51 hours because
# ``alert_templates._format_window_span`` rounds to whole hours and the sentence
# it produces — "over the 51h rollout overlap" — is the one the send has printed
# since the scope shipped.
_ROLLOUT_OVERLAP = timedelta(hours=51)
# Real numbers rather than round ones: 345 against 715.7 is 51.8% share-for-share,
# and a baseline with a decimal point is what makes ``expected=715.7`` visibly
# different from ``expected=715.7 (adoption-adjusted)``.
_OBSERVED_COUNT = 345
_EXPECTED_COUNT = 715.7


async def _seed_replay_project(
    client: AsyncClient,
    slug: str,
    *,
    app_version_column: str | None = "app_version",
    drift_scan_bound: bool = True,
    variable_excluded: bool = False,
    seed_value_drift: bool = True,
    seed_release_regression: bool = True,
    seed_schema_drift: bool = False,
    schema_drift_scan_bound: bool = True,
) -> dict[str, uuid.UUID]:
    """One project holding a value drift, a release regression, a schema drift.

    The schema drift is OFF by default: it is the one family here that always
    reached the replay, so only the two tests about its scan predicate ask for
    it, and every other test's counters stay about the families the issue named.

    The first two signals sit on the SAME event on purpose: a release regression's
    ``scope_ref`` is the underlying event's id, so a project that also carries
    an event-anchored value drift is the shape where a scope-name lookup keyed
    on the ref alone, or a cooldown keyed on the ref alone, would collide.

    Every keyword is one of the predicates a loader has to honour, so a test
    that wants to prove a clause flips exactly one of them and asserts the
    signal disappears.
    """
    resp = await client.post("/api/v1/projects", json={"name": f"Replay {slug}", "slug": slug})
    assert resp.status_code == 201, resp.text
    project_id = uuid.UUID(resp.json()["id"])

    now = datetime.now(UTC)
    regression_window_to = now - timedelta(hours=2)
    ids: dict[str, uuid.UUID] = {"project_id": project_id}
    async with TestSessionLocal() as session, session.begin():
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"ds-{uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="h",
            port=8123,
            database_name="d",
            username="u",
            password_encrypted="",
        )
        session.add(data_source)
        await session.flush()
        scan = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project_id,
            name="sc",
            base_query="SELECT 1",
            cardinality_threshold=100,
            interval="1h",
            app_version_column=app_version_column,
        )
        event_type = EventType(
            id=uuid.uuid4(),
            project_id=project_id,
            name="checkout",
            display_name="Checkout",
            description="",
        )
        session.add_all([scan, event_type])
        await session.flush()
        event = Event(
            id=uuid.uuid4(),
            project_id=project_id,
            event_type_id=event_type.id,
            name=_EVENT_NAME,
            description="",
        )
        variable = Variable(
            id=uuid.uuid4(),
            project_id=project_id,
            name=_VARIABLE_NAME,
            description="",
            excluded_from_scans=variable_excluded,
        )
        session.add_all([event, variable])
        await session.flush()
        if seed_value_drift:
            drift = VariableValueDrift(
                id=uuid.uuid4(),
                project_id=project_id,
                variable_id=variable.id,
                event_id=event.id,
                scan_config_id=scan.id if drift_scan_bound else None,
                observed_values=list(_OBSERVED_VALUES),
                status=SchemaDriftStatus.open.value,
                detected_at=now - timedelta(days=2),
            )
            session.add(drift)
            ids["value_drift_id"] = drift.id
        if seed_schema_drift:
            schema_drift = SchemaDrift(
                id=uuid.uuid4(),
                event_type_id=event_type.id,
                scan_config_id=scan.id if schema_drift_scan_bound else None,
                field_name=_SCHEMA_DRIFT_FIELD,
                drift_type=SchemaDriftType.new_field.value,
                observed_type="Float64",
                declared_type=None,
                sample_value="19.99",
                status=SchemaDriftStatus.open.value,
                detected_at=now - timedelta(days=2),
            )
            session.add(schema_drift)
            ids["schema_drift_id"] = schema_drift.id
        if seed_release_regression:
            regression = ReleaseRegression(
                id=uuid.uuid4(),
                scan_config_id=scan.id,
                scope_type=MetricScopeType.event.value,
                scope_ref=str(event.id),
                event_id=event.id,
                event_type_id=None,
                app_version_column=app_version_column or "app_version",
                version=_VERSION,
                previous_version=_PREVIOUS_VERSION,
                kind=ReleaseRegressionKind.volume_drop.value,
                observed_count=_OBSERVED_COUNT,
                expected_count=_EXPECTED_COUNT,
                ratio=0.482,
                share_prev=0.031,
                share_new=0.015,
                release_share=0.42,
                window_from=regression_window_to - _ROLLOUT_OVERLAP,
                window_to=regression_window_to,
            )
            session.add(regression)
            ids["release_regression_id"] = regression.id
        ids["scan_id"] = scan.id
        ids["event_id"] = event.id
        ids["variable_id"] = variable.id
    return ids


async def _make_rule(
    client: AsyncClient,
    slug: str,
    *,
    value_drifts: bool = False,
    release_regressions: bool = False,
    schema_drifts: bool = False,
) -> tuple[str, str]:
    """A destination plus one rule subscribed to the named scopes only.

    Every volume scope is off, so a firing can only have come from the family
    under test. The numeric thresholds are left at their saved defaults
    deliberately: all four drift-shaped scopes bypass them inside
    ``rule_matches_anomaly``, and a test that relaxed them to 0 would hide a
    regression that started routing these candidates through the numeric branch.
    """
    destination_resp = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations",
        json={
            "type": "slack",
            "name": "Replay Slack",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T1/B1/replay",
        },
    )
    assert destination_resp.status_code == 201, destination_resp.text
    destination_id = str(destination_resp.json()["id"])

    rule_resp = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations/{destination_id}/rules",
        json={
            "name": "Replay Rule",
            "enabled": True,
            "include_project_total": False,
            "include_event_types": False,
            "include_events": False,
            "include_schema_drifts": schema_drifts,
            "include_distribution_drifts": False,
            "include_metrics": False,
            "include_variable_value_drifts": value_drifts,
            "include_release_regressions": release_regressions,
            "notify_on_spike": True,
            "notify_on_drop": True,
        },
    )
    assert rule_resp.status_code == 201, rule_resp.text
    return destination_id, str(rule_resp.json()["id"])


async def _replay(
    client: AsyncClient,
    slug: str,
    destination_id: str,
    rule_id: str,
    *,
    days: int = 7,
) -> dict[str, object]:
    resp = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations/{destination_id}"
        f"/rules/{rule_id}/simulate?days={days}"
    )
    assert resp.status_code == 200, resp.text
    body: dict[str, object] = resp.json()
    return body


# ---------------------------------------------------------------------------
# Variable-value drift: the family that reaches the replay through
# ``_load_variable_value_drift_candidates``.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_value_drift_the_pipeline_delivers_now_reaches_the_replay(
    client: AsyncClient,
) -> None:
    """The headline case, asserted through every number the response reports.

    Live dispatch delivers this row: ``signals._get_active_variable_value_drift_candidates``
    selects it, and ``rule_matches_anomaly`` admits it because
    ``include_variable_value_drifts`` is on and the drift scopes bypass the
    numeric thresholds. The replay returned nothing at all about it.

    Delete the ``*variable_value_drift_candidates`` entry from the candidate
    merge in ``alerting_service.simulate_rule`` and all four counters go to zero
    together — which is the point: they are one list seen five ways, so a fix
    that moved only ``firings`` would be a different bug.

    ``scope_name`` reverts separately. Drop the ``SCOPE_VARIABLE_VALUE_DRIFT``
    branch from ``_build_scope_name_map`` and the caller's fallback names the
    firing after its ``scope_ref``, which is the drift ROW's uuid — the
    "variable_value_drift <uuid>" the operator would have had to decode.
    """
    slug = "b4-158-value-drift"
    ids = await _seed_replay_project(client, slug, seed_release_regression=False)
    destination_id, rule_id = await _make_rule(client, slug, value_drifts=True)

    body = await _replay(client, slug, destination_id, rule_id)

    assert body["anomalies_considered"] == 1
    assert body["matched_before_cooldown"] == 1
    firings = body["firings"]
    assert isinstance(firings, list)
    assert len(firings) == 1
    firing = firings[0]

    assert firing["scope_type"] == MetricScopeType.variable_value_drift.value
    assert firing["scope_ref"] == str(ids["value_drift_id"])
    # Borrowed from the event it was observed on, the way the live payload
    # builder names it — not the drift row's id.
    assert firing["scope_name"] == f"{_EVENT_NAME}.{_VARIABLE_NAME}"
    assert firing["event_id"] == str(ids["event_id"])
    assert firing["drift_type"] == "value_drift"
    assert firing["drift_field"] == _VARIABLE_NAME
    assert firing["sample_value"] == ", ".join(_OBSERVED_VALUES)
    assert firing["direction"] == "spike"
    # ``actual`` is how many novel values were observed; there is no baseline to
    # compare them against, so the ratio is null rather than the stored 0.0
    # placeholder (this family is the newest consumer of it).
    assert firing["actual_count"] == pytest.approx(float(len(_OBSERVED_VALUES)))
    assert firing["expected_count"] == pytest.approx(0.0)
    assert firing["percent_delta"] is None
    assert NO_BASELINE_LABEL in firing["rendered_item"]
    # Only release regressions carry a window; this family leaves it null and
    # the rendered line is unaffected.
    assert firing["window_from"] is None

    # The whole message, not only the row: ``rendered_message`` was one of the
    # fields the issue named, and it is built from the same list.
    assert "value drift: ${tier} observed platinum, diamond" in body["rendered_message"]


@pytest.mark.asyncio
async def test_the_toggle_still_decides_whether_a_value_drift_fires(
    client: AsyncClient,
) -> None:
    """Loading the rows must not be the same thing as firing on them.

    The two numbers disagree on purpose and that disagreement IS the assertion:
    the drift is ``considered`` (the loader found it) and does not
    ``match`` (``include_variable_value_drifts`` is off). A fix that appended
    the candidates straight to the fired list, or that gated them in the service
    instead of in ``alerting_matching``, passes the test above and reddens this
    one — and would have re-opened the divergence the shared matcher exists to
    prevent, in the opposite direction.
    """
    slug = "b4-158-toggle-off"
    await _seed_replay_project(client, slug, seed_release_regression=False)
    destination_id, rule_id = await _make_rule(client, slug, value_drifts=False)

    body = await _replay(client, slug, destination_id, rule_id)

    assert body["anomalies_considered"] == 1
    assert body["matched_before_cooldown"] == 0
    assert body["firings"] == []
    assert body["noisy"] is False


@pytest.mark.asyncio
async def test_a_value_drift_no_scan_can_reach_stays_out_of_the_replay(
    client: AsyncClient,
) -> None:
    """A NULL ``scan_config_id`` is unreachable live, so it must be here too.

    ``signals`` selects ``VariableValueDrift.scan_config_id == config.id`` and a
    NULL never equals a config id, so the live pipeline can never deliver such a
    row — the column is ``SET NULL`` on scan deletion, and the demo seeds one
    deliberately (``services/demo/builders/variables._build_value_drift``).

    Drop ``VariableValueDrift.scan_config_id.is_not(None)`` from
    ``_load_variable_value_drift_candidates`` and ``anomalies_considered``
    becomes 1 and the firing appears — a replay LOUDER than the pipeline it
    predicts, which fails the operator the same way the silence did.
    """
    slug = "b4-158-orphan-drift"
    await _seed_replay_project(
        client,
        slug,
        drift_scan_bound=False,
        seed_release_regression=False,
    )
    destination_id, rule_id = await _make_rule(client, slug, value_drifts=True)

    body = await _replay(client, slug, destination_id, rule_id)

    assert body["anomalies_considered"] == 0
    assert body["firings"] == []


@pytest.mark.asyncio
async def test_an_excluded_variable_replays_as_quietly_as_it_alerts(
    client: AsyncClient,
) -> None:
    """Excluding a variable from scans silences its existing drift rows.

    The live loader joins ``Variable`` for the ``excluded_from_scans`` gate and
    not only for the display name: exclusion stops new drift being detected but
    the rows already written outlive it, and the branch-merge and branch-revert
    paths carry the flag across without a purge. Drop
    ``Variable.excluded_from_scans.is_(False)`` from the replay loader and the
    simulator promises pages for a variable the operator took out of scanning.
    """
    slug = "b4-158-excluded-variable"
    await _seed_replay_project(
        client,
        slug,
        variable_excluded=True,
        seed_release_regression=False,
    )
    destination_id, rule_id = await _make_rule(client, slug, value_drifts=True)

    body = await _replay(client, slug, destination_id, rule_id)

    assert body["anomalies_considered"] == 0
    assert body["firings"] == []


# ---------------------------------------------------------------------------
# Schema drift: the family the replay could always LOAD, and loaded too much of.
#
# Nothing here is about reachability — ``_load_schema_drift_candidates`` has
# shipped since the schema scope did. It is about the one predicate it lacked:
# the live loader selects ``SchemaDrift.scan_config_id == config.id``, so the
# orphan rows a scan deletion leaves behind (``ondelete="SET NULL"``) are
# undeliverable forever, and the replay listed them anyway. Same defect as the
# value drift's, pointed the same way, and the argument is the one the sibling
# loader states in prose.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_schema_drift_the_pipeline_delivers_now_reaches_the_replay(
    client: AsyncClient,
) -> None:
    """The control that keeps the orphan test below from passing vacuously.

    Identical seed, one field flipped: this drift still points at its scan, so
    ``signals._get_active_schema_drift_candidates`` selects it and the replay
    must too. It also pins the scope NAME, because ``_build_scope_name_map``
    resolves a schema drift through the event TYPE the drift row carries — the
    ref is the drift's own id, which would be an unreadable uuid on its own.
    """
    slug = "b4-158-schema-drift"
    ids = await _seed_replay_project(
        client,
        slug,
        seed_schema_drift=True,
        seed_value_drift=False,
        seed_release_regression=False,
    )
    destination_id, rule_id = await _make_rule(client, slug, schema_drifts=True)

    body = await _replay(client, slug, destination_id, rule_id)

    assert body["anomalies_considered"] == 1
    assert body["matched_before_cooldown"] == 1
    firings = body["firings"]
    assert isinstance(firings, list)
    assert len(firings) == 1
    assert firings[0]["scope_type"] == MetricScopeType.schema.value
    assert firings[0]["scope_ref"] == str(ids["schema_drift_id"])
    assert firings[0]["scope_name"] == f"Checkout.{_SCHEMA_DRIFT_FIELD}"


@pytest.mark.asyncio
async def test_a_schema_drift_no_scan_can_reach_stays_out_of_the_replay(
    client: AsyncClient,
) -> None:
    """A NULL ``scan_config_id`` is unreachable live, so it must be here too.

    ``signals`` selects ``SchemaDrift.scan_config_id == config.id`` and a NULL
    never equals a config id, so the live pipeline can never deliver this row:
    the column is ``SET NULL`` when a scan is deleted
    (``scan_service.delete_scan_config``, and the ``DataSource.scan_configs``
    cascade), nothing purges the drift rows the delete orphans, and they keep
    ``status="open"`` until the 30-day prune.

    Drop ``SchemaDrift.scan_config_id.is_not(None)`` from
    ``_load_schema_drift_candidates`` and ``anomalies_considered`` becomes 1 and
    the firing reappears — the operator tunes a rule against pages the pipeline
    will never send, which is the same defect as the silence already
    filed, pointed the other way. The test above is the proof this one is not
    green merely because no schema drift was loaded at all.
    """
    slug = "b4-158-orphan-schema-drift"
    await _seed_replay_project(
        client,
        slug,
        seed_schema_drift=True,
        schema_drift_scan_bound=False,
        seed_value_drift=False,
        seed_release_regression=False,
    )
    destination_id, rule_id = await _make_rule(client, slug, schema_drifts=True)

    body = await _replay(client, slug, destination_id, rule_id)

    assert body["anomalies_considered"] == 0
    assert body["firings"] == []


# ---------------------------------------------------------------------------
# Release regressions: the family that reaches the replay through
# ``_load_release_regression_candidates``.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_release_regression_the_pipeline_delivers_now_reaches_the_replay(
    client: AsyncClient,
) -> None:
    """The second family, and the one whose WORDING also had to move.

    Three independent reverts redden three different assertions here:

    * remove ``*release_regression_candidates`` from the candidate merge and the
      counters go to zero — the filed defect;
    * restore the hard-coded ``"expected_basis": ""`` in
      ``alerting_rendering.render_firing_item`` and the preview prints
      ``expected=715.7`` where the delivery says
      ``expected=715.7 (adoption-adjusted)`` about the same firing;
    * drop ``window_from`` at ANY of its three hops — the loader's candidate,
      ``simulate_rule``'s ``SimulatedRuleFiring(...)``, or
      ``alerting_rendering._drift_facts`` — and the rollout-overlap clause
      vanishes from the preview alone, because
      ``alert_templates._format_window_span`` is written to return None rather
      than to fail. The middle hop is the only one no other test covers:
      ``_release_regression_pair`` below hand-builds a firing that already
      carries the window, so the renderer parity tests never exercise it.

    ``scope_name`` is the fourth: a release regression's ``scope_ref`` IS the
    underlying event's id, so without the borrow branch in
    ``_build_scope_name_map`` the row is named by a raw uuid.
    """
    slug = "b4-158-release-regression"
    ids = await _seed_replay_project(client, slug, seed_value_drift=False)
    destination_id, rule_id = await _make_rule(client, slug, release_regressions=True)

    body = await _replay(client, slug, destination_id, rule_id)

    assert body["anomalies_considered"] == 1
    assert body["matched_before_cooldown"] == 1
    firings = body["firings"]
    assert isinstance(firings, list)
    assert len(firings) == 1
    firing = firings[0]

    assert firing["scope_type"] == MetricScopeType.release_regression.value
    assert firing["scope_ref"] == str(ids["event_id"])
    assert firing["scope_name"] == _EVENT_NAME
    assert firing["direction"] == "drop"
    assert firing["drift_field"] == _VERSION
    assert firing["drift_type"] == ReleaseRegressionKind.volume_drop.value
    assert firing["sample_value"] == _PREVIOUS_VERSION
    assert firing["actual_count"] == pytest.approx(float(_OBSERVED_COUNT))
    assert firing["expected_count"] == pytest.approx(_EXPECTED_COUNT)
    assert firing["percent_delta"] == pytest.approx(51.795, abs=0.01)
    # The window is on the wire, it is the START — ``bucket`` is the end — and
    # it is the REGRESSION's own rather than a stand-in: the two ends sit
    # exactly the seeded rollout overlap apart, which is the span the clause
    # below quotes. Revert ``window_from=getattr(anomaly, "window_from", None)``
    # in ``simulate_rule``'s ``SimulatedRuleFiring(...)`` and this reads None,
    # because the schema defaults the field rather than demanding it.
    assert firing["window_from"] is not None
    # Compared naive on both sides: the two datetimes come off ONE row through
    # ONE serializer, so they are either both aware (Postgres) or both naive
    # (SQLite drops the offset on round-trip) and the span is the same either
    # way.
    window_start = datetime.fromisoformat(str(firing["window_from"])).replace(tzinfo=None)
    window_end = datetime.fromisoformat(str(firing["bucket"])).replace(tzinfo=None)
    assert window_end - window_start == _ROLLOUT_OVERLAP

    rendered_item = firing["rendered_item"]
    assert isinstance(rendered_item, str)
    assert "(adoption-adjusted)" in rendered_item
    assert "release: dropped in 15.7.5 vs 15.7.4 over the 51h rollout overlap" in rendered_item
    assert "715.7 is 15.7.4's share of this event at 15.7.5's own volume" in rendered_item
    assert "so 51.8% is share-for-share" in rendered_item
    assert rendered_item in body["rendered_message"]


@pytest.mark.asyncio
async def test_a_scan_with_no_version_column_replays_no_regressions(
    client: AsyncClient,
) -> None:
    """The live short-circuit, mirrored rather than assumed.

    ``signals._get_active_release_regression_candidates`` returns ``{}`` before
    it queries when the scan has no ``app_version_column``, and rows OUTLIVE the
    setting: nothing purges them when an operator clears the column, so "no rows
    exist anyway" is not a substitute for asking. Drop the two
    ``ScanConfig.app_version_column`` clauses from the replay loader and this
    row — which live stopped delivering the moment the column was cleared —
    comes back in the simulator alone.
    """
    slug = "b4-158-no-version-column"
    await _seed_replay_project(
        client,
        slug,
        app_version_column=None,
        seed_value_drift=False,
    )
    destination_id, rule_id = await _make_rule(client, slug, release_regressions=True)

    body = await _replay(client, slug, destination_id, rule_id)

    assert body["anomalies_considered"] == 0
    assert body["firings"] == []


@pytest.mark.asyncio
async def test_a_regression_whose_window_closed_before_the_replay_is_out_of_range(
    client: AsyncClient,
) -> None:
    """The one semantic the replay had to CHOOSE, pinned so it stays chosen.

    The live loader has no time filter at all — it runs per collection, right
    after the pass that rewrote the rows, so every row it sees is current. A
    replay cannot reconstruct that history from one row per (scan, scope,
    release), so it places the row at the window it measured and admits it when
    that window's END falls inside the range.

    A one-day replay therefore does not see a regression measured three days
    ago, while a seven-day replay does. Both arms are asserted in one test
    because they are two faces of one rule: drop the ``window_to`` bounds and
    the first arm reddens with a firing; widen the bound past the window and the
    replay stops meaning "the last N days" for this family only.
    """
    slug = "b4-158-window-bounds"
    await _seed_replay_project(client, slug, seed_value_drift=False)
    destination_id, rule_id = await _make_rule(client, slug, release_regressions=True)

    # Seeded at now-2h, so a 1-day window contains it and the assertions below
    # are about the bound rather than about the fixture drifting out of both.
    inside = await _replay(client, slug, destination_id, rule_id, days=1)
    assert inside["anomalies_considered"] == 1

    wide = await _replay(client, slug, destination_id, rule_id, days=30)
    assert wide["anomalies_considered"] == 1
    # Exactly once in the wider window too: the table holds one row per release,
    # so a standing regression is a LOWER bound on what a live rule sends, not a
    # per-collection replay. Widening the range must not multiply it.
    assert len(wide["firings"]) == 1


@pytest.mark.asyncio
async def test_the_replay_counts_both_new_families_in_every_number_it_reports(
    client: AsyncClient,
) -> None:
    """Both at once, on the same event, through one rule.

    The two signals share an event, which is the shape where a scope-name map or
    a cooldown key that forgot ``scope_type`` would collapse them into one. Two
    firings is the assertion; ``simulate_rule_firings`` keys its cooldown on the
    (scope_type, scope_ref, scan) triple, so the pair survives.

    This is also the test that reads the filed issue back: the operator's
    complaint was that ``anomalies_considered``, ``matched_before_cooldown``,
    ``firings``, ``noisy`` and ``rendered_message`` ALL omitted these rows.
    """
    slug = "b4-158-both-families"
    ids = await _seed_replay_project(client, slug)
    destination_id, rule_id = await _make_rule(
        client,
        slug,
        value_drifts=True,
        release_regressions=True,
    )

    body = await _replay(client, slug, destination_id, rule_id)

    assert body["anomalies_considered"] == 2
    assert body["matched_before_cooldown"] == 2
    firings = body["firings"]
    assert isinstance(firings, list)
    assert {firing["scope_type"] for firing in firings} == {
        MetricScopeType.variable_value_drift.value,
        MetricScopeType.release_regression.value,
    }
    # Same underlying event, two different scopes — the release regression is
    # keyed on the event id, the value drift on the drift row's id.
    by_scope = {firing["scope_type"]: firing for firing in firings}
    assert by_scope[MetricScopeType.release_regression.value]["scope_ref"] == str(ids["event_id"])
    assert by_scope[MetricScopeType.variable_value_drift.value]["scope_ref"] == str(
        ids["value_drift_id"]
    )

    message = body["rendered_message"]
    assert isinstance(message, str)
    assert "value drift: ${tier}" in message
    assert "release: dropped in 15.7.5 vs 15.7.4" in message


# ---------------------------------------------------------------------------
# Project total: the family the replay could always LOAD and could never NAME.
# ---------------------------------------------------------------------------


async def _seed_project_total_replay(
    client: AsyncClient,
    slug: str,
) -> tuple[uuid.UUID, str, str]:
    """One project-total anomaly, and a rule subscribed to that scope alone.

    Seeded here rather than through ``_seed_replay_project`` / ``_make_rule``
    because this family contradicts what both of those document: it is a VOLUME
    scope, so unlike the four drift-shaped ones it runs through the NUMERIC
    branch of ``rule_matches_anomaly``. The thresholds are still left at their
    saved defaults — the seeded spike clears them on its own numbers, 4800
    against 1200 being 300% where ``DEFAULT_MIN_PERCENT_DELTA`` asks 30 — so a
    rule that stopped admitting this row would still be visible here.

    ``scope_ref`` is the SCAN CONFIG's id, which is what ``detect`` writes for
    this scope and the whole reason the label cannot be read off the ref.
    """
    resp = await client.post("/api/v1/projects", json={"name": f"Replay {slug}", "slug": slug})
    assert resp.status_code == 201, resp.text
    project_id = uuid.UUID(resp.json()["id"])

    async with TestSessionLocal() as session, session.begin():
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"ds-{uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="h",
            port=8123,
            database_name="d",
            username="u",
            password_encrypted="",
        )
        session.add(data_source)
        await session.flush()
        scan = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project_id,
            name="sc",
            base_query="SELECT 1",
            cardinality_threshold=100,
            interval="1h",
        )
        session.add(scan)
        await session.flush()
        session.add(
            MetricAnomaly(
                id=uuid.uuid4(),
                scan_config_id=scan.id,
                scope_type=MetricScopeType.project_total.value,
                scope_ref=str(scan.id),
                event_id=None,
                event_type_id=None,
                bucket=datetime.now(UTC) - timedelta(days=1),
                actual_count=4800.0,
                expected_count=1200.0,
                stddev=120.0,
                z_score=6.0,
                direction="spike",
            )
        )
        scan_id = scan.id

    destination_resp = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations",
        json={
            "type": "slack",
            "name": "Replay Slack",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T1/B1/replay",
        },
    )
    assert destination_resp.status_code == 201, destination_resp.text
    destination_id = str(destination_resp.json()["id"])

    rule_resp = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations/{destination_id}/rules",
        json={
            "name": "Replay Rule",
            "enabled": True,
            "include_project_total": True,
            "include_event_types": False,
            "include_events": False,
            "include_schema_drifts": False,
            "include_distribution_drifts": False,
            "include_metrics": False,
            "include_variable_value_drifts": False,
            "include_release_regressions": False,
            "notify_on_spike": True,
            "notify_on_drop": True,
        },
    )
    assert rule_resp.status_code == 201, rule_resp.text
    return scan_id, destination_id, str(rule_resp.json()["id"])


@pytest.mark.asyncio
async def test_the_replay_names_a_project_total_firing_the_way_the_send_does(
    client: AsyncClient,
) -> None:
    """The scope most rules carry, and the one the name map never resolved.

    ``_build_scope_name_map`` resolved event, event type, schema drift,
    distribution drift, value drift, release regression and metric — every
    family except this one. Its ``scope_ref`` is the scan config's uuid, so with
    no branch for it the caller's ``scope_names.get(..., anomaly.scope_ref)``
    fallback labelled the row with that uuid while the delivery built from the
    same firing says "All events": exactly the preview/send disagreement the
    ``trim_scope_name`` comment beside that fallback calls "the one thing this
    module exists to prevent". ``include_project_total`` defaults on, so this was
    the disagreement operators hit most often, and it also made the replay table
    unreadable — the Scope column asked them to decode a uuid to know which scan
    a row was about.

    Delete the project-total seed from ``_build_scope_name_map`` and the two
    assertions redden together: ``scope_name`` reads back the scan uuid, and that
    uuid appears in ``rendered_message`` where "All events" belongs.
    """
    slug = "b4-158-project-total"
    scan_id, destination_id, rule_id = await _seed_project_total_replay(client, slug)

    body = await _replay(client, slug, destination_id, rule_id)

    assert body["anomalies_considered"] == 1
    assert body["matched_before_cooldown"] == 1
    firings = body["firings"]
    assert isinstance(firings, list)
    assert len(firings) == 1
    firing = firings[0]

    assert firing["scope_type"] == MetricScopeType.project_total.value
    # The ref IS the scan config's id — which is precisely why it must not be
    # what the operator is shown.
    assert firing["scope_ref"] == str(scan_id)
    assert firing["scope_name"] == "All events"

    message = body["rendered_message"]
    assert isinstance(message, str)
    assert "- Project total All events: up," in message
    assert str(scan_id) not in message


@pytest.mark.asyncio
async def test_both_scope_name_builders_spell_the_project_total_label_alike() -> None:
    """The literal is written twice, so it is pinned in one place.

    ``alert_payload._build_alert_scope_names`` (send) and
    ``alerting_service._build_scope_name_map`` (preview) each carry their own
    ``"All events"``: the live one lives in the worker package and the two share
    no leaf to import a constant from, the same situation
    ``alerting_rendering._ADOPTION_ADJUSTED_LABEL`` is in and is pinned the same
    way. One candidate through both builders, whole maps compared — retype
    either spelling and this reddens instead of production.

    Neither builder queries anything for a candidate with no event, no event type
    and a non-metric scope, which is what lets both run without a session.
    ``_NoSession`` turns a query added later into a stated failure rather than an
    ``AttributeError`` on ``None``.
    """

    class _NoSession:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(
                f"a project-total candidate must not need session.{name}: it is named "
                "by a constant, not resolved from a row"
            )

    scope_ref = str(uuid.uuid4())
    anomaly = MetricAnomaly(
        id=uuid.uuid4(),
        scan_config_id=uuid.uuid4(),
        scope_type=MetricScopeType.project_total.value,
        scope_ref=scope_ref,
        event_id=None,
        event_type_id=None,
        bucket=datetime(2026, 9, 12, 19, tzinfo=UTC),
        actual_count=4800.0,
        expected_count=1200.0,
        stddev=120.0,
        z_score=6.0,
        direction="spike",
    )
    session: Any = _NoSession()

    previewed = await _build_scope_name_map(session, [anomaly])
    delivered = _build_alert_scope_names(session, [anomaly])

    assert previewed == delivered
    assert previewed == {(MetricScopeType.project_total.value, scope_ref): "All events"}


# ---------------------------------------------------------------------------
# What the newly reachable families SAY. Pure renderers — no DB, no network.
# ---------------------------------------------------------------------------


def _release_regression_pair(
    *,
    expected_count: float = _EXPECTED_COUNT,
) -> tuple[AlertDeliveryItem, SimulatedRuleFiring]:
    """One release regression described twice, from ONE set of facts.

    The same construction ``test_alert_message_content._pair`` uses, with one
    difference that is the whole point of this file: ``window_from`` is set on
    BOTH sides. It was the one field a simulated firing could not carry while
    the replay never loaded a release regression; now it can, so leaving it off
    the firing here would make the comparison agree by making both sides
    equally silent.

    Setting it by hand is also the LIMIT of what the three renderer tests below
    can prove. None of them calls ``simulate_rule`` at all, so the only hop they
    exercise is ``alerting_rendering._drift_facts``: revert
    ``SimulatedRuleFiring(window_from=...)`` in ``simulate_rule`` and every one
    of them still prints the clause. The API test above is what covers that hop
    and the loader's.
    """
    bucket = datetime(2026, 9, 12, 19, tzinfo=UTC)
    absolute_delta = abs(_OBSERVED_COUNT - expected_count)
    percent_delta = absolute_delta / abs(expected_count) * 100 if expected_count else 0.0
    shared = {
        "scope_type": MetricScopeType.release_regression.value,
        "scope_ref": "scope-ref",
        "scope_name": _EVENT_NAME,
        "event_id": uuid.UUID("c36b3ba0-0000-4000-8000-000000000158"),
        "event_type_id": None,
        "drift_field": _VERSION,
        "drift_type": ReleaseRegressionKind.volume_drop.value,
        "sample_value": _PREVIOUS_VERSION,
        "bucket": bucket,
        "window_from": bucket - _ROLLOUT_OVERLAP,
        "direction": "drop",
        "actual_count": float(_OBSERVED_COUNT),
        "expected_count": expected_count,
        "absolute_delta": absolute_delta,
        "percent_delta": percent_delta,
    }
    item = AlertDeliveryItem(id=uuid.uuid4(), delivery_id=uuid.uuid4(), **shared)
    firing = SimulatedRuleFiring(anomaly_id=uuid.uuid4(), **shared)
    return item, firing


@pytest.mark.parametrize(
    "message_format",
    [ALERT_MESSAGE_FORMAT_PLAIN, ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2],
)
def test_the_preview_words_a_release_regression_the_way_the_send_does(
    message_format: str,
) -> None:
    """The whole default item, byte for byte, for the family the replay newly made reachable.

    ``test_alert_message_content`` asserts this equality for schema and distribution
    drift and deliberately excludes release regressions, because at the time
    they were "not reachable from a replay yet" and the preview's
    ``${expected_basis}`` was "deliberately empty". Both clauses expired with
    this change: the replay reaches them, so the preview is a promise about a
    message an operator will actually receive.

    Two reverts, two different ways of failing:

    * restore ``"expected_basis": ""`` and the delivered side carries
      "(adoption-adjusted)" while the preview does not;
    * remove ``window_from=firing.window_from`` from
      ``alerting_rendering._drift_facts`` and the delivered side carries "over
      the 51h rollout overlap" while the preview does not.

    MarkdownV2 rides along because both strings are escaped as a WHOLE, once, on
    each side — if either side ever escapes its halves separately the escaping
    diverges before the wording does.
    """
    item, firing = _release_regression_pair()
    template = get_default_items_template(message_format)

    previewed = render_firing_item(firing, message_format=message_format, items_template=template)
    delivered = _build_items_text([item], message_format=message_format, items_template=template)

    assert previewed == delivered


def test_the_preview_adopted_the_send_s_release_wording_and_not_an_empty_one() -> None:
    """Which side moved, stated as text rather than as an equality.

    The equality above goes green if BOTH sides lose the qualifier and the
    window — the exact failure mode of "fixing" a divergence by deleting the
    harder half. The send's wording is what ships and what
    ``website/docs/use/alerting.md`` quotes, so this pins that the preview grew
    the two clauses and the delivered message kept them.
    """
    item, firing = _release_regression_pair()
    template = get_default_items_template(ALERT_MESSAGE_FORMAT_PLAIN)

    previewed = render_firing_item(
        firing, message_format=ALERT_MESSAGE_FORMAT_PLAIN, items_template=template
    )
    delivered = _build_items_text(
        [item], message_format=ALERT_MESSAGE_FORMAT_PLAIN, items_template=template
    )

    for rendered in (previewed, delivered):
        assert "expected=715.7 (adoption-adjusted)" in rendered
        assert "release: dropped in 15.7.5 vs 15.7.4 over the 51h rollout overlap" in rendered
        assert "715.7 is 15.7.4's share of this event at 15.7.5's own volume" in rendered


def test_a_release_regression_with_no_baseline_is_not_called_adoption_adjusted() -> None:
    """The qualifier explains a ratio, so it must not appear where there is none.

    ``has_baseline`` — not ``expected_count > 0`` and not a local truth test — is
    what both renderers ask, which is why a SIGNED expectation is qualified
    rather than denied. At exactly zero there is no expectation
    to describe, ``${percent_delta_label}`` already says "no baseline", and the
    parenthetical would qualify a number that is not one.

    Make the preview's condition unconditional — drop the ``has_baseline`` call
    and print the label for every release regression — and this reddens while
    the equality test above still passes, because the send would be wrong in the
    same way.
    """
    item, firing = _release_regression_pair(expected_count=0.0)
    template = get_default_items_template(ALERT_MESSAGE_FORMAT_PLAIN)

    previewed = render_firing_item(
        firing, message_format=ALERT_MESSAGE_FORMAT_PLAIN, items_template=template
    )
    delivered = _build_items_text(
        [item], message_format=ALERT_MESSAGE_FORMAT_PLAIN, items_template=template
    )

    assert previewed == delivered
    assert "(adoption-adjusted)" not in previewed
    assert NO_BASELINE_LABEL in previewed
    # The release line still names the build and the window; only the clause
    # that would have quoted a zero as an expectation is gone.
    assert "release: dropped in 15.7.5 vs 15.7.4 over the 51h rollout overlap" in previewed
    assert "share-for-share" not in previewed


# ---------------------------------------------------------------------------
# The limiter the replay does NOT model: a destination on a delivery cadence.
# ---------------------------------------------------------------------------


async def _seed_cadence_replay(
    client: AsyncClient,
    slug: str,
    *,
    cron: str | None,
) -> tuple[str, str]:
    """One event scope anomalous TWICE inside one cooldown, behind cadence ``cron``.

    Two buckets ten minutes apart, one scope, one scan: under the rule's saved
    1440-minute cooldown the replay can report only the first, so the gap
    between ``matched_before_cooldown`` and ``firings`` is the limiter's own
    fingerprint rather than anything about matching. The numbers clear the saved
    thresholds unaided — 4800 against 1200 is 300% where
    ``DEFAULT_MIN_PERCENT_DELTA`` asks 30 — so nothing here depends on the rule
    being loosened for the test.

    ``cron`` is the only thing that differs between the two projects seeded
    below. It is the column ``dispatch._prepare_alert_deliveries`` reads to
    decide whether the cooldown applies at all; the replay reads nothing.
    """
    resp = await client.post("/api/v1/projects", json={"name": f"Replay {slug}", "slug": slug})
    assert resp.status_code == 201, resp.text
    project_id = uuid.UUID(resp.json()["id"])

    first_bucket = datetime.now(UTC) - timedelta(days=2)
    async with TestSessionLocal() as session, session.begin():
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"ds-{uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="h",
            port=8123,
            database_name="d",
            username="u",
            password_encrypted="",
        )
        session.add(data_source)
        await session.flush()
        scan = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project_id,
            name="sc",
            base_query="SELECT 1",
            cardinality_threshold=100,
            interval="1h",
        )
        event_type = EventType(
            id=uuid.uuid4(),
            project_id=project_id,
            name="checkout",
            display_name="Checkout",
            description="",
        )
        session.add_all([scan, event_type])
        await session.flush()
        event = Event(
            id=uuid.uuid4(),
            project_id=project_id,
            event_type_id=event_type.id,
            name=_EVENT_NAME,
            description="",
        )
        session.add(event)
        await session.flush()
        for offset in (timedelta(0), timedelta(minutes=10)):
            session.add(
                MetricAnomaly(
                    id=uuid.uuid4(),
                    scan_config_id=scan.id,
                    scope_type=MetricScopeType.event.value,
                    scope_ref=str(event.id),
                    event_id=event.id,
                    event_type_id=event_type.id,
                    bucket=first_bucket + offset,
                    actual_count=4800.0,
                    expected_count=1200.0,
                    stddev=120.0,
                    z_score=6.0,
                    direction="spike",
                )
            )

    destination_resp = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations",
        json={
            "type": "slack",
            "name": "Digest Slack",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T1/B1/replay",
            "delivery_schedule_cron": cron,
        },
    )
    assert destination_resp.status_code == 201, destination_resp.text
    destination = destination_resp.json()
    # The cadence actually landed on the row. Without this the cadence arm of
    # the test below could pass by being an immediate destination in disguise.
    assert destination["delivery_schedule_cron"] == cron
    destination_id = str(destination["id"])

    rule_resp = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations/{destination_id}/rules",
        json={
            "name": "Replay Rule",
            "enabled": True,
            "include_project_total": False,
            "include_event_types": False,
            "include_events": True,
            "include_schema_drifts": False,
            "include_distribution_drifts": False,
            "include_metrics": False,
            "include_variable_value_drifts": False,
            "include_release_regressions": False,
            "notify_on_spike": True,
            "notify_on_drop": True,
        },
    )
    assert rule_resp.status_code == 201, rule_resp.text
    return destination_id, str(rule_resp.json()["id"])


@pytest.mark.asyncio
async def test_a_cadence_destination_is_replayed_under_a_limiter_live_switches_off(
    client: AsyncClient,
) -> None:
    """The replay gates on a cooldown that production does not apply here.

    Two identical projects, one difference: ``delivery_schedule_cron``. The
    replay answers the same "1 firing out of 2 matches" for both, because
    ``simulate_rule_firings`` is handed a rule and never a destination, while
    ``dispatch._prepare_alert_deliveries`` reads exactly that column to compute
    ``cooldown_applies`` and then stops consulting ``cooldown_minutes``
    altogether. The cadence project's "1" is an immediate-delivery answer to a
    question nobody asked about it, which is the whole of finding replay-5.

    This test does NOT redden on the prose repair it belongs to — it is the
    premise that prose states, pinned so the paragraphs on
    ``simulate_rule_firings`` and ``alerting_service.simulate_rule`` are
    checkable rather than merely plausible. It DOES redden on the fix those
    paragraphs argue against: skip the cooldown gate when a cadence is set, the
    naive mirror of ``cooldown_applies``, and the cadence arm reports 2 — a
    number further from what the digest delivers than the 1 it replaces, because
    the buffer collapses a re-firing scope to one line per direction per window.
    """
    from pathlib import Path

    from tripl.worker.tasks.metrics import dispatch as metrics_dispatch

    immediate = await _seed_cadence_replay(client, "b4-replay5-immediate", cron=None)
    on_cadence = await _seed_cadence_replay(client, "b4-replay5-cadence", cron="0 9 * * *")

    immediate_body = await _replay(client, "b4-replay5-immediate", *immediate)
    cadence_body = await _replay(client, "b4-replay5-cadence", *on_cadence)

    for label, body in (("immediate", immediate_body), ("cadence", cadence_body)):
        assert body["anomalies_considered"] == 2, label
        # The shared matcher admits both buckets — predicates do not diverge,
        # and this is the number on the response that no limiter touches.
        assert body["matched_before_cooldown"] == 2, label
        # ...and the limiter is the only thing standing between it and this one.
        assert len(body["firings"]) == 1, label
        assert body["cooldown_minutes_used"] == 1440, label

    # The live half of the same sentence, read where it is decided. A source
    # check rather than a dispatch run because this file holds no sync worker
    # session; ``test_alert_cadence_switch`` exercises the buffering path itself.
    dispatch_source = Path(metrics_dispatch.__file__).read_text(encoding="utf-8")
    assert "cooldown_applies = destination.delivery_schedule_cron is None" in dispatch_source, (
        "the live switch this replay cannot see has moved; re-read the cadence "
        "paragraphs on simulate_rule_firings before assuming they are still true"
    )
    assert dispatch_source.count("not cooldown_applies") == 2, (
        "both send gates are supposed to short-circuit on the cadence, which is "
        "what those paragraphs claim about production"
    )


def test_the_parity_promise_names_the_destination_class_it_excludes() -> None:
    """Three docstrings promise the replay matches production; each must say where not.

    ``alerting_matching``'s module docstring used to end "guarantees the
    simulator never diverges from production behavior", full stop, and
    ``simulate_rule_firings`` called AlertRuleState "the clock the live pipeline
    actually gates on" — neither is true for a destination on a cadence, whose
    cooldown dispatch switches off and whose held scopes are never stamped with
    a ``last_notified_at`` for a clock to read. A sentence that is not true is
    the defect here, and these three are the sentences a reader consults before
    trusting a replay number, or before "fixing" the replay by mirroring a gate
    whose mirror is worse.

    Revert any one span and this reddens on that span alone: the blanket
    sentence returns to the module docstring, or ``delivery_schedule_cron``
    disappears from the function that applies the cooldown, or from the service
    docstring that promises "what it would have sent".
    """
    import tripl.alerting_matching as matching_module
    from tripl.alerting_matching import simulate_rule_firings
    from tripl.services.alerting_service import simulate_rule

    module_doc = matching_module.__doc__ or ""
    firings_doc = simulate_rule_firings.__doc__ or ""
    service_doc = simulate_rule.__doc__ or ""

    spans = {
        "alerting_matching module docstring": module_doc,
        "simulate_rule_firings docstring": firings_doc,
        "alerting_service.simulate_rule docstring": service_doc,
    }
    for label, span in spans.items():
        assert span.strip(), f"{label} no longer resolves; this guard guards nothing"
        assert "delivery_schedule_cron" in span, (
            f"{label} promises the replay matches production without naming the "
            "one destination class where it does not"
        )

    # The false sentences themselves, gone rather than merely surrounded.
    assert "never diverges from production behavior" not in module_doc
    assert "actually gates on" not in firings_doc
    assert "the one place this module" not in firings_doc
    # And the honest number stays pointed at: the divergence cannot move it, and
    # the replay dialog already renders it beside the cooldown it did apply.
    assert "matched_before_cooldown" in service_doc
