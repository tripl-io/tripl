"""An organization's audit webhook (F20, GH #273).

* the settings are an OWNER's, from a browser session (an admin and a member
  403, a stranger 404, any API key 403); the secret is generated server-side,
  shown once (create, rotate), stored encrypted and never in an audit row;
* the URL must be https, and on a hosted instance public — at save and at send;
* an outbox row is written in the audit row's own transaction, only for an
  organization with an enabled webhook;
* the beat task signs (HMAC-SHA256 of ``t=<ts>.<body>``), treats 2xx as sent,
  refuses redirects, backs off 1m/5m/30m/2h/6h and gives up after 8 attempts,
  skips suspended organizations, and purges sent rows after 7 days and dead
  ones after 30.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import re
import socket
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from tripl import crypto
from tripl.config import settings
from tripl.main import app
from tripl.models import Base
from tripl.models.audit_log import AuditLog
from tripl.models.audit_webhook import AuditWebhookOutbox, AuditWebhookStatus, OrgAuditWebhook
from tripl.models.domain_enums import OrganizationStatus
from tripl.models.organization import Organization
from tripl.services import audit_service, audit_webhook_delivery, safe_http
from tripl.services.audit_rows import COLUMNS, row_record
from tripl.services.oidc import idp_http
from tripl.services.safe_http import HttpResponse
from tripl.tests._members import add_org_member
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.celery_app import celery_app
from tripl.worker.tasks import audit_webhook as task

PASSWORD = "Password123!"
API = "/api/v1"
ACME = "acme"
WEBHOOK_URL = f"{API}/orgs/{ACME}/audit/webhook"
RECEIVER = "https://siem.example.com/hooks/tripl"


def _new_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _verify(secret: str, headers: dict[str, str], body: bytes) -> None:
    """What a receiver does: recompute the signature over ``t=<ts>.<body>``."""
    timestamp = headers["X-Tripl-Timestamp"]
    expected = (
        "sha256="
        + hmac.new(secret.encode(), f"t={timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    )
    assert hmac.compare_digest(expected, headers["X-Tripl-Signature"])


@dataclass
class FakeReceiver:
    """Stands in for the network at ``audit_webhook_delivery._send``."""

    status: int = 200
    requests: list[tuple[str, dict[str, str], bytes]] = field(default_factory=list)

    def send(self, url: str, headers: dict[str, str], body: bytes) -> HttpResponse:
        self.requests.append((url, dict(headers), body))
        return HttpResponse(status=self.status, body=b"ignored")


@pytest.fixture
def receiver(monkeypatch: pytest.MonkeyPatch) -> FakeReceiver:
    fake = FakeReceiver()
    monkeypatch.setattr(audit_webhook_delivery, "_send", fake.send)
    return fake


@pytest.fixture
def encryption_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "encryption_key", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()
    yield
    crypto._fernet.cache_clear()


@dataclass
class World:
    """``root`` owns acme and globex; ``bob`` is acme's admin, ``mia`` a member."""

    clients: dict[str, AsyncClient]
    acme_id: uuid.UUID
    globex_id: uuid.UUID

    def __getitem__(self, name: str) -> AsyncClient:
        return self.clients[name]


@pytest.fixture
async def world() -> AsyncIterator[World]:
    clients: dict[str, AsyncClient] = {}
    ids: dict[str, uuid.UUID] = {}
    for name in ("root", "bob", "mia", "carol"):
        client = _new_client()
        resp = await client.post(
            f"{API}/auth/register",
            json={"email": f"{name}@example.com", "password": PASSWORD, "name": name},
        )
        assert resp.status_code == 201, resp.text
        clients[name] = client
        ids[name] = uuid.UUID(resp.json()["id"])
    try:
        acme = await clients["root"].post(f"{API}/orgs", json={"slug": ACME, "name": "Acme"})
        assert acme.status_code == 201, acme.text
        globex = await clients["root"].post(
            f"{API}/orgs", json={"slug": "globex", "name": "Globex"}
        )
        assert globex.status_code == 201, globex.text
        acme_id = uuid.UUID(acme.json()["id"])
        async with TestSessionLocal() as session:
            await add_org_member(session, ids["bob"], "admin", org_id=acme_id)
            await add_org_member(session, ids["mia"], "member", org_id=acme_id)
        yield World(clients=clients, acme_id=acme_id, globex_id=uuid.UUID(globex.json()["id"]))
    finally:
        for client in clients.values():
            await client.aclose()


async def _audit_rows(action_prefix: str) -> list[AuditLog]:
    async with TestSessionLocal() as session:
        return list(
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.action.startswith(action_prefix))
                    .order_by(AuditLog.created_at, AuditLog.id)
                )
            )
            .scalars()
            .all()
        )


async def _outbox_count() -> int:
    async with TestSessionLocal() as session:
        return int(await session.scalar(select(func.count()).select_from(AuditWebhookOutbox)) or 0)


# ── the settings ─────────────────────────────────────────────────────────────


