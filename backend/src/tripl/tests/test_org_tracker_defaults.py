"""Organization tracker defaults under per-project tracker config (F20 PR12).

* resolution: a project's own value wins, else the organization's default; the
  Jira site, account and token are one unit, so a project naming its own site
  or account never has the organization's token sent there;
* the worker creates and syncs tickets with the resolved values, and re-checks
  the site against private addresses whichever side it came from;
* ``/orgs/{org}/settings/trackers`` is for the organization's owners/admins,
  never returns a secret, refuses a private Jira site, and is invisible to
  another organization;
* the project's tracker config reports which fields it inherits.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from cryptography.fernet import Fernet
from httpx import AsyncClient
from sqlalchemy import select

import tripl.alerting_validation as av
from tripl import crypto
from tripl.config import settings
from tripl.models.app_setting import TRACKER_DEFAULTS_KEY, AppSetting
from tripl.models.audit_log import AuditLog
from tripl.models.implementation_ticket import ImplementationTicket
from tripl.models.organization import Organization
from tripl.models.plan_branch import PlanBranch
from tripl.models.project import Project
from tripl.models.project_tracker_config import ProjectTrackerConfig
from tripl.services import org_tracker_defaults_service as defaults_service
from tripl.services.org_tracker_defaults_service import (
    NO_DEFAULTS,
    OrgTrackerDefaults,
    effective_jira,
    effective_linear,
    inherited_fields,
)
from tripl.tests._tenancy import use_multi_tenant
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_org_settings import _move_to_org, _new_client, _register
from tripl.worker.tasks import implementation_tickets as impl_tasks

API = "/api/v1"
ORG_A_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
ORG_B_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")

ORG_DEFAULTS = OrgTrackerDefaults(
    jira_base_url="https://alpha.atlassian.net",
    jira_auth_email="bot@alpha.example.com",
    jira_api_token="org-jira-token",
    jira_project_key="ALPHA",
    linear_api_key="lin_org_key",
    linear_team_id="team-alpha",
)


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "encryption_key", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()
    yield
    crypto._fernet.cache_clear()


def _config(**values: Any) -> ProjectTrackerConfig:
    base: dict[str, Any] = {
        "project_id": uuid.uuid4(),
        "enabled": True,
        "tracker_type": "jira",
        "base_url": "",
        "project_key": "",
        "auth_email": "",
        "api_token_encrypted": "",
        "issue_type": "Task",
    }
    base.update(values)
    return ProjectTrackerConfig(**base)


# ── resolution (pure) ───────────────────────────────────────────────────────


def test_an_empty_project_config_runs_on_the_org_defaults() -> None:
    target = effective_jira(_config(), ORG_DEFAULTS)
    assert (target.base_url, target.auth_email, target.api_token, target.project_key) == (
        "https://alpha.atlassian.net",
        "bot@alpha.example.com",
        "org-jira-token",
        "ALPHA",
    )
    assert set(target.sources.values()) == {"org"}
    assert inherited_fields(_config(), ORG_DEFAULTS) == [
        "api_token",
        "auth_email",
        "base_url",
        "project_key",
    ]


def test_a_project_value_overrides_the_org_default() -> None:
    target = effective_jira(_config(project_key="OWN"), ORG_DEFAULTS)
    assert target.project_key == "OWN"
    assert target.sources["project_key"] == "project"
    # The endpoint group is still the organization's.
    assert target.api_token == "org-jira-token"


def test_a_project_site_never_receives_the_org_token() -> None:
    target = effective_jira(_config(base_url="https://attacker.atlassian.net"), ORG_DEFAULTS)
    assert target.base_url == "https://attacker.atlassian.net"
    assert target.api_token == ""
    assert target.auth_email == ""
    assert target.sources["api_token"] == "project"
    # A project's own account alone does not borrow the org's token either.
    assert effective_jira(_config(auth_email="me@example.com"), ORG_DEFAULTS).api_token == ""


def test_a_project_with_its_own_group_uses_its_own_token() -> None:
    config = _config(
        base_url="https://own.atlassian.net",
        auth_email="own@example.com",
        api_token_encrypted=crypto.encrypt_value("own-token"),
    )
    target = effective_jira(config, ORG_DEFAULTS)
    assert (target.base_url, target.api_token, target.project_key) == (
        "https://own.atlassian.net",
        "own-token",
        "ALPHA",
    )


def test_linear_key_and_team_fall_back_independently() -> None:
    target = effective_linear(_config(tracker_type="linear"), ORG_DEFAULTS)
    assert (target.api_key, target.team_id) == ("lin_org_key", "team-alpha")
    own = _config(
        tracker_type="linear",
        api_token_encrypted=crypto.encrypt_value("lin_own"),
        project_key="team-own",
    )
    target = effective_linear(own, ORG_DEFAULTS)
    assert (target.api_key, target.team_id) == ("lin_own", "team-own")
    assert inherited_fields(own, ORG_DEFAULTS) == []


def test_no_defaults_is_the_project_alone() -> None:
    """An empty project config with no organization defaults stays incomplete."""
    assert impl_tasks._resolve_jira_config(_config(), NO_DEFAULTS) is None
    assert impl_tasks._resolve_linear_config(_config(tracker_type="linear")) is None


def _public_dns(monkeypatch: pytest.MonkeyPatch, address: str = "93.184.216.34") -> None:
    def fake(host: str, *args: object, **kwargs: object) -> list[Any]:
        return [(2, 1, 6, "", (address, 0))]

    monkeypatch.setattr(av.socket, "getaddrinfo", fake)


def test_the_worker_resolves_with_the_org_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _public_dns(monkeypatch)
    assert impl_tasks._resolve_jira_config(_config(), ORG_DEFAULTS) == (
        "https://alpha.atlassian.net",
        "bot@alpha.example.com",
        "org-jira-token",
        "ALPHA",
        "Task",
    )
    assert impl_tasks._resolve_linear_config(_config(tracker_type="linear"), ORG_DEFAULTS) == (
        "lin_org_key",
        "team-alpha",
    )


def test_a_private_org_site_is_refused_at_use(monkeypatch: pytest.MonkeyPatch) -> None:
    _public_dns(monkeypatch, "10.1.2.3")
    assert impl_tasks._resolve_jira_config(_config(), ORG_DEFAULTS) is None


# ── the ticket worker ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_ticket_is_created_with_the_org_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch)
    monkeypatch.setattr(impl_tasks, "_find_jira_issue_by_label", lambda *_a, **_k: (None, None))
    project_id, branch_id = uuid.uuid4(), uuid.uuid4()
    async with TestSessionLocal() as session:
        session.add(Organization(id=ORG_A_ID, slug="alpha", name="Alpha"))
        await session.flush()
        session.add(Project(id=project_id, name="T", slug="t-alpha", organization_id=ORG_A_ID))
        await session.flush()
        session.add(PlanBranch(id=branch_id, project_id=project_id, name="feature"))
        # The project only switched the automation on and chose its own Jira project.
        session.add(_config(project_id=project_id, project_key="OWN"))
        session.add(
            AppSetting(
                key=TRACKER_DEFAULTS_KEY,
                organization_id=ORG_A_ID,
                value={
                    "jira_base_url": "https://alpha.atlassian.net",
                    "jira_auth_email": "bot@alpha.example.com",
                    "jira_api_token": crypto.encrypt_value("org-jira-token"),
                    "jira_project_key": "ALPHA",
                },
            )
        )
        await session.commit()

    calls: list[tuple[str, dict[str, Any], dict[str, str] | None]] = []

    def fake_post_json(url: str, body: dict[str, Any], headers: dict[str, str] | None = None):  # noqa: ANN202
        calls.append((url, body, headers))
        return {"id": "10001", "key": "OWN-1"}

    monkeypatch.setattr(impl_tasks, "_post_json", fake_post_json)
    async with TestSessionLocal() as session:
        await impl_tasks._create_ticket(session, str(project_id), str(branch_id), [], "Implement")

    assert len(calls) == 1
    url, body, _headers = calls[0]
    assert url == "https://alpha.atlassian.net/rest/api/3/issue"
    assert body["fields"]["project"]["key"] == "OWN"
    async with TestSessionLocal() as session:
        ticket = await session.scalar(select(ImplementationTicket))
    assert ticket is not None
    assert ticket.external_url == "https://alpha.atlassian.net/browse/OWN-1"


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
    use_multi_tenant(monkeypatch)
    async with TestSessionLocal() as session:
        session.add_all(
            [
                Organization(id=ORG_A_ID, slug="alpha", name="Alpha"),
                Organization(id=ORG_B_ID, slug="bravo", name="Bravo"),
            ]
        )
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


_DEFAULTS_BODY = {
    "jira": {
        "base_url": "https://alpha.atlassian.net/",
        "auth_email": "bot@alpha.example.com",
        "api_token": "org-jira-token",
        "project_key": "alpha",
    },
    "linear": {"api_key": "lin_org_key", "team_id": "team-alpha"},
}


@pytest.mark.asyncio
async def test_org_admins_set_tracker_defaults_without_ever_reading_a_secret(
    hosted: Hosted,
) -> None:
    empty = await hosted.a_admin.get(f"{API}/orgs/alpha/settings/trackers")
    assert empty.status_code == 200, empty.text
    assert empty.json()["sources"]["jira.base_url"] == "default"

    resp = await hosted.a_admin.patch(f"{API}/orgs/alpha/settings/trackers", json=_DEFAULTS_BODY)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["jira"]["base_url"] == "https://alpha.atlassian.net"
    assert body["jira"]["project_key"] == "ALPHA"
    assert body["jira"]["api_token_configured"] is True
    assert body["linear"] == {"api_key_configured": True, "team_id": "team-alpha"}
    assert body["sources"]["jira.api_token"] == "org"
    assert "org-jira-token" not in resp.text and "lin_org_key" not in resp.text

    async with TestSessionLocal() as session:
        row = await session.scalar(
            select(AppSetting).where(
                AppSetting.key == TRACKER_DEFAULTS_KEY, AppSetting.organization_id == ORG_A_ID
            )
        )
        assert row is not None
        # Encrypted at rest.
        assert row.value["jira_api_token"] != "org-jira-token"
        assert crypto.decrypt_value(row.value["jira_api_token"]) == "org-jira-token"
        audit = await session.scalar(
            select(AuditLog)
            .where(AuditLog.action == "settings.update")
            .order_by(AuditLog.created_at.desc())
        )
        assert audit is not None
        assert audit.organization_id == ORG_A_ID
        assert audit.payload["section"] == "trackers"
        assert "org-jira-token" not in str(audit.payload)
        # Bravo has none.
        assert await defaults_service.get_org_tracker_defaults(session, ORG_B_ID) == NO_DEFAULTS

    # Inherit again: null clears.
    cleared = await hosted.a_admin.patch(
        f"{API}/orgs/alpha/settings/trackers", json={"jira": {"api_token": None}}
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["jira"]["api_token_configured"] is False
    assert cleared.json()["jira"]["base_url"] == "https://alpha.atlassian.net"


@pytest.mark.parametrize(
    "payload",
    [
        {"jira": {"base_url": "https://169.254.169.254"}},
        {"jira": {"base_url": "http://alpha.atlassian.net"}},
        {"jira": {"project_key": "not a key"}},
        {"linear": {"team_id": "bad id!"}},
        {"jira": {"issue_type": "Bug"}},
    ],
)
@pytest.mark.asyncio
async def test_invalid_or_private_defaults_are_refused(
    hosted: Hosted, payload: dict[str, Any]
) -> None:
    resp = await hosted.a_admin.patch(f"{API}/orgs/alpha/settings/trackers", json=payload)
    assert resp.status_code == 422, resp.text
    async with TestSessionLocal() as session:
        assert await defaults_service.get_stored_defaults(session, ORG_A_ID) == {}


@pytest.mark.asyncio
async def test_tracker_defaults_are_for_the_orgs_admins(hosted: Hosted) -> None:
    for method in ("GET", "PATCH"):
        kwargs: dict[str, Any] = {"json": _DEFAULTS_BODY} if method == "PATCH" else {}
        member = await hosted.a_member.request(
            method, f"{API}/orgs/alpha/settings/trackers", **kwargs
        )
        assert member.status_code == 403, member.text
        stranger = await hosted.b_owner.request(
            method, f"{API}/orgs/alpha/settings/trackers", **kwargs
        )
        assert stranger.status_code == 404, stranger.text


@pytest.mark.asyncio
async def test_the_project_config_reports_what_it_inherits(hosted: Hosted) -> None:
    async with TestSessionLocal() as session:
        session.add(Project(name="P", slug="p-alpha", organization_id=ORG_A_ID))
        await session.commit()
    saved = await hosted.a_admin.patch(f"{API}/orgs/alpha/settings/trackers", json=_DEFAULTS_BODY)
    assert saved.status_code == 200, saved.text

    fresh = await hosted.a_admin.get(f"{API}/orgs/alpha/projects/p-alpha/tracker-config")
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["inherited_fields"] == [
        "api_token",
        "auth_email",
        "base_url",
        "project_key",
    ]
    own = await hosted.a_admin.patch(
        f"{API}/orgs/alpha/projects/p-alpha/tracker-config",
        json={"enabled": True, "project_key": "OWN"},
    )
    assert own.status_code == 200, own.text
    assert own.json()["inherited_fields"] == ["api_token", "auth_email", "base_url"]
    assert own.json()["api_token_set"] is False
