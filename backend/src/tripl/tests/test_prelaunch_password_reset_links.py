"""Pre-launch: a way back into an account on an instance that cannot send email.

Without a mail relay "Forgot your password?" issues nothing, and the sign-in
page told people to ask an owner who had no way to help. Now:

* an owner or admin creates a single-use reset link for a member on the
  Members page (``POST /orgs/{org}/members/{id}/password-reset-link``) and
  hands it over: the emailed reset's own token, redeemed by the ordinary
  confirm, refused where the caller does not already vouch for the account;
* ``tripl-admin password-reset-link <email>`` prints one for the operator,
  for when nobody who could hand one over can sign in.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from tripl import admin_cli
from tripl.api.deps import ORG_ADMIN_REQUIRED, OWNER_SESSION_REQUIRED
from tripl.api.v1._members import MEMBER_NOT_FOUND, OWNER_MANAGEMENT_REQUIRED
from tripl.auth_utils import hash_session_token
from tripl.config import settings
from tripl.main import app
from tripl.models import Base
from tripl.models.audit_log import AuditLog
from tripl.models.organization import DEFAULT_ORG_ID
from tripl.models.password_reset_token import PasswordResetToken
from tripl.models.user import User
from tripl.services import password_reset_links
from tripl.tests._members import add_org_member
from tripl.tests._sqlite import enable_sqlite_foreign_keys
from tripl.tests._tenancy import use_multi_org, use_public_demo
from tripl.tests.conftest import TestSessionLocal

API = "/api/v1"
PASSWORD = "Password123!"
NEW_PASSWORD = "BrandNewPass9!"


def _new_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class People:
    """``root`` registers first: owner of the default organization and platform admin.

    Everyone after joins it as a member, with a verified address (self-hosted).
    """

    def __init__(self) -> None:
        self.clients: dict[str, AsyncClient] = {}
        self.ids: dict[str, uuid.UUID] = {}

    async def register(self, name: str) -> None:
        client = _new_client()
        resp = await client.post(
            f"{API}/auth/register",
            json={"email": f"{name}@example.com", "password": PASSWORD, "name": name},
        )
        assert resp.status_code == 201, resp.text
        self.clients[name] = client
        self.ids[name] = uuid.UUID(resp.json()["id"])

    def __getitem__(self, name: str) -> AsyncClient:
        return self.clients[name]

    async def aclose(self) -> None:
        for client in self.clients.values():
            await client.aclose()


@pytest.fixture
async def people() -> AsyncIterator[People]:
    crowd = People()
    for name in ("root", "alice", "bob", "carol"):
        await crowd.register(name)
    try:
        yield crowd
    finally:
        await crowd.aclose()


async def _set_role(user_id: uuid.UUID, role: str, org_id: uuid.UUID = DEFAULT_ORG_ID) -> None:
    async with TestSessionLocal() as session:
        await add_org_member(session, user_id, role, org_id=org_id)


async def _mint(client: AsyncClient, user_id: uuid.UUID, org: str = "default") -> Response:
    return await client.post(f"{API}/orgs/{org}/members/{user_id}/password-reset-link")


def _token(minted_body: dict[str, str]) -> str:
    query = parse_qs(urlsplit(minted_body["reset_path"]).query)
    return query["reset_token"][0]


async def _login(email: str, password: str) -> int:
    async with _new_client() as anon:
        resp = await anon.post(f"{API}/auth/login", json={"email": email, "password": password})
    return resp.status_code


async def _confirm(token: str, password: str = NEW_PASSWORD) -> int:
    async with _new_client() as anon:
        resp = await anon.post(
            f"{API}/auth/password-reset/confirm",
            json={"token": token, "new_password": password},
        )
    return resp.status_code


# --------------------------------------------------------------------------- #
# The Members page action
# --------------------------------------------------------------------------- #


async def test_an_owner_hands_a_member_a_link_that_resets_the_password_once(
    people: People,
) -> None:
    before = datetime.now(UTC)
    minted = await _mint(people["root"], people.ids["alice"])

    assert minted.status_code == 201, minted.text
    body = minted.json()
    assert body["user_id"] == str(people.ids["alice"])
    assert body["email"] == "alice@example.com"
    assert body["reset_path"].startswith("/auth?reset_token=")
    # The emailed reset's lifetime, not a longer one for a link sent by hand.
    expires_at = datetime.fromisoformat(body["expires_at"])
    assert before < expires_at <= datetime.now(UTC) + timedelta(hours=1)

    # Nothing changes until the link is used.
    assert await _login("alice@example.com", PASSWORD) == 200

    token = _token(body)
    assert await _confirm(token) == 200
    assert await _login("alice@example.com", PASSWORD) == 401
    assert await _login("alice@example.com", NEW_PASSWORD) == 200
    # Single use.
    assert await _confirm(token, "AnotherPass9!x") == 400


async def test_the_link_is_audited_without_its_token(people: People) -> None:
    minted = await _mint(people["root"], people.ids["alice"])
    assert minted.status_code == 201, minted.text

    async with TestSessionLocal() as session:
        rows = (
            await session.scalars(
                select(AuditLog).where(AuditLog.action == "user.password_reset_link")
            )
        ).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.organization_id == DEFAULT_ORG_ID
    assert row.user_id == people.ids["root"]
    assert (row.target_id, row.target_name) == (people.ids["alice"], "alice@example.com")
    assert row.payload == {"role": "member"}
    assert _token(minted.json()) not in str(row.payload)


async def test_a_newer_link_replaces_the_older_one(people: People) -> None:
    first = _token((await _mint(people["root"], people.ids["alice"])).json())
    second = _token((await _mint(people["root"], people.ids["alice"])).json())

    assert await _confirm(first) == 400
    assert await _confirm(second) == 200


async def test_an_admin_hands_a_member_a_link_but_never_an_owner(people: People) -> None:
    await _set_role(people.ids["bob"], "admin")

    assert (await _mint(people["bob"], people.ids["alice"])).status_code == 201
    refused = await _mint(people["bob"], people.ids["root"])
    assert refused.status_code == 403
    assert refused.json()["detail"] == OWNER_MANAGEMENT_REQUIRED


async def test_a_member_cannot_create_one(people: People) -> None:
    refused = await _mint(people["alice"], people.ids["bob"])

    assert refused.status_code == 403
    assert refused.json()["detail"] == ORG_ADMIN_REQUIRED


async def test_nobody_creates_one_for_themselves(people: People) -> None:
    refused = await _mint(people["root"], people.ids["root"])

    assert refused.status_code == 403
    assert refused.json()["detail"] == password_reset_links.OWN_ACCOUNT


async def test_only_a_platform_admin_creates_one_for_a_platform_admin(people: People) -> None:
    # alice owns the organization too, but root is also the instance's operator.
    await _set_role(people.ids["alice"], "owner")
    refused = await _mint(people["alice"], people.ids["root"])
    assert refused.status_code == 403
    assert refused.json()["detail"] == password_reset_links.PLATFORM_ADMIN

    async with TestSessionLocal() as session:
        await session.execute(
            update(User).where(User.id == people.ids["bob"]).values(is_platform_admin=True)
        )
        await session.commit()
    assert (await _mint(people["root"], people.ids["bob"])).status_code == 201


async def test_a_member_of_an_organization_the_caller_does_not_manage_is_refused(
    people: People, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_multi_org(monkeypatch)
    created = await people["root"].post(f"{API}/orgs", json={"slug": "acme", "name": "Acme"})
    assert created.status_code == 201, created.text
    acme = uuid.UUID(created.json()["id"])
    # alice owns acme; carol is a member of acme AND of the default
    # organization, where alice is only a member.
    await _set_role(people.ids["alice"], "owner", acme)
    await _set_role(people.ids["carol"], "member", acme)

    refused = await _mint(people["alice"], people.ids["carol"], org="acme")
    assert refused.status_code == 403
    assert refused.json()["detail"] == password_reset_links.ANOTHER_ORGANIZATION

    # root owns both of carol's organizations.
    assert (await _mint(people["root"], people.ids["carol"], org="acme")).status_code == 201


async def test_an_unverified_account_gets_no_link(people: People) -> None:
    async with TestSessionLocal() as session:
        await session.execute(
            update(User).where(User.id == people.ids["alice"]).values(email_verified_at=None)
        )
        await session.commit()

    refused = await _mint(people["root"], people.ids["alice"])

    assert refused.status_code == 409
    assert refused.json()["detail"] == password_reset_links.UNVERIFIED
    async with TestSessionLocal() as session:
        assert (await session.scalar(select(PasswordResetToken.id))) is None


async def test_an_account_outside_the_organization_is_not_found(people: People) -> None:
    missing = await _mint(people["root"], uuid.uuid4())

    assert missing.status_code == 404
    assert missing.json()["detail"] == MEMBER_NOT_FOUND


async def test_an_api_key_cannot_create_one(people: People) -> None:
    key = await people["root"].post(f"{API}/me/api-keys", json={"name": "k", "scope": "write"})
    assert key.status_code == 201, key.text
    async with _new_client() as bearer:
        refused = await bearer.post(
            f"{API}/orgs/default/members/{people.ids['alice']}/password-reset-link",
            headers={"Authorization": f"Bearer {key.json()['token']}"},
        )

    assert refused.status_code == 403
    assert refused.json()["detail"] == OWNER_SESSION_REQUIRED


async def test_a_public_demo_creates_none(people: People, monkeypatch: pytest.MonkeyPatch) -> None:
    use_public_demo(monkeypatch)

    refused = await _mint(people["root"], people.ids["alice"])

    assert refused.status_code == 403
    assert "password reset links" in refused.json()["detail"]


# --------------------------------------------------------------------------- #
# tripl-admin password-reset-link
# --------------------------------------------------------------------------- #


@pytest.fixture
def shell_db(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """A sync SQLite database, as ``test_admin_cli`` drives the shell tool."""
    engine = create_engine(f"sqlite:///{tmp_path / 'reset_link.db'}")
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    make = sessionmaker(engine, expire_on_commit=False)
    with make() as session:
        # Unverified, as a hosted sign-up can be: the operator vouches for it.
        session.add(User(email="dev@example.com", name="Dev", password_hash="x"))
        session.commit()
    try:
        yield make
    finally:
        engine.dispose()


def _shell(factory: sessionmaker[Session], *argv: str) -> tuple[int, list[str]]:
    out = io.StringIO()
    code = admin_cli.run(list(argv), session_factory=factory, out=out)
    return code, out.getvalue().splitlines()


def test_the_shell_prints_a_link_on_the_public_url(
    shell_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "app_base_url", "https://tripl.example.com/")

    code, lines = _shell(shell_db, "password-reset-link", "  DEV@Example.com ")

    assert code == admin_cli.EXIT_OK
    assert lines[0].startswith("Password reset link for dev@example.com (works once, until ")
    assert lines[1].startswith("https://tripl.example.com/auth?reset_token=")
    raw_token = parse_qs(urlsplit(lines[1]).query)["reset_token"][0]
    with shell_db() as session:
        stored = session.scalars(select(PasswordResetToken)).all()
        audit = session.execute(
            select(
                AuditLog.action, AuditLog.target_name, AuditLog.organization_id, AuditLog.payload
            )
        ).all()
    # The emailed reset's own token: only its keyed hash is stored.
    assert [row.token_hash for row in stored] == [hash_session_token(raw_token)]
    assert [tuple(row) for row in audit] == [
        (
            "user.password_reset_link",
            "dev@example.com",
            None,
            {"email": "dev@example.com", "via": "tripl-admin"},
        )
    ]


def test_the_shell_says_when_it_only_has_the_path(
    shell_db: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(settings, "app_base_url", "")

    code, lines = _shell(shell_db, "password-reset-link", "dev@example.com")

    assert code == admin_cli.EXIT_OK
    assert lines[1].startswith("/auth?reset_token=")
    assert "APP_BASE_URL is not set" in capsys.readouterr().err


def test_a_second_shell_link_replaces_the_first(shell_db: sessionmaker[Session]) -> None:
    assert _shell(shell_db, "password-reset-link", "dev@example.com")[0] == admin_cli.EXIT_OK
    assert _shell(shell_db, "password-reset-link", "dev@example.com")[0] == admin_cli.EXIT_OK

    with shell_db() as session:
        assert len(session.scalars(select(PasswordResetToken)).all()) == 1


def test_the_shell_refuses_an_unknown_account(
    shell_db: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    code, lines = _shell(shell_db, "password-reset-link", "nobody@example.com")

    assert code == admin_cli.EXIT_UNKNOWN_USER
    assert lines == []
    assert "No account with email nobody@example.com" in capsys.readouterr().err
