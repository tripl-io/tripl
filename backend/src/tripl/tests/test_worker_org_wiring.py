"""Worker and background paths resolve the PROJECT's organization (F20 PR9 follow-up).

``test_org_settings`` proves the getters (``get_runtime_config_sync``,
``get_ai_config_for_project_sync``, ``alerts._resolve_email_context``). This
module proves the CALLERS: each path below is run end to end against a
database holding three distinct configurations — the operator's (which the
self-hosted default organization resolves to), organization Alpha's and
organization Beta's, plus env values that match none of them — and the
lowest-level consumer is replaced by a recorder:

* ``scan.run_scan``: the row limit handed to the warehouse analyzer;
* ``metrics.collect_metrics``: the scan and metrics row limits handed to the
  catalog sync;
* ``alerts_digest`` (weekly plan digest and sunset alert): the SMTP relay,
  password and sender handed to the mail sender, per project;
* ``alert_owner_notify.notify_owners`` (worker) and
  ``alert_owner_notify_service`` (manual "Notify owners"): the same, for the
  owner follow-up;
* ``incident_summary_service.ensure_summary``: the AI config handed to the LLM.

A path that read the default organization, the operator scope or env instead
of the project's organization would hand the recorder a value that is not the
project's organization's, and fail here.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from httpx import AsyncClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from tripl import crypto
from tripl.config import DEPLOYMENT_SELF_HOSTED, settings
from tripl.core.adapters.base import ColumnInfo
from tripl.middleware.org_context import OrgRef, bind_org, reset_org
from tripl.models import Base
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.app_setting import SERVICE_SETTINGS_KEY, AppSetting
from tripl.models.data_source import DataSource
from tripl.models.event_type import EventType
from tripl.models.event_type_owner import EventTypeOwner
from tripl.models.incident_summary import IncidentSummary
from tripl.models.organization import DEFAULT_ORG_ID, Organization, OrganizationMember
from tripl.models.project import Project
from tripl.models.project_member import ProjectMember
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob
from tripl.models.user import User
from tripl.services import alert_owner_notify_service, alert_owner_routing, incident_summary_service
from tripl.tests._incident_summary_seed import GOOD_REPLY, FakeLlm, registered_user, seed_incident
from tripl.tests._sqlite import enable_sqlite_foreign_keys
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks import alert_owner_notify, alerts, alerts_digest
from tripl.worker.tasks import scan as scan_tasks
from tripl.worker.tasks.metrics import tasks as metrics_tasks

ALPHA_ID = uuid.UUID("00000000-0000-0000-0000-00000000a11a")
BETA_ID = uuid.UUID("00000000-0000-0000-0000-00000000b22b")

# Row limits: env, operator (the self-hosted default organization), Alpha, Beta.
# All distinct; the organizations' are below the operator's ceiling so the
# clamp (critique #15) leaves them as set.
ENV_SCAN_LIMIT, ENV_METRICS_LIMIT = 50_000, 100_000
OPERATOR_SCAN_LIMIT, OPERATOR_METRICS_LIMIT = 10_000, 20_000
ALPHA_SCAN_LIMIT, ALPHA_METRICS_LIMIT = 500, 1_500
BETA_SCAN_LIMIT, BETA_METRICS_LIMIT = 600, 1_600

OPERATOR_RELAY = ("operator-relay.example.com", "operator-smtp-secret", "ops@example.com")
ALPHA_RELAY = ("alpha-relay.example.com", "alpha-smtp-secret", "alpha@example.com")
BETA_RELAY = ("beta-relay.example.com", "beta-smtp-secret", "beta@example.com")

ALPHA_AI = ("https://alpha-llm.example.com/v1", "sk-alpha", "alpha-model")


class _Halt(Exception):
    """Raised by a recorder once it has what it came for: nothing past it matters."""


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A real encryption key, a self-hosted instance and env values none of the scopes use."""
    monkeypatch.setattr(settings, "encryption_key", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()
    monkeypatch.setattr(settings, "deployment_mode", DEPLOYMENT_SELF_HOSTED)
    monkeypatch.setattr(settings, "org_settings_operator_fallback", "all")
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "ai_api_key", "sk-env")
    monkeypatch.setattr(settings, "ai_base_url", "https://env-llm.example.com/v1")
    monkeypatch.setattr(settings, "ai_model", "env-model")
    monkeypatch.setattr(settings, "smtp_host", "env-relay.example.com")
    monkeypatch.setattr(settings, "smtp_password", "env-smtp-secret")
    monkeypatch.setattr(settings, "smtp_from_address", "env@example.com")
    monkeypatch.setattr(settings, "scan_row_limit_default", ENV_SCAN_LIMIT)
    monkeypatch.setattr(settings, "metrics_row_limit_default", ENV_METRICS_LIMIT)
    yield
    crypto._fernet.cache_clear()


