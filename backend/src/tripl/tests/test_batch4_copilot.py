"""Batch 4, the Copilot review of PR #169: the metric-state creation race.

``uq_alert_rule_state_metric_scope`` — the partial unique index batch 4 added
over the NULL ``scan_config_id`` space — DETECTS a duplicate project-global
metric state. It does not make the creation of one race-safe, and adding it
turned a survivable bug into a fatal one.

The race is real and it is the ordinary configuration, not a corner:

* ``metrics/schedule.check_metrics_due`` dispatches
  ``collect_metrics.delay(str(config.id), str(job.id))`` PER SCAN CONFIG, so a
  project with two configs runs two ``collect_metrics`` in two workers at once.
* A ``metric`` scope is project-global: its anomaly row carries a NULL
  ``scan_config_id`` and so does its ``AlertRuleState``, which is exactly why
  both of those runs load, and want to create, the SAME state row
  (``dispatch._scope_partition_id``). ``signals._get_active_metric_anomaly_candidates``
  already says so in as many words: "This pass runs once per scan config while
  the anomalies it judges are project-global and share ONE ``AlertRuleState``
  row".
* ``dispatch._prepare_alert_deliveries`` loaded that row and, on a miss,
  inserted it — a check-then-act with the two halves in different statements
  and, across workers, in different transactions.

Before the index, the loser of that race merely wrote a SECOND project-global
row: one cooldown clock per config, which is the duplicate the index exists to
forbid. With the index the loser's INSERT violates it, and nothing wraps
``_prepare_alert_deliveries`` in a savepoint — so the IntegrityError takes the
whole ``collect_metrics`` run down with it, discarding the anomaly
recalculation, the cooldown updates and every delivery for every scope of that
scan, on every collection for as long as the scope keeps firing. That is an
earlier blast radius reached from a different direction, and batch 4 is
what made it reachable.

The fix is ``dispatch._claim_rule_state``: an INSERT ... ON CONFLICT DO NOTHING
that converges instead of raising, with the row re-read afterwards — the shape
``_buffer_pending_items`` uses for the digest buffer and ``detect.py`` for its
anomaly rows. Its RETURNING clause is the SEND CLAIM: the run whose INSERT
actually landed owns the notification, and the run that lost adopts the row
already there and stays quiet for that collection instead of shipping the peer's
alert a second time.

Three properties are pinned below, because a fix for one is easy to write
without the others:

* CONVERGENCE. A racing sibling no longer kills the collection.
* NO DUPLICATE. The loser does not deliver. A "catch the IntegrityError,
  re-select, then carry on through the ordinary gate" fix passes the first and
  fails this one: the peer's row has ``last_notified_at IS NULL`` at that
  instant, which is the arm of the gate that sends.
* NOTHING LOST. ``last_notified_at`` is stamped only on a SUCCESSFUL send, so a
  peer whose delivery never lands leaves the NULL that makes the NEXT collection
  deliver. Silence for one tick, not a swallowed alert.

The race cannot be staged with two real connections here: SQLite admits one
writer at a time, and ``_prepare_alert_deliveries`` has already opened a write
transaction (``_retire_config_anchored_metric_states`` issues a DELETE) by the
time the claim runs, so a competitor on a second connection would meet
"database is locked" rather than the unique index. The competitor is therefore
written through the SAME connection at the last line before the claim, which is
precisely where a concurrent commit sits — the pattern
``test_event_generator.py`` uses for the event-identity race, for the same
reason and with the same caveat. What is under test is what the database does
when a second INSERT of the same key arrives, and that is identical either way.
The PostgreSQL-only half — which unique index the ON CONFLICT names — is pinned
by compiling the real statement against the real dialect.

THE SAME BUG ONE TABLE OVER: the incident row.

``_touch_correlation_state`` was the other select-then-``session.add`` in this
module, and batch 4 made it collidable for the same reason. Its key is
``uq_alert_correlation_state_project_group`` on ``(project_id,
correlation_group_id)``, and since an earlier fix the handle in that second column
is the same in every worker by design: it hashes the scope's PARTITION, which for
a project-global ``metric`` scope is one shared NULL. Before that change each
scan hashed its own firing config and the two runs wrote DIFFERENT rows, so the
collision could not happen — agreeing on one handle is the fix, and it is also
what made the row's creation a race.

``_claim_rule_state`` does NOT settle this one. It serialises the creation of a
rule STATE, and ``AlertRuleState`` carries no ``direction`` while the incident
handle does. So a scope that dropped in the morning and spikes in the afternoon
reuses the single state row it already has — both racing runs take the branch for
an EXISTING state, where nothing is claimed — and then touches a second,
brand-new handle. That flip is the ordinary case: 106 of 223 live scopes fired in
BOTH directions inside one day (``dispatch._buffer_pending_items``).

The constraint is a plain, non-deferrable UNIQUE — ``f4a8d3c72e19`` deletes
losers before re-pointing a survivor precisely because of it — so the loser's
INSERT raised IntegrityError at the next flush and took the collection with it,
exactly as the rule-state race did.

Same fix, same shape: INSERT ... ON CONFLICT DO NOTHING, then re-read. Two
details are specific to this table and are pinned below:

* DO NOTHING is not a preference here, it protects the OPERATOR. That row holds
  an inbox decision — ``status``, ``muted_until``, ``note``, ``acted_at``,
  ``false_positive_count`` — and a collection has no business restating any of
  it.
* ``last_seen_at`` advances in PYTHON after the re-read, not in a SET clause,
  because the two backends have no shared spelling of a maximum. PostgreSQL's
  ``GREATEST`` IGNORES a NULL argument; SQLite's two-argument ``max()`` RETURNS
  NULL if any argument is NULL. On a row whose ``last_seen_at`` is still NULL —
  which is every row an operator created from the Inbox, see
  ``_alerting_deliveries._get_or_create_correlation_state`` — the identical SET
  clause would set the stamp in production and erase it in this suite.

No network, and the DB is a throwaway sqlite file.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.bucketing import to_utc
from tripl.models import Base
from tripl.models.alert_correlation_state import AlertCorrelationState
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_rule import AlertRule
from tripl.models.alert_rule_state import AlertRuleState
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.scan_config import ScanConfig
from tripl.worker.tasks.metrics import dispatch as metrics_dispatch

_METRIC_SCOPE = "metric"


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """A file-backed SQLite database carrying the partial index under test.

    The unit suite builds its schema from ``Base.metadata.create_all`` and never
    runs the (Postgres-gated) migration chain, so ``sqlite_where`` on
    ``uq_alert_rule_state_metric_scope`` is the only reason SQLite enforces the
    NULL-space uniqueness at all — without it the race below would write two
    rows quietly and every assertion here would be vacuous.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'batch4_copilot.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def _seed_metric_alerting(session: Session) -> tuple[ScanConfig, AlertRule, str, datetime]:
    """A spiked catalog metric with a rule that alerts on it, plus its scope key."""
    from tripl.tests.test_metric_anomaly_scope import _add_rule, _seed_spiked_metric

    config, metric = _seed_spiked_metric(session)
    rule = _add_rule(session, config, include_metrics=True)
    anomaly = session.execute(
        select(MetricAnomaly).where(MetricAnomaly.scope_ref == str(metric.id))
    ).scalar_one()
    assert anomaly.scan_config_id is None, "a metric anomaly is project-global; that is the premise"
    return config, rule, str(metric.id), anomaly.bucket


