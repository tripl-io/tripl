"""A public demo sends nothing out of the instance (tripl-sav5.3).

Generated demo projects are already zero-egress (their only alert destination
is the local ``demo_sink``). On a public demo every other way out is closed: a
real project (which could gain Slack, webhook or email destinations), an issue
tracker, the audit webhook, an organization's own SMTP / AI / storage / SSO,
SCIM, invitations, and more organizations. Reading those settings still works.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tripl.config import settings

API = "/api/v1"


@pytest.fixture(autouse=True)
def public_demo(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """After ``client`` has signed up: a public demo takes no password sign-ups."""
    monkeypatch.setattr(settings, "public_demo", True)


@pytest.mark.parametrize(
    ("method", "path", "body", "what"),
    [
        (
            "post",
            "/projects",
            {"name": "Real one", "slug": "real-one"},
            "create projects other than demo ones",
        ),
        ("post", "/orgs", {"name": "Another", "slug": "another"}, "create more organizations"),
        (
            "post",
            "/users/invitations",
            {"email": "someone@example.com", "role": "editor"},
            "send invitations",
        ),
        ("patch", "/orgs/default/settings", {}, "change organization settings"),
        ("post", "/orgs/default/settings/email/test", {}, "change organization settings"),
        ("put", "/orgs/default/sso", {}, "configure single sign-on"),
        ("post", "/orgs/default/scim/tokens", {}, "provision users over SCIM"),
        ("put", "/audit/webhook", {}, "send audit events to a webhook"),
    ],
)
async def test_every_way_out_is_closed(
    client: AsyncClient, method: str, path: str, body: dict[str, object], what: str
) -> None:
    resp = await client.request(method.upper(), f"{API}{path}", json=body)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == f"This public demo does not {what}."


async def test_settings_still_read(client: AsyncClient) -> None:
    resp = await client.get(f"{API}/orgs/default/settings")
    assert resp.status_code == 200, resp.text


async def test_off_the_demo_nothing_changes(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "public_demo", False)
    resp = await client.post(f"{API}/projects", json={"name": "Real one", "slug": "real-one"})
    assert resp.status_code == 201, resp.text


async def test_no_issue_tracker(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "public_demo", False)
    created = await client.post(f"{API}/projects", json={"name": "Tracked", "slug": "tracked"})
    assert created.status_code == 201, created.text
    monkeypatch.setattr(settings, "public_demo", True)

    resp = await client.patch(f"{API}/projects/tracked/tracker-config", json={})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "This public demo does not file tickets in an issue tracker."
    assert (await client.get(f"{API}/projects/tracked/tracker-config")).status_code == 200
