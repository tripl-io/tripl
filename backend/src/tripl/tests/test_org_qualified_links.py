"""Every server-emitted link names the organization (F20 PR8, tripl-oam4.7, tripl-0chm).

Since F20 PR5 a project slug is unique only inside its organization, so a
``/p/{slug}/...`` link opens whichever project of that slug the READER's
organization holds. Links are ``/o/{org}/p/{slug}/...`` now, built by
``services.project_links``; stored text that feeds a hash stays org-less and is
completed on read (critique #21).
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.alert_templates import ALERT_MESSAGE_FORMAT_PLAIN, validate_template_configuration
from tripl.models import Base
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.event_photo import EventPhoto
from tripl.models.incident_summary import IncidentSummary
from tripl.models.organization import Organization
from tripl.models.project import Project
from tripl.models.search_document import SearchDocument
from tripl.schemas.activity import ActivityItemResponse
from tripl.services import activity_service, event_photo_service
from tripl.services._dependency_model import url_for
from tripl.services.alerting_rendering import render_firings_message
from tripl.services.incident_summary_facts import PROMPT_VERSION, SummaryFact, compute_facts_hash
from tripl.services.incident_summary_service import _stored_fact
from tripl.services.project_links import (
    legacy_project_path,
    project_link,
    project_url,
    qualify_project_path,
)
from tripl.tests._members import add_org_member
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks.alerts_messages import _build_email_subject, _render_delivery_message
from tripl.worker.tasks.metrics.urls import _build_item_paths
from tripl.worker.tasks.notification_producers import _url_forms

ACME_ID = uuid.UUID("00000000-0000-0000-0000-0000000a8e01")
ACME = "links-acme"
BASE = "https://tripl.example"

# --- the helper -------------------------------------------------------------


def test_project_url_is_relative_or_absolute() -> None:
    assert project_url("acme", "web") == "/o/acme/p/web"
    assert project_url("acme", "web", "/alerting") == "/o/acme/p/web/alerting"
    assert project_url("acme", "web", "alerting") == "/o/acme/p/web/alerting"
    assert project_url("acme", "web", "?tab=1") == "/o/acme/p/web?tab=1"
    assert project_url("acme", "web", "/alerting", base_url=f"{BASE}/") == (
        f"{BASE}/o/acme/p/web/alerting"
    )


def test_qualify_project_path_only_touches_org_less_paths() -> None:
    assert legacy_project_path("web", "/events") == "/p/web/events"
    assert qualify_project_path("acme", "/p/web/events") == "/o/acme/p/web/events"
    # Idempotent, and blind to anything that is not an org-less project path.
    assert qualify_project_path("acme", "/o/acme/p/web/events") == "/o/acme/p/web/events"
    assert qualify_project_path("acme", "/invite/abc") == "/invite/abc"
    assert qualify_project_path("acme", f"{BASE}/p/web") == f"{BASE}/p/web"


# --- alert deep links -----------------------------------------------------------


@pytest.mark.parametrize(
    ("scope_type", "group", "expected"),
    [
        ("event", True, "/alerting/"),
        ("release_regression", False, "/alerting/"),
        ("event", False, "/monitoring/event/"),
        ("metric", False, "/monitoring/metric/"),
        ("project_total", False, "/monitoring/project-total/"),
    ],
)
def test_every_alert_deep_link_names_the_org(scope_type: str, group: bool, expected: str) -> None:
    details, monitoring = _build_item_paths(
        "web",
        org_slug=ACME,
        app_base_url=BASE,
        scope_type=scope_type,
        scope_ref=str(uuid.uuid4()),
        event_id=uuid.uuid4() if scope_type in {"event", "release_regression"} else None,
        delivery_id=uuid.uuid4(),
        correlation_group_id=uuid.uuid4() if group else None,
    )
    links = [link for link in (details, monitoring) if link]
    assert links
    assert all(link.startswith(f"{BASE}/o/{ACME}/p/web/") for link in links)
    assert any(expected in link for link in links)


def test_org_slug_is_a_template_variable_beside_project_slug() -> None:
    validate_template_configuration(
        destination_type="slack",
        message_format=None,
        message_template="${org_slug}/${project_slug}",
        items_template=None,
    )
    rule = AlertRule(
        id=uuid.uuid4(),
        name="Drops",
        message_template="${org_slug}|${project_slug}",
        items_template=None,
        message_format=None,
    )
    destination = AlertDestination(id=uuid.uuid4(), type="slack", name="ops")
    project = Project(id=uuid.uuid4(), name="Web", slug="web")
    _items, message = render_firings_message(
        rule, [], destination=destination, project=project, org_slug=ACME
    )
    # project_slug is not repurposed: it stays the bare project slug.
    assert message == f"{ACME}|web"


@pytest.fixture
def sync_session(tmp_path: Path) -> Iterator[Session]:
    engine = create_engine(f"sqlite:///{tmp_path / 'links.db'}")
    Base.metadata.create_all(engine)
    try:
        with sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
    finally:
        engine.dispose()


def _acme_project_sync(session: Session, slug: str = "web") -> Project:
    session.add(Organization(id=ACME_ID, slug=ACME, name="Links Acme"))
    session.flush()
    project = Project(id=uuid.uuid4(), name=slug.title(), slug=slug, organization_id=ACME_ID)
    session.add(project)
    session.commit()
    return project


def test_a_delivered_alert_and_its_email_subject_render_org_slug(sync_session: Session) -> None:
    """The worker path, not the simulate path: without the worker's ``org_slug``
    a real alert would carry the literal ``${org_slug}``."""
    project = _acme_project_sync(sync_session)
    destination = AlertDestination(id=uuid.uuid4(), project_id=project.id, type="slack", name="ops")
    rule = AlertRule(
        id=uuid.uuid4(),
        destination_id=destination.id,
        name="Drops",
        message_template="${org_slug}/${project_slug}",
        message_format=ALERT_MESSAGE_FORMAT_PLAIN,
    )
    delivery = AlertDelivery(
        id=uuid.uuid4(),
        project_id=project.id,
        scan_config_id=uuid.uuid4(),
        destination_id=destination.id,
        rule_id=rule.id,
        channel="slack",
        matched_count=0,
    )
    delivery.items = []
    message, _format = _render_delivery_message(
        delivery,
        destination=destination,
        rule=rule,
        scan_name="Scan",
        project=project,
        session=sync_session,
    )
    assert message == f"{ACME}/web"

    # The subject builder takes no session: it reads the project's own.
    subject = _build_email_subject(
        template="${org_slug}",
        rule=rule,
        project=project,
        matched_count=0,
        destination=destination,
        message_format=ALERT_MESSAGE_FORMAT_PLAIN,
    )
    assert subject == ACME


# --- dependency / impact links ----------------------------------------------------


def test_dependency_url_hint_names_the_org() -> None:
    metric_id = uuid.uuid4()
    assert url_for("web", "metric", metric_id, org_slug=ACME) == (
        f"/o/{ACME}/p/web/monitoring/metric/{metric_id}"
    )
    assert url_for("web", "relation", metric_id, org_slug=ACME) == f"/o/{ACME}/p/web/relations"
    # Without the organization there is no link rather than an ambiguous one.
    assert url_for("web", "metric", metric_id, org_slug=None) is None


# --- notifications --------------------------------------------------------------


def test_signal_dedupe_matches_the_pre_upgrade_url_too() -> None:
    url = f"/o/{ACME}/p/web/monitoring/event/1?signal=2026-09-27T09:00:00Z"
    assert _url_forms(url) == (url, "/p/web/monitoring/event/1?signal=2026-09-27T09:00:00Z")
    assert _url_forms("/p/web/x") == ("/p/web/x",)


# --- incident summaries ------------------------------------------------------------


def _migration() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[3]
        / "alembic"
        / "versions"
        / "d4e8f1a2b3c5_incident_summary_hash_without_hrefs.py"
    )
    spec = importlib.util.spec_from_file_location("_rehash_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_hash_migration_computes_what_the_service_computes() -> None:
    migration = _migration()
    assert migration._PROMPT_VERSION == PROMPT_VERSION
    facts = [
        SummaryFact(1, "incident", "A drop.", "/p/web/alerting?incident=1"),
        SummaryFact(2, "scope", "Scope — Checkout.", None),
    ]
    stored = [fact.to_payload() for fact in facts]
    new_hash = migration._hash(stored, with_href=False)
    assert new_hash == compute_facts_hash(facts)
    # The same facts with org-qualified links are still the same summary.
    relinked = [
        SummaryFact(fact.id, fact.kind, fact.text, qualify_project_path(ACME, fact.href or ""))
        for fact in facts
    ]
    assert compute_facts_hash(relinked) == new_hash
    assert migration._hash(stored, with_href=True) != new_hash


def _summary_row(
    session: Session, project_id: uuid.UUID, facts_hash: str, facts: list
) -> uuid.UUID:
    row = IncidentSummary(
        id=uuid.uuid4(),
        project_id=project_id,
        correlation_group_id=uuid.uuid4(),
        facts_hash=facts_hash,
        facts=facts,
        sentences=[],
    )
    session.add(row)
    session.commit()
    return row.id


def _stored(session: Session, row_id: uuid.UUID) -> IncidentSummary:
    row = session.get(IncidentSummary, row_id, populate_existing=True)
    assert row is not None
    return row


def test_the_hash_migration_rewrites_matching_rows_and_round_trips(
    sync_session: Session,
) -> None:
    migration = _migration()
    project = _acme_project_sync(sync_session)
    facts = [
        SummaryFact(1, "incident", "A drop.", "/p/web/alerting?incident=1"),
        SummaryFact(2, "scope", "Scope — Checkout.", None),
    ]
    stored = [fact.to_payload() for fact in facts]
    fresh = _summary_row(sync_session, project.id, migration._hash(stored, with_href=True), stored)
    # Cached under something else (an older prompt, a hand edit): stays as stale as it was.
    stale = _summary_row(sync_session, project.id, "0" * 64, stored)

    migration._rehash_up(sync_session.connection())
    sync_session.commit()
    assert _stored(sync_session, fresh).facts_hash == compute_facts_hash(facts)
    assert _stored(sync_session, stale).facts_hash == "0" * 64

    # A summary written after the upgrade stores org-qualified hrefs. Downgrade
    # strips them, so the pre-PR8 hash (over freshly built /p/ hrefs) matches.
    qualified = [
        {**fact, "href": qualify_project_path(ACME, fact["href"]) if fact["href"] else None}
        for fact in stored
    ]
    after = _summary_row(sync_session, project.id, compute_facts_hash(facts), qualified)

    migration._rehash_down(sync_session.connection())
    sync_session.commit()
    old_hash = migration._hash(stored, with_href=True)
    assert _stored(sync_session, fresh).facts_hash == old_hash
    downgraded = _stored(sync_session, after)
    assert downgraded.facts_hash == old_hash
    assert downgraded.facts == stored
    assert _stored(sync_session, stale).facts_hash == "0" * 64


def test_the_downgrade_strips_only_org_qualified_project_paths() -> None:
    migration = _migration()
    assert migration._legacy_href(f"/o/{ACME}/p/web/alerting") == "/p/web/alerting"
    assert migration._legacy_href("/p/web/alerting") == "/p/web/alerting"
    assert migration._legacy_href(f"/o/{ACME}/settings") == f"/o/{ACME}/settings"
    assert migration._legacy_href(None) is None


def test_a_stored_legacy_href_is_org_qualified_on_read() -> None:
    fact = _stored_fact(
        {"id": 1, "kind": "incident", "text": "A drop.", "href": "/p/web/alerting?incident=1"},
        ACME,
    )
    assert fact.href == f"/o/{ACME}/p/web/alerting?incident=1"
    assert _stored_fact({"id": 2, "kind": "scope", "text": "x", "href": None}, ACME).href is None


# --- event photo file URL (tripl-0chm) ------------------------------------------------


async def test_photo_file_url_names_the_org_of_the_request() -> None:
    photo = EventPhoto(
        id=uuid.uuid4(),
        event_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        kind=event_photo_service.PHOTO_KIND_PHOTO,
        storage_backend=None,
        storage_key=None,
    )
    tail = f"/projects/web/events/{photo.event_id}/photos/{photo.id}/file"
    assert await event_photo_service.url_for(photo, "web", ACME) == f"/api/v1/orgs/{ACME}{tail}"
    assert await event_photo_service.url_for(photo, "web") == f"/api/v1{tail}"


# --- against the database: a second organization ----------------------------------------


@pytest.fixture
async def acme_member(client: AsyncClient) -> AsyncClient:
    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 200, me.text
    async with TestSessionLocal() as session:
        session.add(Organization(id=ACME_ID, slug=ACME, name="Links Acme"))
        await session.commit()
        await add_org_member(session, uuid.UUID(me.json()["id"]), "admin", org_id=ACME_ID)
    return client


async def _acme_project(client: AsyncClient, slug: str) -> uuid.UUID:
    created = await client.post(
        f"/api/v1/orgs/{ACME}/projects", json={"name": slug.title(), "slug": slug}
    )
    assert created.status_code == 201, created.text
    return uuid.UUID(created.json()["id"])


async def test_search_routes_name_the_org_but_stored_routes_do_not(
    acme_member: AsyncClient,
) -> None:
    client = acme_member
    await _acme_project(client, "web")
    made = await client.post(
        f"/api/v1/orgs/{ACME}/projects/web/event-types",
        json={"name": "zebra_checkout", "display_name": "Zebra checkout"},
    )
    assert made.status_code == 201, made.text

    found = await client.get(
        f"/api/v1/orgs/{ACME}/projects/web/search", params={"q": "zebra", "types": "event_type"}
    )
    assert found.status_code == 200, found.text
    items = found.json()["items"]
    assert items
    assert all(item["route_path"].startswith(f"/o/{ACME}/p/web/") for item in items)

    # Critique #21: the stored route (part of content_hash) is org-less, so this
    # release re-embeds nothing.
    async with TestSessionLocal() as session:
        stored = (await session.scalars(select(SearchDocument.route_path))).all()
    assert stored
    assert all(path.startswith("/p/web/") for path in stored)


async def test_project_link_reads_the_projects_own_org(acme_member: AsyncClient) -> None:
    project_id = await _acme_project(acme_member, "shop")
    async with TestSessionLocal() as session:
        link = await project_link(session, project_id, "/branches/b1")
        with pytest.raises(LookupError):
            await project_link(session, uuid.uuid4())
    assert link == f"/o/{ACME}/p/shop/branches/b1"


async def test_activity_target_paths_name_each_projects_org(acme_member: AsyncClient) -> None:
    project_id = await _acme_project(acme_member, "feed")
    item = ActivityItemResponse(
        id="scan-job:1",
        project_id=project_id,
        project_slug="feed",
        project_name="Feed",
        type="scan",
        severity="low",
        title="Scan completed",
        detail="",
        occurred_at=datetime(2026, 9, 28, tzinfo=UTC),
        target_path="/p/feed/scans",
    )
    async with TestSessionLocal() as session:
        (qualified,) = await activity_service._org_qualified(session, [item])
    assert qualified.target_path == f"/o/{ACME}/p/feed/scans"
    assert item.target_path == "/p/feed/scans"
