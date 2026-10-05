from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl.config import REGISTRATION_DISABLED, REGISTRATION_OPEN, settings
from tripl.models.organization import DEFAULT_ORG_ID
from tripl.models.password_reset_token import PasswordResetToken
from tripl.models.user import User
from tripl.services import auth_service
from tripl.tests.conftest import TestSessionLocal

RESET_REQUEST_URL = "/api/v1/auth/password-reset/request"
RESET_CONFIRM_URL = "/api/v1/auth/password-reset/confirm"


@pytest.fixture
def reset_email_sink(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """Configure SMTP and capture reset emails instead of sending them.

    Setting ``smtp_host`` (via the settings singleton, reverted after the test)
    flips the request endpoint into "email configured" mode, and stubbing the
    background sender records ``recipient`` + ``reset_link`` so a test can pull
    the raw token out of the link the user would have received.
    """
    from tripl.api.v1 import auth as auth_router

    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_from_address", "noreply@example.com")
    monkeypatch.setattr(settings, "app_base_url", "https://tripl.example.com")

    sent: list[dict[str, str]] = []

    def _capture(*, recipient: str, reset_link: str, email_config: object) -> None:
        sent.append({"recipient": recipient, "reset_link": reset_link})

    monkeypatch.setattr(auth_router, "_send_password_reset_email", _capture)
    return sent


def _token_from_link(reset_link: str) -> str:
    return reset_link.split("reset_token=", 1)[1]


async def _register(client: AsyncClient, email: str, password: str) -> None:
    response = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": password}
    )
    assert response.status_code == 201


@pytest.mark.asyncio
async def test_register_creates_user_and_session_cookie(anon_client: AsyncClient):
    response = await anon_client.post(
        "/api/v1/auth/register",
        json={
            "email": "owner@example.com",
            "password": "Password123!",
            "name": "Owner",
        },
    )

    assert response.status_code == 201
    assert response.json()["email"] == "owner@example.com"
    assert "tripl_session=" in response.headers["set-cookie"]


@pytest.mark.asyncio
async def test_register_rejects_weak_password(anon_client: AsyncClient):
    # Under the unified policy a register password must be >= 12 chars with a
    # number and a symbol; "password" (8 chars, no digit, no symbol) is rejected
    # at the schema boundary before any user is stored.
    response = await anon_client.post(
        "/api/v1/auth/register",
        json={
            "email": "weak@example.com",
            "password": "password",
        },
    )

    assert response.status_code == 422
    body = response.json()
    assert any("password" in error.get("loc", []) for error in body["detail"])


@pytest.mark.asyncio
async def test_status_reports_empty_instance_then_populated(anon_client: AsyncClient):
    fresh = await anon_client.get("/api/v1/auth/status")
    assert fresh.status_code == 200
    assert fresh.json()["has_users"] is False

    register = await anon_client.post(
        "/api/v1/auth/register",
        json={
            "email": "owner@example.com",
            "password": "Password123!",
        },
    )
    assert register.status_code == 201

    populated = await anon_client.get("/api/v1/auth/status")
    assert populated.status_code == 200
    assert populated.json()["has_users"] is True


@pytest.mark.asyncio
async def test_first_owner_lock_is_noop_off_postgres():
    # The first-owner TOCTOU guard is the default organization's owner-set
    # pg_advisory_xact_lock, taken before the has_any_users() check; PostgreSQL
    # serialises concurrent first registrations there. On SQLite (this suite)
    # the helper must be a silent no-op — it must not emit SQL the dialect can't parse or raise. The
    # behavioural race itself is untestable here: the suite runs on a single
    # in-memory connection, so two registrations can never interleave.
    async with TestSessionLocal() as session:
        await auth_service.acquire_owner_set_xact_lock(session, DEFAULT_ORG_ID)