def _sibling_worker_opens_the_state_first(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    *,
    rule: AlertRule,
    scope_ref: str,
    opened_at: datetime,
    last_anomaly_bucket: datetime,
) -> tuple[uuid.UUID, dict[str, int]]:
    """Land a peer's state row in the window the race lives in.

    ``_prepare_alert_deliveries`` calls ``_scope_partition_id`` on the statement
    immediately above ``_claim_rule_state``, so patching it puts the competitor
    between the load that found nothing and the insert that acts on it — the
    window a sibling config's worker commits in. Injected ONCE, and the counter
    is returned so the caller can assert the injection actually happened: a test
    whose competitor silently stopped landing would pass for the wrong reason.

    The row is written through the session's own connection rather than a second
    one, and the row it writes is what a peer that has just opened this incident
    holds: active, never yet notified (``last_notified_at`` is stamped by
    ``alerts._stamp_rule_state`` only on a successful send, which has not
    happened yet).
    """
    original = metrics_dispatch._scope_partition_id
    competitor_id = uuid.uuid4()
    raced = {"count": 0}

    def _partition_after_a_competitor_lands(scope_type: str, *, config_id: uuid.UUID) -> Any:
        if raced["count"] == 0:
            raced["count"] += 1
            session.connection().execute(
                insert(AlertRuleState.__table__).values(
                    id=competitor_id,
                    rule_id=rule.id,
                    scan_config_id=None,
                    scope_type=_METRIC_SCOPE,
                    scope_ref=scope_ref,
                    is_active=True,
                    opened_at=opened_at,
                    closed_at=None,
                    last_anomaly_bucket=last_anomaly_bucket,
                    last_notified_at=None,
                )
            )
        return original(scope_type, config_id=config_id)

    monkeypatch.setattr(
        metrics_dispatch, "_scope_partition_id", _partition_after_a_competitor_lands
    )
    return competitor_id, raced


