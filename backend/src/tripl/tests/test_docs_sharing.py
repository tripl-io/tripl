"""Note visibility and sharing in the docs catalog (F24, GH #308).

Every read path honours a note's visibility — the tree, a read by path, its
revisions, back-links, exports, API keys — and a note hidden from the caller is
a 404 that is never counted. Organization owners and admins keep an audited
break-glass read that lists nothing. Search is covered by
``test_docs_sharing_search``.
"""

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tripl.main import app
from tripl.models.doc_share import DocShare
from tripl.models.organization import DEFAULT_ORG_ID
from tripl.services import org_service
from tripl.tests._docs_sharing_helpers import (
    BASE,
    Crew,
    audit_rows,
    crew,  # noqa: F401 - the fixture
    leave_group,
    make_group,
    put,
    read,
    share,
    tree_paths,
)
from tripl.tests.conftest import TestSessionLocal

SECRET = "# Secret plan\n\nSee [[event:purchase]] before launch.\n"


async def test_a_private_note_is_hidden_from_other_members_everywhere(crew: Crew) -> None:  # noqa: F811
    alice, bob = crew.alice, crew.bob
    await put(alice, "drafts/secret.md", SECRET)
    shared = await share(alice, "drafts/secret.md", "private")
    assert shared["visibility"] == "private" and shared["inherited"] is False

    mine = await read(alice, "drafts/secret.md")
    assert (mine["visibility"], mine["my_permission"], mine["shared"]) == ("private", "edit", False)
    assert mine["break_glass"] is False
    assert "drafts/secret.md" in await tree_paths(alice)

    # Tree, read, sharing, revisions: all as if the note did not exist.
    assert "drafts/secret.md" not in await tree_paths(bob)
    await read(bob, "drafts/secret.md", expect=404)
    params = {"scope": "project", "path": "drafts/secret.md"}
    assert (await bob.get(f"{BASE}/file/sharing", params=params)).status_code == 404
    assert (await bob.get(f"{BASE}/revisions", params=params)).status_code == 404
    history = await alice.get(f"{BASE}/revisions", params=params)
    assert history.status_code == 200, history.text
    revision_id = history.json()["items"][0]["id"]
    assert (await bob.get(f"{BASE}/revisions/{revision_id}")).status_code == 404
    restore = await bob.post(f"{BASE}/revisions/{revision_id}/restore", json={})
    assert restore.status_code == 404

    # Back-links on the event page.
    for who, expected in ((alice, ["drafts/secret.md"]), (bob, [])):
        links = await who.get(f"{BASE}/backlinks", params={"kind": "event", "name": "purchase"})
        assert links.status_code == 200, links.text
        assert [item["path"] for item in links.json()["items"]] == expected

    # Export.
    exported = await bob.get(f"{BASE}/export", params={"scope": "project"})
    assert exported.status_code == 200, exported.text
    assert all(item["path"] != "drafts/secret.md" for item in exported.json()["files"])

    # Writes: the path is taken, the note itself is 404.
    await put(bob, "drafts/secret.md", "overwrite", expect=409)
    delete = await bob.delete(f"{BASE}/file", params=params)
    assert delete.status_code == 404
    move = await bob.post(
        f"{BASE}/move",
        json={"scope": "project", "from_path": "drafts/secret.md", "to_path": "mine.md"},
    )
    assert move.status_code == 404
    assert (await read(alice, "drafts/secret.md"))["content"] == SECRET