@pytest.mark.asyncio
async def test_login_returns_cookie_and_me(anon_client: AsyncClient):
    await anon_client.post(
        "/api/v1/auth/register",
        json={
            "email": "owner@example.com",
            "password": "Password123!",
        },
    )

    login_response = await anon_client.post(
        "/api/v1/auth/login",
        json={
            "email": "OWNER@example.com",
            "password": "Password123!",
        },
    )

    assert login_response.status_code == 200

    me_response = await anon_client.get("/api/v1/auth/me")
    assert me_response.status_code == 200
    assert me_response.json()["email"] == "owner@example.com"


@pytest.mark.asyncio
async def test_protected_route_requires_auth(anon_client: AsyncClient):
    response = await anon_client.get("/api/v1/projects")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_logout_clears_cookie_and_blocks_follow_up_request(client: AsyncClient):
    logout_response = await client.post("/api/v1/auth/logout")
    assert logout_response.status_code == 204

    me_response = await client.get("/api/v1/auth/me")
    assert me_response.status_code == 401


@pytest.mark.asyncio
async def test_password_reset_request_does_not_reveal_account_existence(
    anon_client: AsyncClient, reset_email_sink: list[dict[str, str]]
):
    # A registered address and a stranger's address must produce byte-identical
    # responses, so the endpoint can't be used to enumerate accounts.
    await _register(anon_client, "known@example.com", "Password123!")

    known = await anon_client.post(RESET_REQUEST_URL, json={"email": "known@example.com"})
    unknown = await anon_client.post(RESET_REQUEST_URL, json={"email": "ghost@example.com"})

    assert known.status_code == 200
    assert unknown.status_code == 200
    assert known.json() == unknown.json()
    assert known.json()["email_configured"] is True
    # Only the real account triggers an outbound email; the stranger triggers none.
    assert [entry["recipient"] for entry in reset_email_sink] == ["known@example.com"]


