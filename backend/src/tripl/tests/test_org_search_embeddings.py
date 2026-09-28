"""Per-organization search embeddings (F20 PR10).

* resolution: the embedding endpoint, provider, model and key are ONE
  credential group; the operator's key never reaches an organization's
  endpoint; ``ORG_SETTINGS_OPERATOR_FALLBACK=none`` leaves an organization
  without its own endpoint with semantic search off;
* provenance is per organization and includes the endpoint, and an
  organization that inherits the operator's endpoint keeps exactly the
  operator's provenance (critique #21: nothing is re-embedded);
* an organization's endpoint is refused when private (save and use) and never
  follows a redirect;
* its model is checked with a test embedding at save (422 otherwise);
* the stale sweep, the stranded chaser and the embed task use each project's
  organization's config; a settings change queues that organization's
  reindex only;
* the query uses the project's organization's provenance (a request bound to
  another organization fails closed).
"""

from __future__ import annotations

import socket
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet
from httpx import AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tripl import crypto
from tripl.config import settings
from tripl.middleware import org_context
from tripl.middleware.org_context import OrgRef, bound_org
from tripl.models import Base
from tripl.models.app_setting import SERVICE_SETTINGS_KEY, AppSetting
from tripl.models.organization import DEFAULT_ORG_ID, Organization
from tripl.models.plan_branch import PlanBranch
from tripl.models.project import Project
from tripl.models.search_document import SearchDocument
from tripl.services import app_settings_service, embedding_service, org_settings_service
from tripl.services._search_documents import DOCUMENT_BUILDER_VERSION
from tripl.services.app_settings_service import ai_config_for, resolve_settings
from tripl.services.embedding_service import embedding_provenance
from tripl.tests._sqlite import enable_sqlite_foreign_keys
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_org_settings import _move_to_org, _new_client, _register
from tripl.worker.tasks import search as search_tasks

API = "/api/v1"
ORG_A_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
ORG_B_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
ALPHA_URL = "https://alpha-embed.example.com/v1"


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "encryption_key", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()
    monkeypatch.setattr(settings, "search_embeddings_enabled", True)
    monkeypatch.setattr(settings, "search_embedding_api_key", "sk-env-embed")
    monkeypatch.setattr(settings, "search_embedding_base_url", "https://env-embed.example.com/v1")
    monkeypatch.setattr(settings, "org_settings_operator_fallback", "all")
    yield
    crypto._fernet.cache_clear()


def _enc(value: str) -> str:
    return crypto.encrypt_value(value)


def _operator() -> dict[str, Any]:
    return {
        "search_embedding_model": "operator-embed",
        "search_embedding_api_key": _enc("sk-operator-embed"),
    }


def _alpha() -> dict[str, Any]:
    return {
        "search_embeddings_enabled": True,
        "search_embedding_model": "alpha-embed",
        "search_embedding_base_url": ALPHA_URL,
        "search_embedding_api_key": _enc("sk-alpha-embed"),
    }


# ── resolution (pure) ───────────────────────────────────────────────────────


def test_an_inheriting_org_keeps_the_operators_provenance() -> None:
    """Critique #21: organizations arriving must not re-embed the corpus."""
    operator = ai_config_for(resolve_settings(_operator(), None))
    org = ai_config_for(resolve_settings(_operator(), {}, org_scope=ORG_A_ID))
    assert org.search_embedding_api_key == "sk-operator-embed"
    assert org.search_embedding_base_url == "https://env-embed.example.com/v1"
    assert embedding_provenance(org) == embedding_provenance(operator)
    assert org.embedding_host_guard is False


def test_an_org_endpoint_never_receives_the_operators_embedding_key() -> None:
    resolved = resolve_settings(
        _operator(), {"search_embedding_base_url": ALPHA_URL}, org_scope=ORG_A_ID
    )
    config = ai_config_for(resolved)
    assert config.search_embedding_base_url == ALPHA_URL
    assert config.search_embedding_api_key == ""
    # The model is part of the group: the built-in default, not the operator's.
    assert config.search_embedding_model == "text-embedding-3-small"
    assert resolved.sources["search_embedding_api_key"] == "default"
    assert config.embedding_host_guard is True