def _states(session: Session) -> list[AlertRuleState]:
    return list(session.execute(select(AlertRuleState)).scalars())


def test_a_racing_sibling_config_no_longer_kills_the_whole_collection(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE GATE. The loser converges on the peer's row and ships nothing.

    Revert ``_claim_rule_state`` to the plain ``current_state =
    AlertRuleState(...)`` / ``session.add(current_state)`` it replaced and this
    call raises ``IntegrityError`` on ``uq_alert_rule_state_metric_scope`` —
    inside ``_prepare_alert_deliveries``, at the first flush after the add — so
    the test reddens on the call itself, before any assertion.

    The assertions then pin the three things a narrower fix would miss:

    * ONE state row, and it is the PEER's — the loser adopted it rather than
      minting its own.
    * ``opened_at`` and ``last_notified_at`` untouched. That row IS the shared
      cooldown clock, so DO NOTHING rather than DO UPDATE: an upsert that wrote
      this run's ``now`` over the peer's ``opened_at`` would be the earlier
      clock reset wearing another hat.
    * ``last_anomaly_bucket`` not rewound. The peer's is deliberately seeded a
      bucket AHEAD of the anomaly this run matched, so a converge path that
      assigned instead of taking the ``max`` would move it backwards.

    And no delivery. That is the assertion that separates this fix from
    "catch the IntegrityError and carry on": the peer's row has
    ``last_notified_at IS NULL``, which is the arm of the send gate that fires,
    so a converge-then-fall-through would put the peer's alert on the wire twice.
    """
    with sync_session_factory() as session:
        config, rule, scope_ref, bucket = _seed_metric_alerting(session)
        peer_opened_at = bucket - timedelta(hours=1)
        peer_bucket = bucket + timedelta(hours=1)
        competitor_id, raced = _sibling_worker_opens_the_state_first(
            monkeypatch,
            session,
            rule=rule,
            scope_ref=scope_ref,
            opened_at=peer_opened_at,
            last_anomaly_bucket=peer_bucket,
        )

        delivery_ids = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)

        assert raced["count"] == 1, "the competitor never landed; this test proves nothing"
        assert delivery_ids == [], "the peer that opened the incident owns its first notification"
        assert session.execute(select(AlertDelivery)).scalars().all() == []

        states = _states(session)
        assert len(states) == 1, "one project-global scope, one state row"
        state = states[0]
        assert state.id == competitor_id, "converged on the row already there"
        assert state.scan_config_id is None
        assert to_utc(state.opened_at) == peer_opened_at, (
            "the shared cooldown clock was overwritten"
        )
        assert state.last_notified_at is None
        assert to_utc(state.last_anomaly_bucket) == peer_bucket, (
            "a later bucket must not be rewound"
        )

        # The run's transaction is still usable, which is the half the
        # IntegrityError destroyed: everything else this collection did survives.
        session.commit()


def test_the_alert_the_loser_withheld_is_delivered_by_the_next_collection(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Staying quiet is one tick of silence, not a swallowed alert.

    The suppression above is only safe because of what ``last_notified_at``
    means: ``alerts._stamp_rule_state`` writes it on a SUCCESSFUL send and
    nowhere else. So if the peer's delivery never lands — the worker dies, the
    webhook 400s — the NULL it left is exactly the condition the send gate's
    ``current_state.last_notified_at is None`` arm fires on, and the next
    collection delivers the incident.

    Written as a second collection on the same session with no competitor: the
    first call converged and sent nothing, the second finds the state through the
    ordinary load and sends. Delete that ``is None`` arm from the gate and this
    goes red while the test above still passes, which is why both are here.
    """
    with sync_session_factory() as session:
        config, rule, scope_ref, bucket = _seed_metric_alerting(session)
        _competitor_id, raced = _sibling_worker_opens_the_state_first(
            monkeypatch,
            session,
            rule=rule,
            scope_ref=scope_ref,
            opened_at=bucket,
            last_anomaly_bucket=bucket,
        )

        assert metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None) == []
        assert raced["count"] == 1
        session.commit()

        # Next tick. The competitor is not re-injected (the patch fires once),
        # so this is the ordinary path over the row the race left behind.
        second = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)

        assert len(second) == 1, "a peer's unsent alert must not be lost with its state row"
        assert len(_states(session)) == 1