async def test_restricted_notes_share_view_or_edit(crew: Crew) -> None:  # noqa: F811
    await put(crew.alice, "plans/q3.md", "# Q3\n")
    await share(
        crew.alice,
        "plans/q3.md",
        "restricted",
        [("user", crew.ids["bob"], "view"), ("user", crew.ids["dave"], "edit")],
    )

    viewing = await read(crew.bob, "plans/q3.md")
    assert (viewing["visibility"], viewing["my_permission"], viewing["shared"]) == (
        "restricted",
        "view",
        True,
    )
    refused = await crew.bob.put(
        f"{BASE}/file", params={"scope": "project", "path": "plans/q3.md"}, json={"content": "x"}
    )
    assert refused.status_code == 403, refused.text

    editing = await read(crew.dave, "plans/q3.md")
    assert editing["my_permission"] == "edit"
    await put(crew.dave, "plans/q3.md", "# Q3\n\nDave was here.\n")
    # An edit share is not a say over the sharing: the author's or an org admin's.
    await share(crew.dave, "plans/q3.md", "level", expect=403)
    await read(crew.carol, "plans/q3.md", expect=404)

    # An edit share never lifts the project role: a viewer still only reads.
    await share(
        crew.alice,
        "plans/q3.md",
        "restricted",
        [("user", crew.ids["carol"], "edit")],
    )
    assert (await read(crew.carol, "plans/q3.md"))["my_permission"] == "view"
    await read(crew.bob, "plans/q3.md", expect=404)

    # The sharing is listed with names; only the author or an org admin changes it.
    listed = await crew.alice.get(
        f"{BASE}/file/sharing", params={"scope": "project", "path": "plans/q3.md"}
    )
    assert listed.status_code == 200, listed.text
    assert [(item["name"], item["permission"]) for item in listed.json()["shares"]] == [
        ("Carol", "edit")
    ]
    assert listed.json()["can_manage"] is True


async def test_group_shares_follow_group_membership(crew: Crew) -> None:  # noqa: F811
    group_id = await make_group("Data team", [crew.ids["bob"]])
    await put(crew.alice, "warehouse.md", "# Warehouse\n")
    await share(crew.alice, "warehouse.md", "restricted", [("group", group_id, "view")])

    assert (await read(crew.bob, "warehouse.md"))["my_permission"] == "view"
    await read(crew.dave, "warehouse.md", expect=404)

    await leave_group(group_id, crew.ids["bob"])
    await read(crew.bob, "warehouse.md", expect=404)
    assert "warehouse.md" not in await tree_paths(crew.bob)


async def test_sharing_with_a_non_member_grants_nothing(crew: Crew) -> None:  # noqa: F811
    await put(crew.alice, "hello.md", "# Hello\n")
    await share(
        crew.alice,
        "hello.md",
        "restricted",
        [("user", crew.ids["stranger"], "view")],
        expect=422,
    )
    assert (await crew.stranger.get(BASE)).status_code == 404
    await read(crew.stranger, "hello.md", expect=404)
    # Nothing was written: the note is still visible at its level.
    assert (await read(crew.bob, "hello.md"))["visibility"] == "level"


async def test_folders_are_inherited_unless_a_note_overrides(crew: Crew) -> None:  # noqa: F811
    alice, bob, dave = crew.alice, crew.bob, crew.dave
    await put(alice, "team/a.md", "# A\n")
    await put(bob, "team/b.md", "# B\n")
    await put(dave, "loose.md", "# Loose\n")

    folder = await share(alice, "team", "private", folder=True)
    assert (folder["visibility"], folder["inherited"]) == ("private", False)

    # Private folder: every note is its author's only.
    assert "team/a.md" not in await tree_paths(bob)
    assert "team/b.md" in await tree_paths(bob)
    assert "team/b.md" not in await tree_paths(alice)
    inherited = await bob.get(
        f"{BASE}/file/sharing", params={"scope": "project", "path": "team/b.md"}
    )
    assert inherited.status_code == 200, inherited.text
    assert (
        inherited.json()["visibility"],
        inherited.json()["inherited"],
        inherited.json()["inherited_from"],
    ) == ("private", True, "team")
    # Bob cannot edit alice's note under the folder, so he may not change it.
    await share(bob, "team", "level", folder=True, expect=403)

    # A note's own setting wins over its folder's, until it inherits again.
    await share(alice, "team/a.md", "level")
    assert (await read(bob, "team/a.md"))["visibility"] == "level"
    await share(alice, "team/a.md", "level", inherited=True)
    await read(bob, "team/a.md", expect=404)

    # A note moved into the folder takes its setting while it inherits.
    moved = await dave.post(
        f"{BASE}/move",
        json={"scope": "project", "from_path": "loose.md", "to_path": "team/loose.md"},
    )
    assert moved.status_code == 200, moved.text
    await read(bob, "team/loose.md", expect=404)
    assert (await read(dave, "team/loose.md"))["visibility"] == "private"

    # An org admin turns the folder into a shared one.
    await share(crew.owner, "team", "restricted", [("user", crew.ids["bob"], "view")], folder=True)
    assert (await read(bob, "team/a.md"))["my_permission"] == "view"
    assert (await read(bob, "team/loose.md"))["shared"] is True
    subfolder = await bob.get(
        f"{BASE}/folder/sharing", params={"scope": "project", "path": "team/sub"}
    )
    # No note lives under team/sub: the folder does not exist for bob.
    assert subfolder.status_code == 404