def test_an_org_key_alone_does_not_ride_the_operators_endpoint() -> None:
    config = ai_config_for(
        resolve_settings(
            _operator(), {"search_embedding_api_key": _enc("sk-org")}, org_scope=ORG_A_ID
        )
    )
    assert config.search_embedding_api_key == "sk-org"
    assert config.search_embedding_base_url == "https://api.openai.com/v1"
    assert config.search_embedding_model != "operator-embed"


def test_provenance_is_per_org_and_includes_the_endpoint() -> None:
    alpha = ai_config_for(resolve_settings(_operator(), _alpha(), org_scope=ORG_A_ID))
    operator = ai_config_for(resolve_settings(_operator(), None))
    assert embedding_provenance(alpha) != embedding_provenance(operator)
    moved = replace(alpha, search_embedding_base_url="https://other.example.com/v1")
    assert embedding_provenance(moved) != embedding_provenance(alpha)


def test_fallback_none_turns_semantic_search_off_without_an_org_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "org_settings_operator_fallback", "none")
    resolved = resolve_settings(_operator(), {}, org_scope=ORG_A_ID)
    config = ai_config_for(resolved)
    assert config.search_embeddings_enabled is False
    assert config.search_embedding_api_key == ""
    assert config.search_embedding_base_url == ""
    assert resolved.sources["search_embeddings_enabled"] == "disabled"
    assert resolved.sources["search_embedding_base_url"] == "disabled"
    # The operator's own view is untouched by the policy.
    assert ai_config_for(resolve_settings(_operator(), None)).search_embeddings_enabled


def test_fallback_none_runs_an_org_with_its_own_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "org_settings_operator_fallback", "none")
    config = ai_config_for(resolve_settings(_operator(), _alpha(), org_scope=ORG_A_ID))
    assert config.search_embeddings_enabled is True
    assert config.search_embedding_api_key == "sk-alpha-embed"


def test_unreadable_org_settings_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_session: object) -> dict[str, object]:
        raise RuntimeError("db down")

    monkeypatch.setattr(app_settings_service, "get_service_overrides_sync", boom)
    config = app_settings_service.get_embedding_config_sync(object(), org_id=ORG_A_ID)  # type: ignore[arg-type]
    assert config.search_embeddings_enabled is False
    assert config.search_embedding_api_key == ""


# ── the endpoint at use time ────────────────────────────────────────────────


def test_a_private_org_endpoint_is_refused_at_use_time(monkeypatch: pytest.MonkeyPatch) -> None:
    config = ai_config_for(resolve_settings(_operator(), _alpha(), org_scope=ORG_A_ID))

    def rebound(*_args: object, **_kwargs: object) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", rebound)
    called: list[object] = []
    monkeypatch.setattr(
        embedding_service.urllib.request, "urlopen", lambda *a, **k: called.append(a)
    )
    monkeypatch.setattr(
        embedding_service.NO_REDIRECT_OPENER, "open", lambda *a, **k: called.append(a)
    )
    assert embedding_service.embed_texts(["hello"], config=config) == []
    assert called == []


class _Response:
    def __init__(self, width: int) -> None:
        self.width = width

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def read(self) -> bytes:
        import json

        return json.dumps({"data": [{"index": 0, "embedding": [0.01] * self.width}]}).encode()


def test_an_org_endpoint_never_follows_a_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    config = ai_config_for(resolve_settings(_operator(), _alpha(), org_scope=ORG_A_ID))
    via_opener: list[str] = []

    def opener(request: Any, timeout: float | None = None) -> _Response:
        via_opener.append(request.full_url)
        return _Response(4)

    def plain(*_args: object, **_kwargs: object) -> _Response:
        raise AssertionError("an organization's endpoint went through the redirecting opener")

    monkeypatch.setattr(embedding_service.NO_REDIRECT_OPENER, "open", opener)
    monkeypatch.setattr(embedding_service.urllib.request, "urlopen", plain)
    assert embedding_service.embed_texts(["hello"], config=config) == [[0.01] * 4]
    assert via_opener == [f"{ALPHA_URL}/embeddings"]


