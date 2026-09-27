"""Project-scoped cache keys, realtime channels and plan locks are keyed by id (F20 PR3).

A slug names a project only inside its organization, so two organizations may
each own a project ``web``. Every piece of shared state that used to be keyed by
the slug (the Redis cache, the SSE channel with its replay ring, sequence and
epoch, and main's plan lock) is keyed by the project's id instead, and every
instance-level list by the organization's id. These tests pin that:

* no key builder accepts a slug, and two projects (or two organizations) never
  share a key, channel or prefix;
* a write invalidates the id-keyed entry it made stale and leaves another
  organization's entries alone;
* ``GET /projects/{slug}/events/stream`` subscribes to the id the slug resolves
  to within the request's organization, and main's plan lock is taken by id;
* the data-source list's defensive cap applies per organization.

Project slugs are still unique instance-wide (``uq_projects_slug`` goes in a
later PR), so the two-organization HTTP cases use two slugs; the key-builder
cases show a slug cannot reach a key at all.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl import cache, realtime
from tripl.api import deps
from tripl.middleware.org_context import OrgRef, bound_org
from tripl.models.data_source import DataSource, DBType
from tripl.models.domain_enums import OrganizationRole
from tripl.models.organization import (
    DEFAULT_ORG_ID,
    DEFAULT_ORG_SLUG,
    Organization,
    OrganizationMember,
)
from tripl.models.user import User
from tripl.services import datasource_service
from tripl.tests._project_ids import project_id_by_slug
from tripl.tests.conftest import TestSessionLocal

ACME = OrgRef(id=uuid.UUID("00000000-0000-0000-0000-0000000ac3e2"), slug="acme-rekey")
DEFAULT = OrgRef(id=DEFAULT_ORG_ID, slug=DEFAULT_ORG_SLUG)

# Each test binds the organization it needs, like the requests it makes.
pytestmark = pytest.mark.no_default_org

# Every project-scoped builder, by name, as a function of the project id alone.
_PROJECT_KEYS: dict[str, Callable[[uuid.UUID], str]] = {
    "key_signals_all": cache.key_signals_all,
    "key_signals_all_expanded": cache.key_signals_all_expanded,
    "key_event_types_list": cache.key_event_types_list,
    "key_meta_fields_list": cache.key_meta_fields_list,
    "key_project_health": lambda pid: cache.key_project_health(pid, 30),
    "key_health_sort": lambda pid: cache.key_health_sort(pid, "digest"),
    "prefix_signals": cache.prefix_signals,
    "prefix_event_types": cache.prefix_event_types,
    "prefix_meta_fields": cache.prefix_meta_fields,
    "prefix_health": cache.prefix_health,
    "channel": realtime.channel,
    "buffer_key": realtime._buffer_key,
    "seq_key": realtime._seq_key,
    "epoch_key": realtime._epoch_key,
}
# Each project-scoped key and the prefix its invalidation drops.
_KEY_UNDER_PREFIX: list[tuple[Callable[[uuid.UUID], str], Callable[[uuid.UUID], str]]] = [
    (cache.key_signals_all, cache.prefix_signals),
    (cache.key_signals_all_expanded, cache.prefix_signals),
    (cache.key_event_types_list, cache.prefix_event_types),
    (cache.key_meta_fields_list, cache.prefix_meta_fields),
    (lambda pid: cache.key_project_health(pid, 30), cache.prefix_health),
]


class _FakeCache:
    """A dict standing in for Redis behind ``cache``'s JSON helpers."""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}

    async def get_json(self, key: str) -> Any | None:
        return self.store.get(key)

    async def set_json(self, key: str, value: Any, ttl_seconds: int) -> None:
        self.store[key] = value

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.store.pop(key, None)

    async def delete_prefix(self, prefix: str) -> None:
        for key in [key for key in self.store if key.startswith(prefix)]:
            del self.store[key]


@pytest.fixture
def fake_cache(monkeypatch: pytest.MonkeyPatch) -> _FakeCache:
    fake = _FakeCache()
    for name in ("get_json", "set_json", "delete", "delete_prefix"):
        monkeypatch.setattr(cache, name, getattr(fake, name))
    return fake


async def _add_acme_membership() -> None:
    """Acme exists and the registered test user is a member of it."""
    async with TestSessionLocal() as session:
        user_id = await session.scalar(select(User.id).where(User.email == "test@example.com"))
        assert user_id is not None
        session.add(Organization(id=ACME.id, slug=ACME.slug, name="Acme"))
        await session.flush()
        session.add(
            OrganizationMember(
                organization_id=ACME.id, user_id=user_id, role=OrganizationRole.member.value
            )
        )
        await session.commit()