async def test_a_folder_move_keeps_its_notes_access(crew: Crew) -> None:  # noqa: F811
    await put(crew.alice, "vault/one.md", "# One\n")
    await share(crew.alice, "vault", "private", folder=True)
    moved = await crew.alice.post(
        f"{BASE}/move",
        json={"scope": "project", "from_path": "vault", "to_path": "safe", "folder": True},
    )
    assert moved.status_code == 200, moved.text
    await read(crew.bob, "safe/one.md", expect=404)
    assert (await read(crew.alice, "safe/one.md"))["visibility"] == "private"


async def test_break_glass_reads_are_audited_and_never_listed(crew: Crew) -> None:  # noqa: F811
    owner = crew.owner
    await put(crew.alice, "diary.md", "# Diary\n\nzephyrine thoughts\n")
    await share(crew.alice, "diary.md", "private")

    assert "diary.md" not in await tree_paths(owner)
    found = await owner.get(f"{BASE}/search", params={"q": "zephyrine"})
    assert found.status_code == 200, found.text
    assert found.json()["items"] == [] and found.json()["total"] == 0

    opened = await read(owner, "diary.md")
    assert opened["break_glass"] is True and opened["my_permission"] == "view"
    rows = await audit_rows("doc.break_glass_read")
    assert [(row.target_name, row.payload["read"]) for row in rows] == [("diary.md", "file")]
    history = await owner.get(f"{BASE}/revisions", params={"scope": "project", "path": "diary.md"})
    assert history.status_code == 200, history.text
    assert len(await audit_rows("doc.break_glass_read")) == 2

    # Reading is not editing.
    refused = await owner.put(
        f"{BASE}/file", params={"scope": "project", "path": "diary.md"}, json={"content": "x"}
    )
    assert refused.status_code == 403, refused.text

    # But an org admin can change the sharing, and that is audited too.
    await share(owner, "diary.md", "level")
    assert (await read(crew.bob, "diary.md"))["visibility"] == "level"
    updates = await audit_rows("doc.share_update")
    by_admin = [row for row in updates if not row.payload["as_author"]]
    assert len(by_admin) == 1
    assert by_admin[0].payload["before"]["visibility"] == "private"
    assert by_admin[0].payload["after"]["visibility"] == "level"
    assert "zephyrine" not in str(by_admin[0].payload)


async def test_only_the_author_or_an_org_admin_changes_sharing(crew: Crew) -> None:  # noqa: F811
    await put(crew.alice, "open.md", "# Open\n")
    await share(crew.bob, "open.md", "private", expect=403)
    await share(crew.carol, "open.md", "private", expect=403)
    await share(crew.alice, "open.md", "private")
    rows = await audit_rows("doc.share_update")
    assert rows[-1].payload["as_author"] is True


async def test_import_ignores_visibility_and_export_never_writes_one(crew: Crew) -> None:  # noqa: F811
    content = "---\ntitle: Imported\nvisibility: private\n---\nBody\n"
    imported = await crew.alice.post(
        f"{BASE}/import",
        params={"scope": "project"},
        json={"files": [{"path": "imported.md", "content": content}]},
    )
    assert imported.status_code == 200, imported.text
    seen = await read(crew.bob, "imported.md")
    assert seen["visibility"] == "level"
    assert seen["extra_frontmatter"] == {"visibility": "private"}

    await put(crew.alice, "p.md", "# P\n")
    await share(crew.alice, "p.md", "private")
    mine = await crew.alice.get(f"{BASE}/export", params={"scope": "project"})
    assert {item["path"]: item["content"] for item in mine.json()["files"]}["p.md"] == "# P\n"

    # Bob's import cannot overwrite, nor his mirror delete, a note hidden from him.
    clash = await crew.bob.post(
        f"{BASE}/import",
        params={"scope": "project", "dry_run": "true"},
        json={"files": [{"path": "p.md", "content": "hijack"}]},
    )
    assert clash.status_code == 200, clash.text
    assert clash.json()["errors"] == [{"path": "p.md", "detail": "the path is taken"}]
    mirror = await crew.bob.post(
        f"{BASE}/import",
        params={"scope": "project", "mode": "mirror"},
        json={"files": [{"path": "imported.md", "content": content}]},
    )
    assert mirror.status_code == 200, mirror.text
    assert "p.md" not in mirror.json()["deleted"]
    assert (await read(crew.alice, "p.md"))["content"] == "# P\n"


