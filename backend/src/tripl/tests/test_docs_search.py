"""Docs catalog notes in the hybrid search index (F22, GH #299)."""

from httpx import AsyncClient

from tripl.services._search_documents import DOCUMENT_BUILDER_VERSION
from tripl.tests._docs_helpers import create_project, put_doc

NOTE = (
    "---\n"
    "title: Axolotl warehouse gotchas\n"
    "description: Why the events table double counts\n"
    "tags: [warehouse]\n"
    "audience: agent\n"
    "---\n"
    "Join on [[event:purchase|the purchase event]] carefully.\n"
)


def test_builder_version_was_bumped_for_docs() -> None:
    assert DOCUMENT_BUILDER_VERSION == 4


async def test_notes_are_searchable_with_their_own_route(client: AsyncClient) -> None:
    await create_project(client, "searchable")
    await put_doc(client, "searchable", "guides/warehouse.md", NOTE)

    resp = await client.get(
        "/api/v1/projects/searchable/search", params={"q": "axolotl", "types": "doc"}
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [(item["entity_type"], item["title"]) for item in items] == [
        ("doc", "Axolotl warehouse gotchas")
    ]
    assert items[0]["route_path"] == "/p/searchable/docs/project/guides/warehouse.md"
    assert items[0]["subtitle"] == "Project notes · guides/warehouse.md"

    docs = await client.get("/api/v1/projects/searchable/docs/search", params={"q": "axolotl"})
    assert docs.status_code == 200, docs.text
    body = docs.json()
    assert body["total"] == 1
    hit = body["items"][0]
    assert (hit["scope"], hit["path"], hit["audience"], hit["tags"]) == (
        "project",
        "guides/warehouse.md",
        "agent",
        ["warehouse"],
    )


async def test_organization_notes_reach_every_project_of_the_organization(
    client: AsyncClient,
) -> None:
    await create_project(client, "writer")
    await create_project(client, "reader")
    await put_doc(client, "writer", "shared.md", "# Quokka conventions", scope="organization")

    resp = await client.get("/api/v1/projects/reader/docs/search", params={"q": "quokka"})
    assert resp.status_code == 200, resp.text
    assert [(item["scope"], item["path"]) for item in resp.json()["items"]] == [
        ("organization", "shared.md")
    ]
    only_project = await client.get(
        "/api/v1/projects/reader/docs/search", params={"q": "quokka", "scope": "project"}
    )
    assert only_project.json()["items"] == []

    routed = await client.get(
        "/api/v1/projects/reader/search", params={"q": "quokka", "types": "doc"}
    )
    assert routed.json()["items"][0]["route_path"] == "/p/reader/docs/organization/shared.md"


async def test_a_deleted_note_leaves_the_index(client: AsyncClient) -> None:
    await create_project(client, "forgetful")
    await put_doc(client, "forgetful", "tmp.md", "# Narwhal scratchpad")
    found = await client.get("/api/v1/projects/forgetful/docs/search", params={"q": "narwhal"})
    assert found.json()["total"] == 1

    deleted = await client.delete(
        "/api/v1/projects/forgetful/docs/file", params={"scope": "project", "path": "tmp.md"}
    )
    assert deleted.status_code == 204
    gone = await client.get("/api/v1/projects/forgetful/docs/search", params={"q": "narwhal"})
    assert gone.json()["items"] == []


async def test_a_deleted_note_leaves_feature_branch_indexes_too(client: AsyncClient) -> None:
    """Doc rows are copied into every branch; a delete must not leave them there."""
    await create_project(client, "branchy")
    await put_doc(client, "branchy", "secret.md", "# Walrus credentials")
    branch = await client.post("/api/v1/projects/branchy/branches", json={"name": "feature"})
    assert branch.status_code == 201, branch.text
    branch_id = branch.json()["id"]
    reindexed = await client.post(
        "/api/v1/projects/branchy/search/reindex", params={"branch": branch_id}
    )
    assert reindexed.status_code == 200, reindexed.text
    params = {"q": "walrus", "types": "doc", "branch": branch_id}
    before = await client.get("/api/v1/projects/branchy/search", params=params)
    assert [item["title"] for item in before.json()["items"]] == ["Walrus credentials"]

    deleted = await client.delete(
        "/api/v1/projects/branchy/docs/file", params={"scope": "project", "path": "secret.md"}
    )
    assert deleted.status_code == 204
    after = await client.get("/api/v1/projects/branchy/search", params=params)
    assert after.json()["items"] == []