def _enc(value: str) -> str:
    return crypto.encrypt_value(value)


def _relay(relay: tuple[str, str, str]) -> dict[str, Any]:
    host, password, sender = relay
    return {"smtp_host": host, "smtp_password": _enc(password), "smtp_from_address": sender}


def _operator_values() -> dict[str, Any]:
    return {
        "ai_base_url": "https://operator-llm.example.com/v1",
        "ai_api_key": _enc("sk-operator"),
        "ai_model": "operator-model",
        "scan_row_limit_default": OPERATOR_SCAN_LIMIT,
        "metrics_row_limit_default": OPERATOR_METRICS_LIMIT,
        **_relay(OPERATOR_RELAY),
    }


def _alpha_values() -> dict[str, Any]:
    base_url, key, model = ALPHA_AI
    return {
        "ai_base_url": base_url,
        "ai_api_key": _enc(key),
        "ai_model": model,
        "scan_row_limit_default": ALPHA_SCAN_LIMIT,
        "metrics_row_limit_default": ALPHA_METRICS_LIMIT,
        **_relay(ALPHA_RELAY),
    }


def _beta_values() -> dict[str, Any]:
    return {
        "ai_base_url": "https://beta-llm.example.com/v1",
        "ai_api_key": _enc("sk-beta"),
        "ai_model": "beta-model",
        "scan_row_limit_default": BETA_SCAN_LIMIT,
        "metrics_row_limit_default": BETA_METRICS_LIMIT,
        **_relay(BETA_RELAY),
    }


def _settings_rows() -> tuple[list[Organization], list[AppSetting]]:
    orgs = [
        Organization(id=ALPHA_ID, slug="alpha", name="Alpha"),
        Organization(id=BETA_ID, slug="beta", name="Beta"),
    ]
    rows = [
        AppSetting(key=SERVICE_SETTINGS_KEY, value=_operator_values(), organization_id=None),
        AppSetting(key=SERVICE_SETTINGS_KEY, value=_alpha_values(), organization_id=ALPHA_ID),
        AppSetting(key=SERVICE_SETTINGS_KEY, value=_beta_values(), organization_id=BETA_ID),
    ]
    return orgs, rows


# ── the worker's (sync) database ────────────────────────────────────────────


@pytest.fixture
def worker_db(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'worker_org_wiring.db'}")
    enable_sqlite_foreign_keys(engine)
    # ``create_all`` seeds the default organization; Alpha and Beta join it.
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    orgs, rows = _settings_rows()
    with factory() as session:
        session.add_all(orgs)
        session.commit()
        session.add_all(rows)
        session.commit()
    try:
        yield factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _project(session: Session, org_id: uuid.UUID, slug: str) -> Project:
    project = Project(id=uuid.uuid4(), name=slug.title(), slug=slug, organization_id=org_id)
    session.add(project)
    session.flush()
    return project


def _scan_config(session: Session, project: Project, **extra: Any) -> ScanConfig:
    data_source = DataSource(
        id=uuid.uuid4(),
        organization_id=project.organization_id,
        name=f"Warehouse {project.slug}",
        db_type="clickhouse",
        host="warehouse.example.com",
        port=8123,
        database_name="analytics",
        username="default",
        password_encrypted="",
    )
    session.add(data_source)
    session.flush()
    config = ScanConfig(
        id=uuid.uuid4(),
        project_id=project.id,
        data_source_id=data_source.id,
        name="Scan",
        base_query="SELECT * FROM events",
        **extra,
    )
    session.add(config)
    session.flush()
    return config