async def test_an_api_key_acts_as_its_user(crew: Crew) -> None:  # noqa: F811
    await put(crew.alice, "keyed.md", "# Keyed\n")
    await share(crew.alice, "keyed.md", "private")
    tokens = {}
    for name, who in (("alice", crew.alice), ("bob", crew.bob)):
        minted = await who.post("/api/v1/me/api-keys", json={"name": "agent", "scope": "read"})
        assert minted.status_code == 201, minted.text
        tokens[name] = {"Authorization": f"Bearer {minted.json()['token']}"}

    params = {"scope": "project", "path": "keyed.md"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as bearer:
        hidden = await bearer.get(f"{BASE}/file", params=params, headers=tokens["bob"])
        assert hidden.status_code == 404
        tree = await bearer.get(BASE, headers=tokens["bob"])
        assert all(item["path"] != "keyed.md" for item in tree.json()["project_docs"])
        own = await bearer.get(f"{BASE}/file", params=params, headers=tokens["alice"])
        assert own.status_code == 200, own.text
        # A read-scope key reads, and says so.
        assert own.json()["my_permission"] == "view"


async def test_folder_operations_never_count_hidden_notes(crew: Crew) -> None:  # noqa: F811
    await put(crew.alice, "mixed/hers.md", "# Hers\n")
    await share(crew.alice, "mixed/hers.md", "private")
    await put(crew.bob, "mixed/his.md", "# His\n")
    await put(crew.alice, "hidden/only.md", "# Only\n")
    await share(crew.alice, "hidden/only.md", "private")

    deleted = await crew.bob.delete(f"{BASE}/folder", params={"scope": "project", "path": "mixed"})
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted": ["mixed/his.md"]}
    await read(crew.alice, "mixed/hers.md")

    gone = await crew.bob.delete(f"{BASE}/folder", params={"scope": "project", "path": "hidden"})
    assert gone.status_code == 404
    moved = await crew.bob.post(
        f"{BASE}/move",
        json={"scope": "project", "from_path": "hidden", "to_path": "found", "folder": True},
    )
    assert moved.status_code == 404
    tree = await crew.bob.get(BASE)
    assert len(tree.json()["project_docs"]) == 0


async def test_organization_notes_have_visibility_too(crew: Crew) -> None:  # noqa: F811
    owner = crew.owner
    await put(owner, "handbook/pay.md", "# Pay bands\n", scope="organization")
    await share(owner, "handbook/pay.md", "private", scope="organization")

    await read(crew.alice, "handbook/pay.md", scope="organization", expect=404)
    tree = await crew.alice.get(BASE)
    assert all(item["path"] != "handbook/pay.md" for item in tree.json()["organization_docs"])

    await share(
        owner,
        "handbook/pay.md",
        "restricted",
        [("user", crew.ids["alice"], "view")],
        scope="organization",
    )
    seen = await read(crew.alice, "handbook/pay.md", scope="organization")
    assert (seen["visibility"], seen["my_permission"]) == ("restricted", "view")
    await read(crew.bob, "handbook/pay.md", scope="organization", expect=404)


async def test_leaving_the_organization_drops_the_users_shares(crew: Crew) -> None:  # noqa: F811
    await put(crew.alice, "bye.md", "# Bye\n")
    await share(crew.alice, "bye.md", "restricted", [("user", crew.ids["bob"], "view")])
    async with TestSessionLocal() as session:
        await org_service.remove_member(
            session, DEFAULT_ORG_ID, crew.ids["bob"], actor_id=crew.ids["owner"]
        )
        await session.commit()
        left = await session.scalars(select(DocShare).where(DocShare.user_id == crew.ids["bob"]))
        assert left.all() == []