async def test_only_an_owner_session_manages_the_webhook(world: World) -> None:
    body = {"url": RECEIVER, "enabled": True}
    for name in ("bob", "mia"):
        assert (await world[name].get(WEBHOOK_URL)).status_code == 403, name
        assert (await world[name].put(WEBHOOK_URL, json=body)).status_code == 403, name
        assert (await world[name].get(f"{WEBHOOK_URL}/deliveries")).status_code == 403, name
    # A stranger does not learn that acme exists.
    assert (await world["carol"].get(WEBHOOK_URL)).status_code == 404
    assert (await world["carol"].put(WEBHOOK_URL, json=body)).status_code == 404

    # The owner's own key, at any scope, is refused: browser session only.
    for scope in ("read", "write"):
        minted = await world["root"].post(
            f"{API}/orgs/{ACME}/me/api-keys", json={"name": f"hook-{scope}", "scope": scope}
        )
        assert minted.status_code == 201, minted.text
        async with _new_client() as bearer:
            headers = {"Authorization": f"Bearer {minted.json()['token']}"}
            assert (await bearer.get(WEBHOOK_URL, headers=headers)).status_code == 403
            assert (await bearer.put(WEBHOOK_URL, json=body, headers=headers)).status_code == 403

    assert (await world["root"].get(WEBHOOK_URL)).json()["configured"] is False


async def test_the_secret_is_shown_once_and_stored_encrypted(
    world: World, encryption_key: None
) -> None:
    root = world["root"]
    created = await root.put(WEBHOOK_URL, json={"url": RECEIVER, "enabled": True})
    assert created.status_code == 200, created.text
    secret = created.json()["secret"]
    assert secret.startswith("whsec_")
    assert created.json()["secret_configured"] is True

    read = await root.get(WEBHOOK_URL)
    assert read.status_code == 200
    assert read.json()["configured"] is True
    assert read.json()["url"] == RECEIVER
    assert "secret" not in read.json()
    assert secret not in read.text

    async with TestSessionLocal() as session:
        stored = await session.scalar(
            select(OrgAuditWebhook).where(OrgAuditWebhook.organization_id == world.acme_id)
        )
    assert stored is not None
    assert stored.secret_encrypted != secret
    assert crypto.decrypt_value(stored.secret_encrypted) == secret

    # An update keeps the secret and does not show it again.
    updated = await root.put(WEBHOOK_URL, json={"url": RECEIVER + "/v2", "enabled": False})
    assert updated.status_code == 200, updated.text
    assert updated.json()["secret"] is None
    assert updated.json()["enabled"] is False

    rotated = await root.post(f"{WEBHOOK_URL}/rotate-secret")
    assert rotated.status_code == 200, rotated.text
    new_secret = rotated.json()["secret"]
    assert new_secret.startswith("whsec_") and new_secret != secret

    deleted = await root.delete(WEBHOOK_URL)
    assert deleted.status_code == 204
    assert (await root.get(WEBHOOK_URL)).json()["configured"] is False
    assert (await root.delete(WEBHOOK_URL)).status_code == 404
    assert (await root.post(f"{WEBHOOK_URL}/rotate-secret")).status_code == 404

    rows = await _audit_rows("org.audit_webhook.")
    # A set: rows of one second share ``created_at`` on SQLite.
    assert sorted(row.action for row in rows) == [
        "org.audit_webhook.create",
        "org.audit_webhook.delete",
        "org.audit_webhook.rotate_secret",
        "org.audit_webhook.update",
    ]
    for row in rows:
        assert row.organization_id == world.acme_id
        text = json.dumps(row.payload)
        assert secret not in text and new_secret not in text
        # The host only: a webhook URL may carry a token in its path.
        assert "/hooks/tripl" not in text
        assert row.payload["host"] == "siem.example.com"


