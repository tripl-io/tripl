"""The note editor's link and @mention picker (F24 part 2, GH #308).

``GET /projects/{slug}/docs/link-suggestions`` offers only what the caller may
link to: notes they can see, members of the organization, and the project's
own entities. Every suggestion carries the canonical reference to insert.
"""

import pytest
from httpx import AsyncClient

from tripl.api.v1 import docs as docs_api
from tripl.config import settings
from tripl.middleware.rate_limit import TokenBucketLimiter
from tripl.tests._docs_helpers import create_project, put_doc
from tripl.tests._docs_sharing_helpers import (
    BASE,
    Crew,
    crew,  # noqa: F401 - the fixture
    put,
    share,
)
from tripl.tests.test_docs_links_kinds import seed_entities

URL = "/api/v1/projects/{slug}/docs/link-suggestions"


async def _suggest(client: AsyncClient, slug: str, **params: object) -> list[dict[str, object]]:
    resp = await client.get(URL.format(slug=slug), params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


async def test_each_kind_suggests_its_canonical_reference(client: AsyncClient) -> None:
    await create_project(client, "picker")
    ids = await seed_entities(client, "picker")
    note = await put_doc(client, "picker", "guides/setup.md", "# Setup guide\n")

    cases = {
        "metric": ("weekly", "[[metric:weekly_revenue]]"),
        "variable": ("plan", "[[variable:plan_tier]]"),
        "event": ("purch", "[[event:purchase]]"),
        "event_type": ("check", "[[event-type:checkout]]"),
        "field": ("amount", "[[field:checkout/amount]]"),
        "branch": ("onboard", "[[branch:feature-onboarding]]"),
        "scan": ("nightly", "[[scan:nightly scan]]"),
        "data_source": ("warehouse", f"[[data-source:{ids['data_source']}]]"),
        "alert_rule": ("checkout", f"[[alert-rule:{ids['rule_id']}]]"),
        "doc": ("setup", f"[[doc:{note['id']}]]"),
    }
    for kind, (q, insert) in cases.items():
        found = await _suggest(client, "picker", kind=kind, q=q)
        assert found, kind
        assert {item["kind"] for item in found} == {kind}, kind
        assert found[0]["insert"] == insert, (kind, found)

    doc = (await _suggest(client, "picker", kind="doc", q="setup"))[0]
    assert (doc["label"], doc["detail"]) == ("Setup guide", "Project notes · guides/setup.md")

    # An empty query lists by name; no kind mixes every kind, round-robin.
    assert (await _suggest(client, "picker", kind="metric"))[0]["label"] == "Weekly revenue"
    mixed = await _suggest(client, "picker", limit=50)
    assert {"doc", "event", "metric", "branch", "user"} <= {item["kind"] for item in mixed}
    assert len(await _suggest(client, "picker", limit=2)) == 2

    # A NUL is stripped, not a 500 nor a 422.
    assert (await _suggest(client, "picker", kind="metric", q="week\x00ly"))[0][
        "insert"
    ] == "[[metric:weekly_revenue]]"


async def test_suggestions_are_bounded_and_validated(client: AsyncClient) -> None:
    await create_project(client, "bounds")
    url = URL.format(slug="bounds")
    assert (await client.get(url, params={"kind": "widget"})).status_code == 422
    assert (await client.get(url, params={"limit": 0})).status_code == 422
    assert (await client.get(url, params={"limit": 51})).status_code == 422
    assert (await client.get(url, params={"q": "x" * 201})).status_code == 422
    assert (await client.get(URL.format(slug="nowhere"))).status_code == 404


async def test_suggestions_are_rate_limited_per_user(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await create_project(client, "limited")
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(
        docs_api,
        "doc_link_suggestions_rate_limiter",
        TokenBucketLimiter(capacity=2, per_seconds=60.0, name="doc_link_suggestions"),
    )
    url = URL.format(slug="limited")
    assert [(await client.get(url)).status_code for _ in range(2)] == [200, 200]
    limited = await client.get(url)
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1


async def test_only_readable_notes_and_organization_members(crew: Crew) -> None:  # noqa: F811
    await put(crew.alice, "drafts/secret-roadmap.md", "# Secret roadmap\n")
    await share(crew.alice, "drafts/secret-roadmap.md", "private")
    await put(crew.alice, "roadmap.md", "# Public roadmap\n")

    async def labels(who: AsyncClient, **params: object) -> list[object]:
        resp = await who.get(f"{BASE}/link-suggestions", params=params)
        assert resp.status_code == 200, resp.text
        return [item["label"] for item in resp.json()["items"]]

    assert set(await labels(crew.alice, kind="doc", q="roadmap")) == {
        "Public roadmap",
        "Secret roadmap",
    }
    assert await labels(crew.bob, kind="doc", q="roadmap") == ["Public roadmap"]
    assert await labels(crew.bob, q="secret") == []

    people = await crew.bob.get(f"{BASE}/link-suggestions", params={"kind": "user", "limit": 50})
    names = {item["label"] for item in people.json()["items"]}
    assert {"Alice", "Bob", "Carol", "Dave"} <= names
    assert "Stranger" not in names
    by_prefix = await labels(crew.bob, kind="user", q="car")
    assert by_prefix == ["Carol"]
    carol = await crew.bob.get(f"{BASE}/link-suggestions", params={"kind": "user", "q": "car"})
    assert carol.json()["items"][0]["insert"] == f"[[user:{crew.ids['carol']}]]"

    # Not a member of the project: the route is not there at all.
    outsider = await crew.stranger.get(f"{BASE}/link-suggestions", params={"q": "roadmap"})
    assert outsider.status_code == 404