def test_an_uncontended_metric_scope_still_opens_its_state_and_alerts(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The common case, and the reason the claim cannot be a quiet no-op.

    ``created`` is read off RETURNING, and RETURNING yields nothing when ON
    CONFLICT suppressed the insert. Name the wrong conflict target, or infer
    ``created`` from what the re-read finds instead of from the insert, and every
    first-ever metric incident converges onto itself, ``should_send`` never
    becomes True, and the product stops alerting on catalog metrics entirely —
    silently, with the race tests above still green.
    """
    with sync_session_factory() as session:
        config, _rule, scope_ref, bucket = _seed_metric_alerting(session)

        delivery_ids = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)

        assert len(delivery_ids) == 1
        state = session.execute(select(AlertRuleState)).scalars().one()
        assert state.scope_type == _METRIC_SCOPE
        assert state.scope_ref == scope_ref
        assert state.scan_config_id is None, "a metric scope belongs to the project, not a scan"
        assert state.is_active is True
        assert state.opened_at is not None
        assert to_utc(state.last_anomaly_bucket) == bucket
        assert state.last_notified_at is None, "only a successful send stamps the clock"


def test_the_claim_converges_on_both_partitions_and_keeps_them_apart(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """``_claim_rule_state`` directly: create, then converge, in each partition.

    TWO conflict targets, because there are two partitions. A config-scoped scope
    conflicts on the four-column ``uq_alert_rule_state_scope``; a ``metric`` one
    stores NULL there, SQL treats NULLs as DISTINCT, so that constraint can never
    fire for it and the claim has to name the partial index instead. Getting the
    metric arm wrong is the whole finding; getting the CONFIG arm wrong would
    break every event-scope alert instead, so both are exercised here rather than
    only the one that was filed.

    The config-scoped race is not hypothetical either: two runs of the SAME
    config overlap whenever a manual replay meets a scheduled collection, which
    is the concurrency ``detect.py`` upserts its anomaly rows against.

    The last two assertions keep the arms honest about each other. A NULL row and
    a config row for the SAME ``(rule, scope_type, scope_ref)`` are different
    scopes and must coexist: a metric arm that conflicted against the
    four-column constraint, or a config arm that reached the partial index, would
    make one of these claims adopt the other's row.
    """
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
    bucket = datetime(2026, 9, 18, 11, 0, 0, tzinfo=UTC)
    with sync_session_factory() as session:
        config, rule, scope_ref, _bucket = _seed_metric_alerting(session)

        for partition in (None, config.id):
            scope_type = _METRIC_SCOPE if partition is None else "event"
            first, created = metrics_dispatch._claim_rule_state(
                session,
                rule_id=rule.id,
                scan_config_id=partition,
                scope_type=scope_type,
                scope_ref=scope_ref,
                now=now,
                bucket=bucket,
            )
            assert created is True and first is not None
            assert first.scan_config_id == partition

            second, created_again = metrics_dispatch._claim_rule_state(
                session,
                rule_id=rule.id,
                scan_config_id=partition,
                scope_type=scope_type,
                scope_ref=scope_ref,
                # A later ``now``: were this a DO UPDATE it would land on the row
                # and the assertion below would catch it.
                now=now + timedelta(hours=6),
                bucket=bucket + timedelta(hours=6),
            )
            assert created_again is False, "the second claim must converge, not create"
            assert second is not None and second.id == first.id
            assert second.opened_at == first.opened_at
            assert to_utc(second.last_anomaly_bucket) == to_utc(first.last_anomaly_bucket)

        # Same rule, same scope_ref, different partitions: two rows, no adoption.
        rows = {state.scan_config_id: state for state in _states(session)}
        assert set(rows) == {None, config.id}


def test_the_claim_names_the_partial_index_on_postgresql() -> None:
    """The half SQLite cannot prove: WHICH index each arm conflicts against.

    Production is PostgreSQL, and there ``ON CONFLICT (rule_id, scope_type,
    scope_ref)`` without the index predicate does not merely dedupe wrongly — it
    fails to plan at all ("there is no unique or exclusion constraint matching
    the ON CONFLICT specification"), which would be every metric alert raising on
    every collection. SQLite accepts both spellings, so no test that executes can
    tell them apart; this one compiles the statement the helper ACTUALLY builds
    against the real dialect instead of rebuilding it here, so it cannot drift
    from the code it stands for.

    ``DO NOTHING`` is asserted as SQL too, not just as behaviour: it is what
    guarantees a converging run cannot write over the peer's cooldown clock.
    """

    class _NoRow:
        def scalar_one_or_none(self) -> None:
            return None

    captured: list[Any] = []

    def _capture(statement: Any, *args: Any, **kwargs: Any) -> _NoRow:
        captured.append(statement)
        return _NoRow()

    # A PostgreSQL bind that is never connected to: the helper branches on
    # ``session.bind.dialect.name``, and every statement is intercepted before it
    # can reach a socket.
    engine = create_engine("postgresql+psycopg://tripl:tripl@localhost:1/unused")
    session = Session(engine)
    session.execute = _capture  # type: ignore[method-assign]

    rule_id, config_id = uuid.uuid4(), uuid.uuid4()
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
    for partition, scope_type in ((None, _METRIC_SCOPE), (config_id, "event")):
        metrics_dispatch._claim_rule_state(
            session,
            rule_id=rule_id,
            scan_config_id=partition,
            scope_type=scope_type,
            scope_ref="ref",
            now=now,
            bucket=now,
        )

    # Two statements per call: the claim, then the re-read.
    metric_claim, _metric_read, config_claim, _config_read = captured
    metric_sql = str(metric_claim.compile(dialect=postgresql.dialect()))
    config_sql = str(config_claim.compile(dialect=postgresql.dialect()))

    assert (
        "ON CONFLICT (rule_id, scope_type, scope_ref) WHERE scan_config_id IS NULL DO NOTHING"
        in metric_sql
    ), metric_sql
    assert "ON CONFLICT (rule_id, scan_config_id, scope_type, scope_ref) DO NOTHING" in config_sql
    # RETURNING is the send claim; without it ``created`` cannot be answered.
    assert metric_sql.rstrip().endswith("RETURNING alert_rule_states.id")
    assert config_sql.rstrip().endswith("RETURNING alert_rule_states.id")
    assert "DO UPDATE" not in metric_sql and "DO UPDATE" not in config_sql

    session.close()
    engine.dispose()


# --------------------------------------------------------------------------
# The incident row: ``_touch_correlation_state``.
# --------------------------------------------------------------------------


def _metric_incident_handle(session: Session, *, rule: AlertRule, scope_ref: str) -> uuid.UUID:
    """The handle this project-global scope's incident will be filed under.

    Rebuilt through ``_correlation_group_id`` rather than read back off the
    delivery the run writes, and that is not a style choice: every read of this
    session AUTOFLUSHES, and the whole point of the test below is to reach a
    moment where the collection's own write is still pending. So the handle has
    to be known BEFORE the collection runs.

    ``_get_active_metric_anomaly_candidates`` hands ``_prepare_alert_deliveries``
    the ``MetricAnomaly`` rows themselves, so the ``direction`` read here is the
    same attribute the run hashes.
    """
    anomaly = session.execute(
        select(MetricAnomaly).where(MetricAnomaly.scope_ref == scope_ref)
    ).scalar_one()
    return metrics_dispatch._correlation_group_id(
        scan_config_id=None,
        rule_id=rule.id,
        scope_type=_METRIC_SCOPE,
        scope_ref=scope_ref,
        direction=anomaly.direction,
    )


def _a_sibling_worker_opens_the_incident(
    session: Session,
    *,
    project_id: uuid.UUID,
    correlation_group_id: uuid.UUID,
    seen_at: datetime,
) -> uuid.UUID:
    """Land a peer's incident row, through the statement a peer actually emits.

    The competitor runs ``_touch_correlation_state``'s own claim — an ON CONFLICT
    DO NOTHING insert — because that is what the sibling worker is running: the
    fixed code, concurrently. Writing it as a plain INSERT instead would make the
    competitor itself the thing that raises once the fix is in, which would test
    the test rather than the code.

    Through ``session.connection()`` rather than ``session.add``, for a reason
    the assertions depend on: ``Connection.execute`` does not autoflush, so
    whatever the caller has pending stays pending. That is what keeps the
    reverted code's ``session.add(AlertCorrelationState(...))`` unwritten until
    the explicit flush below, which is the window the race lives in.

    Same connection as the run, not a second one, for the reason the module
    docstring gives for the rule-state competitor: SQLite admits one writer and
    the collection is already holding the write lock. What is under test is what
    the database does when a second INSERT of this key arrives.
    """
    peer_id = uuid.uuid4()
    session.connection().execute(
        sqlite_insert(AlertCorrelationState.__table__)
        .values(
            id=peer_id,
            project_id=project_id,
            correlation_group_id=correlation_group_id,
            status="open",
            last_seen_at=seen_at,
        )
        .on_conflict_do_nothing(index_elements=["project_id", "correlation_group_id"])
    )
    return peer_id


def _correlation_states(session: Session) -> list[AlertCorrelationState]:
    return list(session.execute(select(AlertCorrelationState)).scalars())


def test_a_racing_sibling_config_no_longer_kills_the_incident_row(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """THE GATE for the second race. Two workers, one incident handle, one row.

    Revert ``_touch_correlation_state`` to the ``select`` / ``if state is None:
    session.add(AlertCorrelationState(...))`` it replaced and the
    ``session.flush()`` below raises ``IntegrityError`` on
    ``uq_alert_correlation_state_project_group`` — so this reddens on the flush,
    before any assertion.

    Dropping only the ``DO NOTHING``, leaving a bare INSERT, does NOT redden this
    test: there is no row when the collection runs, so that insert lands and it
    is the peer's converging one that swallows the collision. What catches that
    edit is the SQL-shape test at the bottom of this file, which is why it is
    there.

    That flush is where the reverted code fails because ``session.add`` DEFERS:
    the old select missed, the object went into the session unwritten, and the
    peer landed in the gap before anything flushed it. On this seed — one
    destination, one rule, one anomaly — ``_prepare_alert_deliveries`` issues no
    further statement after the touch, so that gap reaches the line below. In
    production it closes at whatever the collection does next: a second rule, a
    second destination, or the commit. Nothing about the collision depends on how
    long it lasts — only on a second INSERT of this key arriving inside it.

    The assertions then pin what converging has to leave behind:

    * ONE row for the handle, which is what the constraint is for.
    * It is THIS run's row, not the peer's: this run's INSERT went first and won,
      and the peer's DO NOTHING is the half that converged. The sibling test
      above is the mirror image — there the peer won — and between them they say
      the outcome does not depend on who arrives first.
    * ``last_seen_at`` is the bucket that was actually seen, so the incident is
      not left looking unseen by the collection that just delivered it.
    """
    with sync_session_factory() as session:
        config, rule, scope_ref, bucket = _seed_metric_alerting(session)
        handle = _metric_incident_handle(session, rule=rule, scope_ref=scope_ref)

        delivery_ids = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        assert len(delivery_ids) == 1, "the first-ever metric incident still alerts"

        peer_id = _a_sibling_worker_opens_the_incident(
            session,
            project_id=config.project_id,
            correlation_group_id=handle,
            seen_at=bucket,
        )
        session.flush()
        session.commit()

        states = _correlation_states(session)
        assert len(states) == 1, "one incident, one inbox row"
        state = states[0]
        assert state.correlation_group_id == handle
        assert state.id != peer_id, "this run's INSERT landed; the sibling's converged onto it"
        assert state.status == "open"
        assert to_utc(state.last_seen_at) == bucket


def test_converging_leaves_the_operators_inbox_decision_alone(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """DO NOTHING, because the row already there belongs to a human.

    This one does NOT redden on the revert — the old select-then-add found an
    existing row and updated only ``last_seen_at`` too. It is here for the fix
    that would be written next: an ON CONFLICT DO UPDATE, which is the shape
    ``_buffer_pending_items`` uses one table over and the obvious thing to copy.
    On THIS table it would hand every collection a write over the inbox decision
    — the mute a user set for a week, the note they left, the false-positive
    clicks the detector ratchet counts — and none of those are a collection's to
    restate.

    The second touch arrives with an OLDER bucket, which is the late collection
    ``_buffer_pending_items`` guards its own upsert against. ``last_seen_at`` must
    not rewind: an incident that reads as last seen before it actually was is one
    the Inbox sorts and ages wrongly.
    """
    with sync_session_factory() as session:
        config, _rule, _scope_ref, bucket = _seed_metric_alerting(session)
        handle = uuid.uuid4()
        acted_at = bucket - timedelta(days=1)
        muted_until = bucket + timedelta(days=7)
        session.add(
            AlertCorrelationState(
                id=uuid.uuid4(),
                project_id=config.project_id,
                correlation_group_id=handle,
                status="muted",
                muted_until=muted_until,
                note="these screens are switched off",
                false_positive_count=3,
                last_seen_at=bucket,
                acted_at=acted_at,
            )
        )
        session.commit()

        metrics_dispatch._touch_correlation_state(
            session,
            project_id=config.project_id,
            correlation_group_id=handle,
            seen_at=bucket + timedelta(hours=6),
        )
        metrics_dispatch._touch_correlation_state(
            session,
            project_id=config.project_id,
            correlation_group_id=handle,
            seen_at=bucket - timedelta(hours=6),
        )
        session.commit()

        states = _correlation_states(session)
        assert len(states) == 1, "converged, did not mint a second row"
        state = states[0]
        assert state.status == "muted", "a collection may not end a mute"
        assert to_utc(state.muted_until) == muted_until
        assert state.note == "these screens are switched off"
        assert state.false_positive_count == 3
        assert to_utc(state.acted_at) == acted_at
        assert to_utc(state.last_seen_at) == bucket + timedelta(hours=6), "and never rewound"


def test_an_incident_an_operator_opened_gets_its_first_last_seen(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The NULL a SQL-side maximum would erase instead of set.

    ``_alerting_deliveries._get_or_create_correlation_state`` mints a row from
    ``project_id``, the handle and ``status`` alone when an operator acts on an
    incident from the Inbox, so ``last_seen_at IS NULL`` on a row a collection
    then touches is an ordinary production state, not a contrivance.

    It is exactly where the two backends part company if the advance is moved
    into a SET clause. PostgreSQL's ``GREATEST`` ignores a NULL argument and
    would set the stamp; SQLite's two-argument ``max()`` returns NULL when ANY
    argument is NULL and would leave the column empty — an incident that is
    firing right now and reads as never seen, in the suite that is supposed to
    catch that. Keeping the comparison in Python is what makes the two agree,
    and this test is what says so.
    """
    with sync_session_factory() as session:
        config, _rule, _scope_ref, bucket = _seed_metric_alerting(session)
        handle = uuid.uuid4()
        session.add(
            AlertCorrelationState(
                id=uuid.uuid4(),
                project_id=config.project_id,
                correlation_group_id=handle,
                status="acknowledged",
            )
        )
        session.commit()
        assert _correlation_states(session)[0].last_seen_at is None, "that is the premise"

        metrics_dispatch._touch_correlation_state(
            session,
            project_id=config.project_id,
            correlation_group_id=handle,
            seen_at=bucket,
        )
        session.commit()

        state = _correlation_states(session)[0]
        assert to_utc(state.last_seen_at) == bucket, (
            "a never-seen incident takes the bucket it just saw"
        )
        assert state.status == "acknowledged", "and keeps the decision that created it"


def test_the_touch_converges_against_the_project_group_constraint_on_postgresql() -> None:
    """The statement itself, compiled against the dialect production runs.

    SQLite accepts more spellings of ON CONFLICT than PostgreSQL does, so no test
    that merely EXECUTES can say the conflict target is right. This compiles the
    statement the helper actually builds — rather than a copy rebuilt here, which
    could drift from it — and reads the SQL.

    ``(project_id, correlation_group_id)`` are named as COLUMNS. ``ON CONFLICT ON
    CONSTRAINT uq_alert_correlation_state_project_group`` would be equivalent on
    PostgreSQL and a syntax error on SQLite, so the column list is the only
    spelling that is one statement for both.

    Reverting the helper to its select-then-``session.add`` reddens this on the
    unpacking below: the old shape emits ONE statement, a SELECT, and never an
    INSERT at all.

    ``DO NOTHING`` and the absence of ``GREATEST`` are asserted as SQL and not
    only as behaviour, so the intent is recorded next to the statement that
    carries it. The NULL test above does catch a SET clause, but only because
    SQLite's ``max()`` mishandles the NULL — an accident of the test backend, not
    of the one production runs, where ``GREATEST`` would have behaved and the
    write over the operator's row would have gone unnoticed.
    """

    class _NoRow:
        def scalar_one_or_none(self) -> None:
            return None

    captured: list[Any] = []

    def _capture(statement: Any, *args: Any, **kwargs: Any) -> _NoRow:
        captured.append(statement)
        return _NoRow()

    # A PostgreSQL bind that is never connected to: the helper branches on
    # ``session.bind.dialect.name``, and every statement is intercepted before it
    # can reach a socket. Both are captured because ``_NoRow`` answers "you did
    # not insert it", which is the arm that goes on to re-read.
    engine = create_engine("postgresql+psycopg://tripl:tripl@localhost:1/unused")
    session = Session(engine)
    session.execute = _capture  # type: ignore[method-assign]

    metrics_dispatch._touch_correlation_state(
        session,
        project_id=uuid.uuid4(),
        correlation_group_id=uuid.uuid4(),
        seen_at=datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC),
    )

    claim, _reread = captured
    claim_sql = str(claim.compile(dialect=postgresql.dialect()))

    assert "ON CONFLICT (project_id, correlation_group_id) DO NOTHING" in claim_sql, claim_sql
    # RETURNING is what answers "did this statement open the incident", which is
    # what lets the winning run skip the re-read entirely.
    assert claim_sql.rstrip().endswith("RETURNING alert_correlation_states.id")
    assert "DO UPDATE" not in claim_sql
    assert "GREATEST" not in claim_sql, "the maximum belongs in Python; see the module docstring"

    session.close()
    engine.dispose()
