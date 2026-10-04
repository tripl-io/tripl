import smtplib
import socket
from collections.abc import AsyncGenerator, Iterator
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Connection, Row
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tripl.config import REGISTRATION_OPEN, settings

# Disable rate limiting in tests so the shared in-memory buckets across the
# session don't cause spurious 429s on repeated /auth/register calls.
settings.rate_limit_enabled = False

# Pin self-service registration open for the suite. This matches the shipped
# default (see Settings.registration_mode) but is set explicitly so the suite
# does not silently change behaviour if that default ever moves — most suites
# register a second/third user to exercise editor and viewer roles. Tests that
# assert the closed behaviour set the mode themselves via monkeypatch or a
# persisted override.
settings.registration_mode = REGISTRATION_OPEN

# Cheap scrypt for the suite. Production's N=2**16 costs ~64MB and a noticeable
# fraction of a second per hash, and almost every test pays it several times:
# the ``client`` fixture registers a user, most suites register more and log
# in. The algorithm, the stored format and the rehash-on-login path are all
# unchanged — only the cost parameter drops. It must be set before
# ``tripl.main`` is imported, because ``auth_service`` hashes its timing-dummy
# password at import time. Nothing in the suite asserts the production cost.
import tripl.auth_utils as _auth_utils  # noqa: E402

_auth_utils.SCRYPT_N = 2**10
_auth_utils.SCRYPT_MAXMEM = _auth_utils._scrypt_maxmem(
    _auth_utils.SCRYPT_N, _auth_utils.SCRYPT_R, _auth_utils.SCRYPT_P
)

import tripl.services.app_settings_service as _app_settings  # noqa: E402
from tripl.database import get_session  # noqa: E402

# ``tripl.main`` applies persisted service overrides at import, reading
# app_settings through the SYNC engine, which points at a PostgreSQL the suite
# does not have. It degrades correctly, but logs a full connection traceback;
# under xdist that output escapes capture and reads like a failure at the end of
# every run. The import sees a no-op, and the real function is put back for the
# tests that exercise it (test_app_settings).
_real_apply_startup_service_overrides = _app_settings.apply_startup_service_overrides
_app_settings.apply_startup_service_overrides = lambda session=None: []
from tripl.main import app  # noqa: E402

_app_settings.apply_startup_service_overrides = _real_apply_startup_service_overrides
from tripl.middleware.org_context import OrgRef, bind_org, reset_org  # noqa: E402
from tripl.middleware.rate_limit import (  # noqa: E402
    audit_export_rate_limiter,
    audit_webhook_probe_rate_limiter,
    doc_link_suggestions_rate_limiter,
    login_rate_limiter,
    register_rate_limiter,
    scim_auth_failure_rate_limiter,
    scim_rate_limiter,
    sso_probe_rate_limiter,
    sso_rate_limiter,
    status_rate_limiter,
    verify_email_rate_limiter,
)
from tripl.models import Base  # noqa: E402
from tripl.models.data_source import TestStatus  # noqa: E402
from tripl.models.organization import DEFAULT_ORG_ID, DEFAULT_ORG_SLUG  # noqa: E402

# A model enum whose name starts with "Test"; test modules that import it made
# pytest try to collect it and warn once per worker.
TestStatus.__test__ = False  # type: ignore[attr-defined]
from tripl.tests._default_org import (  # noqa: E402
    install_default_organization_seeding,
    install_test_row_default_org,
    seed_default_organization,
)
from tripl.tests._sqlite import enable_sqlite_foreign_keys  # noqa: E402

