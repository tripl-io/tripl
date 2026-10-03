"""Guard against drift between the live OpenAPI schema and the committed snapshot.

The committed ``backend/openapi.json`` is the source of truth the frontend
codegen (``bun run gen:api`` -> ``src/types/api.gen.ts``) consumes. Any backend
change that alters the API surface (routes, request/response models, status
codes, etc.) changes ``app.openapi()`` and so must be reflected in the snapshot.

This test fails until the snapshot is regenerated, making schema drift visible
in code review instead of silently desynchronising the FE<->BE contract.
"""

import json
from pathlib import Path

import pytest
from httpx import AsyncClient

from tripl.config import settings
from tripl.main import app

# ``openapi.json`` lives at the backend project root, four levels up from this
# file: tests -> tripl -> src -> backend.
_OPENAPI_SNAPSHOT = Path(__file__).resolve().parents[3] / "openapi.json"

_REGENERATE_HINT = (
    'cd backend && uv run python -c "import json; from tripl.main import app; '
    'print(json.dumps(app.openapi(), indent=2, sort_keys=True))" > openapi.json'
    "\nThen regenerate the frontend types: cd frontend && bun run gen:api"
)


def _canonical(schema: object) -> str:
    """Serialise a schema the same way the snapshot is written on disk."""
    return json.dumps(schema, indent=2, sort_keys=True)


def test_openapi_snapshot_matches_live_schema() -> None:
    assert _OPENAPI_SNAPSHOT.exists(), (
        f"Missing OpenAPI snapshot at {_OPENAPI_SNAPSHOT}. Regenerate it:\n{_REGENERATE_HINT}"
    )

    # The snapshot is written with ``print(...) > openapi.json``, which appends
    # a single trailing newline that ``json.dumps`` does not emit. Normalise it
    # so the comparison reflects the schema content, not the redirection.
    snapshot = _OPENAPI_SNAPSHOT.read_text(encoding="utf-8").rstrip("\n")
    live = _canonical(app.openapi())

    assert snapshot == live, (
        "OpenAPI schema drift detected: the live schema no longer matches the "
        f"committed snapshot at {_OPENAPI_SNAPSHOT}.\n"
        "If this change to the API surface is intentional, regenerate the "
        "snapshot (and the frontend types it drives):\n"
        f"{_REGENERATE_HINT}"
    )


def test_branch_override_is_a_declared_parameter() -> None:
    """``branch`` must reach the spec, not just the request object.

    It was read straight off ``request.query_params`` for a long time, so
    FastAPI never declared it and the one documented way to keep an agent's
    edits off the main plan was invisible in all three generated artifacts. A
    client built from the spec then dropped it and silently wrote to main
    (tripl-l33u.7).
    """
    schema = app.openapi()
    declaring = {
        (method, path)
        for path, operations in schema["paths"].items()
        for method, operation in operations.items()
        if isinstance(operation, dict)
        for parameter in operation.get("parameters", ())
        if parameter.get("name") == "branch"
    }

    # A representative branch-scoped read and write: both resolve the override
    # through ``BranchIdDep``, so both must advertise it.
    assert ("get", "/api/v1/projects/{slug}/events") in declaring
    assert ("patch", "/api/v1/projects/{slug}/events/{event_id}") in declaring

    for method, path in declaring:
        parameter = next(
            p for p in schema["paths"][path][method]["parameters"] if p["name"] == "branch"
        )
        assert parameter["in"] == "query"
        assert parameter["required"] is False


def test_live_schema_carries_no_servers_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """``app.openapi()`` must stay independent of the environment.

    It is what ``bin/sync-api-types.sh`` and ``bin/dump-openapi.sh`` call, with
    no database and whatever env the generating machine happens to have, so a
    ``servers`` entry baked in here would make the committed artifact above
    differ between contributors purely by APP_BASE_URL.

    ``app_base_url`` is set for the duration of the assertion on purpose. It is
    empty in the test environment, so asserting against the ambient value would
    pass just as happily against the code that baked
    ``[{"url": settings.app_base_url}]`` in at import — the test has to prove the
    schema IGNORES the setting, not that the setting happens to be blank.
    """
    monkeypatch.setattr(settings, "app_base_url", "https://baked-in.example")
    app.openapi_schema = None  # force a rebuild rather than reading the cache
    try:
        assert "servers" not in app.openapi()
    finally:
        app.openapi_schema = None


@pytest.mark.asyncio
async def test_served_document_never_advertises_app_base_url(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The document clients fetch is the document we commit — no ``servers``.

    With no ``servers`` block a client resolves the API against the URL it
    fetched the spec from, which is right for every origin the instance answers
    on. Advertising ``app_base_url`` instead was one origin guessed in advance,
    and production proved the guess wrong: a spec served over https advertised
    the deployment's plaintext internal address, so "Try it out" became a
    cross-origin call the browser blocked as mixed content — and one CORS
    would have refused too, since ``cors_origins()`` derives from the same
    value (tripl-ouxw). Resolving it per request from the runtime override
    (tripl-mfqm) only made the wrong answer editable, not right.

    Both sources of ``app_base_url`` are set to non-empty values here, because
    an assertion against the ambient (blank) config would pass against either
    implementation.
    """
    monkeypatch.setattr(settings, "app_base_url", "https://from-env.example")
    patched = await client.patch(
        "/api/v1/settings",
        json={"runtime": {"app_base_url": "https://from-override.example"}},
    )
    assert patched.status_code == 200

    served = await client.get("/openapi.json")
    assert served.status_code == 200
    assert "servers" not in served.json()
    # Whole-document compare, not just the ``servers`` key: it pins that the HTTP
    # response is not a second, near-differing spelling of the spec at all. With
    # the snapshot test above (snapshot == app.openapi()) this transitively pins
    # served == the committed artifact, while each test still fails for its own
    # reason — a stale snapshot does not also indict the route.
    assert _canonical(served.json()) == _canonical(app.openapi())
