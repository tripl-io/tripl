import logging
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

import tripl.database as database
import tripl.main as main_module
from tripl.config import settings
from tripl.database import get_session
from tripl.logging_config import _RequestIDFilter
from tripl.main import app
from tripl.middleware.security_headers import build_security_headers


@contextmanager
def _boom_route(path: str) -> Iterator[None]:
    """Register a route that raises, then take it back out.

    Routes live on the module-level ``app`` that every test in the session
    shares, so leaving one behind would leak a 500 into unrelated tests.
    """

    @app.get(path, include_in_schema=False)
    async def _boom() -> None:
        raise RuntimeError("kaboom")

    try:
        yield
    finally:
        app.router.routes = [r for r in app.router.routes if getattr(r, "path", None) != path]


class _RecordingHandler(logging.Handler):
    """Root handler wired the way the production one is.

    ``_RequestIDFilter`` is what stamps ``record.request_id`` in a real deploy
    (see ``logging_config._build_handler``), so attaching the same filter here
    means the assertion covers that wiring rather than just the contextvar.
    """

    def __init__(self) -> None:
        super().__init__()
        self.addFilter(_RequestIDFilter())
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.mark.asyncio
async def test_get_session_rolls_back_on_handler_error() -> None:
    """A handler raising mid-request must leave the connection clean: the
    session is rolled back and the exception re-raised."""
    fake_session = AsyncMock()

    class _FakeCtx:
        async def __aenter__(self) -> object:
            return fake_session

        async def __aexit__(self, *exc: object) -> bool:
            return False

    # Swap the sessionmaker for a fake so we can assert rollback without a real
    # engine, then drive the generator manually and throw at the yield point.
    original = database.async_session
    database.async_session = lambda: _FakeCtx()  # type: ignore[assignment]
    try:
        gen = get_session()
        await gen.__anext__()
        with pytest.raises(RuntimeError):
            await gen.athrow(RuntimeError("boom"))
    finally:
        database.async_session = original

    fake_session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_unhandled_exception_returns_generic_500_with_request_id() -> None:
    with _boom_route("/__boom__"):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/__boom__", headers={settings.request_id_header: "boom-body-1"})

    assert resp.status_code == 500
    body = resp.json()
    assert body["detail"] == "Internal server error"
    # The *real* id, not the "-" placeholder. Asserting mere truthiness passed
    # for as long as the handler could only see the already-reset contextvar,
    # which is how tripl-qu9m survived having a test.
    assert body["request_id"] == "boom-body-1"
    assert "kaboom" not in resp.text


@pytest.mark.asyncio
async def test_unhandled_exception_response_carries_request_id_and_security_headers() -> None:
    """A 500 is written by ServerErrorMiddleware, which wraps the app from
    outside every middleware, so nothing they add on the way out lands on this
    response unless the handler reproduces it.

    The header set is compared against ``build_security_headers()`` rather than
    a literal list: a header added there must not be able to quietly skip the
    error path, which is the drift the shared builder exists to prevent.
    """
    with _boom_route("/__boom_headers__"):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get(
                "/__boom_headers__", headers={settings.request_id_header: "boom-hdr-2"}
            )

    assert resp.status_code == 500
    assert resp.headers.get(settings.request_id_header) == "boom-hdr-2"
    expected = build_security_headers()
    assert expected, "empty header set would make the comparison below vacuous"
    assert {name: resp.headers.get(name) for name in expected} == expected


@pytest.mark.asyncio
async def test_unhandled_exception_log_line_shares_the_response_request_id() -> None:
    """Correlating a user's 500 with its log line is the entire point of the id.
    Before tripl-qu9m both sides read "-", which correlated nothing."""
    handler = _RecordingHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        with _boom_route("/__boom_log__"):
            transport = ASGITransport(app=app, raise_app_exceptions=False)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                resp = await ac.get(
                    "/__boom_log__", headers={settings.request_id_header: "boom-log-3"}
                )
    finally:
        root.removeHandler(handler)

    logged = [r for r in handler.records if r.getMessage() == "unhandled exception"]
    assert len(logged) == 1
    assert getattr(logged[0], "request_id", None) == "boom-log-3"
    # Same id on both sides — that is what makes the pair usable in support.
    assert resp.json()["request_id"] == "boom-log-3"


@pytest.mark.asyncio
async def test_health_db_failure_returns_generic_body_without_exception_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unauthenticated /health probe must not leak DSN/driver detail: on a DB
    failure it returns a generic 503 body with no raw exception string."""
    secret = "postgresql+asyncpg://supersecretuser:supersecretpw@db-host:5432/tripl"

    class _ExplodingEngine:
        # AsyncEngine.connect is a read-only method, so we can't patch it on the
        # real engine instance; swap in a stand-in whose connect() raises before
        # any context-manager entry, simulating an unreachable database.
        def connect(self) -> object:
            raise RuntimeError(secret)

    monkeypatch.setattr(main_module, "engine", _ExplodingEngine())

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body == {"status": "error", "component": "database"}
    # No exception text — neither a "detail" field nor the leaked secret anywhere.
    assert "detail" not in body
    assert "supersecret" not in resp.text
    assert "asyncpg" not in resp.text
