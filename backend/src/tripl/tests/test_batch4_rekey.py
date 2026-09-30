"""``f4a8d3c72e19`` driven against a real database, both directions.

The revision re-keys every LIVE catalog-metric incident after tripl-0zpq.27 made
that scope project-global: N per-scan handles collapse onto one, the Inbox
decisions recorded against them merge onto the survivor, and the copy of the
handle baked into ``AlertDeliveryItem.details_path`` moves with the column. Its
own docstring says the work lives in bind-taking functions "so a test can drive
them against a real database instead of asserting SQL text", and until this file
nothing did: ``rekey_metric_incidents`` appeared in no test in the repo. What
was covered (``test_batch4_dispatch.test_the_rekey_migration_computes_exactly_
the_handle_dispatch_computes``) is the KEY — that ``_group_id`` still equals
``dispatch._correlation_group_id``. A correct key applied by a broken pass
orphans every ack and every mute in the product just as thoroughly.

So nothing here re-derives the key. Every expected handle below is computed by
calling ``dispatch._correlation_group_id`` itself — the project-global partition
for the upgrade, a config id for the downgrade — because the id this migration
must land on is defined by the product code, not by the migration's frozen copy
of it. The copy is somebody else's test; this one is about the pass.

WHAT SQLITE PROVES, and it is most of it. The merge rule, the delete-before-
repoint ordering, the re-run guard, the link rewrite and the downgrade are all
plain row behaviour, and ``uq_alert_correlation_state_project_group`` is a real
constraint under ``Base.metadata.create_all`` on SQLite too — so an
out-of-order repoint fails here exactly as it would in production
(:func:`test_the_constraint_that_forces_that_ordering_really_bites` pins that
the constraint bites at all, which is what makes the ordering assertion
load-bearing rather than decorative).

WHAT ONLY THE POSTGRES ARM CAN PROVE, at the bottom of this file:

* ``_is_metric`` and ``_metric_handle_rows`` compare a NATIVE enum column
  (``metric_scope_type``, ``anomaly_direction``) through an explicit CAST.
  SQLite stores both as VARCHAR, so the cast is a no-op there and a predicate
  that could not compile against a real enum would still pass every test above.
* alembic reaches this database through asyncpg, which prepares the statement
  and asks the SERVER to deduce one type per parameter. That is the failure
  ``f3a9b7c15d2e``'s deploy died on ("inconsistent types deduced for parameter
  $1"), before touching a row, and it is unreachable on SQLite, which binds
  client-side. It is also why ``_relink`` rewrites the URL in Python instead of
  as a ``replace()`` repeating one bind parameter.
* the unique constraint is genuinely NOT DEFERRABLE on the server. SQLite has
  no deferrable constraints to get wrong, so "not deferrable" is vacuous there.

Gated like the other PostgreSQL gates: skipped without ``TRIPL_TEST_PG_URL``, a
failure when ``TRIPL_TEST_PG_REQUIRED=1`` says CI must not skip it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from tripl.models import Base
from tripl.models.alert_correlation_state import AlertCorrelationState
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_pending_item import AlertPendingItem
from tripl.models.alert_rule import AlertRule
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import AnomalyDirection, MetricScopeType
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.tests._sqlite import enable_sqlite_foreign_keys
from tripl.tests.test_alembic_revisions import _load_migration
from tripl.tests.test_alert_digest_concurrency_pg import _engine_or_skip
from tripl.tests.test_repair_migration_pg import _asyncpg_url
from tripl.worker.tasks.metrics import dispatch as metrics_dispatch
from tripl.worker.tasks.metrics.urls import ALERT_INCIDENT_PARAM, _build_alert_audit_url

_MIGRATION = "f4a8d3c72e19_metric_incident_ids_are_project_global.py"

_METRIC = MetricScopeType.metric.value
_EVENT = MetricScopeType.event.value
_SPIKE = AnomalyDirection.spike.value

_APP_BASE_URL = "https://tripl.example"

# CONSTRUCTED, never drawn — the same rule ``test_batch4_dispatch`` states for
# the sibling-config helper it added for tripl-0zpq.28. ``_anchor_by_project``
# picks the project's LOWEST config id and the downgrade re-keys onto it, so a
# test that drew its ids would assert the anchor was config A only 1/N of the
# time and pass by luck the rest.
_CONFIG_A = uuid.UUID(int=1)
_CONFIG_B = uuid.UUID(int=2)
_CONFIG_C = uuid.UUID(int=3)


def _migration() -> Any:
    return _load_migration("metric_incident_rekey_pass", _MIGRATION)


def _handle(
    partition: uuid.UUID | None, *, rule_id: uuid.UUID, scope_ref: str, direction: str = _SPIKE
) -> uuid.UUID:
    """The handle the PRODUCT computes for a metric scope in ``partition``.

    ``None`` is the project-global partition the fixed code hashes (what the
    upgrade must land on); a config id is what the reverted code hashes (what
    the downgrade must land on). Taken from ``dispatch`` rather than from the
    migration's frozen copy on purpose — see the module docstring.
    """
    return metrics_dispatch._correlation_group_id(
        scan_config_id=partition,
        rule_id=rule_id,
        scope_type=_METRIC,
        scope_ref=scope_ref,
        direction=direction,
    )


@dataclass(frozen=True)
class _Seeded:
    """Ids of a seeded project, frozen so a test cannot re-point its own fixture."""

    project_id: uuid.UUID
    project_slug: str
    destination_id: uuid.UUID
    rule_id: uuid.UUID
    config_ids: tuple[uuid.UUID, ...]
    scope_ref: str

    def old(self, config_id: uuid.UUID, *, direction: str = _SPIKE) -> uuid.UUID:
        return _handle(
            config_id, rule_id=self.rule_id, scope_ref=self.scope_ref, direction=direction
        )

    def new(self, *, direction: str = _SPIKE) -> uuid.UUID:
        return _handle(None, rule_id=self.rule_id, scope_ref=self.scope_ref, direction=direction)


@pytest.fixture
def sqlite_engine(tmp_path: Path) -> Iterator[Engine]:
    """A file-backed SQLite database with the model schema and FKs enforced.

    Foreign keys ON for the reason ``tests/_sqlite`` gives: the deliveries this
    migration walks reach their project and rule THROUGH ``alert_deliveries``,
    and a test whose join could survive a dangling ``delivery_id`` is not
    testing the join.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'batch4_rekey.db'}")
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def sqlite_sessions(sqlite_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(sqlite_engine, expire_on_commit=False)


def _now() -> datetime:
    """An aware "now" for the columns nothing asserts on.

    Aware because PostgreSQL stores these as ``timestamptz``; SQLite's DATETIME
    bind processor drops the offset and hands the value back naive, which is
    precisely the mix the migration's ``_as_utc`` exists for — ``max()`` over
    aware and naive datetimes raises, and a merge that raised would abort the
    whole upgrade.
    """
    return datetime.now(UTC).replace(microsecond=0)


def _seed(
    session: Session,
    *,
    config_ids: tuple[uuid.UUID, ...] = (_CONFIG_A, _CONFIG_B, _CONFIG_C),
) -> _Seeded:
    """A project whose catalog metric was collected by every one of ``config_ids``."""
    project = Project(
        id=uuid.uuid4(),
        name="Rekey",
        slug=f"rekey-{uuid.uuid4().hex[:8]}",
        description="",
        timezone="UTC",
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
    destination = AlertDestination(
        id=uuid.uuid4(),
        project_id=project.id,
        type="slack",
        name="Slack",
        enabled=True,
        webhook_url_encrypted="secret",
    )
    session.add_all([project, data_source])
    session.flush()
    session.add_all(
        [
            ScanConfig(
                id=config_id,
                data_source_id=data_source.id,
                project_id=project.id,
                name=f"Scan {index}",
                base_query="SELECT time, event_name FROM events",
                time_column="time",
                cardinality_threshold=100,
                interval="1h",
            )
            for index, config_id in enumerate(config_ids)
        ]
    )
    session.add(destination)
    session.flush()
    rule = AlertRule(
        id=uuid.uuid4(),
        destination_id=destination.id,
        name="Everything",
        enabled=True,
        min_percent_delta=0,
        min_absolute_delta=0,
        min_expected_count=0,
    )
    session.add(rule)
    session.flush()
    session.commit()
    return _Seeded(
        project_id=project.id,
        project_slug=project.slug,
        destination_id=destination.id,
        rule_id=rule.id,
        config_ids=tuple(config_ids),
        scope_ref=str(uuid.uuid4()),
    )


def _add_delivery_item(
    session: Session,
    seeded: _Seeded,
    *,
    config_id: uuid.UUID,
    correlation_group_id: uuid.UUID | None,
    scope_type: str = _METRIC,
    scope_ref: str | None = None,
    direction: str = _SPIKE,
    link_carries_handle: bool = True,
    with_link: bool = True,
) -> uuid.UUID:
    """One delivered item, with the deep link the LIVE builder would have minted.

    ``details_path`` is built by ``urls._build_alert_audit_url`` rather than
    hand-spelled, so the substring this migration has to find is the substring
    production actually writes. ``link_carries_handle=False`` reproduces a row
    minted before tripl-pq97 put the incident in the URL at all; ``with_link=
    False`` one that has no link to rewrite (an unconfigured ``app_base_url``).
    """
    delivery = AlertDelivery(
        id=uuid.uuid4(),
        project_id=seeded.project_id,
        scan_config_id=config_id,
        destination_id=seeded.destination_id,
        rule_id=seeded.rule_id,
        channel="slack",
        matched_count=1,
    )
    session.add(delivery)
    session.flush()
    details_path = _build_alert_audit_url(
        seeded.project_slug,
        delivery.id,
        org_slug="default",
        app_base_url=_APP_BASE_URL if with_link else "",
        scope_type=scope_type,
        scope_ref=scope_ref or seeded.scope_ref,
        correlation_group_id=correlation_group_id if link_carries_handle else None,
    )
    item = AlertDeliveryItem(
        id=uuid.uuid4(),
        delivery_id=delivery.id,
        scope_type=scope_type,
        scope_ref=scope_ref or seeded.scope_ref,
        scope_name="Signups per hour",
        bucket=_now(),
        direction=direction,
        actual_count=120.0,
        expected_count=40.0,
        absolute_delta=80.0,
        percent_delta=200.0,
        details_path=details_path,
        correlation_group_id=correlation_group_id,
    )
    session.add(item)
    session.commit()
    return item.id


def _add_pending_item(
    session: Session,
    seeded: _Seeded,
    *,
    correlation_group_id: uuid.UUID,
    direction: str = _SPIKE,
) -> uuid.UUID:
    """One buffered digest line, shaped as ``c9e2a71b4d38`` leaves it.

    ``scan_config_id`` is NULL: the preceding revision already collapsed the N
    per-scan buffered rows onto one project-global row (and
    ``uq_alert_pending_item_metric_scope`` now forbids a second). What that
    revision did NOT do is fix the handle the surviving row carries, which is
    why this row is still sitting on a per-scan id when this migration runs.
    """
    item = AlertPendingItem(
        id=uuid.uuid4(),
        project_id=seeded.project_id,
        destination_id=seeded.destination_id,
        rule_id=seeded.rule_id,
        scan_config_id=None,
        scope_type=_METRIC,
        scope_ref=seeded.scope_ref,
        scope_name="Signups per hour",
        bucket=_now(),
        direction=direction,
        actual_count=120.0,
        expected_count=40.0,
        correlation_group_id=correlation_group_id,
    )
    session.add(item)
    session.commit()
    return item.id


def _add_state(
    session: Session,
    seeded: _Seeded,
    *,
    correlation_group_id: uuid.UUID,
    status: str,
    acted_at: datetime | None,
    last_seen_at: datetime | None = None,
    false_positive_count: int = 0,
    muted_until: datetime | None = None,
    note: str | None = None,
) -> uuid.UUID:
    state = AlertCorrelationState(
        id=uuid.uuid4(),
        project_id=seeded.project_id,
        correlation_group_id=correlation_group_id,
        status=status,
        acted_at=acted_at,
        last_seen_at=last_seen_at,
        false_positive_count=false_positive_count,
        muted_until=muted_until,
        note=note,
    )
    session.add(state)
    session.commit()
    return state.id


def _run(engine: Engine, **kwargs: Any) -> int:
    migration = _migration()
    with engine.begin() as connection:
        return migration.rekey_metric_incidents(connection, **kwargs)


def _items(session: Session) -> list[AlertDeliveryItem]:
    return list(session.execute(select(AlertDeliveryItem).order_by(AlertDeliveryItem.id)).scalars())


def _states(session: Session) -> list[AlertCorrelationState]:
    return list(
        session.execute(select(AlertCorrelationState).order_by(AlertCorrelationState.id)).scalars()
    )


def _incident_in(details_path: str | None) -> str | None:
    """The handle the stored URL names, read the way the frontend reads it."""
    assert details_path is not None
    values = parse_qs(urlparse(details_path).query).get(ALERT_INCIDENT_PARAM, [])
    return values[0] if values else None


# --------------------------------------------------------------------------
# The pass itself
# --------------------------------------------------------------------------


def test_three_per_scan_handles_collapse_onto_the_one_the_fixed_code_computes(
    sqlite_engine: Engine, sqlite_sessions: sessionmaker[Session]
) -> None:
    """The whole point: after this, one project-global metric scope has ONE handle.

    Three scans collected one project-wide catalog metric, so three deliveries
    carry three different ids for a single incident. The buffered digest line
    carries a fourth reference to one of them. Every one of them must come out
    equal to ``dispatch._correlation_group_id(scan_config_id=None, ...)``, which
    is the id the next collection will compute and the id the Inbox will act on.
    """
    with sqlite_sessions() as session:
        seeded = _seed(session)
        for config_id in seeded.config_ids:
            _add_delivery_item(
                session, seeded, config_id=config_id, correlation_group_id=seeded.old(config_id)
            )
        _add_pending_item(session, seeded, correlation_group_id=seeded.old(_CONFIG_A))

    assert len({seeded.old(config_id) for config_id in seeded.config_ids}) == 3, (
        "the fixture has to actually reproduce the defect: three scans, three handles"
    )

    rewritten = _run(sqlite_engine)

    # 3 delivery items + 1 buffered row. No correlation states exist yet, so
    # nothing is added by the second half of the pass.
    assert rewritten == 4

    with sqlite_sessions() as session:
        assert {item.correlation_group_id for item in _items(session)} == {seeded.new()}
        buffered = session.execute(select(AlertPendingItem)).scalars().all()
        assert [row.correlation_group_id for row in buffered] == [seeded.new()]


def test_the_link_and_the_column_name_the_same_incident_afterwards(
    sqlite_engine: Engine, sqlite_sessions: sessionmaker[Session]
) -> None:
    """``details_path`` is a SECOND, untyped copy of the handle, and it moves too.

    The send path re-reads this column on every attempt — a stranded requeue, an
    auto-retry, the Inbox's Retry button — so a row left with the old id in its
    URL ships a message pointing at an incident this same transaction retired,
    and ``get_alert_inbox_group`` answers a retired handle with 404.

    Three shapes, because the rewrite has to be exact rather than enthusiastic:
    a link carrying the handle moves; a link minted before the incident was in
    the URL at all comes back BYTE-IDENTICAL (``_relink`` returns ``None`` and
    the column is left out of the UPDATE); a NULL stays NULL.
    """
    with sqlite_sessions() as session:
        seeded = _seed(session, config_ids=(_CONFIG_A, _CONFIG_B))
        linked = _add_delivery_item(
            session, seeded, config_id=_CONFIG_A, correlation_group_id=seeded.old(_CONFIG_A)
        )
        legacy = _add_delivery_item(
            session,
            seeded,
            config_id=_CONFIG_B,
            correlation_group_id=seeded.old(_CONFIG_B),
            link_carries_handle=False,
        )
        unlinked = _add_delivery_item(
            session,
            seeded,
            config_id=_CONFIG_B,
            correlation_group_id=seeded.old(_CONFIG_B),
            with_link=False,
        )
        before = {
            item.id: item.details_path for item in _items(session) if item.id in {legacy, unlinked}
        }
        assert _incident_in(before[legacy]) is None, "the legacy fixture must carry no handle"

    _run(sqlite_engine)

    with sqlite_sessions() as session:
        rows = {item.id: item for item in _items(session)}

        moved = rows[linked]
        assert _incident_in(moved.details_path) == str(seeded.new())
        assert str(moved.correlation_group_id) == _incident_in(moved.details_path), (
            "the URL and the column are the same fact; a row where they disagree "
            "is a row whose reader lands on a card that is not there"
        )
        assert str(seeded.old(_CONFIG_A)) not in (moved.details_path or "")

        assert rows[legacy].details_path == before[legacy], (
            "a link that never carried a handle must come back byte-identical"
        )
        assert rows[legacy].correlation_group_id == seeded.new()
        assert rows[unlinked].details_path is None
        assert rows[unlinked].correlation_group_id == seeded.new()


def test_a_non_metric_scope_is_not_touched_at_all(
    sqlite_engine: Engine, sqlite_sessions: sessionmaker[Session]
) -> None:
    """Only ``metric`` went project-global. Every other scope keys on its own scan.

    Re-keying an event-scoped incident would orphan its ack for nothing: the
    code still hashes the firing config for it, so the migration would move the
    row onto an id no collection will ever compute again.
    """
    with sqlite_sessions() as session:
        seeded = _seed(session, config_ids=(_CONFIG_A,))
        event_ref = str(uuid.uuid4())
        event_handle = metrics_dispatch._correlation_group_id(
            scan_config_id=_CONFIG_A,
            rule_id=seeded.rule_id,
            scope_type=_EVENT,
            scope_ref=event_ref,
            direction=_SPIKE,
        )
        event_item = _add_delivery_item(
            session,
            seeded,
            config_id=_CONFIG_A,
            correlation_group_id=event_handle,
            scope_type=_EVENT,
            scope_ref=event_ref,
        )

    with sqlite_sessions() as session:
        before = session.get(AlertDeliveryItem, event_item)
        assert before is not None
        before_path = before.details_path
        assert _incident_in(before_path) == str(event_handle)

    rewritten = _run(sqlite_engine)

    assert rewritten == 0
    with sqlite_sessions() as session:
        after = session.get(AlertDeliveryItem, event_item)
        assert after is not None
        assert after.correlation_group_id == event_handle
        assert after.details_path == before_path


def test_a_direction_flip_stays_two_incidents(
    sqlite_engine: Engine, sqlite_sessions: sessionmaker[Session]
) -> None:
    """``direction`` is part of the key, so a scope that dropped and spiked keeps two.

    tripl-0zpq.108 argues this at length for the buffer: a single
    ``correlation_group_id`` column cannot name two incidents, and folding them
    would leave one of the operator's decisions unhonourable. A migration that
    collapsed on ``(rule, scope)`` alone would do exactly that folding.
    """
    with sqlite_sessions() as session:
        seeded = _seed(session, config_ids=(_CONFIG_A, _CONFIG_B))
        for config_id in (_CONFIG_A, _CONFIG_B):
            for direction in (AnomalyDirection.spike.value, AnomalyDirection.drop.value):
                _add_delivery_item(
                    session,
                    seeded,
                    config_id=config_id,
                    correlation_group_id=seeded.old(config_id, direction=direction),
                    direction=direction,
                )

    assert _run(sqlite_engine) == 4

    with sqlite_sessions() as session:
        handles = {item.direction: item.correlation_group_id for item in _items(session)}
        assert handles == {
            AnomalyDirection.spike.value: seeded.new(direction=AnomalyDirection.spike.value),
            AnomalyDirection.drop.value: seeded.new(direction=AnomalyDirection.drop.value),
        }
        assert len(set(handles.values())) == 2


# --------------------------------------------------------------------------
# The merge: which human decision survives N-onto-1
# --------------------------------------------------------------------------


def test_decision_rank_prefers_silence_then_recency_then_a_stable_id() -> None:
    """``_decision_rank`` as a rule, stated where it can be read in one screen.

    Term 1 is the silence guarantee — the survivor is suppressing whenever ANY
    colliding row was, so the deploy pages nobody. Term 2 is parity with
    ``_apply_inbox_action_to_state``, which resolves two decisions on one card
    by keeping the later one, a mute included. Term 3 only makes the choice
    reproducible.
    """
    migration = _migration()

    early = datetime(2026, 9, 1, tzinfo=UTC)
    late = datetime(2026, 9, 10, tzinfo=UTC)

    def row(status: str, acted_at: datetime | None, id_int: int) -> Any:
        return SimpleNamespace(status=status, acted_at=acted_at, id=uuid.UUID(int=id_int))

    # A suppressing status outranks ``open`` even when ``open`` acted later.
    assert migration._decision_rank(row("muted", early, 1)) > migration._decision_rank(
        row("open", late, 2)
    )
    # Among suppressing statuses the LATER action wins — an acknowledgement over
    # an older indefinite mute included. The revision's docstring argues that
    # trade; read it before promoting ``muted``.
    assert migration._decision_rank(row("acknowledged", late, 1)) > migration._decision_rank(
        row("muted", early, 2)
    )
    # A never-acted row sorts below every acted one rather than raising on a
    # ``None`` comparison.
    assert migration._decision_rank(row("muted", None, 1)) < migration._decision_rank(
        row("muted", early, 2)
    )
    # Fully tied, the id breaks it, so two runs pick the same survivor.
    assert migration._decision_rank(row("muted", early, 2)) > migration._decision_rank(
        row("muted", early, 1)
    )


def test_the_merge_keeps_the_decision_the_inbox_would_be_holding(
    sqlite_engine: Engine, sqlite_sessions: sessionmaker[Session]
) -> None:
    """Three cards become one, and it is the acknowledgement — not the open row.

    Also pins the two aggregates the revision names: ``last_seen_at`` takes the
    group max (the incident was last seen when the latest scan saw it) and
    ``false_positive_count`` the SUM, because the ratchet counts clicks and an
    incident marked a false positive under two scans was marked twice.
    ``muted_until`` is deliberately neither read nor written — the survivor
    keeps its own.
    """
    with sqlite_sessions() as session:
        seeded = _seed(session)
        for config_id in seeded.config_ids:
            _add_delivery_item(
                session, seeded, config_id=config_id, correlation_group_id=seeded.old(config_id)
            )
        muted = _add_state(
            session,
            seeded,
            correlation_group_id=seeded.old(_CONFIG_A),
            status="muted",
            acted_at=datetime(2026, 9, 1, 9, 0),
            last_seen_at=datetime(2026, 9, 12, 9, 0),
            false_positive_count=1,
            muted_until=datetime(2026, 12, 1, 9, 0),
        )
        acknowledged = _add_state(
            session,
            seeded,
            correlation_group_id=seeded.old(_CONFIG_B),
            status="acknowledged",
            acted_at=datetime(2026, 9, 5, 9, 0),
            last_seen_at=datetime(2026, 9, 14, 9, 0),
            false_positive_count=2,
            note="on it",
        )
        still_open = _add_state(
            session,
            seeded,
            correlation_group_id=seeded.old(_CONFIG_C),
            status="open",
            acted_at=datetime(2026, 9, 9, 9, 0),
            last_seen_at=datetime(2026, 9, 13, 9, 0),
            false_positive_count=4,
        )

    # 3 delivery items + (1 survivor + 2 losers).
    assert _run(sqlite_engine) == 6

    with sqlite_sessions() as session:
        rows = _states(session)
        assert [row.id for row in rows] == [acknowledged], (
            "the suppressing status outranks the more recent open row, and the "
            "later of the two suppressing ones wins"
        )
        survivor = rows[0]
        assert survivor.correlation_group_id == seeded.new()
        assert survivor.status == "acknowledged"
        assert survivor.note == "on it"
        assert survivor.last_seen_at == datetime(2026, 9, 14, 9, 0)
        assert survivor.false_positive_count == 1 + 2 + 4
        assert survivor.muted_until is None, (
            "the survivor keeps ITS OWN muted_until, which an acknowledgement "
            "already NULLs — the merge does not carry the loser's mute across"
        )
        assert session.get(AlertCorrelationState, muted) is None
        assert session.get(AlertCorrelationState, still_open) is None


def test_a_decision_whose_deliveries_are_gone_is_left_where_it_is(
    sqlite_engine: Engine, sqlite_sessions: sessionmaker[Session]
) -> None:
    """An orphan state has no discoverable scope, so nothing can re-key it.

    A correlation state knows only its project and its id; the scope lives on
    the delivery items, and if those are gone there is no rule/scope_ref/
    direction to hash. It was already an orphan by any reading, and
    ``_silenced_orphan_group_ids`` keeps a silenced one visible in the Inbox —
    so leaving it is strictly better than guessing.
    """
    stranded_handle = uuid.uuid4()
    with sqlite_sessions() as session:
        seeded = _seed(session, config_ids=(_CONFIG_A,))
        _add_delivery_item(
            session, seeded, config_id=_CONFIG_A, correlation_group_id=seeded.old(_CONFIG_A)
        )
        orphan = _add_state(
            session,
            seeded,
            correlation_group_id=stranded_handle,
            status="muted",
            acted_at=datetime(2026, 9, 1, 9, 0),
        )

    _run(sqlite_engine)

    with sqlite_sessions() as session:
        row = session.get(AlertCorrelationState, orphan)
        assert row is not None
        assert row.correlation_group_id == stranded_handle
        assert row.status == "muted"


# --------------------------------------------------------------------------
# The ordering the non-deferrable unique constraint forces
# --------------------------------------------------------------------------


def test_the_constraint_that_forces_that_ordering_really_bites(
    sqlite_engine: Engine, sqlite_sessions: sessionmaker[Session]
) -> None:
    """``uq_alert_correlation_state_project_group`` rejects the second row.

    Without this, the ordering assertion below is decorative: a DELETE that
    happened to run after the UPDATE would pass on a database that never
    checked. Asserted against the live schema rather than against the model
    metadata, because the constraint the test needs is the one the test
    database actually built.
    """
    shared = uuid.uuid4()
    with sqlite_sessions() as session:
        seeded = _seed(session, config_ids=(_CONFIG_A,))
        _add_state(session, seeded, correlation_group_id=shared, status="open", acted_at=None)
        second = _add_state(
            session,
            seeded,
            correlation_group_id=uuid.uuid4(),
            status="open",
            acted_at=None,
        )

    with pytest.raises(IntegrityError), sqlite_engine.begin() as connection:
        connection.execute(
            sa.update(AlertCorrelationState.__table__)
            .where(AlertCorrelationState.__table__.c.id == second)
            .values(correlation_group_id=shared)
        )


def test_the_losers_are_deleted_before_the_survivor_is_repointed(
    sqlite_engine: Engine, sqlite_sessions: sessionmaker[Session]
) -> None:
    """The one ordering in this revision that is not a preference.

    The collision is reachable whenever a row is ALREADY sitting on the target
    handle — a re-run, a partially applied deploy, or a collection that landed
    between the code and the migration. Here the row on the new handle is
    ``open`` and a row on an old handle is ``acknowledged``, so the survivor is
    NOT the one already on the target: re-pointing it first would put two rows
    of one project on one handle, which
    :func:`test_the_constraint_that_forces_that_ordering_really_bites` shows the
    database refuses. Both halves are asserted — the pass completes, and the
    DELETE is observed to precede the UPDATE — because "it did not raise" alone
    would also be true of an implementation that got lucky with row order.
    """
    with sqlite_sessions() as session:
        seeded = _seed(session, config_ids=(_CONFIG_A, _CONFIG_B))
        for config_id in (_CONFIG_A, _CONFIG_B):
            _add_delivery_item(
                session, seeded, config_id=config_id, correlation_group_id=seeded.old(config_id)
            )
        already_on_target = _add_state(
            session,
            seeded,
            correlation_group_id=seeded.new(),
            status="open",
            acted_at=datetime(2026, 9, 12, 9, 0),
        )
        acknowledged = _add_state(
            session,
            seeded,
            correlation_group_id=seeded.old(_CONFIG_A),
            status="acknowledged",
            acted_at=datetime(2026, 9, 5, 9, 0),
        )
        loser = _add_state(
            session,
            seeded,
            correlation_group_id=seeded.old(_CONFIG_B),
            status="open",
            acted_at=None,
        )

    statements: list[str] = []

    @event.listens_for(sqlite_engine, "before_cursor_execute")
    def _record(_conn, _cursor, statement, _params, _context, _executemany) -> None:  # noqa: ANN001
        statements.append(" ".join(statement.split()))

    try:
        # 2 delivery items + (1 survivor + 2 losers).
        assert _run(sqlite_engine) == 5
    finally:
        event.remove(sqlite_engine, "before_cursor_execute", _record)

    states = [
        index
        for index, statement in enumerate(statements)
        if "alert_correlation_states" in statement
    ]
    deletes = [index for index in states if statements[index].startswith("DELETE")]
    updates = [index for index in states if statements[index].startswith("UPDATE")]
    assert deletes and updates, statements
    assert max(deletes) < min(updates), (
        "the survivor may not be re-pointed onto the target while another row "
        f"of the same project still holds it: {statements}"
    )

    with sqlite_sessions() as session:
        rows = _states(session)
        assert [row.id for row in rows] == [acknowledged]
        assert rows[0].correlation_group_id == seeded.new()
        assert session.get(AlertCorrelationState, already_on_target) is None
        assert session.get(AlertCorrelationState, loser) is None


# --------------------------------------------------------------------------
# Re-running, and going back
# --------------------------------------------------------------------------


def test_a_second_run_rewrites_nothing_and_does_not_merge_twice(
    sqlite_engine: Engine, sqlite_sessions: sessionmaker[Session]
) -> None:
    """Rows already on the target are skipped, so a re-run is a no-op.

    ``false_positive_count`` is the assertion that can actually fail here: it is
    a SUM over the colliding rows, so a second pass that re-discovered the same
    group would double it silently. A row count would not notice.
    """
    with sqlite_sessions() as session:
        seeded = _seed(session, config_ids=(_CONFIG_A, _CONFIG_B))
        for config_id in (_CONFIG_A, _CONFIG_B):
            _add_delivery_item(
                session, seeded, config_id=config_id, correlation_group_id=seeded.old(config_id)
            )
        _add_state(
            session,
            seeded,
            correlation_group_id=seeded.old(_CONFIG_A),
            status="muted",
            acted_at=datetime(2026, 9, 1, 9, 0),
            false_positive_count=3,
        )
        _add_state(
            session,
            seeded,
            correlation_group_id=seeded.old(_CONFIG_B),
            status="open",
            acted_at=None,
            false_positive_count=5,
        )

    assert _run(sqlite_engine) == 4

    with sqlite_sessions() as session:
        after_first = {
            item.id: (item.correlation_group_id, item.details_path) for item in _items(session)
        }
        states = _states(session)
        assert len(states) == 1
        assert states[0].false_positive_count == 8

    assert _run(sqlite_engine) == 0, "every row is already on the target"

    with sqlite_sessions() as session:
        assert {
            item.id: (item.correlation_group_id, item.details_path) for item in _items(session)
        } == after_first
        states = _states(session)
        assert len(states) == 1
        assert states[0].false_positive_count == 8, "the sum must not be re-applied"


def test_the_downgrade_puts_the_column_and_the_link_back_together(
    sqlite_engine: Engine, sqlite_sessions: sessionmaker[Session]
) -> None:
    """A rollback re-keys onto the project's LOWEST config, and takes the URL with it.

    Not reversible in EFFECT — the old handles were derived from whichever scan
    collected first, which nothing records, and decisions merged on the way up
    are not un-merged. What must hold is that the reverted code can find these
    incidents: the handle has to be the one ``_correlation_group_id`` computes
    on config A's own collections, and the deep link has to name it too, or the
    rollback strands every live link in a different direction from the one the
    upgrade was written to prevent.
    """
    with sqlite_sessions() as session:
        seeded = _seed(session, config_ids=(_CONFIG_A, _CONFIG_B))
        for config_id in (_CONFIG_A, _CONFIG_B):
            _add_delivery_item(
                session, seeded, config_id=config_id, correlation_group_id=seeded.old(config_id)
            )
        _add_pending_item(session, seeded, correlation_group_id=seeded.old(_CONFIG_B))
        _add_state(
            session,
            seeded,
            correlation_group_id=seeded.old(_CONFIG_A),
            status="muted",
            acted_at=datetime(2026, 9, 1, 9, 0),
        )

    _run(sqlite_engine)

    migration = _migration()
    with sqlite_engine.begin() as connection:
        anchors = migration._anchor_by_project(connection)
        assert anchors[seeded.project_id] == _CONFIG_A, (
            "the anchor is the project's lowest config id, compared as strings"
        )
        rewritten = migration.rekey_metric_incidents(connection, partition_for=anchors.get)

    # 2 delivery items + 1 buffered row + the single surviving state.
    assert rewritten == 4

    reverted = seeded.old(_CONFIG_A)
    with sqlite_sessions() as session:
        for item in _items(session):
            assert item.correlation_group_id == reverted
            assert _incident_in(item.details_path) == str(reverted), (
                "a rollback that moved the column without the link leaves the "
                "send path shipping a URL for an incident nothing answers for"
            )
        buffered = session.execute(select(AlertPendingItem)).scalars().all()
        assert [row.correlation_group_id for row in buffered] == [reverted]
        states = _states(session)
        assert [row.correlation_group_id for row in states] == [reverted]


def test_a_project_with_no_scan_configs_left_keeps_the_project_global_handle(
    sqlite_engine: Engine, sqlite_sessions: sessionmaker[Session]
) -> None:
    """``partition_for`` answers ``None`` for a project with no anchor to name.

    Which is also what the reverted code would do with those rows, since it
    could not collect for that project at all. The alternative — skipping them —
    would mean the downgrade silently left some projects re-keyed and some not.
    """
    with sqlite_sessions() as session:
        seeded = _seed(session, config_ids=(_CONFIG_A,))
        _add_delivery_item(
            session, seeded, config_id=_CONFIG_A, correlation_group_id=seeded.old(_CONFIG_A)
        )

    _run(sqlite_engine)

    with sqlite_sessions() as session:
        # The delivery cascades with its config; the item cascades with the
        # delivery. Re-seed the history the way a deleted scan leaves it: the
        # buffered row survives, because a metric row anchors on no config.
        _add_pending_item(session, seeded, correlation_group_id=seeded.new())
        session.execute(
            sa.delete(ScanConfig.__table__).where(ScanConfig.__table__.c.id == _CONFIG_A)
        )
        session.commit()

    migration = _migration()
    with sqlite_engine.begin() as connection:
        anchors = migration._anchor_by_project(connection)
        assert seeded.project_id not in anchors
        assert migration.rekey_metric_incidents(connection, partition_for=anchors.get) == 0

    with sqlite_sessions() as session:
        buffered = session.execute(select(AlertPendingItem)).scalars().all()
        assert [row.correlation_group_id for row in buffered] == [seeded.new()]


# --------------------------------------------------------------------------
# The alembic entrypoints
# --------------------------------------------------------------------------


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_neither_direction_touches_a_non_postgresql_bind(
    monkeypatch: pytest.MonkeyPatch, direction: str
) -> None:
    """Postgres-only, per repo convention (``a1b2c3d4e5f6``).

    The unit suite builds its schema from ``Base.metadata.create_all`` and never
    runs the chain, so a pass that ran on the SQLite bind would be doing work
    nobody asked for against a database that has no rows to fix. The bind-taking
    functions stay dialect-neutral — that is what every test above drives — but
    the entrypoints must return.
    """
    migration = _load_migration(f"metric_incident_rekey_guard_{direction}", _MIGRATION)
    monkeypatch.setattr(
        migration.op, "get_bind", lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    )

    def _fail(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("the pass must not run off PostgreSQL")

    monkeypatch.setattr(migration, "rekey_metric_incidents", _fail)
    monkeypatch.setattr(migration, "_anchor_by_project", _fail)

    getattr(migration, direction)()


# --------------------------------------------------------------------------
# The PostgreSQL arm — see the module docstring for what only it can prove
# --------------------------------------------------------------------------


@pytest.fixture
def pg_engine() -> Iterator[Engine]:
    engine = _engine_or_skip()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_the_rekey_runs_on_postgresql_over_asyncpg(pg_engine: Engine) -> None:
    """The real pass, on the real driver, against native enum columns.

    Everything asserted here is already asserted on SQLite. What is NEW is that
    it compiled and executed at all: ``scope_type`` and ``direction`` are native
    PostgreSQL enums here, so ``_is_metric``'s explicit CAST is load-bearing
    rather than a no-op, and alembic reaches this database through asyncpg,
    which prepares each statement and asks the server to deduce one type per
    parameter. That is the class of failure ``f3a9b7c15d2e``'s deploy died on,
    before touching a row, with the whole upgrade rolled back.

    The merge also runs here against a genuinely NOT DEFERRABLE
    ``uq_alert_correlation_state_project_group`` — SQLite has no deferrable
    constraints, so the ordering the delete-before-repoint exists for is only
    really tested on this arm.
    """
    sessions = sessionmaker(pg_engine, expire_on_commit=False)
    with sessions() as session:
        seeded = _seed(session)
        for config_id in seeded.config_ids:
            _add_delivery_item(
                session, seeded, config_id=config_id, correlation_group_id=seeded.old(config_id)
            )
        _add_pending_item(session, seeded, correlation_group_id=seeded.old(_CONFIG_A))
        # One decision already on the target, so the delete-before-repoint
        # ordering is exercised against the server's own constraint.
        _add_state(
            session,
            seeded,
            correlation_group_id=seeded.new(),
            status="open",
            acted_at=datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
        )
        _add_state(
            session,
            seeded,
            correlation_group_id=seeded.old(_CONFIG_B),
            status="muted",
            acted_at=datetime(2026, 9, 5, 9, 0, tzinfo=UTC),
            last_seen_at=datetime(2026, 9, 14, 9, 0, tzinfo=UTC),
            false_positive_count=2,
        )

    migration = _migration()
    async_engine = create_async_engine(_asyncpg_url())
    try:
        async with async_engine.begin() as connection:
            rewritten = await connection.run_sync(migration.rekey_metric_incidents)
    finally:
        await async_engine.dispose()

    # 3 delivery items + 1 buffered row + (1 survivor + 1 loser).
    assert rewritten == 6

    with sessions() as session:
        for item in _items(session):
            assert item.correlation_group_id == seeded.new()
            assert _incident_in(item.details_path) == str(seeded.new())
        buffered = session.execute(select(AlertPendingItem)).scalars().all()
        assert [row.correlation_group_id for row in buffered] == [seeded.new()]
        states = _states(session)
        assert len(states) == 1
        assert states[0].status == "muted"
        assert states[0].correlation_group_id == seeded.new()
        assert states[0].false_positive_count == 2