async def _create_project(client: AsyncClient, slug: str, *, org: OrgRef) -> uuid.UUID:
    base = "/api/v1" if org == DEFAULT else f"/api/v1/orgs/{org.slug}"
    resp = await client.post(f"{base}/projects", json={"name": slug.upper(), "slug": slug})
    assert resp.status_code == 201, resp.text
    project_id = await project_id_by_slug(slug, org_id=org.id)
    assert str(project_id) == resp.json()["id"]
    return project_id


# ── Key builders ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(_PROJECT_KEYS))
def test_two_projects_never_share_a_key(name: str) -> None:
    """``web`` in two organizations is two ids, and so two keys."""
    build = _PROJECT_KEYS[name]
    web_in_default, web_in_acme = uuid.uuid4(), uuid.uuid4()
    one, other = build(web_in_default), build(web_in_acme)
    assert one != other
    assert str(web_in_default) in one
    assert str(web_in_acme) not in one
    assert not other.startswith(one) and not one.startswith(other)


@pytest.mark.parametrize(
    "builder",
    [
        cache.key_signals_all,
        cache.key_signals_all_expanded,
        cache.key_event_types_list,
        cache.key_meta_fields_list,
        cache.key_project_health,
        cache.key_health_sort,
        cache.prefix_signals,
        cache.prefix_event_types,
        cache.prefix_meta_fields,
        cache.prefix_health,
        cache.key_projects_list,
        cache.key_data_sources_list,
        realtime.channel,
        realtime._buffer_key,
        realtime._seq_key,
        realtime._epoch_key,
        realtime.read_resume_point,
        realtime.replay_buffered_events,
        realtime.redis_message_iterator,
        realtime.subscribed_messages,
        deps.hold_main_plan_for_write,
    ],
)
def test_no_key_builder_takes_a_slug(builder: Callable[..., Any]) -> None:
    params = inspect.signature(builder).parameters
    assert "slug" not in params
    first = next(p for p in params.values() if p.name != "session")
    assert first.name in {"project_id", "org_id"}, (builder.__name__, first.name)


def test_one_projects_prefix_never_covers_anothers_key() -> None:
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    for key, prefix in _KEY_UNDER_PREFIX:
        assert key(mine).startswith(prefix(mine))
        assert not key(theirs).startswith(prefix(mine))


def test_instance_lists_are_keyed_per_organization() -> None:
    assert cache.key_projects_list(DEFAULT.id) != cache.key_projects_list(ACME.id)
    assert cache.key_data_sources_list(DEFAULT.id) != cache.key_data_sources_list(ACME.id)
    for org in (DEFAULT, ACME):
        # A project or data-source write drops every organization's list.
        assert cache.key_projects_list(org.id).startswith(cache.prefix_projects())
        assert cache.key_data_sources_list(org.id).startswith(cache.prefix_data_sources())


# ── Invalidation over HTTP ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_write_clears_its_projects_id_key_only(
    client: AsyncClient, fake_cache: _FakeCache
) -> None:
    await _add_acme_membership()
    web = await _create_project(client, "rekey-web", org=DEFAULT)
    acme_web = await _create_project(client, "rekey-acme-web", org=ACME)

    assert (await client.get("/api/v1/projects/rekey-web/event-types")).status_code == 200
    acme_list = await client.get("/api/v1/orgs/acme-rekey/projects/rekey-acme-web/event-types")
    assert acme_list.status_code == 200, acme_list.text
    assert cache.key_event_types_list(web) in fake_cache.store
    assert cache.key_event_types_list(acme_web) in fake_cache.store

    created = await client.post(
        "/api/v1/projects/rekey-web/event-types",
        json={"name": "signup", "display_name": "Signup"},
    )
    assert created.status_code == 201, created.text
    assert cache.key_event_types_list(web) not in fake_cache.store
    # The other organization's project keeps its entry.
    assert cache.key_event_types_list(acme_web) in fake_cache.store


@pytest.mark.asyncio
async def test_project_lists_are_cached_per_organization(
    client: AsyncClient, fake_cache: _FakeCache
) -> None:
    await _add_acme_membership()
    assert (await client.get("/api/v1/projects")).status_code == 200
    assert (await client.get("/api/v1/orgs/acme-rekey/projects")).status_code == 200
    assert cache.key_projects_list(DEFAULT.id) in fake_cache.store
    assert cache.key_projects_list(ACME.id) in fake_cache.store

    await _create_project(client, "rekey-listed", org=ACME)
    assert not any(key.startswith(cache.prefix_projects()) for key in fake_cache.store)