def test_the_dimension_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    config = ai_config_for(resolve_settings(_operator(), _alpha(), org_scope=ORG_A_ID))
    monkeypatch.setattr(
        embedding_service.NO_REDIRECT_OPENER, "open", lambda *a, **k: _Response(768)
    )
    problem = embedding_service.probe_embedding_dimensions(config)
    assert problem is not None and "768" in problem and "1536" in problem
    monkeypatch.setattr(
        embedding_service.NO_REDIRECT_OPENER, "open", lambda *a, **k: _Response(1536)
    )
    assert embedding_service.probe_embedding_dimensions(config) is None
    keyless = replace(config, search_embedding_api_key="")
    assert embedding_service.probe_embedding_dimensions(keyless) is not None


def test_a_malformed_endpoint_never_raises_out_of_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank stored base URL would give "/embeddings", which ``Request`` rejects."""
    config = replace(
        ai_config_for(resolve_settings(_operator(), _alpha(), org_scope=ORG_A_ID)),
        search_embedding_base_url="",
        embedding_host_guard=False,
    )
    called: list[object] = []
    monkeypatch.setattr(
        embedding_service.urllib.request, "urlopen", lambda *a, **k: called.append(a)
    )
    monkeypatch.setattr(
        embedding_service.NO_REDIRECT_OPENER, "open", lambda *a, **k: called.append(a)
    )
    assert embedding_service.embed_texts(["hello"], config=config) == []
    assert embedding_service.probe_embedding_dimensions(config) is not None
    assert called == []


# ── the organization surface ────────────────────────────────────────────────


class Hosted:
    def __init__(self) -> None:
        self.a_admin = _new_client()
        self.a_member = _new_client()
        self.b_owner = _new_client()

    def clients(self) -> list[AsyncClient]:
        return [self.a_admin, self.a_member, self.b_owner]


@pytest.fixture
async def hosted(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Hosted]:
    monkeypatch.setattr(settings, "deployment_mode", "hosted")
    async with TestSessionLocal() as session:
        session.add_all(
            [
                Organization(id=ORG_A_ID, slug="alpha", name="Alpha"),
                Organization(id=ORG_B_ID, slug="bravo", name="Bravo"),
            ]
        )
        session.add(AppSetting(key=SERVICE_SETTINGS_KEY, value=_operator(), organization_id=None))
        await session.commit()
    h = Hosted()
    try:
        await _move_to_org(await _register(h.a_admin, "alpha-admin"), ORG_A_ID, "admin")
        await _move_to_org(await _register(h.a_member, "alpha-member"), ORG_A_ID, "member")
        await _move_to_org(await _register(h.b_owner, "bravo-owner"), ORG_B_ID, "owner")
        yield h
    finally:
        for client in h.clients():
            await client.aclose()


@pytest.fixture
def reindexed(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    queued: list[uuid.UUID] = []

    async def capture(org_id: uuid.UUID) -> bool:
        queued.append(org_id)
        return True

    monkeypatch.setattr(org_settings_service, "enqueue_org_search_reindex", capture)
    return queued


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """The save-time probe; append a message to make it fail."""
    outcome: list[Any] = []
    seen: list[Any] = []

    def fake(config: Any, **_kwargs: object) -> str | None:
        seen.append(config)
        return outcome[0] if outcome else None

    monkeypatch.setattr(embedding_service, "probe_embedding_dimensions", fake)
    return [outcome, seen]


_ALPHA_SEARCH = {
    "search": {
        "search_embeddings_enabled": True,
        "search_embedding_provider": "openai",
        "search_embedding_model": "alpha-embed",
        "search_embedding_base_url": ALPHA_URL,
        "search_embedding_api_key": "sk-alpha-embed",
    }
}


@pytest.mark.asyncio
async def test_an_org_saves_its_own_embeddings_and_reindexes_only_itself(
    hosted: Hosted, reindexed: list[uuid.UUID], probe: list[Any]
) -> None:
    before = await hosted.a_admin.get(f"{API}/orgs/alpha/settings")
    assert before.status_code == 200, before.text
    # The operator's endpoint is not shown to an organization inheriting it.
    assert before.json()["search"]["search_embedding_base_url"] == ""
    assert before.json()["search"]["search_embedding_dimensions"] == 1536

    resp = await hosted.a_admin.patch(f"{API}/orgs/alpha/settings", json=_ALPHA_SEARCH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["search"]["search_embedding_base_url"] == ALPHA_URL
    assert body["search"]["search_embedding_api_key_configured"] is True
    assert body["sources"]["search.search_embedding_model"] == "org"
    assert "sk-alpha-embed" not in resp.text
    # Probed with the organization's own config, then the reindex of ALPHA only.
    assert probe[1][-1].search_embedding_base_url == ALPHA_URL
    assert reindexed == [ORG_A_ID]

    # Bravo still runs on the operator's space.
    bravo = await hosted.b_owner.get(f"{API}/orgs/bravo/settings")
    assert bravo.json()["sources"]["search.search_embedding_model"] == "override"
    async with TestSessionLocal() as session:
        a = await app_settings_service.get_embedding_config(session, org_id=ORG_A_ID)
        b = await app_settings_service.get_embedding_config(session, org_id=ORG_B_ID)
    assert a.search_embedding_model == "alpha-embed"
    assert b.search_embedding_model == "operator-embed"
    assert b.search_embedding_api_key == "sk-operator-embed"

    # Saving the same identity again, or an unrelated field, queues nothing.
    again = await hosted.a_admin.patch(f"{API}/orgs/alpha/settings", json=_ALPHA_SEARCH)
    assert again.status_code == 200, again.text
    other = await hosted.a_admin.patch(
        f"{API}/orgs/alpha/settings", json={"ai": {"describe_system_prompt": "Brief."}}
    )
    assert other.status_code == 200, other.text
    assert reindexed == [ORG_A_ID]

    # Switching back to the operator's space (clearing the group) reindexes again.
    cleared = await hosted.a_admin.patch(
        f"{API}/orgs/alpha/settings",
        json={
            "search": {
                "search_embeddings_enabled": None,
                "search_embedding_provider": None,
                "search_embedding_model": None,
                "search_embedding_base_url": None,
                "search_embedding_api_key": None,
            }
        },
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["sources"]["search.search_embedding_model"] == "override"
    assert reindexed == [ORG_A_ID, ORG_A_ID]


@pytest.mark.asyncio
async def test_a_model_of_the_wrong_width_is_refused_at_save(
    hosted: Hosted, reindexed: list[uuid.UUID], probe: list[Any]
) -> None:
    probe[0].append("The embedding model returned 768-dimension vectors")
    resp = await hosted.a_admin.patch(f"{API}/orgs/alpha/settings", json=_ALPHA_SEARCH)
    assert resp.status_code == 422, resp.text
    assert "768" in resp.json()["detail"]
    async with TestSessionLocal() as session:
        stored = await app_settings_service.get_org_overrides(session, ORG_A_ID)
    assert "search_embedding_model" not in stored
    assert reindexed == []


@pytest.mark.asyncio
async def test_turning_embeddings_off_is_not_probed(
    hosted: Hosted, reindexed: list[uuid.UUID], probe: list[Any]
) -> None:
    probe[0].append("unreachable")
    resp = await hosted.a_admin.patch(
        f"{API}/orgs/alpha/settings", json={"search": {"search_embeddings_enabled": False}}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["search"]["search_embeddings_enabled"] is False
    assert probe[1] == []
    assert reindexed == [ORG_A_ID]


@pytest.mark.asyncio
async def test_a_private_embedding_endpoint_is_refused_at_save(
    hosted: Hosted, reindexed: list[uuid.UUID], probe: list[Any]
) -> None:
    for url in ("http://169.254.169.254/latest", "http://127.0.0.1:8080/v1"):
        resp = await hosted.a_admin.patch(
            f"{API}/orgs/alpha/settings",
            json={"search": {"search_embedding_base_url": url, "search_embedding_api_key": "k"}},
        )
        assert resp.status_code == 422, resp.text
    assert probe[1] == []
    assert reindexed == []


@pytest.mark.asyncio
async def test_a_blank_embedding_base_url_clears_the_field(
    hosted: Hosted, reindexed: list[uuid.UUID], probe: list[Any]
) -> None:
    saved = await hosted.a_admin.patch(f"{API}/orgs/alpha/settings", json=_ALPHA_SEARCH)
    assert saved.status_code == 200, saved.text
    resp = await hosted.a_admin.patch(
        f"{API}/orgs/alpha/settings", json={"search": {"search_embedding_base_url": "  "}}
    )
    assert resp.status_code == 200, resp.text
    async with TestSessionLocal() as session:
        stored = await app_settings_service.get_org_overrides(session, ORG_A_ID)
        config = await app_settings_service.get_embedding_config(session, org_id=ORG_A_ID)
    assert "search_embedding_base_url" not in stored
    # Still the organization's group (its model and key), on the provider default.
    assert config.search_embedding_base_url == "https://api.openai.com/v1"
    assert probe[1][-1].search_embedding_base_url == "https://api.openai.com/v1"

    # Blank alone on an inheriting organization stores nothing either.
    async with TestSessionLocal() as session:
        await app_settings_service.update_org_overrides(
            session, ORG_B_ID, {"search_embedding_base_url": None}
        )
    bravo = await hosted.b_owner.patch(
        f"{API}/orgs/bravo/settings", json={"search": {"search_embedding_base_url": ""}}
    )
    assert bravo.status_code == 200, bravo.text
    assert bravo.json()["sources"]["search.search_embedding_model"] == "override"
    async with TestSessionLocal() as session:
        assert await app_settings_service.get_org_overrides(session, ORG_B_ID) == {}


@pytest.mark.asyncio
async def test_switching_on_with_an_inherited_keyless_config_is_refused(
    hosted: Hosted, reindexed: list[uuid.UUID], probe: list[Any]
) -> None:
    async with TestSessionLocal() as session:
        row = await session.scalar(
            select(AppSetting).where(
                AppSetting.key == SERVICE_SETTINGS_KEY, AppSetting.organization_id.is_(None)
            )
        )
        assert row is not None
        row.value = {"search_embeddings_enabled": False}
        await session.commit()
    settings_key = settings.search_embedding_api_key
    try:
        settings.search_embedding_api_key = ""
        resp = await hosted.a_admin.patch(
            f"{API}/orgs/alpha/settings", json={"search": {"search_embeddings_enabled": True}}
        )
    finally:
        settings.search_embedding_api_key = settings_key
    assert resp.status_code == 422, resp.text
    assert "API key" in resp.json()["detail"]
    # Not probed over HTTP (the operator's endpoint), nothing stored or queued.
    assert probe[1] == []
    assert reindexed == []
    async with TestSessionLocal() as session:
        stored = await app_settings_service.get_org_overrides(session, ORG_A_ID)
    assert "search_embeddings_enabled" not in stored


@pytest.mark.parametrize(
    "payload",
    [
        {"search": {"search_embedding_dimensions": 768}},
        {"search": {"search_embedding_provider": "cohere"}},
        {"search": {"search_embedding_base_url": "ftp://example.com"}},
    ],
)
@pytest.mark.asyncio
async def test_the_width_and_unknown_providers_are_not_an_orgs_to_set(
    hosted: Hosted, payload: dict[str, Any]
) -> None:
    resp = await hosted.a_admin.patch(f"{API}/orgs/alpha/settings", json=payload)
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_embedding_settings_are_for_the_orgs_admins(hosted: Hosted) -> None:
    member = await hosted.a_member.patch(f"{API}/orgs/alpha/settings", json=_ALPHA_SEARCH)
    assert member.status_code == 403, member.text
    stranger = await hosted.b_owner.patch(f"{API}/orgs/alpha/settings", json=_ALPHA_SEARCH)
    assert stranger.status_code == 404, stranger.text


@pytest.mark.asyncio
async def test_the_self_hosted_operator_endpoint_stays_env_only(client: AsyncClient) -> None:
    """The default organization is the operator scope when self-hosted: its
    endpoint is SEARCH_EMBEDDING_BASE_URL, pinned env-only (tripl-wkwv.2)."""
    resp = await client.patch(
        f"{API}/orgs/default/settings",
        json={"search": {"search_embedding_base_url": ALPHA_URL}},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == org_settings_service.OPERATOR_EMBEDDING_URL_READ_ONLY


# ── the worker, per organization ────────────────────────────────────────────


@pytest.fixture
def world(tmp_path: Path) -> Iterator[tuple[sessionmaker[Session], dict[str, uuid.UUID]]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'org_embeddings.db'}")
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    ids: dict[str, uuid.UUID] = {}
    old = datetime.now(UTC) - timedelta(hours=1)
    with factory() as session:
        session.add(Organization(id=ORG_A_ID, slug="alpha", name="Alpha"))
        session.commit()
        # The operator has semantic search OFF; alpha runs its own.
        session.add(
            AppSetting(
                key=SERVICE_SETTINGS_KEY,
                value={**_operator(), "search_embeddings_enabled": False},
                organization_id=None,
            )
        )
        session.add(AppSetting(key=SERVICE_SETTINGS_KEY, value=_alpha(), organization_id=ORG_A_ID))
        for name, org_id in (("alpha", ORG_A_ID), ("default", DEFAULT_ORG_ID)):
            project = Project(name=name, slug=f"{name}-p", organization_id=org_id)
            session.add(project)
            session.flush()
            branch = PlanBranch(project_id=project.id, name="main", kind="main")
            session.add(branch)
            session.flush()
            for status in ("ready", "pending"):
                session.add(
                    SearchDocument(
                        project_id=project.id,
                        branch_id=branch.id,
                        entity_type="event",
                        entity_id=uuid.uuid4(),
                        title=f"{name} {status}",
                        route_path="/events/x",
                        content_hash="h",
                        builder_version=DOCUMENT_BUILDER_VERSION,
                        embedding_status=status,
                        embedding_model="sha256:previous-space" if status == "ready" else None,
                        updated_at=old,
                    )
                )
            ids[f"{name}_project"] = project.id
            ids[f"{name}_branch"] = branch.id
        session.commit()
    try:
        yield factory, ids
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_the_stale_sweep_decides_per_organization(
    world: tuple[sessionmaker[Session], dict[str, uuid.UUID]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, ids = world
    monkeypatch.setattr(search_tasks, "_get_sync_session", factory)
    rebuilt: list[tuple[uuid.UUID, uuid.UUID]] = []
    monkeypatch.setattr(
        search_tasks,
        "reindex_branch_from_worker",
        lambda _session, project_id, branch_id: rebuilt.append((project_id, branch_id)),
    )
    result = search_tasks.reindex_stale_search_documents()
    # Alpha's vectors are from another space than alpha's own; the default
    # organization has embeddings off, so its ready rows are left alone.
    assert rebuilt == [(ids["alpha_project"], ids["alpha_branch"])]
    assert result == {"branches_reindexed": 1}


def test_the_org_reindex_touches_only_that_organization(
    world: tuple[sessionmaker[Session], dict[str, uuid.UUID]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, ids = world
    monkeypatch.setattr(search_tasks, "_get_sync_session", factory)
    fake = MagicMock()
    monkeypatch.setattr(search_tasks, "reindex_search_branch", fake)

    assert search_tasks.reindex_org_search_documents(str(ORG_A_ID)) == {"branches_queued": 1}
    fake.delay.assert_called_once_with(str(ids["alpha_project"]), str(ids["alpha_branch"]))

    # The default organization (embeddings off): its pending rows are due, so
    # nothing waits for a worker that will never embed them — and only its own.
    fake.reset_mock()
    assert search_tasks.reindex_org_search_documents(str(DEFAULT_ORG_ID)) == {"branches_queued": 1}
    fake.delay.assert_called_once_with(str(ids["default_project"]), str(ids["default_branch"]))


def test_the_stranded_chaser_asks_each_organization(
    world: tuple[sessionmaker[Session], dict[str, uuid.UUID]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, ids = world
    monkeypatch.setattr(search_tasks, "_get_sync_session", factory)
    fake = MagicMock()
    monkeypatch.setattr(search_tasks, "embed_search_documents", fake)
    assert search_tasks.requeue_stranded_search_embeddings() == {"branches_requeued": 1}
    fake.delay.assert_called_once_with(str(ids["alpha_project"]), str(ids["alpha_branch"]))


def test_the_stranded_chaser_skips_an_organization_that_cannot_embed(
    world: tuple[sessionmaker[Session], dict[str, uuid.UUID]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, _ids = world
    with factory() as session:
        row = session.scalar(select(AppSetting).where(AppSetting.organization_id == ORG_A_ID))
        assert row is not None
        row.value = {**_alpha(), "search_embedding_api_key": ""}
        session.commit()
    monkeypatch.setattr(search_tasks, "_get_sync_session", factory)
    fake = MagicMock()
    monkeypatch.setattr(search_tasks, "embed_search_documents", fake)
    assert search_tasks.requeue_stranded_search_embeddings() == {"branches_requeued": 0}
    fake.delay.assert_not_called()


def test_the_embed_task_embeds_with_the_projects_organization(
    world: tuple[sessionmaker[Session], dict[str, uuid.UUID]],
) -> None:
    factory, ids = world
    with factory() as session:
        alpha = app_settings_service.get_embedding_config_for_project_sync(
            session, ids["alpha_project"]
        )
        default = app_settings_service.get_embedding_config_for_project_sync(
            session, ids["default_project"]
        )
        unknown = app_settings_service.get_embedding_config_for_project_sync(session, uuid.uuid4())
    assert (alpha.search_embeddings_enabled, alpha.search_embedding_model) == (
        True,
        "alpha-embed",
    )
    assert alpha.search_embedding_base_url == ALPHA_URL
    assert default.search_embeddings_enabled is False
    assert unknown.search_embeddings_enabled is False
    assert unknown.search_embedding_api_key == ""


# ── the query ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_query_uses_the_projects_organizations_provenance() -> None:
    async with TestSessionLocal() as session:
        session.add(Organization(id=ORG_A_ID, slug="alpha", name="Alpha"))
        await session.flush()
        session.add(AppSetting(key=SERVICE_SETTINGS_KEY, value=_operator(), organization_id=None))
        session.add(AppSetting(key=SERVICE_SETTINGS_KEY, value=_alpha(), organization_id=ORG_A_ID))
        project = Project(name="p", slug="p-alpha", organization_id=ORG_A_ID)
        session.add(project)
        await session.commit()

        with bound_org(OrgRef(id=ORG_A_ID, slug="alpha")):
            bound = await app_settings_service.get_search_embedding_config(
                session, project_id=project.id
            )
        # No request bound (the relevance harness): the project's organization.
        token = org_context._org_var.set(None)
        try:
            unbound = await app_settings_service.get_search_embedding_config(
                session, project_id=project.id
            )
            missing = await app_settings_service.get_search_embedding_config(
                session, project_id=uuid.uuid4()
            )
        finally:
            org_context._org_var.reset(token)
        # A request bound to another organization: neither config, semantic off.
        with bound_org(OrgRef(id=DEFAULT_ORG_ID, slug="default")):
            foreign = await app_settings_service.get_search_embedding_config(
                session, project_id=project.id
            )
        operator = await app_settings_service.get_embedding_config(session, org_id=None)
        rows = (await session.execute(select(AppSetting))).scalars().all()
    assert len(rows) == 2
    assert embedding_provenance(bound) == embedding_provenance(unbound)
    assert embedding_provenance(bound) != embedding_provenance(operator)
    assert bound.search_embedding_model == "alpha-embed"
    assert missing.search_embeddings_enabled is False
    assert foreign.search_embeddings_enabled is False
    assert foreign.search_embedding_api_key == ""
