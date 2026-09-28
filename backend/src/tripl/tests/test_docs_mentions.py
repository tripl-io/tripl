"""@mentions in docs catalog notes notify their readers, once (F24 part 2, GH #308)."""

from typing import Any

from httpx import AsyncClient

from tripl.services.docs_mentions import mentioned_user_ids, new_mentions
from tripl.tests._docs_sharing_helpers import (
    BASE,
    Crew,
    crew,  # noqa: F401 - the fixture
    put,
    read,
    share,
)

ALPHA = "11111111-2222-4333-8444-555555555555"
BETA = "66666666-7777-4888-9999-aaaaaaaaaaaa"


def test_mentions_are_parsed_outside_code_and_diffed() -> None:
    body = f"Hi [[user:{ALPHA}]] and [[user:{ALPHA.upper()}|again]], `[[user:{BETA}]]`."
    assert [str(user_id) for user_id in mentioned_user_ids(body)] == [ALPHA]
    before = f"---\ntitle: T\n---\n[[user:{ALPHA}]]"
    after = f"---\ntitle: T\n---\n[[user:{ALPHA}]] [[user:{BETA}]]"
    assert [str(user_id) for user_id in new_mentions(before, after)] == [BETA]
    assert new_mentions(after, after) == []
    assert [str(user_id) for user_id in new_mentions(None, before)] == [ALPHA]


async def _mentions(who: AsyncClient) -> list[dict[str, Any]]:
    resp = await who.get("/api/v1/me/notifications")
    assert resp.status_code == 200, resp.text
    return [item for item in resp.json()["items"] if item["kind"] == "mention"]


def _at(crew: Crew, *names: str) -> str:  # noqa: F811
    return " ".join(f"[[user:{crew.ids[name]}]]" for name in names)


async def test_a_mention_notifies_each_reader_once(crew: Crew) -> None:  # noqa: F811
    body = (
        f"# Launch checklist\n\nPlease review {_at(crew, 'bob', 'carol', 'stranger', 'alice')}.\n"
    )
    written = await put(crew.alice, "launch.md", body)
    mentions = {link["target"]: link for link in written["links"] if link["kind"] == "user"}
    assert mentions[str(crew.ids["bob"])]["label"] == "@Bob"
    # Not a member of the organization: a broken mention, and no notification.
    assert mentions[str(crew.ids["stranger"])]["status"] == "broken"

    for who in (crew.bob, crew.carol):
        found = await _mentions(who)
        assert len(found) == 1
        note = found[0]
        assert note["entity_type"] == "doc"
        assert note["entity_id"] == written["id"]
        assert note["title"] == "Alice mentioned you in Launch checklist"
        assert note["url"] == "/o/default/p/vault/docs/project/launch.md"
    # Nobody hears about their own mention, and an outsider hears nothing.
    assert await _mentions(crew.alice) == []
    assert await _mentions(crew.stranger) == []
    assert await _mentions(crew.dave) == []

    # Saving again, with the same mentions and more text, notifies nobody again.
    await put(crew.alice, "launch.md", body + "\nUpdated.\n")
    assert len(await _mentions(crew.bob)) == 1
    # A newly added mention notifies only its person.
    await put(crew.alice, "launch.md", body + f"\nAlso {_at(crew, 'dave')}.\n")
    assert len(await _mentions(crew.dave)) == 1
    assert len(await _mentions(crew.bob)) == 1


async def test_only_people_who_can_read_the_note_are_told(crew: Crew) -> None:  # noqa: F811
    await put(crew.alice, "plans/q3.md", "# Q3\n")
    await share(crew.alice, "plans/q3.md", "restricted", [("user", crew.ids["bob"], "view")])
    await put(crew.alice, "plans/q3.md", f"# Q3\n\n{_at(crew, 'bob', 'dave')}\n")
    assert len(await _mentions(crew.bob)) == 1
    assert await _mentions(crew.dave) == []
    # Dave cannot read the note, and the mention's name is not a way in.
    await read(crew.dave, "plans/q3.md", expect=404)

    await put(crew.alice, "diary.md", "# Diary\n")
    await share(crew.alice, "diary.md", "private")
    await put(crew.alice, "diary.md", f"# Diary\n\n{_at(crew, 'carol')}\n")
    assert await _mentions(crew.carol) == []


async def test_an_import_never_notifies(crew: Crew) -> None:  # noqa: F811
    resp = await crew.alice.post(
        f"{BASE}/import",
        params={"scope": "project", "mode": "merge"},
        json={"files": [{"path": "imported.md", "content": f"# Imported\n\n{_at(crew, 'bob')}\n"}]},
    )
    assert resp.status_code == 200, resp.text
    assert await _mentions(crew.bob) == []
    # The mention is still a link (and a back-link on Bob) all the same.
    backlinks = await crew.bob.get(
        f"{BASE}/backlinks", params={"kind": "user", "name": str(crew.ids["bob"])}
    )
    assert [item["path"] for item in backlinks.json()["items"]] == ["imported.md"]