@pytest.mark.asyncio
async def test_password_reset_request_sends_nothing_when_email_unconfigured(
    anon_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    from tripl.api.v1 import auth as auth_router

    # No SMTP host → email is not configured for this instance.
    monkeypatch.setattr(settings, "smtp_host", "")
    sent: list[str] = []
    monkeypatch.setattr(
        auth_router,
        "_send_password_reset_email",
        lambda **kwargs: sent.append(kwargs["recipient"]),
    )

    await _register(anon_client, "solo@example.com", "Password123!")
    response = await anon_client.post(RESET_REQUEST_URL, json={"email": "solo@example.com"})

    assert response.status_code == 200
    assert response.json()["email_configured"] is False
    # Neither an email nor a token is produced when delivery is impossible.
    assert sent == []
    async with TestSessionLocal() as session:
        tokens = (await session.scalars(select(PasswordResetToken))).all()
    assert tokens == []


@pytest.mark.asyncio
async def test_password_reset_happy_path_sets_new_password(
    anon_client: AsyncClient, reset_email_sink: list[dict[str, str]]
):
    await _register(anon_client, "reset@example.com", "Password123!")

    request = await anon_client.post(RESET_REQUEST_URL, json={"email": "reset@example.com"})
    assert request.status_code == 200
    assert len(reset_email_sink) == 1
    assert reset_email_sink[0]["recipient"] == "reset@example.com"
    token = _token_from_link(reset_email_sink[0]["reset_link"])

    new_password = "FreshPassword9!"
    confirm = await anon_client.post(
        RESET_CONFIRM_URL, json={"token": token, "new_password": new_password}
    )
    assert confirm.status_code == 200

    # Old credentials no longer work; the new ones do.
    old_login = await anon_client.post(
        "/api/v1/auth/login",
        json={"email": "reset@example.com", "password": "Password123!"},
    )
    assert old_login.status_code == 401
    new_login = await anon_client.post(
        "/api/v1/auth/login",
        json={"email": "reset@example.com", "password": new_password},
    )
    assert new_login.status_code == 200


@pytest.mark.asyncio
async def test_password_reset_confirm_is_single_use(
    anon_client: AsyncClient, reset_email_sink: list[dict[str, str]]
):
    await _register(anon_client, "once@example.com", "Password123!")
    await anon_client.post(RESET_REQUEST_URL, json={"email": "once@example.com"})
    token = _token_from_link(reset_email_sink[0]["reset_link"])

    first = await anon_client.post(
        RESET_CONFIRM_URL, json={"token": token, "new_password": "FirstNewPass9!"}
    )
    assert first.status_code == 200

    # The same token cannot be redeemed twice.
    second = await anon_client.post(
        RESET_CONFIRM_URL, json={"token": token, "new_password": "SecondNewPass9!"}
    )
    assert second.status_code == 400

    # The second (rejected) password never took effect.
    relogin = await anon_client.post(
        "/api/v1/auth/login",
        json={"email": "once@example.com", "password": "SecondNewPass9!"},
    )
    assert relogin.status_code == 401


@pytest.mark.asyncio
async def test_password_reset_confirm_rejects_expired_token(
    anon_client: AsyncClient, reset_email_sink: list[dict[str, str]]
):
    await _register(anon_client, "stale@example.com", "Password123!")
    await anon_client.post(RESET_REQUEST_URL, json={"email": "stale@example.com"})
    token = _token_from_link(reset_email_sink[0]["reset_link"])

    async with TestSessionLocal() as session:
        row = await session.scalar(select(PasswordResetToken))
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()

    confirm = await anon_client.post(
        RESET_CONFIRM_URL, json={"token": token, "new_password": "AfterExpiry9!"}
    )
    assert confirm.status_code == 400


@pytest.mark.asyncio
async def test_password_reset_confirm_rejects_unknown_token(anon_client: AsyncClient):
    confirm = await anon_client.post(
        RESET_CONFIRM_URL,
        json={"token": "definitely-not-a-real-token", "new_password": "Whatever123!"},
    )
    assert confirm.status_code == 400


@pytest.mark.asyncio
async def test_password_reset_confirm_enforces_password_policy(
    anon_client: AsyncClient, reset_email_sink: list[dict[str, str]]
):
    await _register(anon_client, "weakpw@example.com", "Password123!")
    await anon_client.post(RESET_REQUEST_URL, json={"email": "weakpw@example.com"})
    token = _token_from_link(reset_email_sink[0]["reset_link"])

    # "short" fails the shared policy (>= 12 chars, a digit and a symbol) at the
    # schema boundary — a valid token does not exempt a weak new password.
    confirm = await anon_client.post(
        RESET_CONFIRM_URL, json={"token": token, "new_password": "short"}
    )
    assert confirm.status_code == 422

    body = confirm.json()
    assert any("new_password" in error.get("loc", []) for error in body["detail"])


# --- registration policy -------------------------------------


@pytest.mark.asyncio
async def test_closed_instance_still_allows_the_first_owner(
    anon_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    # An instance whose operator closed registration must still be claimable
    # after a reset: the bootstrap registration on an empty instance is exempt
    # from the policy.
    monkeypatch.setattr(settings, "registration_mode", REGISTRATION_DISABLED)

    first = await anon_client.post(
        "/api/v1/auth/register",
        json={"email": "bootstrap@example.com", "password": "Password123!"},
    )

    assert first.status_code == 201
    assert first.json()["role"] == "owner"


@pytest.mark.asyncio
async def test_closed_instance_rejects_every_registration_after_the_first(
    anon_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "registration_mode", REGISTRATION_DISABLED)
    await _register(anon_client, "closed-owner@example.com", "Password123!")
    await anon_client.post("/api/v1/auth/logout")

    stranger = await anon_client.post(
        "/api/v1/auth/register",
        json={"email": "stranger@example.com", "password": "Password123!"},
    )

    assert stranger.status_code == 403
    assert "registration is closed" in stranger.json()["detail"].lower()
    # No account and no session were handed out.
    assert "set-cookie" not in stranger.headers
    async with TestSessionLocal() as session:
        emails = set((await session.scalars(select(User.email))).all())
    assert emails == {"closed-owner@example.com"}


@pytest.mark.asyncio
async def test_closed_registration_does_not_leak_which_emails_exist(
    anon_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    # The policy check runs before the duplicate-email lookup, so a closed
    # instance answers identically for a known and an unknown address.
    monkeypatch.setattr(settings, "registration_mode", REGISTRATION_DISABLED)
    await _register(anon_client, "known-user@example.com", "Password123!")
    await anon_client.post("/api/v1/auth/logout")

    known = await anon_client.post(
        "/api/v1/auth/register",
        json={"email": "known-user@example.com", "password": "Password123!"},
    )
    unknown = await anon_client.post(
        "/api/v1/auth/register",
        json={"email": "nobody@example.com", "password": "Password123!"},
    )

    assert known.status_code == unknown.status_code == 403
    assert known.json() == unknown.json()


@pytest.mark.asyncio
async def test_open_mode_allows_registration_after_the_first_user(
    anon_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "registration_mode", REGISTRATION_OPEN)
    await _register(anon_client, "open-owner@example.com", "Password123!")
    await anon_client.post("/api/v1/auth/logout")

    second = await anon_client.post(
        "/api/v1/auth/register",
        json={"email": "teammate@example.com", "password": "Password123!"},
    )

    assert second.status_code == 201
    assert second.json()["role"] == "member"
    assert second.json()["is_platform_admin"] is False


@pytest.mark.asyncio
async def test_owner_can_close_registration_without_a_restart(
    anon_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    # The env default is open, but the persisted instance-setting override wins
    # and takes effect on the very next request — no redeploy.
    monkeypatch.setattr(settings, "registration_mode", REGISTRATION_OPEN)
    await _register(anon_client, "toggle-owner@example.com", "Password123!")

    patched = await anon_client.patch(
        "/api/v1/settings",
        json={"security": {"registration_mode": "disabled"}},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["security"]["registration_mode"] == "disabled"
    assert "registration_mode" in patched.json()["overridden_fields"]

    await anon_client.post("/api/v1/auth/logout")
    blocked = await anon_client.post(
        "/api/v1/auth/register",
        json={"email": "too-late@example.com", "password": "Password123!"},
    )
    assert blocked.status_code == 403


@pytest.mark.asyncio
async def test_owner_can_open_registration_on_a_closed_instance(
    anon_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "registration_mode", REGISTRATION_DISABLED)
    await _register(anon_client, "invite-owner@example.com", "Password123!")

    patched = await anon_client.patch(
        "/api/v1/settings",
        json={"security": {"registration_mode": "open"}},
    )
    assert patched.status_code == 200, patched.text

    await anon_client.post("/api/v1/auth/logout")
    invited = await anon_client.post(
        "/api/v1/auth/register",
        json={"email": "invited@example.com", "password": "Password123!"},
    )
    assert invited.status_code == 201


@pytest.mark.asyncio
async def test_settings_reject_an_unknown_registration_mode(
    anon_client: AsyncClient,
):
    await _register(anon_client, "strict-owner@example.com", "Password123!")

    resp = await anon_client.patch(
        "/api/v1/settings",
        json={"security": {"registration_mode": "invite-only"}},
    )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_auth_status_reports_whether_registration_is_accepted(
    anon_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "registration_mode", REGISTRATION_DISABLED)

    # Empty instance: the bootstrap signup is always on offer.
    empty = await anon_client.get("/api/v1/auth/status")
    assert empty.json() == {
        "has_users": False,
        "registration_enabled": True,
        "email_configured": False,
        "deployment_mode": "self_hosted",
        "email_verification_required": False,
        "google_sign_in": False,
        "oidc_sign_in": False,
        "oidc_button_label": None,
        "public_demo": False,
        "multi_org": False,
    }

    await _register(anon_client, "status-owner@example.com", "Password123!")

    provisioned = await anon_client.get("/api/v1/auth/status")
    assert provisioned.json() == {
        "has_users": True,
        "registration_enabled": False,
        "email_configured": False,
        "deployment_mode": "self_hosted",
        "email_verification_required": False,
        "google_sign_in": False,
        "oidc_sign_in": False,
        "oidc_button_label": None,
        "public_demo": False,
        "multi_org": False,
    }