@pytest.mark.asyncio
async def test_deleting_a_project_clears_its_id_keys(
    client: AsyncClient, fake_cache: _FakeCache
) -> None:
    project_id = await _create_project(client, "rekey-gone", org=DEFAULT)
    other_id = uuid.uuid4()
    for key, _prefix in _KEY_UNDER_PREFIX[:4]:
        fake_cache.store[key(project_id)] = []
        fake_cache.store[key(other_id)] = []

    assert (await client.delete("/api/v1/projects/rekey-gone")).status_code == 204
    for key, _prefix in _KEY_UNDER_PREFIX[:4]:
        assert key(project_id) not in fake_cache.store
        assert key(other_id) in fake_cache.store


# ── Realtime subscribe and the plan lock ─────────────────────────────────


@pytest.mark.asyncio
async def test_stream_subscribes_to_the_id_the_slug_resolves_to_in_the_org(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _add_acme_membership()
    web = await _create_project(client, "rekey-stream", org=DEFAULT)
    acme_web = await _create_project(client, "rekey-acme-stream", org=ACME)
    subscribed: list[dict[str, Any]] = []

    async def stream(**kwargs: Any) -> AsyncIterator[str]:
        subscribed.append(kwargs)
        yield "event: hello\ndata: {}\n\n"

    monkeypatch.setattr(realtime, "project_response_stream", stream)

    resp = await client.get("/api/v1/projects/rekey-stream/events/stream?max_events=0")
    assert resp.status_code == 200, resp.text
    resp = await client.get(
        "/api/v1/orgs/acme-rekey/projects/rekey-acme-stream/events/stream?max_events=0"
    )
    assert resp.status_code == 200, resp.text
    assert [(s["project_id"], s["slug"]) for s in subscribed] == [
        (web, "rekey-stream"),
        (acme_web, "rekey-acme-stream"),
    ]

    # A slug from another organization resolves to nothing, so nothing subscribes.
    resp = await client.get(
        "/api/v1/orgs/acme-rekey/projects/rekey-stream/events/stream?max_events=0"
    )
    assert resp.status_code == 404
    assert len(subscribed) == 2


@pytest.mark.asyncio
async def test_a_main_plan_write_locks_the_orgs_project_by_id(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _add_acme_membership()
    await _create_project(client, "rekey-lock", org=DEFAULT)
    acme_id = await _create_project(client, "rekey-acme-lock", org=ACME)
    held: list[uuid.UUID] = []

    async def hold(_session: object, project_id: uuid.UUID) -> None:
        held.append(project_id)

    monkeypatch.setattr(deps, "hold_main_plan_for_write", hold)
    # The lock only runs where rows lock (PostgreSQL); pretend so on SQLite.
    monkeypatch.setattr(deps, "locks_rows", lambda _session: True)

    resp = await client.post(
        "/api/v1/orgs/acme-rekey/projects/rekey-acme-lock/event-types",
        json={"name": "signup", "display_name": "Signup"},
    )
    assert resp.status_code == 201, resp.text
    assert held == [acme_id]

    # The default organization's slug does not name a project in acme: no lock.
    resp = await client.post(
        "/api/v1/orgs/acme-rekey/projects/rekey-lock/event-types",
        json={"name": "signup", "display_name": "Signup"},
    )
    assert resp.status_code == 404
    assert held == [acme_id]


# ── Data-source list ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_data_source_list_is_fenced_to_the_bound_organization(
    client: AsyncClient, fake_cache: _FakeCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _add_acme_membership()
    now = datetime.now(UTC)
    async with TestSessionLocal() as session:
        for index, org in enumerate((DEFAULT, DEFAULT, ACME, ACME)):
            session.add(
                DataSource(
                    organization_id=org.id,
                    name=f"rekey-ds-{index}",
                    db_type=DBType.postgres,
                    host="db.example",
                    port=5432,
                    database_name="warehouse",
                    created_at=now - timedelta(minutes=index),
                )
            )
        await session.commit()
    monkeypatch.setattr(datasource_service, "_LIST_LIMIT_PER_ORG", 1)

    with bound_org(ACME):
        async with TestSessionLocal() as session:
            listed = await datasource_service.list_data_sources(session, visible_project_ids=None)
    # Only the bound organization's sources, newest first under the cap: the
    # default organization's warehouses do not exist for ACME (F20 PR4).
    assert [ds.name for ds in listed] == ["rekey-ds-2"]
    assert cache.key_data_sources_list(ACME.id) in fake_cache.store
    assert cache.key_data_sources_list(DEFAULT.id) not in fake_cache.store