# Every schema built from the models — this one and each module's own engine —
# starts with the default organization every project belongs to (F20 PR1), and a
# row a test builds without an organization lands in it (F20 PR5: the models no
# longer default it, so application code that forgets still fails).
install_default_organization_seeding()
install_test_row_default_org()

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(TEST_DATABASE_URL)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# Enforce ON DELETE CASCADE in tests, matching production Postgres. Without it
# the in-memory DB leaves orphaned child rows on a parent delete (e.g. deleting
# a FieldDefinition strands its event_field_values), which then crashes the
# search reindexer on a NULL parent deref. StaticPool keeps one
# connection, so this fires once. See ``_sqlite`` for why it is shared.
enable_sqlite_foreign_keys(engine.sync_engine)


@pytest.fixture(scope="session", autouse=True)
async def _dispose_engine() -> AsyncGenerator[None]:
    """Close the shared async engine at session end.

    pytest-asyncio 1.x ignores a custom ``event_loop`` fixture; loop scope is
    pinned to ``session`` in pyproject so every test and fixture shares one loop
    and the StaticPool's single in-memory sqlite connection stays put. Disposing
    it here closes that connection deterministically (no ``ResourceWarning``).
    """
    yield
    await engine.dispose()


#: ``sqlite_master`` as ``create_all`` left it: every table and index with its
#: DDL. ``None`` until the first test in this process builds the schema.
_pristine_schema: tuple[Row[Any], ...] | None = None


def _schema_fingerprint(conn: Connection) -> tuple[Row[Any], ...]:
    return tuple(
        conn.exec_driver_sql(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).all()
    )


def _build_schema(conn: Connection) -> tuple[Row[Any], ...]:
    Base.metadata.drop_all(conn)
    Base.metadata.create_all(conn)
    return _schema_fingerprint(conn)


def _empty_all_tables(conn: Connection) -> None:
    # Children before parents, with foreign_keys=ON left as it is: no row that
    # references another survives to its parent's DELETE, so no ON DELETE action
    # fires and no constraint can trip.
    for table in reversed(Base.metadata.sorted_tables):
        conn.execute(table.delete())
    # Part of "what a fresh schema gave it": create_all seeds this row.
    seed_default_organization(conn)


@pytest.fixture(autouse=True)
async def setup_db() -> AsyncGenerator[None]:
    """Give every test an empty database with the full schema.

    This used to run ``create_all`` before and ``drop_all`` after EVERY test,
    rebuilding every table and index ~4,400 times per run. The
    schema is now built once per process (per xdist worker, each of which has
    its own in-memory database) and a test is isolated by deleting every row
    afterwards — what a fresh schema gave it: the same tables and indexes, no
    rows, and rowids that restart at 1 (no model uses AUTOINCREMENT).

    A test that changes the schema of the shared database itself (drops an
    index, creates a table) would leak that through a DELETE-only reset, so
    ``sqlite_master`` is compared after every test and any drift gets the old
    full ``drop_all`` + ``create_all``. Tests that build their own sync engines
    keep their own lifecycles and are unaffected.
    """
    global _pristine_schema
    if _pristine_schema is None:
        async with engine.begin() as conn:
            _pristine_schema = await conn.run_sync(_build_schema)
    yield
    async with engine.begin() as conn:
        if await conn.run_sync(_schema_fingerprint) == _pristine_schema:
            await conn.run_sync(_empty_all_tables)
        else:
            _pristine_schema = await conn.run_sync(_build_schema)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "no_default_org: do not bind the default organization around the test "
        "(for the org-context tests that assert what happens with none bound)",
    )


@pytest.fixture(autouse=True)
async def _bind_default_org(request: pytest.FixtureRequest) -> AsyncGenerator[None]:
    """Act in the default organization when a test calls a service directly.

    Outside HTTP nothing resolves an organization, and a slug lookup without one
    raises ``OrgContextMissing`` by design (F20 PR2). Every project a test makes
    lives in the default organization, so tests that call ``*_service.x(session,
    slug)`` act there. An HTTP request does not inherit this binding:
    ``OrgPathRewriteMiddleware`` unbinds for the request and the auth dependency
    resolves the organization as in production.

    Async on purpose: pytest-asyncio propagates contextvars set in an async
    fixture into the test, which a sync fixture's binding may not reach.
    """
    if request.node.get_closest_marker("no_default_org") is not None:
        yield
        return
    token = bind_org(OrgRef(id=DEFAULT_ORG_ID, slug=DEFAULT_ORG_SLUG))
    yield
    try:
        reset_org(token)
    except ValueError:
        # Teardown ran in a different Context than setup; unbind directly.
        bind_org(None)


