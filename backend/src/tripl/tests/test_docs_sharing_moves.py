"""Moves never change who sees a note behind its author's back (F24, GH #308).

A note that follows its folder's rule would gain or lose readers just by moving
under another folder. Only the note's author or an org owner/admin may make that
change, and only through a single-note move (audited as ``doc.share_update``);
every other move keeps the note's old rule, pinned onto it. Also: folder sharing
needs more than an ``edit`` share, and a break-glass read of a note's sharing is
audited like any other.
"""

from typing import Any

from httpx import AsyncClient

from tripl.tests._docs_sharing_helpers import (
    BASE,
    Crew,
    audit_rows,
    crew,  # noqa: F401 - the fixture
    put,
    read,
    share,
    tree_paths,
)


async def _move(
    who: AsyncClient, source: str, target: str, *, folder: bool = False, expect: int = 200
) -> dict[str, Any]:
    resp = await who.post(
        f"{BASE}/move",
        json={"scope": "project", "from_path": source, "to_path": target, "folder": folder},
    )
    assert resp.status_code == expect, resp.text
    return resp.json()


async def _sharing(who: AsyncClient, path: str, *, folder: bool = False) -> dict[str, Any]:
    kind = "folder" if folder else "file"
    resp = await who.get(f"{BASE}/{kind}/sharing", params={"scope": "project", "path": path})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_an_edit_share_cannot_expose_a_note_by_moving_it(crew: Crew) -> None:  # noqa: F811
    alice, bob, dave = crew.alice, crew.bob, crew.dave
    await put(alice, "team/plan.md", "# Plan\n")
    await put(alice, "team/budget.md", "# Budget\n")
    await share(alice, "team", "restricted", [("user", crew.ids["dave"], "edit")], folder=True)
    await read(bob, "team/plan.md", expect=404)

    # Dave may edit, not re-share: neither the folder nor, by a move, the note.
    await share(dave, "team", "level", folder=True, expect=403)
    await _move(dave, "team/plan.md", "plan.md")
    await read(bob, "plan.md", expect=404)
    assert "plan.md" not in await tree_paths(bob)
    assert (await read(dave, "plan.md"))["my_permission"] == "edit"
    pinned = await _sharing(alice, "plan.md")
    assert (pinned["visibility"], pinned["inherited"]) == ("restricted", False)
    assert [(s["principal_id"], s["permission"]) for s in pinned["shares"]] == [
        (str(crew.ids["dave"]), "edit")
    ]
    moves = await audit_rows("doc.move")
    assert moves[-1].payload["access_kept"] == ["plan.md"]
    assert not [row for row in await audit_rows("doc.share_update") if row.payload.get("via_move")]


async def test_a_colleague_cannot_hide_a_note_by_moving_it(crew: Crew) -> None:  # noqa: F811
    alice, bob = crew.alice, crew.bob
    await put(alice, "vault/own.md", "# Own\n")
    await share(alice, "vault", "private", folder=True)
    await put(bob, "notes.md", "# Bob's notes\n")

    # Alice (a level editor) moves Bob's open note into her private folder.
    await _move(alice, "notes.md", "vault/notes.md")
    assert (await read(crew.carol, "vault/notes.md"))["visibility"] == "level"
    assert "vault/notes.md" in await tree_paths(crew.dave)


async def test_the_author_moving_a_note_lets_it_follow_the_folder_audited(crew: Crew) -> None:  # noqa: F811
    alice, bob = crew.alice, crew.bob
    await put(alice, "vault/own.md", "# Own\n")
    await share(alice, "vault", "private", folder=True)
    await put(alice, "draft.md", "# Draft\n")

    await _move(alice, "draft.md", "vault/draft.md")
    await read(bob, "vault/draft.md", expect=404)
    rows = [row for row in await audit_rows("doc.share_update") if row.payload.get("via_move")]
    assert len(rows) == 1
    assert rows[0].payload["before"]["visibility"] == "level"
    assert rows[0].payload["after"]["visibility"] == "private"
    assert (await audit_rows("doc.move"))[-1].payload["access_kept"] == []


async def test_a_folder_move_keeps_an_ancestors_restriction(crew: Crew) -> None:  # noqa: F811
    alice, bob = crew.alice, crew.bob
    await put(alice, "team/sub/a.md", "# A\n")
    await share(alice, "team", "private", folder=True)

    await _move(alice, "team/sub", "open/sub", folder=True)
    await read(bob, "open/sub/a.md", expect=404)
    assert "open/sub/a.md" not in await tree_paths(bob)
    assert (await read(alice, "open/sub/a.md"))["visibility"] == "private"
    assert (await audit_rows("doc.move"))[-1].payload["access_kept"] == ["open/sub/a.md"]


async def test_a_folder_move_into_a_populated_folder_changes_no_bystander(crew: Crew) -> None:  # noqa: F811
    alice, bob = crew.alice, crew.bob
    await put(bob, "guides/old.md", "# Old\n")
    await put(alice, "drafts/d.md", "# D\n")
    await share(alice, "drafts", "private", folder=True)

    await _move(alice, "drafts", "guides", folder=True)
    # Bob's note keeps its readers: no setting was carried onto "guides" ...
    assert (await read(crew.carol, "guides/old.md"))["visibility"] == "level"
    target = await _sharing(bob, "guides", folder=True)
    assert (target["visibility"], target["inherited"]) == ("level", True)
    # ... and alice's moved note keeps its own rule.
    await read(bob, "guides/d.md", expect=404)
    assert (await read(alice, "guides/d.md"))["visibility"] == "private"


async def test_a_target_folder_setting_never_widens_moved_notes(crew: Crew) -> None:  # noqa: F811
    alice, bob = crew.alice, crew.bob
    await put(alice, "secret/s.md", "# S\n")
    await share(alice, "secret", "private", folder=True)
    await put(alice, "public/p.md", "# P\n")
    await share(alice, "public", "level", folder=True)

    await _move(alice, "secret", "public", folder=True)
    await read(bob, "public/s.md", expect=404)
    assert (await read(bob, "public/p.md"))["visibility"] == "level"


async def test_an_orphaned_folder_setting_is_dropped_despite_like_wildcards(crew: Crew) -> None:  # noqa: F811
    alice, bob = crew.alice, crew.bob
    await put(alice, "a_b/n.md", "# N\n")
    await put(alice, "axb/y.md", "# Y\n")
    await share(alice, "a_b", "private", folder=True)

    await _move(alice, "a_b", "c", folder=True)
    # "a_b" holds no note any more; a stale setting there would make Bob's new
    # note there private.
    await put(bob, "a_b/new.md", "# New\n")
    assert (await read(alice, "a_b/new.md"))["visibility"] == "level"


async def test_a_break_glass_read_of_sharing_is_audited(crew: Crew) -> None:  # noqa: F811
    await put(crew.alice, "diary.md", "# Diary\n")
    await share(crew.alice, "diary.md", "private")

    await _sharing(crew.alice, "diary.md")
    assert await audit_rows("doc.break_glass_read") == []
    sharing = await _sharing(crew.owner, "diary.md")
    assert sharing["visibility"] == "private"
    rows = await audit_rows("doc.break_glass_read")
    assert [(row.target_name, row.payload["read"]) for row in rows] == [("diary.md", "sharing")]
    resp = await crew.bob.get(
        f"{BASE}/file/sharing", params={"scope": "project", "path": "diary.md"}
    )
    assert resp.status_code == 404
