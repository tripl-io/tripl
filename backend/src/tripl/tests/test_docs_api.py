"""Docs catalog API: CRUD, history, moves and authorization (F22, GH #299)."""

import hashlib
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError

from tripl.main import app
from tripl.services import _docs_store as docs_store
from tripl.services.docs_access import ORG_NOTES_ADMIN_REQUIRED
from tripl.services.docs_paths import MAX_FILE_BYTES
from tripl.tests._docs_helpers import create_project, get_doc, put_doc, register
from tripl.tests._members import add_member_by_slug

BASE = "/api/v1/projects/{slug}/docs"


def _url(slug: str, suffix: str = "") -> str:
    return BASE.format(slug=slug) + suffix


async def test_create_read_update_and_tree(client: AsyncClient) -> None:
    await create_project(client, "notes")
    content = "---\ntitle: Warehouse gotchas\ntags: [sql]\naudience: agent\nx-extra: 1\n---\nBody\n"

    created = await put_doc(client, "notes", "./guides//warehouse.md", content, message="first")

    assert created["created"] is True and created["changed"] is True
    assert created["path"] == "guides/warehouse.md"
    assert created["scope"] == "project"
    assert created["revision"] == 1
    assert created["title"] == "Warehouse gotchas"
    assert created["tags"] == ["sql"]
    assert created["audience"] == "agent"
    assert created["content"] == content
    assert created["body"] == "Body\n"
    assert created["extra_frontmatter"] == {"x-extra": 1}
    assert created["created_by_name"] == "Test User"

    same = await put_doc(client, "notes", "guides/warehouse.md", content)
    assert same["changed"] is False and same["revision"] == 1

    updated = await put_doc(
        client, "notes", "guides/warehouse.md", content + "More\n", base_revision=1
    )
    assert updated["created"] is False and updated["changed"] is True
    assert updated["revision"] == 2

    read = await get_doc(client, "notes", "guides/warehouse.md")
    assert read["content"] == content + "More\n"

    await put_doc(client, "notes", "org-wide.md", "# Org note", scope="organization")
    tree = await client.get(_url("notes"))
    assert tree.status_code == 200, tree.text
    body = tree.json()
    assert body["project"] == {"slug": "notes", "name": "Notes"}
    assert body["organization"]["slug"] == "default"
    assert [doc["path"] for doc in body["project_docs"]] == ["guides/warehouse.md"]
    assert [(doc["path"], doc["title"]) for doc in body["organization_docs"]] == [
        ("org-wide.md", "Org note")
    ]
    assert body["limits"]["max_file_bytes"] == MAX_FILE_BYTES