@pytest.fixture(autouse=True)
def _reset_rate_limiters() -> None:
    """Drop accumulated bucket state between tests so order is irrelevant."""
    login_rate_limiter.reset()
    register_rate_limiter.reset()
    status_rate_limiter.reset()
    verify_email_rate_limiter.reset()
    sso_rate_limiter.reset()
    sso_probe_rate_limiter.reset()
    scim_rate_limiter.reset()
    scim_auth_failure_rate_limiter.reset()
    audit_webhook_probe_rate_limiter.reset()
    audit_export_rate_limiter.reset()
    doc_link_suggestions_rate_limiter.reset()


async def override_get_session() -> AsyncGenerator[AsyncSession]:
    async with TestSessionLocal() as session:
        yield session


app.dependency_overrides[get_session] = override_get_session


# Names the suite uses as "a public destination": RFC 2606 example domains and
# Atlassian Cloud hosts. The SSRF guard (``reject_private_host``) resolves them
# and fails CLOSED, so on a runner without outbound DNS the webhook/Jira tests
# turned 200/201 into 422 "could not be resolved".
_HERMETIC_DNS_SUFFIXES = ("example.com", "example.net", "example.org", "atlassian.net")
# A public address (example.com's long-standing one), so the guard passes.
HERMETIC_DNS_ADDRESS = "93.184.216.34"
_real_getaddrinfo = socket.getaddrinfo


def _hermetic_getaddrinfo(host: object, *args: object, **kwargs: object) -> object:
    name = host.decode() if isinstance(host, bytes) else host
    if isinstance(name, str):
        name = name.rstrip(".").lower()
        if any(name == s or name.endswith("." + s) for s in _HERMETIC_DNS_SUFFIXES):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (HERMETIC_DNS_ADDRESS, 0))]
    return _real_getaddrinfo(host, *args, **kwargs)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _hermetic_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve the suite's placeholder public hosts without the live resolver.

    Every other name still goes to the real resolver, so nothing that expects a
    lookup to FAIL changes, and a test that patches ``getaddrinfo`` itself
    (e.g. to a metadata IP) still wins, being applied after this fixture.
    """
    monkeypatch.setattr(socket, "getaddrinfo", _hermetic_getaddrinfo)


class NetworkAccessError(RuntimeError):
    """Raised by the ``deny_network`` tripwire on any outbound attempt."""


@pytest.fixture
def deny_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Trip on ANY outbound network attempt (socket/httpx/smtplib).

    The in-memory SQLite DB and the in-process ASGI transport never open a real
    socket, so this only fires on genuine egress — making it the capstone guard
    for asserting the demo stack (provisioning, runtime tick, alert dispatch,
    metric collection over the synthetic source) is fully local. Opt-in and not
    autouse, so it never affects tests that don't request it.
    """

    def _blocked(*_args: object, **_kwargs: object) -> object:
        raise NetworkAccessError("outbound network attempted from a demo path")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(smtplib, "SMTP", _blocked)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _blocked)
    monkeypatch.setattr(httpx.Client, "send", _blocked)
    monkeypatch.setattr(httpx.AsyncClient, "send", _blocked)
    yield


@pytest.fixture
async def anon_client() -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def client(anon_client: AsyncClient) -> AsyncGenerator[AsyncClient]:
    resp = await anon_client.post(
        "/api/v1/auth/register",
        json={
            "email": "test@example.com",
            "password": "Password123!",
            "name": "Test User",
        },
    )
    assert resp.status_code == 201
    yield anon_client
