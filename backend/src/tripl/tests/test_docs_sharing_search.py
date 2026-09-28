"""Search honours note visibility (F24, GH #308).

Hidden notes are filtered inside the ranking query, before the candidate window
and the page cut, so they never appear, never take a slot, and never show in
``total`` or ``truncated`` — in the docs search, the project search and any
internal caller that names no viewer.
"""

from tripl.services import search_service
from tripl.tests._docs_sharing_helpers import (
    BASE,
    SLUG,
    Crew,
    crew,  # noqa: F401 - the fixture
    put,
    share,
)
from tripl.tests.conftest import TestSessionLocal

WORD = "quokkafish"


async def _hidden_and_public(crew: Crew) -> None:  # noqa: F811
    for index in range(5):
        await put(crew.alice, f"private/p{index}.md", f"# {WORD} {index}\n\n{WORD} notes\n")
    await share(crew.alice, "private", "private", folder=True)
    await put(crew.alice, "public.md", f"# {WORD} public\n\n{WORD} for everyone\n")


async def test_docs_search_hides_notes_and_their_counts(crew: Crew) -> None:  # noqa: F811
    await _hidden_and_public(crew)

    seen = await crew.bob.get(f"{BASE}/search", params={"q": WORD, "limit": 1})
    assert seen.status_code == 200, seen.text
    body = seen.json()
    assert [item["path"] for item in body["items"]] == ["public.md"]
    assert body["total"] == 1
    assert body["truncated"] is False

    mine = await crew.alice.get(f"{BASE}/search", params={"q": WORD, "limit": 1})
    assert mine.json()["truncated"] is True


async def test_project_search_hides_notes_and_their_counts(crew: Crew) -> None:  # noqa: F811
    await _hidden_and_public(crew)

    resp = await crew.bob.get(
        f"/api/v1/projects/{SLUG}/search", params={"q": WORD, "types": "doc", "limit": 1}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [item["title"] for item in body["items"]] == [f"{WORD} public"]
    assert body["truncated"] is False

    everything = await crew.bob.get(f"/api/v1/projects/{SLUG}/search", params={"q": WORD})
    assert [item["title"] for item in everything.json()["items"]] == [f"{WORD} public"]


async def test_a_shared_note_is_found_by_who_it_is_shared_with(crew: Crew) -> None:  # noqa: F811
    await put(crew.alice, "shared.md", f"# {WORD} shared\n")
    await share(crew.alice, "shared.md", "restricted", [("user", crew.ids["bob"], "view")])

    found = await crew.bob.get(f"{BASE}/search", params={"q": WORD})
    assert [item["path"] for item in found.json()["items"]] == ["shared.md"]
    missed = await crew.dave.get(f"{BASE}/search", params={"q": WORD})
    assert missed.json()["items"] == [] and missed.json()["total"] == 0


async def test_an_internal_search_without_a_viewer_sees_level_notes_only(crew: Crew) -> None:  # noqa: F811
    await _hidden_and_public(crew)
    async with TestSessionLocal() as session:
        found = await search_service.search_project(session, SLUG, WORD, entity_types=["doc"])
    assert [item.title for item in found.items] == [f"{WORD} public"]
