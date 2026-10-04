"""The NUL guard on free-text query filters.

``sanitize_query`` fixed ``/search``. The defect class is wider than
that route: any user string bound as a Postgres parameter aborts inside asyncpg
when it carries U+0000, and the events, metrics, metric-catalog, fact-table and
audit list filters all bind one. The fix is a route-parameter type, so this file
checks the type is actually *on* the parameters as well as that it works.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Any, get_args, get_origin, get_type_hints

import pytest
from fastapi.routing import APIRoute
from httpx import AsyncClient
from pydantic import BeforeValidator

from tripl.main import app
from tripl.schemas.text_filters import FreeTextFilter, strip_nul_bytes
from tripl.services._search_query import sanitize_query


def test_strip_nul_bytes_removes_only_u0000() -> None:
    """The narrow promise the "strip, don't reject" argument rests on.

    Stripping is defensible only because it cannot change which rows match, and
    that holds for U+0000 alone — every other character carries filtering
    meaning. A guard that quietly took more would be rewriting the user's query.
    """
    assert strip_nul_bytes("check\x00out") == "checkout"
    assert strip_nul_bytes("\x00\x00") == ""
    # Whitespace, case, punctuation, accents and non-Latin text all survive:
    # trimming is the search route's own step, not this rule's.
    for untouched in ("  padded  ", "Ünïcode", "завершение покупки", "a%b_c", "tab\tsep"):
        assert strip_nul_bytes(untouched) == untouched


def test_non_strings_pass_through_untouched() -> None:
    """So a wrong type stays a 422 instead of becoming a 500.

    ``BeforeValidator`` runs ahead of pydantic's own coercion, so it sees
    whatever arrived. Calling ``.replace`` on that unconditionally would turn
    pydantic's clean type error into an ``AttributeError`` — the same
    500-instead-of-4xx trade this module exists to stop making.
    """
    for value in (None, 7, ["a"], {"k": "v"}):
        assert strip_nul_bytes(value) is value


def test_search_sanitiser_and_the_filter_type_share_one_implementation() -> None:
    """One rule, one implementation — the property, not just the wiring.

    ``sanitize_query`` keeps its extra whitespace trim (a search concern), so
    identity of the two functions is not the thing to assert; identity of their
    NUL behaviour is.
    """
    for raw in ("check\x00out", "\x00", "plain"):
        assert sanitize_query(raw) == strip_nul_bytes(raw).strip()


def _carries_the_guard(annotation: Any) -> bool:
    """True when ``strip_nul_bytes`` guards this annotation, at any nesting depth.

    Depth matters: the declaration is ``FreeTextFilter | None``, so the
    validator sits inside a union member rather than on the parameter itself.
    """
    if get_origin(annotation) is Annotated:
        args = get_args(annotation)
        if any(
            isinstance(meta, BeforeValidator) and meta.func is strip_nul_bytes for meta in args[1:]
        ):
            return True
        return _carries_the_guard(args[0])
    return any(_carries_the_guard(arg) for arg in get_args(annotation))


def _mentions_str(annotation: Any) -> bool:
    """``str`` reachable through unions and ``Annotated``, but NOT through a container.

    ``list[EventStatus]`` and a ``Depends``-injected session are not free text,
    and a container of strings is a repeated parameter whose members each bind
    separately — worth its own decision rather than a silent yes here.
    """
    if annotation is str:
        return True
    origin = get_origin(annotation)
    if origin is Annotated:
        return _mentions_str(get_args(annotation)[0])
    if origin is None:
        return False
    if origin in (list, set, tuple, dict):
        return False
    return any(_mentions_str(arg) for arg in get_args(annotation))


def _api_routes(router: Any) -> Iterator[APIRoute]:
    """Every ``APIRoute``, descending into included routers.

    ``app.routes`` does NOT contain them: this FastAPI keeps an included router
    as a single ``_IncludedRouter`` entry rather than flattening its children.
    Walking the top level alone finds six routes, all of them FastAPI's own, so
    a pin written that way passes while checking nothing — which is how it was
    first written here.
    """
    for route in getattr(router, "routes", []):
        if isinstance(route, APIRoute):
            yield route
            continue
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _api_routes(inner)


def _unguarded_string_parameters() -> dict[str, set[str]]:
    """String, non-path parameters with no NUL guard, mapped to the routes declaring them."""
    unguarded: dict[str, set[str]] = {}
    for route in _api_routes(app.router):
        try:
            hints = get_type_hints(route.endpoint, include_extras=True)
        except Exception:  # pragma: no cover - a route whose hints cannot resolve
            continue
        for name, annotation in hints.items():
            if name == "return" or f"{{{name}}}" in route.path:
                continue
            if not _mentions_str(annotation) or _carries_the_guard(annotation):
                continue
            unguarded.setdefault(name, set()).add(route.path)
    return unguarded


# String parameters guarded somewhere other than the route type, with the reason.
# The pin below requires this to account for every remaining one, so a new
# unguarded parameter cannot be added without someone making a decision here.
_GUARDED_ELSEWHERE: dict[str, str] = {
    "q": (
        "/search sanitises inside search_service and declares "
        "min_length=1. A BeforeValidator runs BEFORE constraints, so moving the "
        "guard onto the parameter would turn ?q=%00 from its documented "
        "200-with-no-items into a 422 — a behaviour change, not a fix."
    ),
    "cursor": (
        "The alert-inbox and alert-deliveries keyset cursor is not a "
        "text filter: services/_alerting_cursors.py decodes it and answers 422 "
        "for anything that does not decode, so a NUL never reaches a query. "
        "test_alert_cursors_and_signal_fields.py pins ?cursor=%00 to 422."
    ),
    "path": (
        "The docs catalog's note path / folder prefix (F22) is not a text filter: "
        "every route taking it (/docs/file, /docs/folder, /docs/revisions) passes it "
        "through docs_paths.normalize_doc_path or normalize_prefix before any query, "
        "and both refuse any control character, U+0000 included, with a 422. "
        "Stripping would silently address a different note. test_docs_api.py pins "
        "?path=a%00b.md to 422 on all three."
    ),
    "lang": (
        "A docs translation's language (/docs/file, /docs/translations, "
        "/docs/translations/revisions) is not a text filter: "
        "docs_translations.lang_param matches it against a language-tag pattern "
        "before any query and answers 422 for anything else, U+0000 included."
    ),
}


def test_every_string_query_parameter_is_guarded_against_a_nul() -> None:
    """The pin that makes the ninth filter impossible to forget.

    Enumerated from the live app rather than from a list in this file: a list
    would be maintained by whoever adds a parameter, and that is precisely the
    person the pin exists for. It found eight beyond the ones first reported.
    """
    unguarded = {
        name: paths
        for name, paths in _unguarded_string_parameters().items()
        if name not in _GUARDED_ELSEWHERE
    }
    assert not unguarded, (
        "string query parameters with no NUL guard and no entry in "
        f"_GUARDED_ELSEWHERE: { {n: sorted(p) for n, p in sorted(unguarded.items())} }"
    )


def test_the_link_picker_query_carries_the_guard_on_its_type() -> None:
    """``/docs/link-suggestions`` (F24 part 2) guards ``q`` itself, unlike ``/search``.

    Its ``q`` is optional and has no ``min_length``, so the NUL guard can sit on
    the parameter without turning ``?q=%00`` into a 422: it strips to "" and
    lists by name. ``_GUARDED_ELSEWHERE['q']`` therefore does not cover it.
    """
    routes = [
        route for route in _api_routes(app.router) if route.path.endswith("/docs/link-suggestions")
    ]
    assert len(routes) == 1
    hints = get_type_hints(routes[0].endpoint, include_extras=True)
    assert _carries_the_guard(hints["q"])


def test_the_route_walk_finds_the_whole_api() -> None:
    """Guards the pin above, which silently passed while checking six routes.

    ``app.routes`` holds an included router as ONE entry instead of flattening
    it, so the first version of the walk saw only FastAPI's own /docs, /redoc,
    /openapi.json and /health. A pin that enumerates has to prove it enumerated.

    Paths are router-local — the ``/api/v1`` prefix is applied at include time
    and is not on the child route — which is fine here: they are used to say
    where to look, never to address anything.
    """
    paths = {route.path for route in _api_routes(app.router)}
    assert len(paths) > 100, f"the route walk found only {len(paths)} paths"
    assert "/projects/{slug}/events" in paths


def test_the_free_text_filter_type_is_what_it_claims_to_be() -> None:
    """Guards the guard: an alias that lost its validator would silence the pin below."""
    assert _carries_the_guard(FreeTextFilter)
    assert _carries_the_guard(FreeTextFilter | None)
    assert not _carries_the_guard(str | None)


@pytest.mark.asyncio
async def test_events_list_filters_with_a_nul_behave_like_the_clean_filter(
    client: AsyncClient,
) -> None:
    """Four filters on one route, every one of which bound a raw string to the driver.

    Asserted through status and body rather than a driver exception so the test
    fails for the right reason on SQLite, where the NUL never reaches a driver:
    without the guard the laced value is a different filter string and matches
    nothing, so the two bodies below diverge.
    """
    await client.post("/api/v1/projects", json={"name": "Nul filters", "slug": "nul-filters"})
    event_type = await client.post(
        "/api/v1/projects/nul-filters/event-types",
        json={"name": "pv", "display_name": "Page View"},
    )
    assert event_type.status_code in (200, 201), event_type.text
    created = await client.post(
        "/api/v1/projects/nul-filters/events",
        json={
            "name": "checkout",
            "description": "Fires on the checkout screen",
            "event_type_id": event_type.json()["id"],
        },
    )
    assert created.status_code in (200, 201), created.text

    base = "/api/v1/projects/nul-filters/events"
    clean = await client.get(base, params={"search": "checkout"})
    assert clean.status_code == 200
    assert [item["name"] for item in clean.json()["items"]] == ["checkout"]

    laced = await client.get(base, params={"search": "check\x00out"})
    assert laced.status_code == 200
    assert laced.json() == clean.json()

    # The remaining three take the same route-parameter type. A NUL-only value
    # collapses to "" and filters on nothing, exactly as an empty one does —
    # not a 500, and deliberately not a 422 either (see ``text_filters``).
    for name in ("tag", "field_value", "meta_value"):
        laced_other = await client.get(base, params={name: "\x00"})
        assert laced_other.status_code == 200, f"{name}: {laced_other.text}"