class _Adapter:
    """A warehouse that connects, names its columns and holds no rows.

    ``get_time_bucketed_counts`` is the scheduled tick's one shared read
    (``shared_breakdown``); it records the limit it was handed and answers
    nothing.
    """

    def __init__(self) -> None:
        self.bucketed_limits: list[int] = []

    def test_connection(self) -> bool:
        return True

    def get_time_bucketed_counts(
        self, *_args: object, limit: int, **_kwargs: object
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        self.bucketed_limits.append(limit)
        return [], [], []

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        return [
            ColumnInfo(name="ts", type_name="DateTime"),
            ColumnInfo(name="event_name", type_name="String"),
        ]

    def close(self) -> None:
        return None


# ── scan: the row limit handed to the analyzer ──────────────────────────────


@pytest.mark.parametrize(
    ("org_id", "expected"),
    [
        (ALPHA_ID, ALPHA_SCAN_LIMIT),
        (BETA_ID, BETA_SCAN_LIMIT),
        # Self-hosted default organization == operator scope (critique #17).
        (DEFAULT_ORG_ID, OPERATOR_SCAN_LIMIT),
    ],
)
def test_run_scan_uses_the_projects_organization_row_limit(
    worker_db: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    org_id: uuid.UUID,
    expected: int,
) -> None:
    with worker_db() as session:
        project = _project(session, org_id, f"scan-{org_id.hex[-4:]}")
        config = _scan_config(session, project, event_type_column="event_name")
        job = ScanJob(id=uuid.uuid4(), scan_config_id=config.id, status="pending")
        session.add(job)
        session.commit()
        config_id, job_id = config.id, job.id

    seen: list[int] = []

    def analyzer(*_args: object, row_limit: int, **_kwargs: object) -> object:
        seen.append(row_limit)
        raise _Halt

    monkeypatch.setattr(scan_tasks, "_get_sync_session", worker_db)
    monkeypatch.setattr(scan_tasks, "_build_adapter", lambda ds: _Adapter())
    monkeypatch.setattr(scan_tasks, "analyze_cardinality_grouped", analyzer)

    with pytest.raises(_Halt):
        scan_tasks.run_scan.run(str(config_id), str(job_id))

    assert seen == [expected]


# ── metrics collection: the limits handed to the catalog sync ───────────────


@pytest.mark.parametrize(
    ("org_id", "expected"),
    [
        (ALPHA_ID, (ALPHA_SCAN_LIMIT, ALPHA_METRICS_LIMIT)),
        (BETA_ID, (BETA_SCAN_LIMIT, BETA_METRICS_LIMIT)),
        (DEFAULT_ORG_ID, (OPERATOR_SCAN_LIMIT, OPERATOR_METRICS_LIMIT)),
    ],
)
def test_collect_metrics_uses_the_projects_organization_row_limits(
    worker_db: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    org_id: uuid.UUID,
    expected: tuple[int, int],
) -> None:
    with worker_db() as session:
        project = _project(session, org_id, f"metrics-{org_id.hex[-4:]}")
        config = _scan_config(session, project, time_column="ts", interval="1h")
        session.commit()
        config_id = config.id

    seen: list[tuple[int, int]] = []

    def catalog_sync(*_args: object, **kwargs: Any) -> object:
        seen.append((kwargs["scan_row_limit"], kwargs["metrics_row_limit"]))
        raise _Halt

    window = (datetime(2026, 1, 1, 10, tzinfo=UTC), datetime(2026, 1, 1, 12, tzinfo=UTC), False)
    adapter = _Adapter()
    monkeypatch.setattr(metrics_tasks, "_get_sync_session", worker_db)
    monkeypatch.setattr(metrics_tasks, "_build_adapter", lambda ds: adapter)
    monkeypatch.setattr(metrics_tasks, "_resolve_collection_window", lambda *a, **k: window)
    monkeypatch.setattr(
        metrics_tasks, "_widen_for_held_buckets", lambda *a, time_from, **k: time_from
    )
    monkeypatch.setattr(metrics_tasks, "sync_catalog", catalog_sync)

    with pytest.raises(_Halt):
        metrics_tasks.collect_metrics.run(str(config_id))

    assert seen == [expected]
    # The tick's shared read (no declared lookback: the catalog window is the one
    # chunk) is bounded by the organization's metrics limit too.
    assert adapter.bucketed_limits == [expected[1] + 1]


# ── alert digests: the relay handed to the mail sender ──────────────────────


def _email_destination(session: Session, project: Project) -> None:
    session.add(
        AlertDestination(
            id=uuid.uuid4(),
            project_id=project.id,
            type="email",
            name=f"Mail {project.slug}",
            enabled=True,
            email_recipients="team@example.com",
        )
    )


@pytest.mark.parametrize(
    ("task_name", "builder_name"),
    [
        ("send_weekly_plan_digest", "_build_plan_digest_message"),
        ("check_deprecated_sunset_events", "_build_sunset_alert_message"),
    ],
)
def test_digests_use_each_projects_organization_relay(
    worker_db: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    task_name: str,
    builder_name: str,
) -> None:
    with worker_db() as session:
        for org_id, slug in (
            (ALPHA_ID, "digest-alpha"),
            (BETA_ID, "digest-beta"),
            (DEFAULT_ORG_ID, "digest-default"),
        ):
            _email_destination(session, _project(session, org_id, slug))
        session.commit()

    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(alerts_digest, "_get_sync_session", worker_db)
    monkeypatch.setattr(alerts_digest, builder_name, lambda *a, **k: "A synthetic digest body")
    monkeypatch.setattr(alerts_digest, "_send_email_message", lambda **kwargs: sent.append(kwargs))

    result = getattr(alerts_digest, task_name).run()

    assert result["failed"] == 0
    assert result["sent"] == 3
    relays = {
        str(mail["subject"]).split("]", 1)[0].lstrip("["): (
            mail["smtp_host"],
            mail["smtp_password"],
            mail["from_address"],
        )
        for mail in sent
    }
    assert relays == {
        "Digest-Alpha": ALPHA_RELAY,
        "Digest-Beta": BETA_RELAY,
        "Digest-Default": OPERATOR_RELAY,
    }


# ── owner follow-up (worker): the relay handed to the mail sender ───────────


def test_notify_owners_worker_uses_the_projects_organization_relay(
    worker_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with worker_db() as session:
        project = _project(session, ALPHA_ID, "owners-alpha")
        config = _scan_config(session, project, time_column="ts", interval="1h")
        owner = User(
            id=uuid.uuid4(),
            email="owner.alpha@example.com",
            name="Owner Alpha",
            password_hash="x",
        )
        event_type = EventType(
            id=uuid.uuid4(), project_id=project.id, name="page", display_name="Page"
        )
        session.add_all([owner, event_type])
        session.flush()
        # A row counts only for a member of the project's organization.
        session.add(OrganizationMember(organization_id=ALPHA_ID, user_id=owner.id, role="member"))
        session.add(ProjectMember(project_id=project.id, user_id=owner.id, role="editor"))
        session.add(EventTypeOwner(event_type_id=event_type.id, user_id=owner.id))
        destination = AlertDestination(
            id=uuid.uuid4(),
            project_id=project.id,
            type="slack",
            name="Main Slack",
            enabled=True,
            webhook_url_encrypted="secret",
        )
        session.add(destination)
        session.flush()
        rule = AlertRule(
            id=uuid.uuid4(),
            destination_id=destination.id,
            name="Everything",
            enabled=True,
            notify_owners=True,
            min_percent_delta=0,
        )
        session.add(rule)
        session.flush()
        delivery = AlertDelivery(
            id=uuid.uuid4(),
            project_id=project.id,
            scan_config_id=config.id,
            destination_id=destination.id,
            rule_id=rule.id,
            channel="slack",
            status="sent",
            matched_count=1,
            payload_snapshot={},
        )
        session.add(delivery)
        session.flush()
        session.add(
            AlertDeliveryItem(
                id=uuid.uuid4(),
                delivery_id=delivery.id,
                scope_type="event_type",
                scope_ref=str(event_type.id),
                scope_name="Page",
                event_type_id=event_type.id,
                bucket=datetime(2026, 9, 27, 9, tzinfo=UTC),
                direction="drop",
                actual_count=10,
                expected_count=40,
                absolute_delta=30,
                percent_delta=75,
                correlation_group_id=uuid.uuid4(),
            )
        )
        session.commit()
        delivery_id = delivery.id

    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(alert_owner_notify, "_get_sync_session", worker_db)
    # ``alert_owner_routing.send_owner_email`` sends through this at call time.
    monkeypatch.setattr(alerts, "_send_email_message", lambda **kwargs: sent.append(kwargs))

    result = alert_owner_notify.notify_owners.run(str(delivery_id))

    assert result["status"] == "done"
    assert result["sent"] == 1
    assert [(m["smtp_host"], m["smtp_password"], m["from_address"]) for m in sent] == [ALPHA_RELAY]
    assert sent[0]["recipients"] == ["owner.alpha@example.com"]


# ── the request-path twins, on the suite's async database ───────────────────


async def _seed_async_settings() -> None:
    orgs, rows = _settings_rows()
    async with TestSessionLocal() as session:
        session.add_all(orgs)
        await session.commit()
        session.add_all(rows)
        await session.commit()


@pytest.mark.asyncio
async def test_manual_notify_owners_uses_the_projects_organization_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_async_settings()
    async with TestSessionLocal() as session:
        project = Project(id=uuid.uuid4(), name="Manual", slug="manual", organization_id=ALPHA_ID)
        owner = User(
            id=uuid.uuid4(),
            email="manual.owner@example.com",
            name="Manual Owner",
            password_hash="x",
        )
        actor = User(
            id=uuid.uuid4(),
            email="manual.actor@example.com",
            name="Manual Actor",
            password_hash="x",
        )
        session.add_all([project, owner, actor])
        await session.commit()

    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(alerts, "_send_email_message", lambda **kwargs: sent.append(kwargs))

    async with TestSessionLocal() as session:
        loaded = await session.get(Project, project.id)
        loaded_actor = await session.get(User, actor.id)
        assert loaded is not None and loaded_actor is not None
        response = await alert_owner_notify_service._send_all(
            session,
            project=loaded,
            owners=[
                alert_owner_routing.OwnerContact(
                    user_id=owner.id, name="Manual Owner", email="manual.owner@example.com"
                )
            ],
            actor=loaded_actor,
            correlation_group_id=None,
            title="A synthetic title",
            headline="A synthetic headline",
            items_text="- a synthetic item",
            target_key="incident:synthetic",
        )
        await session.commit()

    assert [row.status for row in response.owners] == ["sent"]
    assert [(m["smtp_host"], m["smtp_password"], m["from_address"]) for m in sent] == [ALPHA_RELAY]


@pytest.mark.asyncio
async def test_incident_summary_calls_the_llm_with_the_projects_organization_config(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_async_settings()
    seeded = await seed_incident(client, "summary-alpha")
    # Built over HTTP in the default organization, then handed to Alpha.
    async with TestSessionLocal() as session:
        await session.execute(
            update(Project).where(Project.id == seeded.project_id).values(organization_id=ALPHA_ID)
        )
        await session.commit()
    # ``llm_service.is_enabled`` is NOT patched: Alpha's own config must switch AI on.
    llm = FakeLlm(GOOD_REPLY).install(monkeypatch)
    user = await registered_user()

    # Limit: this cannot tell the project's organization from the bound one.
    # ``resolve_project`` only finds a project of the BOUND organization, so
    # binding another org here would 404 before the AI config is read; the two
    # are equal on every reachable path. The config is read from
    # ``project.organization_id`` (incident_summary_service._disabled_reason).
    token = bind_org(OrgRef(id=ALPHA_ID, slug="alpha"))
    try:
        async with TestSessionLocal() as session:
            response = await incident_summary_service.ensure_summary(
                session, seeded.slug, seeded.group_id, user, force=True
            )
    finally:
        reset_org(token)

    assert response.state == "ready"
    assert len(llm.calls) == 1
    config = llm.calls[0]["config"]
    assert (config.ai_base_url, config.ai_api_key, config.ai_model) == ALPHA_AI
    async with TestSessionLocal() as session:
        stored = await session.scalar(
            select(IncidentSummary.model).where(IncidentSummary.project_id == seeded.project_id)
        )
    assert stored == "alpha-model"
