"""Alert rendering of the stored "why did this move" attribution (GH #255).

The attribution is computed once, at detection time, and stored per anomaly in
``metric_anomaly_attributions``. These tests pin the alert side: the one-liner
is exactly the core ``attribution_headline`` (+ ``release_line``) the API
serves, the message line, the AI-note input, the frozen snapshot, and the
loader that joins items back to their anomaly by the metric_anomalies unique key.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from tripl.alert_templates import (
    ALERT_ITEM_TEMPLATE_VARIABLES,
    ALERT_MESSAGE_FORMAT_PLAIN,
    ALERT_MESSAGE_FORMAT_TELEGRAM_HTML,
    DEFAULT_ALERT_ITEMS_TEMPLATES,
    DIGEST_ALERT_ITEMS_TEMPLATES,
)
from tripl.core.analyzers.attribution import attribution_headline, release_line
from tripl.models import Base
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.data_source import DataSource
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_anomaly_attribution import MetricAnomalyAttribution
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.services import attribution_text
from tripl.services.attribution_text import format_attribution_line
from tripl.worker.tasks import alerts_messages
from tripl.worker.tasks.metrics import alert_payload

BUCKET = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)

# The spec's own example: a −3,390 drop, 92% of it from platform = ios.
DROP_ATTRIBUTION: dict[str, object] = {
    "delta": -3390.0,
    "columns": [
        {
            "column": "platform",
            "explained_share": 0.92,
            "values": [
                {"value": "ios", "delta": -3120.0, "expected": 5000.0, "actual": 1880.0},
                {"value": "web", "delta": 40.0, "expected": 900.0, "actual": 940.0},
            ],
        },
        {
            "column": "country",
            "explained_share": 0.4,
            "values": [{"value": "US", "delta": -1356.0, "expected": 4000, "actual": 2644}],
        },
    ],
    "release": None,
}


# --- format_attribution_line --------------------------------------------------
#
# The alert line is NOT its own formula: it is exactly the core headline (+ "; "
# + release line), the strings the API returns and the UI renders verbatim.


def _core_line(
    payload: Mapping[str, Any], direction: str | None, bucket: datetime | None = None
) -> str:
    parts = [attribution_headline(payload, direction)]
    if bucket is not None:
        parts.append(release_line(payload, anomaly_bucket=bucket, direction=direction))
    return "; ".join(part for part in parts if part)


_RELEASE = {
    "version": "4.12",
    "previous_version": "4.11",
    "share": 0.38,
    "reached_at": (BUCKET - timedelta(hours=3)).isoformat(),
}

_PAYLOADS: list[tuple[str, dict[str, Any], str]] = [
    ("spec-example", DROP_ATTRIBUTION, "drop"),
    ("with-release", {**DROP_ATTRIBUTION, "release": _RELEASE}, "drop"),
    (
        "release-only",
        {"delta": -10.0, "columns": [], "release": {**_RELEASE, "previous_version": None}},
        "drop",
    ),
    (
        "spike-opposite-top",
        {
            "delta": 1000.0,
            "columns": [
                {
                    "column": "country",
                    "explained_share": 0.75,
                    "gross_movement": 1900.0,
                    "values": [
                        {"value": "DE", "delta": -450.0},
                        {"value": "US", "delta": 400.0},
                        {"value": "FR", "delta": 350.0},
                    ],
                }
            ],
        },
        "spike",
    ),
    (
        "offsetting",
        {
            "delta": -50.0,
            "columns": [
                {
                    "column": "platform",
                    "explained_share": 1.0,
                    "gross_movement": 1750.0,
                    "values": [
                        {"value": "ios", "delta": -900.0},
                        {"value": "web", "delta": 850.0},
                    ],
                }
            ],
        },
        "drop",
    ),
    (
        "second-column-explains-more",
        {
            "delta": -1000.0,
            "columns": [
                {
                    "column": "country",
                    "explained_share": 0.6,
                    "values": [{"value": "US", "delta": -600.0}],
                },
                {
                    "column": "platform",
                    "explained_share": 0.9,
                    "values": [
                        {"value": "ios", "delta": -500.0},
                        {"value": "web", "delta": -400.0},
                    ],
                },
            ],
        },
        "drop",
    ),
]


@pytest.mark.parametrize(
    ("payload", "direction"),
    [(payload, direction) for _, payload, direction in _PAYLOADS],
    ids=[name for name, _, _ in _PAYLOADS],
)
def test_the_alert_line_is_exactly_the_core_headline_and_release_line(
    payload: dict[str, Any], direction: str
) -> None:
    line = format_attribution_line(payload, direction, bucket=BUCKET)
    assert line == _core_line(payload, direction, BUCKET)
    assert line  # every shape here has something to say


def test_the_spec_example_reads_the_same_in_the_alert_and_the_api() -> None:
    assert format_attribution_line(DROP_ATTRIBUTION, "drop") == (
        "92% of the drop comes from platform = ios (−3,120 of −3,390)"
    )
    attribution = {**DROP_ATTRIBUTION, "release": _RELEASE}
    assert format_attribution_line(attribution, "drop", bucket=BUCKET) == (
        "92% of the drop comes from platform = ios (−3,120 of −3,390); "
        "Release 4.12 (after 4.11) reached 38% of traffic 3h before the drop"
    )


def test_without_a_bucket_the_release_half_is_left_out() -> None:
    attribution = {**DROP_ATTRIBUTION, "release": _RELEASE}
    assert format_attribution_line(attribution, "drop") == attribution_headline(attribution, "drop")


@pytest.mark.parametrize(
    "attribution",
    [
        None,
        {},
        {"delta": 0, "columns": DROP_ATTRIBUTION["columns"]},
        {"delta": -50.0, "columns": []},
        {"delta": -50.0, "columns": [{"column": "os", "values": "garbage"}]},
        {"delta": "nan", "columns": DROP_ATTRIBUTION["columns"]},
        {"delta": -50.0, "columns": "garbage", "release": "garbage"},
    ],
)
def test_nothing_to_say_renders_empty_like_the_core(attribution: Any) -> None:
    payload = attribution or {}
    expected = _core_line(
        {
            "delta": payload.get("delta"),
            "columns": payload.get("columns") or [],
            "release": payload.get("release"),
        },
        "drop",
        BUCKET,
    )
    assert format_attribution_line(attribution, "drop", bucket=BUCKET) == expected == ""


def test_the_formatter_reads_orm_shaped_rows_too() -> None:
    row = SimpleNamespace(delta=-3390.0, columns=DROP_ATTRIBUTION["columns"], release=_RELEASE)
    assert format_attribution_line(row, "drop", bucket=BUCKET) == format_attribution_line(
        {**DROP_ATTRIBUTION, "release": _RELEASE}, "drop", bucket=BUCKET
    )


# --- templates ----------------------------------------------------------------


def test_every_default_item_template_carries_the_attribution_line() -> None:
    for template in DEFAULT_ALERT_ITEMS_TEMPLATES.values():
        assert "${attribution_line}" in template
    assert "attribution" in ALERT_ITEM_TEMPLATE_VARIABLES
    assert "attribution_line" in ALERT_ITEM_TEMPLATE_VARIABLES


def test_digest_items_stay_one_line() -> None:
    for template in DIGEST_ALERT_ITEMS_TEMPLATES.values():
        assert "${attribution_line}" not in template


def _item(scope_type: str = "event") -> AlertDeliveryItem:
    return AlertDeliveryItem(
        id=uuid.uuid4(),
        scope_type=scope_type,
        scope_ref=str(uuid.uuid4()),
        scope_name="Checkout",
        bucket=BUCKET,
        direction="drop",
        actual_count=1000,
        expected_count=4390,
        absolute_delta=3390,
        percent_delta=-77.2,
    )


def _render(item: AlertDeliveryItem, cache: dict[uuid.UUID, tuple[str, ...]], fmt: str) -> str:
    context = alerts_messages._build_item_template_context(
        item, message_format=fmt, item_context_cache=cache
    )
    return context.variables["attribution_line"]


def test_the_rendered_item_carries_a_why_line_from_the_cache() -> None:
    item = _item()
    line = format_attribution_line(DROP_ATTRIBUTION, "drop")
    rendered = _render(item, {item.id: ("", "", line)}, ALERT_MESSAGE_FORMAT_PLAIN)
    assert rendered == f"\n  why: {line}"


def test_the_why_line_is_escaped_per_format() -> None:
    item = _item()
    rendered = _render(item, {item.id: ("", "", "a <b> & c")}, ALERT_MESSAGE_FORMAT_TELEGRAM_HTML)
    assert "&lt;b&gt; &amp; c" in rendered


def test_a_legacy_two_tuple_cache_entry_renders_no_why_line() -> None:
    item = _item()
    assert _render(item, {item.id: ("▁▂", "os=ios -90%")}, "plain") == ""


def test_the_item_attribution_is_loaded_once_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    item = _item()
    calls: list[dict[str, Any]] = []

    def fake_line(session: object, **kwargs: Any) -> str:
        calls.append(kwargs)
        return "90% of the drop comes from os = ios (−90 of −100)"

    monkeypatch.setattr(alerts_messages, "attribution_line_for_scope", fake_line)
    monkeypatch.setattr(alerts_messages, "build_alert_item_context", lambda *a, **k: ("", ""))
    session = cast(Session, SimpleNamespace(no_autoflush=_NullContext()))
    cache: dict[uuid.UUID, tuple[str, ...]] = {}
    for fmt in ("plain", "slack_mrkdwn"):
        alerts_messages._build_item_template_context(
            item,
            message_format=fmt,
            session=session,
            scan_config_id=uuid.uuid4(),
            item_context_cache=cache,
        )
    assert len(calls) == 1
    assert calls[0]["direction"] == "drop"
    assert cache[item.id][2].startswith("90% of the drop")


def test_non_volume_scopes_never_query_an_attribution(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> str:
        raise AssertionError("must not be called")

    monkeypatch.setattr(alerts_messages, "attribution_line_for_scope", boom)
    session = cast(Session, SimpleNamespace(no_autoflush=_NullContext()))
    for scope_type in ("schema", "distribution", "release_regression", "metric"):
        item = _item(scope_type)
        assert alerts_messages._load_item_attribution(session, uuid.uuid4(), item) == ""


def test_a_failed_attribution_read_costs_the_line_not_the_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a: object, **_k: object) -> str:
        raise RuntimeError("table missing")

    monkeypatch.setattr(alerts_messages, "attribution_line_for_scope", boom)
    session = cast(Session, SimpleNamespace(no_autoflush=_NullContext()))
    assert alerts_messages._load_item_attribution(session, uuid.uuid4(), _item()) == ""


class _NullContext:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: object) -> None:
        return None


# --- AI explanation input -----------------------------------------------------


def test_the_ai_prompt_receives_the_attribution(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def fake_complete(system_prompt: str, user_prompt: str, **kwargs: object) -> str:
        captured["user_prompt"] = user_prompt
        return "explained"

    monkeypatch.setattr("tripl.services.llm_service.is_enabled", lambda: True)
    monkeypatch.setattr("tripl.services.llm_service.complete", fake_complete)
    delivery = AlertDelivery(
        project_id=uuid.uuid4(),
        scan_config_id=uuid.uuid4(),
        destination_id=uuid.uuid4(),
        rule_id=uuid.uuid4(),
        matched_count=1,
    )
    item = _item()
    delivery.items = [item]
    line = format_attribution_line(DROP_ATTRIBUTION, "drop")
    result = alerts_messages._build_ai_explanation(
        delivery,
        scan_name="main",
        project_name="AI",
        item_context_cache={item.id: ("", "", line)},
    )
    assert result == "explained"
    assert f"attribution: {line}" in captured["user_prompt"]


# --- frozen payload snapshot --------------------------------------------------


def _candidate(scope_type: str = "event") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        scan_config_id=uuid.uuid4(),
        scope_type=scope_type,
        scope_ref=str(uuid.uuid4()),
        event_id=None,
        event_type_id=None,
        bucket=BUCKET,
        direction="drop",
        actual_count=1000.0,
        expected_count=4390.0,
    )


def test_the_snapshot_freezes_the_attribution_line() -> None:
    with_line = _candidate()
    without_line = _candidate("event_type")
    names = {
        (with_line.scope_type, with_line.scope_ref): "Checkout",
        (without_line.scope_type, without_line.scope_ref): "Purchases",
    }
    line = format_attribution_line(DROP_ATTRIBUTION, "drop")
    snapshot = alert_payload._build_delivery_snapshot(
        cast(Any, SimpleNamespace(name="scan")),
        project_slug="p",
        app_base_url="",
        rule=cast(Any, SimpleNamespace(name="rule")),
        destination=cast(Any, SimpleNamespace(name="dest", type="slack")),
        anomalies=cast(Any, [with_line, without_line]),
        scope_names=names,
        attribution_lines={(with_line.scope_type, with_line.scope_ref): line},
    )
    items = cast(list[dict[str, object]], snapshot["items"])
    assert items[0]["attribution_line"] == line
    assert items[1]["attribution_line"] is None


def test_snapshot_attribution_lines_skip_foreign_and_non_volume_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SimpleNamespace(id=uuid.uuid4())
    own = _candidate()
    own.scan_config_id = config.id
    foreign = _candidate()  # a different scan's anomaly
    drift = _candidate("schema")
    drift.scan_config_id = config.id
    seen: dict[str, Any] = {}

    def fake_load(session: object, *, scan_config_id: uuid.UUID, keys: Any) -> dict[Any, Any]:
        seen["keys"] = set(keys)
        return {(own.scope_type, own.scope_ref, own.bucket): DROP_ATTRIBUTION}

    monkeypatch.setattr(alert_payload, "load_attributions_for_scopes", fake_load)
    session = cast(Session, SimpleNamespace(no_autoflush=_NullContext()))
    lines = alert_payload._build_attribution_lines(
        session, cast(Any, config), cast(Any, [own, foreign, drift])
    )
    assert seen["keys"] == {(own.scope_type, own.scope_ref, own.bucket)}
    assert lines == {
        (own.scope_type, own.scope_ref): format_attribution_line(DROP_ATTRIBUTION, "drop")
    }


# --- DB loaders -----------------------------------------------------------------


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'attribution_alerts.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def test_loaders_join_items_to_their_stored_attribution(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        project = Project(
            id=uuid.uuid4(), name="P", slug=f"p-{uuid.uuid4().hex[:8]}", description=""
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
        session.add_all([project, data_source])
        config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project.id,
            name="scan",
            base_query="SELECT 1",
            time_column="time",
            cardinality_threshold=100,
            interval="1h",
        )
        session.add(config)
        session.flush()
        anomaly = MetricAnomaly(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            scope_type="project_total",
            scope_ref="all",
            bucket=BUCKET,
            actual_count=1000.0,
            expected_count=4390.0,
            stddev=100.0,
            z_score=-30.0,
            direction="drop",
        )
        bare = MetricAnomaly(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            scope_type="project_total",
            scope_ref="all",
            bucket=BUCKET - timedelta(hours=1),
            actual_count=1.0,
            expected_count=2.0,
            stddev=1.0,
            z_score=-3.0,
            direction="drop",
        )
        session.add_all([anomaly, bare])
        session.flush()
        session.add(
            MetricAnomalyAttribution(
                anomaly_id=anomaly.id,
                delta=DROP_ATTRIBUTION["delta"],
                columns=DROP_ATTRIBUTION["columns"],
                release=None,
            )
        )
        session.commit()

        by_id = attribution_text.load_attributions(session, [anomaly.id, bare.id])
        assert set(by_id) == {anomaly.id}

        line = attribution_text.attribution_line_for_scope(
            session,
            scan_config_id=config.id,
            scope_type="project_total",
            scope_ref="all",
            bucket=BUCKET,
            direction="drop",
        )
        assert line == format_attribution_line(DROP_ATTRIBUTION, "drop")
        assert (
            attribution_text.attribution_line_for_scope(
                session,
                scan_config_id=config.id,
                scope_type="project_total",
                scope_ref="all",
                bucket=BUCKET - timedelta(hours=1),
                direction="drop",
            )
            == ""
        )
        # Another scan's scope with the same ref and bucket does not leak in.
        assert (
            attribution_text.attribution_line_for_scope(
                session,
                scan_config_id=uuid.uuid4(),
                scope_type="project_total",
                scope_ref="all",
                bucket=BUCKET,
                direction="drop",
            )
            == ""
        )


def test_loaders_short_circuit_on_empty_input() -> None:
    session = cast(Session, SimpleNamespace())
    assert attribution_text.load_attributions(session, []) == {}
    assert (
        attribution_text.load_attributions_for_scopes(session, scan_config_id=uuid.uuid4(), keys=[])
        == {}
    )