async def test_the_url_must_be_https_and_public_when_hosted(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = world["root"]
    for url in (
        "http://siem.example.com/hook",
        "https://user:pass@siem.example.com/hook",
        "https://siem.example.com/hook#frag",
        "ftp://siem.example.com",
        "not a url",
    ):
        resp = await root.put(WEBHOOK_URL, json={"url": url})
        assert resp.status_code == 422, f"{url}: {resp.text}"

    monkeypatch.setattr(settings, "deployment_mode", "hosted")
    private = await root.put(WEBHOOK_URL, json={"url": "https://10.0.0.5/hook"})
    assert private.status_code == 422, private.text
    assert "private" in private.text
    metadata = await root.put(WEBHOOK_URL, json={"url": "https://169.254.169.254/latest"})
    assert metadata.status_code == 422, metadata.text


async def test_the_test_button_sends_a_signed_synthetic_event(
    world: World, receiver: FakeReceiver, encryption_key: None
) -> None:
    root = world["root"]
    secret = (await root.put(WEBHOOK_URL, json={"url": RECEIVER})).json()["secret"]

    tested = await root.post(f"{WEBHOOK_URL}/test")

    assert tested.status_code == 200, tested.text
    assert tested.json()["ok"] is True
    assert tested.json()["status_code"] == 200
    url, headers, body = receiver.requests[-1]
    assert url == RECEIVER
    _verify(secret, headers, body)
    event = json.loads(body)
    assert list(event) == list(COLUMNS)
    assert event["action"] == "audit.webhook_test"
    assert event["org_slug"] == ACME
    assert headers["X-Tripl-Event-Id"] == event["id"] == tested.json()["event_id"]

    receiver.status = 500
    failed = await root.post(f"{WEBHOOK_URL}/test")
    assert failed.status_code == 200
    assert failed.json()["ok"] is False
    assert failed.json()["status_code"] == 500
    assert failed.json()["error"] == "HTTP 500"

    receiver.status = 302
    redirected = await root.post(f"{WEBHOOK_URL}/test")
    assert redirected.json()["ok"] is False
    assert redirected.json()["error"] == "redirect refused"


async def test_a_hosted_test_to_a_name_now_private_never_connects(
    world: World, receiver: FakeReceiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Saved while public; the name now resolves inside. Refused at send."""
    root = world["root"]
    assert (await root.put(WEBHOOK_URL, json={"url": RECEIVER})).status_code == 200
    monkeypatch.setattr(settings, "deployment_mode", "hosted")
    monkeypatch.setattr("socket.getaddrinfo", lambda *_a, **_k: [(2, 1, 6, "", ("10.0.0.5", 0))])

    tested = await root.post(f"{WEBHOOK_URL}/test")

    assert tested.status_code == 200, tested.text
    assert tested.json()["ok"] is False
    assert tested.json()["error"] == "private address refused"
    assert "10.0.0.5" not in tested.text
    assert receiver.requests == []


# ── the outbox ───────────────────────────────────────────────────────────────


async def _add_webhook(org_id: uuid.UUID, *, enabled: bool = True) -> None:
    async with TestSessionLocal() as session:
        session.add(
            OrgAuditWebhook(
                organization_id=org_id, url=RECEIVER, secret_encrypted="s", enabled=enabled
            )
        )
        await session.commit()


async def test_an_outbox_row_is_written_in_the_audit_rows_transaction(world: World) -> None:
    await _add_webhook(world.acme_id)
    before = await _outbox_count()

    # Rolled back: neither the audit row nor its delivery exists.
    async with TestSessionLocal() as session:
        await audit_service.record(
            session,
            user=None,
            action="org.update",
            target_type="organization",
            target_id=world.acme_id,
            target_name="rolled-back",
            organization_id=world.acme_id,
            commit=False,
        )
        await session.rollback()
    assert await _outbox_count() == before
    assert all(row.target_name != "rolled-back" for row in await _audit_rows("org.update"))

    # Committed: both, linked.
    async with TestSessionLocal() as session:
        entry = await audit_service.record(
            session,
            user=None,
            action="org.update",
            target_type="organization",
            target_id=world.acme_id,
            target_name="committed",
            organization_id=world.acme_id,
        )
        entry_id = entry.id
    async with TestSessionLocal() as session:
        outbox = (
            await session.execute(
                select(AuditWebhookOutbox).where(AuditWebhookOutbox.audit_log_id == entry_id)
            )
        ).scalar_one()
    assert outbox.organization_id == world.acme_id
    assert outbox.status == AuditWebhookStatus.pending.value
    assert outbox.attempts == 0


async def test_only_an_enabled_webhook_of_the_rows_organization_queues(world: World) -> None:
    await _add_webhook(world.globex_id, enabled=False)
    before = await _outbox_count()
    async with TestSessionLocal() as session:
        for org_id in (world.acme_id, world.globex_id, None):
            await audit_service.record(
                session,
                user=None,
                action="org.update",
                target_type="organization",
                target_id=None,
                organization_id=org_id,
                commit=False,
            )
        await session.commit()
    # acme has none, globex's is disabled, a platform row has no organization.
    assert await _outbox_count() == before


async def test_changes_through_the_api_are_queued_and_listed(
    world: World, encryption_key: None
) -> None:
    root = world["root"]
    assert (await root.put(WEBHOOK_URL, json={"url": RECEIVER})).status_code == 200
    assert (await root.put(WEBHOOK_URL, json={"url": RECEIVER + "/v2"})).status_code == 200

    listed = await root.get(f"{WEBHOOK_URL}/deliveries")

    assert listed.status_code == 200, listed.text
    actions = [row["action"] for row in listed.json()]
    # Newest first; the create row is queued by the webhook it created.
    assert actions == ["org.audit_webhook.update", "org.audit_webhook.create"]
    assert {row["status"] for row in listed.json()} == {"pending"}
    sent = await root.get(f"{WEBHOOK_URL}/deliveries", params={"status": "sent"})
    assert sent.json() == []
    assert (
        await root.get(f"{WEBHOOK_URL}/deliveries", params={"status": "bogus"})
    ).status_code == 422
    assert (await root.get(f"{WEBHOOK_URL}/deliveries", params={"limit": 0})).status_code == 422

    # Deleting the webhook drops its queue.
    assert (await root.delete(WEBHOOK_URL)).status_code == 204
    assert await _outbox_count() == 0


# ── the delivery task ────────────────────────────────────────────────────────

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
SECRET = "whsec_test-secret"


@pytest.fixture
def sync_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'audit_webhook.db'}")
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


@dataclass
class Seed:
    org_id: uuid.UUID
    hook_id: uuid.UUID


def _seed_org(
    factory: sessionmaker[Session],
    slug: str,
    *,
    status: str = OrganizationStatus.active.value,
    url: str = RECEIVER,
) -> Seed:
    org_id = uuid.uuid4()
    hook_id = uuid.uuid4()
    with factory() as session:
        session.add(Organization(id=org_id, slug=slug, name=slug.title(), status=status))
        session.add(
            OrgAuditWebhook(
                id=hook_id,
                organization_id=org_id,
                url=url,
                secret_encrypted=crypto.encrypt_value(SECRET),
                enabled=True,
            )
        )
        session.commit()
    return Seed(org_id=org_id, hook_id=hook_id)


def _queue(
    factory: sessionmaker[Session],
    org_id: uuid.UUID,
    *,
    created_at: datetime = NOW - timedelta(minutes=1),
    status: str = AuditWebhookStatus.pending.value,
    next_attempt_at: datetime | None = None,
    attempts: int = 0,
    target_name: str = "checkout",
) -> tuple[uuid.UUID, uuid.UUID]:
    """An audit row of ``org_id`` and its outbox row: ``(audit_id, outbox_id)``."""
    audit_id = uuid.uuid4()
    outbox_id = uuid.uuid4()
    with factory() as session:
        session.add(
            AuditLog(
                id=audit_id,
                created_at=created_at,
                organization_id=org_id,
                user_email="root@example.com",
                project_slug="shop",
                action="event.update",
                target_type="event",
                target_id=uuid.uuid4(),
                target_name=target_name,
                payload={"field": "description"},
            )
        )
        session.flush()
        session.add(
            AuditWebhookOutbox(
                id=outbox_id,
                organization_id=org_id,
                audit_log_id=audit_id,
                status=status,
                attempts=attempts,
                next_attempt_at=next_attempt_at or created_at,
                created_at=created_at,
            )
        )
        session.commit()
    return audit_id, outbox_id


def _outbox(factory: sessionmaker[Session], outbox_id: uuid.UUID) -> AuditWebhookOutbox | None:
    with factory() as session:
        return session.get(AuditWebhookOutbox, outbox_id)


def _tick(factory: sessionmaker[Session], now: datetime) -> dict[str, int]:
    with factory() as session:
        return task.deliver_due(session, now)


def test_a_2xx_is_sent_and_signed(
    sync_factory: sessionmaker[Session], receiver: FakeReceiver
) -> None:
    seed = _seed_org(sync_factory, "acme")
    audit_id, outbox_id = _queue(sync_factory, seed.org_id)

    stats = _tick(sync_factory, NOW)

    assert stats["sent"] == 1
    row = _outbox(sync_factory, outbox_id)
    assert row is not None
    assert row.status == AuditWebhookStatus.sent.value
    assert row.attempts == 1
    assert row.sent_at == NOW
    url, headers, body = receiver.requests[0]
    assert url == RECEIVER
    _verify(SECRET, headers, body)
    assert headers["X-Tripl-Event-Id"] == str(audit_id)
    assert headers["Content-Type"] == "application/json"
    event = json.loads(body)
    assert list(event) == list(COLUMNS)
    assert event["id"] == str(audit_id)
    assert event["org_slug"] == "acme"
    assert event["payload"] == {"field": "description"}
    with sync_factory() as session:
        hook = session.get(OrgAuditWebhook, seed.hook_id)
        assert hook is not None and hook.last_success_at == NOW

    # Sent is final: the next tick sends nothing.
    assert _tick(sync_factory, NOW + timedelta(hours=1))["claimed"] == 0
    assert len(receiver.requests) == 1


def test_a_tampered_body_fails_the_signature() -> None:
    body = b'{"id":"1"}'
    signature = audit_webhook_delivery.sign(SECRET, 1_700_000_000, body)
    headers = {"X-Tripl-Timestamp": "1700000000", "X-Tripl-Signature": signature}
    _verify(SECRET, headers, body)
    with pytest.raises(AssertionError):
        _verify(SECRET, headers, b'{"id":"2"}')
    with pytest.raises(AssertionError):
        _verify(SECRET, {**headers, "X-Tripl-Timestamp": "1700000001"}, body)


def test_failures_back_off_and_die_after_eight_attempts(
    sync_factory: sessionmaker[Session], receiver: FakeReceiver
) -> None:
    receiver.status = 500
    seed = _seed_org(sync_factory, "acme")
    _audit_id, outbox_id = _queue(sync_factory, seed.org_id)

    now = NOW
    delays: list[timedelta] = []
    for attempt in range(1, audit_webhook_delivery.MAX_ATTEMPTS + 1):
        _tick(sync_factory, now)
        row = _outbox(sync_factory, outbox_id)
        assert row is not None
        assert row.attempts == attempt
        assert row.last_error == "HTTP 500"
        if attempt < audit_webhook_delivery.MAX_ATTEMPTS:
            assert row.status == AuditWebhookStatus.failed.value
            delays.append(row.next_attempt_at - now)
            # Not due before its time.
            assert _tick(sync_factory, row.next_attempt_at - timedelta(seconds=1))["claimed"] == 0
            now = row.next_attempt_at
        else:
            assert row.status == AuditWebhookStatus.dead.value
    assert delays == [
        timedelta(minutes=1),
        timedelta(minutes=5),
        timedelta(minutes=30),
        timedelta(hours=2),
        timedelta(hours=6),
        timedelta(hours=6),
        timedelta(hours=6),
    ]
    assert len(receiver.requests) == audit_webhook_delivery.MAX_ATTEMPTS
    # Dead is final.
    assert _tick(sync_factory, now + timedelta(days=1))["claimed"] == 0
    with sync_factory() as session:
        hook = session.get(OrgAuditWebhook, seed.hook_id)
        assert hook is not None and hook.last_error == "HTTP 500"


def test_a_redirect_is_refused_not_followed(
    sync_factory: sessionmaker[Session], receiver: FakeReceiver
) -> None:
    receiver.status = 302
    seed = _seed_org(sync_factory, "acme")
    _audit_id, outbox_id = _queue(sync_factory, seed.org_id)

    _tick(sync_factory, NOW)

    row = _outbox(sync_factory, outbox_id)
    assert row is not None
    assert row.status == AuditWebhookStatus.failed.value
    assert row.last_error == "redirect refused"
    assert len(receiver.requests) == 1


def test_a_suspended_organization_is_skipped(
    sync_factory: sessionmaker[Session], receiver: FakeReceiver
) -> None:
    seed = _seed_org(sync_factory, "paused", status=OrganizationStatus.suspended.value)
    _audit_id, outbox_id = _queue(sync_factory, seed.org_id)

    stats = _tick(sync_factory, NOW)

    assert stats["claimed"] == 0
    assert receiver.requests == []
    row = _outbox(sync_factory, outbox_id)
    assert row is not None and row.status == AuditWebhookStatus.pending.value


def test_a_hosted_send_to_a_private_address_is_refused(
    sync_factory: sessionmaker[Session],
    receiver: FakeReceiver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = _seed_org(sync_factory, "acme", url="https://10.0.0.5/hook")
    _audit_id, outbox_id = _queue(sync_factory, seed.org_id)
    monkeypatch.setattr(settings, "deployment_mode", "hosted")

    _tick(sync_factory, NOW)

    row = _outbox(sync_factory, outbox_id)
    assert row is not None
    assert row.status == AuditWebhookStatus.failed.value
    assert row.last_error == "private address refused"
    assert receiver.requests == []


def test_one_failure_defers_the_rest_of_that_organization(
    sync_factory: sessionmaker[Session], receiver: FakeReceiver
) -> None:
    receiver.status = 503
    seed = _seed_org(sync_factory, "acme")
    _a, first = _queue(sync_factory, seed.org_id, created_at=NOW - timedelta(minutes=3))
    _b, second = _queue(sync_factory, seed.org_id, created_at=NOW - timedelta(minutes=2))

    stats = _tick(sync_factory, NOW)

    assert stats["failed"] == 1 and stats["deferred"] == 1
    assert len(receiver.requests) == 1
    deferred = _outbox(sync_factory, second)
    assert deferred is not None
    assert deferred.attempts == 0
    assert deferred.status == AuditWebhookStatus.pending.value
    assert deferred.next_attempt_at == NOW + task.SIBLING_DEFER
    tried = _outbox(sync_factory, first)
    assert tried is not None and tried.attempts == 1


def test_rows_are_sent_oldest_audit_row_first(
    sync_factory: sessionmaker[Session], receiver: FakeReceiver
) -> None:
    seed = _seed_org(sync_factory, "acme")
    _queue(sync_factory, seed.org_id, created_at=NOW - timedelta(minutes=1), target_name="b")
    _queue(sync_factory, seed.org_id, created_at=NOW - timedelta(minutes=5), target_name="a")

    _tick(sync_factory, NOW)

    assert [json.loads(body)["target_name"] for _u, _h, body in receiver.requests] == ["a", "b"]


def test_a_claimed_row_is_leased_to_one_run(
    sync_factory: sessionmaker[Session], receiver: FakeReceiver
) -> None:
    seed = _seed_org(sync_factory, "acme")
    _audit_id, outbox_id = _queue(sync_factory, seed.org_id)

    with sync_factory() as session:
        claimed = task.claim_due(session, NOW)
    assert claimed == [outbox_id]
    # A second, overlapping run finds nothing due.
    with sync_factory() as session:
        assert task.claim_due(session, NOW) == []
    row = _outbox(sync_factory, outbox_id)
    assert row is not None and row.next_attempt_at == NOW + task.CLAIM_LEASE
    assert receiver.requests == []


def test_sent_rows_are_purged_after_7_days_and_dead_after_30(
    sync_factory: sessionmaker[Session], receiver: FakeReceiver
) -> None:
    seed = _seed_org(sync_factory, "acme")
    sent_old = _queue(
        sync_factory,
        seed.org_id,
        status=AuditWebhookStatus.sent.value,
        next_attempt_at=NOW - timedelta(days=8),
    )[1]
    sent_new = _queue(
        sync_factory,
        seed.org_id,
        status=AuditWebhookStatus.sent.value,
        next_attempt_at=NOW - timedelta(days=6),
    )[1]
    dead_old = _queue(
        sync_factory,
        seed.org_id,
        status=AuditWebhookStatus.dead.value,
        next_attempt_at=NOW - timedelta(days=31),
    )[1]
    dead_new = _queue(
        sync_factory,
        seed.org_id,
        status=AuditWebhookStatus.dead.value,
        next_attempt_at=NOW - timedelta(days=29),
    )[1]

    stats = _tick(sync_factory, NOW)

    assert stats["purged"] == 2
    assert _outbox(sync_factory, sent_old) is None
    assert _outbox(sync_factory, dead_old) is None
    assert _outbox(sync_factory, sent_new) is not None
    assert _outbox(sync_factory, dead_new) is not None
    assert receiver.requests == []


def test_the_task_is_on_the_beat_every_30_seconds() -> None:
    entry = celery_app.conf.beat_schedule["deliver-audit-webhooks"]
    assert entry["task"] == "tripl.worker.tasks.audit_webhook.deliver_audit_webhooks"
    assert entry["task"] in celery_app.tasks
    assert entry["schedule"] == timedelta(seconds=30)


def test_the_backoff_schedule() -> None:
    assert [audit_webhook_delivery.retry_delay(n) for n in range(1, 9)] == [
        timedelta(minutes=1),
        timedelta(minutes=5),
        timedelta(minutes=30),
        timedelta(hours=2),
        timedelta(hours=6),
        timedelta(hours=6),
        timedelta(hours=6),
        timedelta(hours=6),
    ]


# ── repairs: the documented signature ───────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parents[4]
_DOCS = _REPO_ROOT / "website" / "docs"


def _doc_block(text: str, language: str, marker: str) -> str:
    """The fenced ``language`` block of ``text`` that contains ``marker``."""
    blocks = re.findall(rf"```{language}\n(.*?)```", text, flags=re.DOTALL)
    matching = [block for block in blocks if marker in block]
    assert len(matching) == 1, f"expected one {language} block with {marker!r}"
    return matching[0]


def test_the_documented_python_verifier_accepts_what_tripl_sends() -> None:
    """The admin guide's snippet, run as written, against :func:`sign`."""
    guide = (_DOCS / "administer" / "admin-guide.md").read_text(encoding="utf-8")
    namespace: dict[str, Any] = {}
    # Our own documentation, run as a receiver would copy it.
    exec(_doc_block(guide, "python", "def verify"), namespace)
    verify = namespace["verify"]

    body = audit_webhook_delivery.encode_body({"id": str(uuid.uuid4()), "action": "x"})
    timestamp = int(time.time())
    signature = audit_webhook_delivery.sign(SECRET, timestamp, body)
    assert verify(SECRET, body, str(timestamp), signature) is True
    assert verify(SECRET, body + b" ", str(timestamp), signature) is False
    assert verify("whsec_other", body, str(timestamp), signature) is False
    # Stale: the replay window.
    old = timestamp - 3600
    assert verify(SECRET, body, str(old), audit_webhook_delivery.sign(SECRET, old, body)) is False


def test_every_doc_names_the_t_prefixed_signed_string() -> None:
    guide = (_DOCS / "administer" / "admin-guide.md").read_text(encoding="utf-8")
    node = _doc_block(guide, "js", "function verify")
    assert ".update(`t=${timestamp}.`)" in node
    assert "`t=<timestamp>.<raw body>`" in guide
    security = (_DOCS / "run" / "security.md").read_text(encoding="utf-8")
    assert "`t=<X-Tripl-Timestamp>.<raw body>`" in security
    api_guide = (_DOCS / "integrate" / "agent-api-guide.md").read_text(encoding="utf-8")
    assert '"t=<timestamp>.<raw body>"' in api_guide
    for text in (guide, security, api_guide):
        assert "of `<timestamp>.<raw body>`" not in text
        assert "of `<X-Tripl-Timestamp>.<raw body>`" not in text


def test_the_documented_payload_matches_the_body_shape() -> None:
    """``""`` for an empty text field, never ``null``; the documented keys in order."""
    guide = (_DOCS / "administer" / "admin-guide.md").read_text(encoding="utf-8")
    example = json.loads(_doc_block(guide, "json", '"org_slug"'))
    assert list(example) == list(COLUMNS)
    assert example["branch_name"] == ""
    log = AuditLog(
        id=uuid.uuid4(),
        created_at=datetime(2026, 9, 28, 9, 14, 3, 512000, tzinfo=UTC),
        action="org.member_role_update",
        target_type="user",
        target_id=None,
        payload={},
    )
    record = row_record(log, "acme")
    assert record["branch_name"] == "" and record["project_slug"] == ""
    assert record["created_at"] == "2026-09-28T09:14:03.512000Z"
    assert example["created_at"] == record["created_at"]


# ── repairs: a send has a wall-clock deadline ───────────────────────────────


class _PlainContext:
    """Stands in for TLS so a local plain socket can play the receiver."""

    post_handshake_auth = None
    check_hostname = False

    def wrap_socket(self, sock: socket.socket, server_hostname: str | None = None) -> socket.socket:
        del server_hostname
        return sock

    def set_alpn_protocols(self, protocols: list[str]) -> None:
        del protocols


class _SlowReceiver:
    """A local server that sends ``head`` and then drips ``drip`` forever (or stalls)."""

    def __init__(self, head: bytes, drip: bytes = b"", interval: float = 0.05) -> None:
        self._head = head
        self._drip = drip
        self._interval = interval
        self._stop = threading.Event()
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self.port = int(self._server.getsockname()[1])
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self._server.settimeout(5)
        try:
            conn, _addr = self._server.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(0.5)
            with contextlib.suppress(OSError):
                conn.recv(65536)
            try:
                conn.sendall(self._head)
                while not self._stop.is_set():
                    if self._drip:
                        conn.sendall(self._drip)
                    self._stop.wait(self._interval)
            except OSError:
                return

    def close(self) -> None:
        self._stop.set()
        self._server.close()
        self._thread.join(timeout=2)


@pytest.fixture
def plain_hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosted mode against 127.0.0.1 without TLS: only the deadline is under test."""
    monkeypatch.setattr(settings, "deployment_mode", "hosted")
    monkeypatch.setattr(safe_http, "reject_private_host", lambda *_a, **_k: None)
    monkeypatch.setattr(safe_http.ssl, "create_default_context", lambda *_a, **_k: _PlainContext())


def _timed(fn: Callable[[], object]) -> tuple[object, float]:
    started = time.monotonic()
    try:
        return fn(), time.monotonic() - started
    except BaseException as exc:  # returned for the assertion
        return exc, time.monotonic() - started


def test_a_receiver_dripping_its_headers_is_cut_at_the_deadline(plain_hosted: None) -> None:
    """Each byte arrives well inside the per-read timeout; the deadline still ends it."""
    server = _SlowReceiver(b"HTTP/1.1 200 OK\r\nX-Slow: ", drip=b"a")
    try:
        outcome, elapsed = _timed(
            lambda: safe_http.send(
                "POST",
                f"https://127.0.0.1:{server.port}/hook",
                {},
                b"{}",
                field="Webhook URL",
                timeout=5.0,
                max_response_bytes=0,
                deadline=0.5,
            )
        )
    finally:
        server.close()
    assert isinstance(outcome, TimeoutError), outcome
    assert elapsed < 3.0


def test_a_receiver_dripping_its_body_is_cut_at_the_deadline(plain_hosted: None) -> None:
    """The SSO path reads a body: a slow one is cut at the deadline too."""
    server = _SlowReceiver(
        b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n", drip=b"{", interval=0.05
    )
    try:
        outcome, elapsed = _timed(
            lambda: safe_http.send(
                "GET",
                f"https://127.0.0.1:{server.port}/jwks",
                {},
                None,
                field="Identity provider",
                timeout=5.0,
                max_response_bytes=512 * 1024,
                deadline=0.5,
            )
        )
    finally:
        server.close()
    assert isinstance(outcome, TimeoutError), outcome
    assert elapsed < 3.0


def test_the_webhook_never_waits_for_the_receivers_body(plain_hosted: None) -> None:
    """A 200 whose promised body never comes is a delivery, at once."""
    server = _SlowReceiver(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n")
    try:
        outcome, elapsed = _timed(
            lambda: safe_http.send(
                "POST",
                f"https://127.0.0.1:{server.port}/hook",
                {},
                b"{}",
                field="Webhook URL",
                timeout=5.0,
                max_response_bytes=audit_webhook_delivery.MAX_RESPONSE_BYTES,
                deadline=4.0,
            )
        )
    finally:
        server.close()
    assert isinstance(outcome, HttpResponse), outcome
    assert outcome.status == 200 and outcome.body == b""
    assert elapsed < 3.0


def test_a_self_hosted_send_has_the_same_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The urllib path (proxies kept) is watched as well."""
    for name in ("https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    original_init = safe_http._WatchedHTTPSHandler.__init__

    def plain_init(self: Any, watchdog: Any) -> None:
        original_init(self, watchdog)
        self._context = _PlainContext()

    monkeypatch.setattr(safe_http._WatchedHTTPSHandler, "__init__", plain_init)
    server = _SlowReceiver(b"HTTP/1.1 200 OK\r\nX-Slow: ", drip=b"a")
    try:
        outcome, elapsed = _timed(
            lambda: safe_http.send(
                "POST",
                f"https://127.0.0.1:{server.port}/hook",
                {},
                b"{}",
                field="Webhook URL",
                timeout=5.0,
                max_response_bytes=0,
                deadline=0.5,
            )
        )
    finally:
        server.close()
    assert isinstance(outcome, TimeoutError), outcome
    assert elapsed < 3.0


def test_a_slow_name_lookup_is_abandoned_at_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def slow(*_a: Any, **_k: Any) -> list[Any]:
        time.sleep(1.5)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", slow)
    outcome, elapsed = _timed(
        lambda: safe_http.public_address(
            "slow.example.com", 443, field="Webhook URL", deadline=safe_http.Deadline(0.2)
        )
    )
    assert isinstance(outcome, TimeoutError), outcome
    assert elapsed < 1.0


def test_both_callers_pass_a_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def fake_send(*_a: Any, **kwargs: Any) -> HttpResponse:
        seen.append(kwargs)
        return HttpResponse(status=200, body=b"{}")

    monkeypatch.setattr(safe_http, "send", fake_send)
    audit_webhook_delivery._send(RECEIVER, {}, b"{}")

    idp_http._send("GET", "https://idp.example.com/jwks", {}, None)
    assert seen[0]["deadline"] == audit_webhook_delivery.DEADLINE_SECONDS
    assert seen[0]["max_response_bytes"] == 0
    assert seen[1]["deadline"] == idp_http.DEADLINE_SECONDS
    assert seen[1]["max_response_bytes"] == idp_http.MAX_RESPONSE_BYTES


def test_a_send_past_its_deadline_is_a_timeout_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def too_slow(*_a: Any) -> HttpResponse:
        raise TimeoutError("request exceeded the deadline")

    monkeypatch.setattr(audit_webhook_delivery, "_send", too_slow)
    result = audit_webhook_delivery.post_event(RECEIVER, SECRET, {"id": "1"}, now=NOW)
    assert result == audit_webhook_delivery.DeliveryResult(ok=False, error="timeout")


# ── repairs: the claim lease and ownership ──────────────────────────────────


def test_the_lease_outlives_the_tasks_hard_time_limit() -> None:
    limit = timedelta(seconds=celery_app.conf.task_time_limit)
    assert limit < task.CLAIM_LEASE
    assert timedelta(seconds=celery_app.conf.task_soft_time_limit) < task.CLAIM_LEASE


def test_a_row_claimed_again_by_another_run_is_not_sent_twice(
    sync_factory: sessionmaker[Session], receiver: FakeReceiver
) -> None:
    seed = _seed_org(sync_factory, "acme")
    _audit_id, outbox_id = _queue(sync_factory, seed.org_id)
    with sync_factory() as session:
        ids = task.claim_due(session, NOW)
    # Another run took the row over (its own lease, a later instant).
    other_lease = NOW + task.CLAIM_LEASE + timedelta(minutes=5)
    with sync_factory() as session:
        row = session.get(AuditWebhookOutbox, outbox_id)
        assert row is not None
        row.next_attempt_at = other_lease
        session.commit()

    with sync_factory() as session:
        stats = task.deliver_claimed(session, ids, NOW, lease_until=NOW + task.CLAIM_LEASE)

    assert stats["lost"] == 1 and stats["sent"] == 0
    assert receiver.requests == []
    row = _outbox(sync_factory, outbox_id)
    assert row is not None
    assert row.next_attempt_at == other_lease and row.attempts == 0


def test_an_outcome_never_overwrites_a_row_another_run_finished(
    sync_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """While this run's send is in flight another run sends the row: ``sent`` stays."""
    seed = _seed_org(sync_factory, "acme")
    _audit_id, outbox_id = _queue(sync_factory, seed.org_id)

    def send_while_another_run_finishes(
        _url: str, _headers: dict[str, str], _body: bytes
    ) -> HttpResponse:
        with sync_factory() as session:
            row = session.get(AuditWebhookOutbox, outbox_id)
            assert row is not None
            row.status = AuditWebhookStatus.sent.value
            row.attempts = 1
            row.sent_at = NOW
            row.next_attempt_at = NOW
            session.commit()
        return HttpResponse(status=500, body=b"")

    monkeypatch.setattr(audit_webhook_delivery, "_send", send_while_another_run_finishes)
    stats = _tick(sync_factory, NOW)

    assert stats["lost"] == 1 and stats["failed"] == 0
    row = _outbox(sync_factory, outbox_id)
    assert row is not None
    assert row.status == AuditWebhookStatus.sent.value
    assert row.attempts == 1 and row.last_error is None
    with sync_factory() as session:
        hook = session.get(OrgAuditWebhook, seed.hook_id)
        assert hook is not None and hook.last_error is None


# ── repairs: the outbound probes are rate-limited ───────────────────────────


async def test_the_test_and_save_routes_share_a_small_bucket(
    world: World, receiver: FakeReceiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tripl.middleware.rate_limit import AUDIT_WEBHOOK_PROBE_RATE_LIMIT_PER_MINUTE

    root = world["root"]
    assert (await root.put(WEBHOOK_URL, json={"url": RECEIVER})).status_code == 200
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    for _ in range(AUDIT_WEBHOOK_PROBE_RATE_LIMIT_PER_MINUTE):
        assert (await root.post(f"{WEBHOOK_URL}/test")).status_code == 200
    limited = await root.post(f"{WEBHOOK_URL}/test")
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers
    assert (await root.put(WEBHOOK_URL, json={"url": RECEIVER})).status_code == 429
    # Reads are not on the bucket.
    assert (await root.get(WEBHOOK_URL)).status_code == 200
    assert len(receiver.requests) == AUDIT_WEBHOOK_PROBE_RATE_LIMIT_PER_MINUTE
