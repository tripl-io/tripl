"""Batch 4: what an alert SAYS — one item, one weekly digest — and who gets it.

``tripl-0zpq.165`` — the rule simulator and the live send each built
``${scope_label}`` and ``${drift_line}`` from their own copy of the rules, and
the copies had drifted. One schema drift previewed as
``drift: type_changed: amount — e.g. 9.99`` and was then delivered as
``drift: type_changed amount sample=9.99``: an operator who tested a rule in the
simulator and then armed it got a different sentence about the same firing,
which is worse than having no simulator. Both strings now come from one builder
in ``tripl.alert_templates`` that both renderers call.

The wording kept is the SEND's, because that is the text an operator actually
receives and the text ``website/docs/use/alerting.md`` quotes. Only previews
moved; no delivered message and no stored payload snapshot changed.

Those two renderers are pure — no DB, no network, no Celery — so they are called
directly and compared string for string, because "the preview says what the send
says" is an equality and nothing weaker holds it.

``tripl-0zpq.34`` — the weekly plan digest counted its metric anomalies through
an inner join on ScanConfig, and a catalog metric has no scan config to join to:
``metric``-scope rows carry a NULL ``scan_config_id`` by design. A project whose
week produced only catalog-metric anomalies was told ``Metric anomalies: 0`` and
shown no Top anomalies section at all, while its Anomalies page listed every one
of them. That half needs rows, so it runs the real builder against a sqlite
session the way ``test_batch3_a2.py`` and ``test_alert_digest_delivery.py`` do.

``tripl-0zpq.33`` — and who that digest is sent to. Both tasks in
``worker/tasks/alerts_digest`` select every enabled Slack/email destination in
the database and send to it themselves, without minting an ``AlertDelivery`` and
without going through a send task, so the demo-project egress guard both send
tasks call was the one thing they never consulted: a demo
project holding an enabled external destination would have been posted to for
real, against a workspace the docs promise is zero-egress by construction. They
now exclude demo projects in the SELECT and call the guard at the one helper
both funnel through, and the tests below drive the real tasks over the same
sqlite fixture with the channel transport recorded instead of performed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

# Imported first, and for its side effect. The worker task package is
# import-order sensitive: celery_app's bottom-of-file task registration is what
# pulls the task modules in an order they all survive. Entering at ``alerts`` or
# at ``alerts_digest`` instead raises ImportError, because that cascade reaches
# back into the half-initialized module it started from — ``metrics`` wants
# ``alerts.send_alert_delivery``, and ``alerts`` wants the two task objects
# ``alerts_digest`` has not defined yet. Under pytest the alphabetically earlier
# test_alert_*.py files happen to enter safely first; this file does not rely on
# that, so it can also be run on its own.
import tripl.worker.celery_app  # noqa: F401
from tripl.alert_templates import (
    ALERT_MESSAGE_FORMAT_PLAIN,
    ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2,
    DriftLineFacts,
    build_drift_line,
    get_default_items_template,
)
from tripl.models import Base
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination, AlertDestinationType
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import MetricKind, MetricScopeType, MetricStatus
from tripl.models.event import Event, EventStatus
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.schemas.alerting import SimulatedRuleFiring
from tripl.services import app_settings_service
from tripl.services.alerting_rendering import render_firing_item

# Both digest tasks are taken from ``alerts``, which re-exports them in its
# ``__all__``, the way the existing sunset coverage in test_alerting.py does.
from tripl.worker.tasks.alerts import check_deprecated_sunset_events, send_weekly_plan_digest
from tripl.worker.tasks.alerts_digest import _send_digest_to_destination
from tripl.worker.tasks.alerts_messages import _build_items_text, _build_plan_digest_message

_BUCKET = datetime(2026, 8, 4, 19, tzinfo=UTC)
_EVENT_ID = uuid.UUID("c36b3ba0-0000-4000-8000-00000000165a")
# Monkeypatch targets are spelled as strings for the same reason: resolving the
# digest module at patch time, by which point it is fully imported, rather than
# naming it in the import block above.
_DIGEST = "tripl.worker.tasks.alerts_digest"


def _pair(
    *,
    scope_type: str,
    scope_name: str,
    drift_field: str | None = None,
    drift_type: str | None = None,
    sample_value: str | None = None,
    actual_count: float = 1,
    expected_count: float = 0,
    absolute_delta: float = 1,
    percent_delta: float = 0.0,
    event_id: uuid.UUID | None = None,
    window_from: datetime | None = None,
) -> tuple[AlertDeliveryItem, SimulatedRuleFiring]:
    """One firing described twice: as a delivered row, and as a replayed one.

    Every field the two models share is set from the same argument — including
    ``window_from``, which both of them carry — so any difference in the
    rendered text is the two renderers disagreeing and cannot be the fixture
    feeding them different facts.
    """
    shared = {
        "scope_type": scope_type,
        "scope_ref": "scope-ref",
        "scope_name": scope_name,
        "event_id": event_id,
        "event_type_id": None,
        "drift_field": drift_field,
        "drift_type": drift_type,
        "sample_value": sample_value,
        "bucket": _BUCKET,
        # The window's START, where there is one; ``bucket`` is its end. Only a
        # release regression has one, so it defaults to None and every other
        # scope below leaves it there. It is shared rather than set on the item
        # alone because ``SimulatedRuleFiring`` grew the field in
        # the replay loads ``ReleaseRegression`` rows, which
        # record both ends of the rollout overlap. Giving it to one side only
        # would fake a divergence the renderers no longer have.
        "window_from": window_from,
        "direction": "spike",
        "actual_count": actual_count,
        "expected_count": expected_count,
        "absolute_delta": absolute_delta,
        "percent_delta": percent_delta,
    }
    item = AlertDeliveryItem(id=uuid.uuid4(), delivery_id=uuid.uuid4(), **shared)
    firing = SimulatedRuleFiring(anomaly_id=uuid.uuid4(), **shared)
    return item, firing


# The scopes that put anything in ${drift_line}, with the fields each one packs
# into the three shared drift columns.
_DRIFT_SCOPES: dict[str, dict[str, object]] = {
    "schema drift": {
        "scope_type": MetricScopeType.schema.value,
        "scope_name": "checkout.amount",
        "drift_field": "amount",
        "drift_type": "type_changed",
        "sample_value": "9.99",
    },
    "distribution drift": {
        "scope_type": MetricScopeType.distribution.value,
        "scope_name": "checkout.platform",
        "drift_field": "platform",
        "drift_type": "distribution_shift",
        "sample_value": "psi=0.412; ios 61.0%->38.0%",
    },
    # All four are reachable from a replay: tripl-0zpq.158, in this same batch,
    # put release regressions and value drifts into the simulator's candidate
    # set, so the last two below describe firings an operator can preview today
    # rather than a family this file pinned ahead of time. The shared builder is
    # what keeps them from previewing differently the moment they became
    # reachable — without it a replayed release regression would have printed
    # "drift: volume_drop: 15.7.5 — e.g. 15.7.4" against the send's
    # "release: dropped in 15.7.5 vs 15.7.4 ...".
    "release regression": {
        "scope_type": MetricScopeType.release_regression.value,
        "scope_name": "spot:open:wind",
        "drift_field": "15.7.5",
        "drift_type": "volume_drop",
        "sample_value": "15.7.4",
        "actual_count": 345,
        "expected_count": 715.7,
        "absolute_delta": 370.7,
        "percent_delta": 51.8,
        "event_id": _EVENT_ID,
    },
    "variable value drift": {
        "scope_type": MetricScopeType.variable_value_drift.value,
        "scope_name": "checkout:completed.tier",
        "drift_field": "tier",
        "drift_type": "value_drift",
        "sample_value": "platinum",
        "event_id": _EVENT_ID,
    },
}


def _previewed(firing: SimulatedRuleFiring, *, message_format: str, items_template: str) -> str:
    return render_firing_item(firing, message_format=message_format, items_template=items_template)


def _delivered(item: AlertDeliveryItem, *, message_format: str, items_template: str) -> str:
    return _build_items_text([item], message_format=message_format, items_template=items_template)


@pytest.mark.parametrize(
    "message_format",
    [ALERT_MESSAGE_FORMAT_PLAIN, ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2],
)
@pytest.mark.parametrize("scope", list(_DRIFT_SCOPES))
def test_the_preview_words_a_drift_the_way_the_send_does(scope: str, message_format: str) -> None:
    """The whole default item, byte for byte, for every scope that has a drift line.

    All four replay, so every one of these is the assertion an operator's
    complaint would have produced. Two reverts redden it:

    * put back the preview's old ``${drift_line}`` wording and schema drift
      fails — the complaint this file is named for;
    * hard-code ``"expected_basis": ""`` in ``render_firing_item`` again and the
      release-regression case fails, because the delivered side says
      "expected=715.7 (adoption-adjusted)" and the preview says "expected=715.7".

    The release regression runs here with no window on either side; its windowed
    twin, with ``window_from`` set on both models, is
    ``test_batch4_replay.test_the_preview_words_a_release_regression_the_way_the_send_does``.

    MarkdownV2 rides along because the drift line is escaped as a WHOLE, once,
    on each side: if either side ever escapes its halves separately the escaping
    diverges before the wording does.
    """
    item, firing = _pair(**_DRIFT_SCOPES[scope])
    template = get_default_items_template(message_format)

    assert _previewed(firing, message_format=message_format, items_template=template) == _delivered(
        item, message_format=message_format, items_template=template
    )


def test_the_preview_adopted_the_delivered_wording_and_not_its_own() -> None:
    """Which of the two wordings won, stated as text rather than as an equality.

    The equality above goes green if BOTH sides regress to the preview's old
    phrasing. This pins the direction: the send's wording is what ships, so the
    preview had to move and the delivered message had to stay exactly as it was.
    """
    item, firing = _pair(**_DRIFT_SCOPES["schema drift"])
    template = get_default_items_template(ALERT_MESSAGE_FORMAT_PLAIN)

    previewed = _previewed(
        firing, message_format=ALERT_MESSAGE_FORMAT_PLAIN, items_template=template
    )

    assert "\n  drift: type_changed amount sample=9.99" in previewed
    # The preview's own former phrasing, which no reader ever received.
    assert "type_changed: amount" not in previewed
    assert "e.g. 9.99" not in previewed
    assert "\n  drift: type_changed amount sample=9.99" in _delivered(
        item, message_format=ALERT_MESSAGE_FORMAT_PLAIN, items_template=template
    )


@pytest.mark.parametrize("scope", list(_DRIFT_SCOPES))
def test_one_builder_writes_the_drift_line_for_every_scope_that_has_one(scope: str) -> None:
    """``${drift_line}`` alone, isolated from the rest of the item.

    The equality above renders the whole item, where the drift line is one
    clause among many that agree for reasons having nothing to do with
    ``build_drift_line`` — a builder that returned "" on both sides would pass
    it. Rendering the one variable is what makes the "says something" assertion
    at the end of this test reach the builder itself, for every scope that
    should produce a line.
    """
    item, firing = _pair(**_DRIFT_SCOPES[scope])

    previewed = _previewed(
        firing, message_format=ALERT_MESSAGE_FORMAT_PLAIN, items_template="${drift_line}"
    )

    assert previewed == _delivered(
        item, message_format=ALERT_MESSAGE_FORMAT_PLAIN, items_template="${drift_line}"
    )
    # Every one of them says something, so an equality of two empty strings
    # cannot be what passed.
    assert previewed.startswith("\n  ")


@pytest.mark.parametrize("scope_type", [member.value for member in MetricScopeType])
def test_both_renderers_give_a_scope_the_same_label(scope_type: str) -> None:
    """``${scope_label}`` was the second copy, and it had already split.

    The send's map knew ``release_regression`` and the preview's did not, so a
    replayed release regression would have been labelled with the raw scope
    string while the delivered one read "Release regression". Asserted over
    every member of the enum so a scope added later cannot be taught to one map
    and not the other.
    """
    item, firing = _pair(scope_type=scope_type, scope_name="a-scope")

    assert _previewed(
        firing, message_format=ALERT_MESSAGE_FORMAT_PLAIN, items_template="${scope_label}"
    ) == _delivered(
        item, message_format=ALERT_MESSAGE_FORMAT_PLAIN, items_template="${scope_label}"
    )


def test_a_firing_with_no_drift_context_contributes_no_line() -> None:
    """The ordinary count anomaly: no drift columns, so no line and no blank one.

    ``${drift_line}`` sits mid-template with nothing around it, so returning
    anything other than "" here puts a stray newline into every count alert.
    """
    item, firing = _pair(
        scope_type=MetricScopeType.event.value,
        scope_name="checkout:completed",
        actual_count=137,
        absolute_delta=137,
    )

    assert (
        _previewed(
            firing, message_format=ALERT_MESSAGE_FORMAT_PLAIN, items_template="${drift_line}"
        )
        == ""
    )
    assert (
        _delivered(item, message_format=ALERT_MESSAGE_FORMAT_PLAIN, items_template="${drift_line}")
        == ""
    )
    assert build_drift_line(DriftLineFacts(scope_type=MetricScopeType.event.value)) == ""


def test_the_release_line_names_the_rollout_window_on_both_sides() -> None:
    """The rollout-overlap clause survived the move into ``alert_templates``.

    ``window_from`` was the one fact a delivered item had and a replayed firing
    did not, so this test used to render the send's half alone. tripl-0zpq.158
    ended that inside this same batch: the replay loads ``ReleaseRegression``
    rows, which record both ends of the overlap, and the firing carries the
    start through ``alerting_rendering._drift_facts``. The clause is an equality
    now. The cases above still leave the window unset on both sides — nothing
    there would notice it going missing — which is why this one stays separate.

    Remove ``window_from=firing.window_from`` from ``_drift_facts`` and that
    equality reddens on its own: the delivered side keeps "over the 51h rollout
    overlap" and the preview drops it, because
    ``alert_templates._format_window_span`` is written to return None rather
    than to fail. The substring assertions cover the other direction, where the
    span helper moves and both sides lose the window together.
    """
    item, firing = _pair(
        **_DRIFT_SCOPES["release regression"],
        window_from=_BUCKET - timedelta(hours=51),
    )

    rendered = _delivered(
        item, message_format=ALERT_MESSAGE_FORMAT_PLAIN, items_template="${drift_line}"
    )
    previewed = _previewed(
        firing, message_format=ALERT_MESSAGE_FORMAT_PLAIN, items_template="${drift_line}"
    )

    assert previewed == rendered
    assert rendered.startswith("\n  release: dropped in 15.7.5 vs 15.7.4 over the 51h rollout")
    # The clause that does the real work is still attached behind it.
    assert "715.7 is 15.7.4's share of this event at 15.7.5's own volume" in rendered
    assert "so 51.8% is share-for-share" in rendered


# ---------------------------------------------------------------------------
# what the WEEKLY PLAN DIGEST counts, and what it names.
# ---------------------------------------------------------------------------

# A fixed "now" so the seven-day window and the rows placed either side of it are
# stated as dates rather than as offsets from whenever the suite happens to run.
_NOW = datetime(2026, 9, 14, 9, tzinfo=UTC)


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'batch4_messages.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def _seed_digest_project(session: Session, *, name: str) -> tuple[Project, ScanConfig]:
    """A project with one scan config — the two anchors the digest reads from."""
    project = Project(
        id=uuid.uuid4(),
        name=name,
        slug=f"{name.lower()}-{uuid.uuid4().hex[:8]}",
        description="",
    )
    data_source = DataSource(
        id=uuid.uuid4(),
        name=f"DS {uuid.uuid4().hex[:8]}",
        db_type="clickhouse",
        host="localhost",
        port=8123,
        database_name="default",
        username="default",
        password_encrypted="",
    )
    scan = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=data_source.id,
        project_id=project.id,
        name=f"{name} scan",
        base_query="SELECT time, event_name FROM events",
        time_column="time",
        cardinality_threshold=100,
        interval="1h",
    )
    session.add_all([project, data_source, scan])
    session.commit()
    return project, scan


def _add_catalog_metric(
    session: Session, project: Project, *, display_name: str
) -> MetricDefinition:
    """One catalog metric. Its ``display_name`` is what the digest must print.

    ``name`` is deliberately different from ``display_name`` so a line built
    from the wrong column is visible rather than coincidentally right.
    """
    metric = MetricDefinition(
        id=uuid.uuid4(),
        project_id=project.id,
        name=display_name.lower().replace(" ", "_"),
        display_name=display_name,
        kind=MetricKind.sql.value,
        config={},
        interval="1d",
        status=MetricStatus.active.value,
    )
    session.add(metric)
    session.commit()
    return metric


def _add_scan_anomaly(
    session: Session,
    scan: ScanConfig,
    *,
    scope_ref: str,
    z_score: float,
    created_at: datetime,
    bucket: datetime = _NOW - timedelta(days=1),
) -> None:
    """An ordinary event-scope anomaly: anchored on a scan config, as always."""
    session.add(
        MetricAnomaly(
            id=uuid.uuid4(),
            scan_config_id=scan.id,
            scope_type=MetricScopeType.event.value,
            scope_ref=scope_ref,
            event_id=None,
            event_type_id=None,
            bucket=bucket,
            direction="spike",
            actual_count=200.0,
            expected_count=20.0,
            stddev=1.0,
            z_score=z_score,
            created_at=created_at,
        )
    )
    session.commit()


def _add_catalog_metric_anomaly(
    session: Session,
    metric: MetricDefinition,
    *,
    z_score: float,
    created_at: datetime,
    bucket: datetime = _NOW - timedelta(days=1),
) -> None:
    """A catalog-metric anomaly, shaped exactly as detection writes one.

    NULL ``scan_config_id``, ``scope_type='metric'``, ``scope_ref`` the metric
    definition id — see ``models/metric_anomaly.py`` and
    ``worker/tasks/metrics/detect.py``. The NULL is the whole defect: there is
    no ScanConfig row for an inner join to find.
    """
    session.add(
        MetricAnomaly(
            id=uuid.uuid4(),
            scan_config_id=None,
            scope_type=MetricScopeType.metric.value,
            scope_ref=str(metric.id),
            event_id=None,
            event_type_id=None,
            bucket=bucket,
            direction="drop",
            actual_count=1.0,
            expected_count=10.0,
            stddev=1.0,
            z_score=z_score,
            created_at=created_at,
        )
    )
    session.commit()


def _top_anomaly_lines(message: str) -> list[str]:
    lines = message.splitlines()
    if "Top anomalies:" not in lines:
        return []
    return lines[lines.index("Top anomalies:") + 1 :]


def test_the_weekly_digest_counts_catalog_metric_anomalies(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The count line, plus the three rows that must NOT move it.

    The in-window catalog-metric anomaly is the one the ScanConfig inner join
    dropped: this project's week read "Metric anomalies: 1" while its Anomalies
    page listed two. The other three rows pin the scoping the widened predicate
    must not have loosened — another project's catalog metric, this project's
    own metric from before the window opened, and the scan-backed row that was
    always counted. Reverting the join gives 1; dropping the project scoping
    gives 3; dropping the window gives 3.
    """
    with sync_session_factory() as session:
        project, scan = _seed_digest_project(session, name="Checkout")
        metric = _add_catalog_metric(session, project, display_name="Signup conversion")
        other_project, _ = _seed_digest_project(session, name="Neighbour")
        other_metric = _add_catalog_metric(session, other_project, display_name="Their metric")

        _add_catalog_metric_anomaly(
            session, metric, z_score=-9.0, created_at=_NOW - timedelta(days=1)
        )
        _add_catalog_metric_anomaly(
            session,
            metric,
            z_score=-9.0,
            created_at=_NOW - timedelta(days=30),
            bucket=_NOW - timedelta(days=30),
        )
        _add_catalog_metric_anomaly(
            session, other_metric, z_score=-9.0, created_at=_NOW - timedelta(days=1)
        )
        _add_scan_anomaly(
            session,
            scan,
            scope_ref="checkout:completed",
            z_score=5.0,
            created_at=_NOW - timedelta(days=2),
        )

        message = _build_plan_digest_message(session, project=project, now=_NOW)

    assert "- Metric anomalies: 2" in message