async def test_write_conflicts_and_validation(client: AsyncClient) -> None:
    await create_project(client, "strict")
    await put_doc(client, "strict", "Guide.md", "v1")

    stale = await put_doc(client, "strict", "Guide.md", "v2", base_revision=5, expect=409)
    assert stale["detail"] == "Doc changed since revision 5"
    exists = await put_doc(client, "strict", "Guide.md", "v2", create_only=True, expect=409)
    assert "already exists" in exists["detail"]
    case = await put_doc(client, "strict", "guide.md", "v2", expect=409)
    assert "case-insensitive" in case["detail"]

    await put_doc(client, "strict", "../escape.md", "x", expect=422)
    await put_doc(client, "strict", "notes.txt", "x", expect=422)
    bad_yaml = await put_doc(client, "strict", "y.md", "---\naudience: bots\n---\n", expect=422)
    assert "audience" in bad_yaml["detail"]
    too_big = await put_doc(client, "strict", "big.md", "é" * (MAX_FILE_BYTES // 2 + 1), expect=413)
    assert "KiB" in too_big["detail"]

    missing = await get_doc(client, "strict", "nope.md", expect=404)
    assert missing["detail"] == "Doc not found"


async def test_revisions_diff_and_restore(client: AsyncClient) -> None:
    await create_project(client, "history")
    await put_doc(client, "history", "a.md", "line one\n", message="start")
    await put_doc(client, "history", "a.md", "line one\nline two\n")

    listing = await client.get(
        _url("history", "/revisions"), params={"scope": "project", "path": "a.md"}
    )
    assert listing.status_code == 200, listing.text
    revisions = listing.json()
    assert revisions["current_revision"] == 2
    assert [(item["number"], item["action"]) for item in revisions["items"]] == [
        (2, "update"),
        (1, "create"),
    ]
    assert revisions["items"][1]["message"] == "start"
    assert revisions["items"][0]["author_name"] == "Test User"

    first_id = revisions["items"][1]["id"]
    second = await client.get(_url("history", f"/revisions/{revisions['items'][0]['id']}"))
    assert second.status_code == 200, second.text
    assert "+line two" in second.json()["diff"]
    first = await client.get(_url("history", f"/revisions/{first_id}"))
    assert first.json()["diff"] == ""

    restored = await client.post(_url("history", f"/revisions/{first_id}/restore"), json={})
    assert restored.status_code == 200, restored.text
    assert restored.json()["revision"] == 3
    assert restored.json()["content"] == "line one\n"
    after = await client.get(
        _url("history", "/revisions"), params={"scope": "project", "path": "a.md"}
    )
    newest = after.json()["items"][0]
    assert newest["action"] == "restore" and newest["restored_from_number"] == 1


async def test_revision_of_another_project_is_404(client: AsyncClient) -> None:
    await create_project(client, "mine")
    await create_project(client, "theirs")
    await put_doc(client, "theirs", "secret.md", "x")
    listing = await client.get(
        _url("theirs", "/revisions"), params={"scope": "project", "path": "secret.md"}
    )
    revision_id = listing.json()["items"][0]["id"]

    assert (await client.get(_url("mine", f"/revisions/{revision_id}"))).status_code == 404
    restore = await client.post(_url("mine", f"/revisions/{revision_id}/restore"), json={})
    assert restore.status_code == 404


async def test_move_file_and_folder(client: AsyncClient) -> None:
    await create_project(client, "moves")
    for path in ("skill/SKILL.md", "skill/references/a.md", "skill/references/b.md", "top.md"):
        await put_doc(client, "moves", path, f"# {path}")

    renamed = await client.post(
        _url("moves", "/move"),
        json={"scope": "project", "from_path": "top.md", "to_path": "Top.md"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["moved"] == [{"from_path": "top.md", "to_path": "Top.md"}]

    collision = await client.post(
        _url("moves", "/move"),
        json={"scope": "project", "from_path": "Top.md", "to_path": "skill/SKILL.md"},
    )
    assert collision.status_code == 409
    assert "skill/SKILL.md" in collision.json()["detail"]

    into_itself = await client.post(
        _url("moves", "/move"),
        json={"scope": "project", "from_path": "skill", "to_path": "skill/x", "folder": True},
    )
    assert into_itself.status_code == 422

    folder = await client.post(
        _url("moves", "/move"),
        json={"scope": "project", "from_path": "skill/", "to_path": "tools/skill", "folder": True},
    )
    assert folder.status_code == 200, folder.text
    assert sorted(item["to_path"] for item in folder.json()["moved"]) == [
        "tools/skill/SKILL.md",
        "tools/skill/references/a.md",
        "tools/skill/references/b.md",
    ]
    moved = await get_doc(client, "moves", "tools/skill/references/a.md")
    assert moved["revision"] == 2
    await get_doc(client, "moves", "skill/references/a.md", expect=404)


async def test_delete_file_and_folder(client: AsyncClient) -> None:
    await create_project(client, "deletes")
    for path in ("refs/a.md", "refs/deep/b.md", "refsX.md", "keep.md"):
        await put_doc(client, "deletes", path, "x")

    gone = await client.delete(
        _url("deletes", "/file"), params={"scope": "project", "path": "keep.md"}
    )
    assert gone.status_code == 204
    await get_doc(client, "deletes", "keep.md", expect=404)

    folder = await client.delete(
        _url("deletes", "/folder"), params={"scope": "project", "path": "refs/"}
    )
    assert folder.status_code == 200, folder.text
    assert folder.json() == {"deleted": ["refs/a.md", "refs/deep/b.md"]}
    await get_doc(client, "deletes", "refsX.md")

    empty = await client.delete(
        _url("deletes", "/folder"), params={"scope": "project", "path": "refs"}
    )
    assert empty.status_code == 404


async def test_a_nul_in_a_path_or_backlink_name_is_422_not_500(client: AsyncClient) -> None:
    """Pins the ``path`` entry of test_text_filters._GUARDED_ELSEWHERE and the backlinks guard."""
    await create_project(client, "nul")
    await put_doc(client, "nul", "ab.md", "x")
    laced = {"scope": "project", "path": "a\x00b.md"}

    assert (await client.get(_url("nul", "/file"), params=laced)).status_code == 422
    assert (await client.get(_url("nul", "/revisions"), params=laced)).status_code == 422
    assert (await client.delete(_url("nul", "/file"), params=laced)).status_code == 422
    folder = await client.delete(_url("nul", "/folder"), params={**laced, "path": "a\x00b"})
    assert folder.status_code == 422
    await get_doc(client, "nul", "ab.md")

    only_nul = await client.get(_url("nul", "/backlinks"), params={"kind": "event", "name": "\x00"})
    assert only_nul.status_code == 422
    stripped = await client.get(
        _url("nul", "/backlinks"),
        params={"kind": "event", "name": "pur\x00chase", "qualifier": "x\x00"},
    )
    assert stripped.status_code == 200, stripped.text
    assert stripped.json()["name"] == "purchase"
    assert stripped.json()["qualifier"] == "x"


async def test_writes_are_audited_on_the_project(client: AsyncClient) -> None:
    await create_project(client, "audited")
    await put_doc(client, "audited", "a.md", "one")
    await put_doc(client, "audited", "a.md", "two")
    await client.post(
        _url("audited", "/move"),
        json={"scope": "project", "from_path": "a.md", "to_path": "b.md"},
    )
    await put_doc(client, "audited", "org.md", "x", scope="organization")
    await client.delete(_url("audited", "/file"), params={"scope": "project", "path": "b.md"})

    resp = await client.get("/api/v1/projects/audited/audit", params={"limit": 50})
    assert resp.status_code == 200, resp.text
    actions = [(item["action"], item["target_name"]) for item in resp.json()["items"]]
    for expected in (
        ("doc.create", "a.md"),
        ("doc.update", "a.md"),
        ("doc.move", "b.md"),
        ("doc.create", "org.md"),
        ("doc.delete", "b.md"),
    ):
        assert expected in actions


async def test_organization_notes_are_shared_across_the_organizations_projects(
    client: AsyncClient,
) -> None:
    await create_project(client, "first")
    await create_project(client, "second")
    await put_doc(client, "first", "shared/conventions.md", "# Conventions", scope="organization")

    seen = await get_doc(client, "second", "shared/conventions.md", scope="organization")
    assert seen["title"] == "Conventions"
    await get_doc(client, "second", "shared/conventions.md", scope="project", expect=404)
    # One path per scope: the same path may exist in both.
    await put_doc(client, "second", "shared/conventions.md", "project copy")


# ── Authorization ─────────────────────────────────────────────────────────────


class _People:
    def __init__(self) -> None:
        self.viewer = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.stranger = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest_asyncio.fixture
async def people(client: AsyncClient) -> AsyncGenerator[_People]:
    """``client`` is the organization owner; a project viewer and a non-member."""
    crew = _People()
    await create_project(client, "guarded")
    await put_doc(client, "guarded", "readme.md", "# Readme")
    await put_doc(client, "guarded", "org.md", "# Org", scope="organization")
    await register(crew.viewer, "viewer@example.com", "Viewer")
    await register(crew.stranger, "stranger@example.com", "Stranger")
    await add_member_by_slug("guarded", "viewer@example.com", "viewer")
    yield crew
    await crew.viewer.aclose()
    await crew.stranger.aclose()


async def test_viewer_reads_but_cannot_write(people: _People) -> None:
    viewer = people.viewer
    assert (await viewer.get(_url("guarded"))).status_code == 200
    await get_doc(viewer, "guarded", "readme.md")
    await get_doc(viewer, "guarded", "org.md", scope="organization")

    await put_doc(viewer, "guarded", "readme.md", "changed", expect=403)
    await put_doc(viewer, "guarded", "org2.md", "x", scope="organization", expect=403)
    delete = await viewer.delete(
        _url("guarded", "/file"), params={"scope": "project", "path": "readme.md"}
    )
    assert delete.status_code == 403
    imported = await viewer.post(
        _url("guarded", "/import"), params={"scope": "project"}, json={"files": []}
    )
    assert imported.status_code == 403


async def test_non_member_gets_404(people: _People) -> None:
    stranger = people.stranger
    assert (await stranger.get(_url("guarded"))).status_code == 404
    await get_doc(stranger, "guarded", "readme.md", expect=404)
    await put_doc(stranger, "guarded", "x.md", "x", expect=404)


async def test_read_key_reads_and_is_refused_writes(client: AsyncClient) -> None:
    await create_project(client, "keyed")
    await put_doc(client, "keyed", "a.md", "# A")
    minted = await client.post("/api/v1/me/api-keys", json={"name": "agent", "scope": "read"})
    assert minted.status_code == 201, minted.text
    headers = {"Authorization": f"Bearer {minted.json()['token']}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as bearer:
        read = await bearer.get(
            _url("keyed", "/file"), params={"scope": "project", "path": "a.md"}, headers=headers
        )
        assert read.status_code == 200, read.text
        write = await bearer.put(
            _url("keyed", "/file"),
            params={"scope": "project", "path": "a.md"},
            json={"content": "changed"},
            headers=headers,
        )
        assert write.status_code == 403
        assert write.json()["detail"] == "API key has read-only scope"


# ── Organization notes: project-bound keys and bulk deletes ───────────────────


async def _mint_key(client: AsyncClient, **extra: str) -> dict[str, str]:
    minted = await client.post(
        "/api/v1/me/api-keys", json={"name": "agent", "scope": "write", **extra}
    )
    assert minted.status_code == 201, minted.text
    return {"Authorization": f"Bearer {minted.json()['token']}"}


async def test_a_project_bound_key_cannot_write_organization_notes(client: AsyncClient) -> None:
    await create_project(client, "fenced")
    await put_doc(client, "fenced", "org.md", "# Shared", scope="organization")
    headers = await _mint_key(client, project_slug="fenced")
    refused = "A project-scoped API key cannot edit organization notes"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as bearer:
        own = await bearer.put(
            _url("fenced", "/file"),
            params={"scope": "project", "path": "mine.md"},
            json={"content": "ok"},
            headers=headers,
        )
        assert own.status_code == 200, own.text
        read = await bearer.get(
            _url("fenced", "/file"),
            params={"scope": "organization", "path": "org.md"},
            headers=headers,
        )
        assert read.status_code == 200, read.text

        attempts = [
            bearer.put(
                _url("fenced", "/file"),
                params={"scope": "organization", "path": "org.md"},
                json={"content": "hijacked"},
                headers=headers,
            ),
            bearer.delete(
                _url("fenced", "/file"),
                params={"scope": "organization", "path": "org.md"},
                headers=headers,
            ),
            bearer.post(
                _url("fenced", "/move"),
                json={"scope": "organization", "from_path": "org.md", "to_path": "x.md"},
                headers=headers,
            ),
            bearer.post(
                _url("fenced", "/import"),
                params={"scope": "organization"},
                json={"files": [{"path": "new.md", "content": "x"}]},
                headers=headers,
            ),
        ]
        for attempt in attempts:
            resp = await attempt
            assert resp.status_code == 403, resp.text
            assert resp.json()["detail"] == refused
    assert (await get_doc(client, "fenced", "org.md", scope="organization"))["title"] == "Shared"


async def test_an_owner_key_cannot_bulk_delete_organization_notes(client: AsyncClient) -> None:
    await create_project(client, "bulk")
    await put_doc(client, "bulk", "team/a.md", "a", scope="organization")
    headers = await _mint_key(client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as bearer:
        mirror = await bearer.post(
            _url("bulk", "/import"),
            params={"scope": "organization", "mode": "mirror"},
            json={"files": []},
            headers=headers,
        )
        assert mirror.status_code == 403
        assert mirror.json()["detail"] == "Owner session required to mirror organization notes"
        folder = await bearer.delete(
            _url("bulk", "/folder"),
            params={"scope": "organization", "path": "team"},
            headers=headers,
        )
        assert folder.status_code == 403
        assert (
            folder.json()["detail"]
            == "Owner session required to delete folders of organization notes"
        )
        # A single organization note is still an ordinary edit for an org owner's
        # organization-wide key.
        one = await bearer.put(
            _url("bulk", "/file"),
            params={"scope": "organization", "path": "team/b.md"},
            json={"content": "b"},
            headers=headers,
        )
        assert one.status_code == 200, one.text
    await get_doc(client, "bulk", "team/a.md", scope="organization")


async def test_organization_notes_are_written_by_organization_owners_and_admins(
    client: AsyncClient,
) -> None:
    """A project editor who is a plain organization member reads organization notes
    but cannot change them; promoted to organization admin, they can."""
    await create_project(client, "orgwriters")
    await put_doc(client, "orgwriters", "org.md", "# Org", scope="organization")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as editor:
        member = await register(editor, "org-writer@example.com", "Writer")
        await add_member_by_slug("orgwriters", "org-writer@example.com", "editor")
        await put_doc(editor, "orgwriters", "mine.md", "project note")
        await get_doc(editor, "orgwriters", "org.md", scope="organization")
        refused = await editor.put(
            _url("orgwriters", "/file"),
            params={"scope": "organization", "path": "org.md"},
            json={"content": "changed"},
        )
        assert refused.status_code == 403
        assert refused.json()["detail"] == ORG_NOTES_ADMIN_REQUIRED
        moved = await editor.post(
            _url("orgwriters", "/move"),
            json={"scope": "organization", "from_path": "org.md", "to_path": "x.md"},
        )
        assert moved.status_code == 403
        deleted = await editor.delete(
            _url("orgwriters", "/file"), params={"scope": "organization", "path": "org.md"}
        )
        assert deleted.status_code == 403

        promote = await client.patch(f"/api/v1/users/{member['id']}", json={"role": "admin"})
        assert promote.status_code == 200, promote.text
        await put_doc(editor, "orgwriters", "org.md", "# Changed", scope="organization")
    assert (await get_doc(client, "orgwriters", "org.md", scope="organization"))[
        "title"
    ] == "Changed"


async def test_organization_folder_delete_needs_an_org_admin_and_is_fully_audited(
    client: AsyncClient,
) -> None:
    await create_project(client, "orgfolder")
    first = await put_doc(client, "orgfolder", "team/a.md", "a", scope="organization")
    await put_doc(client, "orgfolder", "team/deep/b.md", "b", scope="organization")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as editor:
        await register(editor, "folder-editor@example.com", "Editor")
        await add_member_by_slug("orgfolder", "folder-editor@example.com", "editor")
        refused = await editor.delete(
            _url("orgfolder", "/folder"), params={"scope": "organization", "path": "team"}
        )
        assert refused.status_code == 403
        assert refused.json()["detail"] == ORG_NOTES_ADMIN_REQUIRED

    deleted = await client.delete(
        _url("orgfolder", "/folder"), params={"scope": "organization", "path": "team"}
    )
    assert deleted.status_code == 200, deleted.text
    audit = await client.get(
        "/api/v1/projects/orgfolder/audit", params={"action": "doc.folder_delete"}
    )
    entry = await client.get(f"/api/v1/projects/orgfolder/audit/{audit.json()['items'][0]['id']}")
    payload = entry.json()["payload"]
    assert payload["deleted"][0] == {
        "path": "team/a.md",
        "revision": first["revision"],
        "content_sha256": hashlib.sha256(b"a").hexdigest(),
    }
    assert [item["path"] for item in payload["deleted"]] == ["team/a.md", "team/deep/b.md"]


async def test_a_racing_writer_gets_409_not_500(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A unique-index clash (two writers of one note or path) is the documented 409."""
    await create_project(client, "racy")

    async def clash(*_args: object, **_kwargs: object) -> None:
        raise IntegrityError("INSERT", {}, Exception("uq_doc_revisions_file_number"))

    monkeypatch.setattr(docs_store, "apply_content", clash)
    resp = await client.put(
        _url("racy", "/file"), params={"scope": "project", "path": "a.md"}, json={"content": "x"}
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "Doc changed concurrently; reload and retry"