def test_a_catalog_metric_takes_its_place_in_the_top_anomalies(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Five scan-backed anomalies and one worse catalog metric, in one bucket.

    The section is capped at five and ordered by |z| inside a bucket, so the
    catalog metric being the worst of the six IS the assertion: before the fix
    it was not a candidate at all and the five weaker scan rows filled the list,
    which is the "its worst catalog metric never makes the top-5" half of the
    defect. The line is checked in full because the outer join hands the
    renderer a NULL scan name, so a query-only fix prints "None" and a raw
    metric uuid here.
    """
    with sync_session_factory() as session:
        project, scan = _seed_digest_project(session, name="Checkout")
        metric = _add_catalog_metric(session, project, display_name="Signup conversion")
        for z_score in (4.0, 5.0, 6.0, 7.0, 8.0):
            _add_scan_anomaly(
                session,
                scan,
                scope_ref=f"checkout:completed-{z_score:.0f}",
                z_score=z_score,
                created_at=_NOW - timedelta(days=1),
            )
        _add_catalog_metric_anomaly(
            session, metric, z_score=-12.0, created_at=_NOW - timedelta(days=1)
        )

        message = _build_plan_digest_message(session, project=project, now=_NOW)

    top_lines = _top_anomaly_lines(message)
    assert len(top_lines) == 5
    assert top_lines[0] == (
        "- catalog metric:Signup conversion drop actual=1.0 expected=10.0 z=-12.0"
    )
    # The weakest scan row is the one it displaced, not a sixth line.
    assert "checkout:completed-4" not in message
    # The two ways a widened query still ships an unreadable line.
    assert "None" not in message
    assert str(metric.id) not in message


def test_a_scan_backed_anomaly_still_reads_exactly_as_it_did(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The unchanged half: a project with no catalog metrics at all.

    The predicate collapses back to the single ``ScanConfig.project_id`` test
    for such a project, and the left join can only match what the inner join
    matched, so both the count and the line must be byte-for-byte what they
    were. Written out in full because widening a join is exactly the kind of
    change that quietly double-counts or re-words the rows it was not about.
    """
    with sync_session_factory() as session:
        project, scan = _seed_digest_project(session, name="Checkout")
        _add_scan_anomaly(
            session,
            scan,
            scope_ref="checkout:completed",
            z_score=5.0,
            created_at=_NOW - timedelta(days=2),
        )

        message = _build_plan_digest_message(session, project=project, now=_NOW)

    assert "- Metric anomalies: 1" in message
    assert _top_anomaly_lines(message) == [
        "- Checkout scan event:checkout:completed spike actual=200.0 expected=20.0 z=5.0"
    ]


# ---------------------------------------------------------------------------
# WHO the weekly digest and the sunset alert go out to.
# ---------------------------------------------------------------------------


def _seed_egress_project(
    session: Session, *, name: str, is_demo: bool
) -> tuple[Project, AlertDestination]:
    """A project plus the one row shape both digest SELECTs admit.

    Enabled, and of type ``slack``: those are the two predicates the tasks
    filter on, so this is the only kind of destination either can reach. A demo
    ships its external example DISABLED (services/demo/builders/alerts.py), so
    an enabled one on a demo is the hand-written row that the API's create and
    update guards refuse and that only these two tasks would ever have sent to.
    """
    project = Project(
        id=uuid.uuid4(),
        name=name,
        slug=f"{name.lower()}-{uuid.uuid4().hex[:8]}",
        description="",
        is_demo=is_demo,
    )
    destination = AlertDestination(
        id=uuid.uuid4(),
        project_id=project.id,
        type=AlertDestinationType.slack.value,
        name=f"{name} Slack",
        enabled=True,
        webhook_url_encrypted="fake-secret",
    )
    session.add_all([project, destination])
    session.commit()
    return project, destination


def _add_overdue_deprecated_event(session: Session, project: Project, *, name: str) -> None:
    """A deprecated event still receiving data past its sunset — the alert's trigger."""
    now = datetime.now(UTC)
    session.add(
        Event(
            id=uuid.uuid4(),
            project_id=project.id,
            # FK not enforced under sqlite, and the sunset query never joins it.
            event_type_id=uuid.uuid4(),
            name=name,
            description="",
            status=EventStatus.deprecated,
            sunset_at=now - timedelta(days=60),
            last_seen_at=now - timedelta(days=1),
        )
    )
    session.commit()


def _capture_digest_sends(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record (project name, message) at the CHANNEL, one level below the guard.

    The existing sunset tests in test_alerting.py stub the module-local
    ``_send_digest_to_destination`` wrapper, which is where the egress guard now
    lives — stubbing it here would remove the thing under test. Patching the
    channel function that wrapper delegates to leaves the guard in the path and
    still stops anything at the socket.
    """
    sends: list[tuple[str, str]] = []

    def fake_transport(
        *,
        destination: AlertDestination,
        message: str,
        project: Project,
        email_config: object,
        send_slack_message: object,
        send_email_message: object,
        subject_title: str = "Weekly tripl digest",
    ) -> None:
        sends.append((project.name, message))

    monkeypatch.setattr(f"{_DIGEST}._channel_send_digest_to_destination", fake_transport)
    return sends


def test_the_weekly_digest_never_reaches_a_demo_projects_slack(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One real project and one demo, both holding an enabled Slack destination.

    A demo workspace is zero-egress by construction, but this task resolves its
    own destinations instead of handing an AlertDelivery to a send task, so
    nothing between the demo's row and a real webhook POST was refusing it. The
    real project in the same run is what separates the fix from breaking the
    digest outright, and ``destinations_checked`` is asserted because the demo
    row is what moves it: with the ``Project.is_demo`` predicate reverted this
    reads ``{"destinations_checked": 2, "sent": 1, "failed": 1}`` — the demo
    caught by the backstop below — and with the whole fix reverted it reads 2
    checked, 2 sent, and the demo's digest in ``sends``.
    """
    with sync_session_factory() as session:
        _seed_egress_project(session, name="Checkout", is_demo=False)
        _seed_egress_project(session, name="Demo", is_demo=True)

    sends = _capture_digest_sends(monkeypatch)
    monkeypatch.setattr(f"{_DIGEST}._get_sync_session", sync_session_factory)

    result = send_weekly_plan_digest.run()

    assert result == {"destinations_checked": 1, "sent": 1, "failed": 0}
    assert [project_name for project_name, _ in sends] == ["Checkout"]
    assert sends[0][1].startswith("Weekly tripl digest for Checkout")


def test_the_sunset_alert_never_reaches_a_demo_projects_slack(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second copy of the same SELECT, with its own overdue event to fire on.

    Pinned separately rather than parametrized with the digest above because
    the predicate is duplicated in the source: reverting either task's
    ``Project.is_demo`` line alone has to redden something, and one shared test
    would leave the other copy unproven. This task runs daily from beat
    (``check-deprecated-sunset-events``, wired for tripl-0zpq.31), so the demo
    row it refuses is one a scheduler now offers it every day.
    """
    with sync_session_factory() as session:
        real, _ = _seed_egress_project(session, name="Checkout", is_demo=False)
        demo, _ = _seed_egress_project(session, name="Demo", is_demo=True)
        _add_overdue_deprecated_event(session, real, name="app:old_purchase")
        _add_overdue_deprecated_event(session, demo, name="demo:old_signup")

    sends = _capture_digest_sends(monkeypatch)
    monkeypatch.setattr(f"{_DIGEST}._get_sync_session", sync_session_factory)

    result = check_deprecated_sunset_events.run()

    assert result == {"destinations_checked": 1, "sent": 1, "failed": 0}
    assert [project_name for project_name, _ in sends] == ["Checkout"]
    assert "app:old_purchase" in sends[0][1]
    # The demo's own overdue event must not even be quoted to the real project.
    assert "demo:old_signup" not in sends[0][1]


def test_the_digest_send_helper_refuses_a_demo_project_on_its_own(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backstop under both SELECTs, exercised the only way it can fire.

    Neither task can reach it while both filters stand, which is exactly what a
    backstop is for: it is what a third digest-shaped task added to this module
    inherits without having to remember the WHERE clause. Calling the wrapper
    directly is therefore the only way to state the contract. The recorder is
    asserted empty as well as the raise, because a bare ``raises(ValueError)``
    would also be satisfied by a later step failing — decrypting the fixture's
    placeholder webhook, say — which is a refusal for the wrong reason.
    """
    with sync_session_factory() as session:
        demo, destination = _seed_egress_project(session, name="Demo", is_demo=True)
        email_config = app_settings_service.get_email_config_sync(session, org_id=None)

    sends = _capture_digest_sends(monkeypatch)

    with pytest.raises(ValueError, match="disabled for demo projects"):
        _send_digest_to_destination(
            destination=destination,
            message="Weekly tripl digest for Demo",
            project=demo,
            email_config=email_config,
        )

    assert sends == []


def test_the_backstop_refuses_egress_rather_than_refusing_demos(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of that contract, and the reason it is not a demo check.

    A ``demo_sink`` is the one destination a demo project may own and it has no
    outside to reach — it records locally — so the guard admits it by type
    rather than by project (models/alert_destination.py, ``_assert_egress_allowed``).
    The refusal above is equally satisfied by a blanket ``if project.is_demo:
    raise``, which would read as a stricter version of the same fix and would
    instead lock a future digest-shaped task out of the only sink a demo has.
    Neither task can route here today — both SELECTs admit ``slack`` and
    ``email`` only — so, like the refusal, this states the helper's contract by
    calling it directly.
    """
    with sync_session_factory() as session:
        demo, _ = _seed_egress_project(session, name="Demo", is_demo=True)
        sink = AlertDestination(
            id=uuid.uuid4(),
            project_id=demo.id,
            type=AlertDestinationType.demo_sink.value,
            name="Demo sink",
            enabled=True,
        )
        session.add(sink)
        session.commit()
        email_config = app_settings_service.get_email_config_sync(session, org_id=None)

    sends = _capture_digest_sends(monkeypatch)

    _send_digest_to_destination(
        destination=sink,
        message="Weekly tripl digest for Demo",
        project=demo,
        email_config=email_config,
    )

    assert sends == [("Demo", "Weekly tripl digest for Demo")]
