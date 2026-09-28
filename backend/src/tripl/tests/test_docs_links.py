"""Plan links in docs catalog notes: extraction, live resolution, back-links (F22)."""

import pytest
from httpx import AsyncClient

from tripl.services.docs_links import (
    extract_links,
    parse_ref,
    rewrite_links_as_text,
)
from tripl.services.docs_paths import MAX_LINKS_PER_FILE
from tripl.tests._docs_helpers import create_project, put_doc, seed_plan


def test_links_are_extracted_outside_code_only() -> None:
    body = (
        "See [[event:purchase|the purchase event]] and [[event-type:checkout]].\n"
        "Inline `[[event:not_a_link]]` is text.\n"
        "```sql\n[[field:also_text]]\n```\n"
        "~~~\n[[field:still_text]]\n~~~\n"
        "Field [[field:checkout/amount]] and bare [[field:amount]].\n"
        "Malformed [[metric:x]] and [[event:]] are ignored.\n"
    )
    links = extract_links(body)

    assert [(link.kind, link.target, link.qualifier, link.label) for link in links] == [
        ("event", "purchase", None, "the purchase event"),
        ("event_type", "checkout", None, None),
        ("field", "amount", "checkout", None),
        ("field", "amount", None, None),
    ]
    # Offsets index the original body.
    first = links[0]
    assert body[first.start : first.end] == "[[event:purchase|the purchase event]]"


def test_link_extraction_is_capped() -> None:
    body = " ".join(f"[[event:e{i}]]" for i in range(MAX_LINKS_PER_FILE + 20))
    assert len(extract_links(body)) == MAX_LINKS_PER_FILE


def test_links_become_plain_words_for_the_index() -> None:
    assert (
        rewrite_links_as_text("a [[event:purchase|the buy]] b [[field:checkout/amount]]")
        == "a purchase the buy b checkout amount"
    )


def test_parse_ref() -> None:
    assert parse_ref("event:purchase") == ("event", "purchase", None)
    assert parse_ref("event-type:checkout") == ("event_type", "checkout", None)
    assert parse_ref("field:checkout/amount") == ("field", "amount", "checkout")
    for bad in ("purchase", "metric:x", "event:"):
        with pytest.raises(ValueError):
            parse_ref(bad)


async def test_links_resolve_against_the_main_plan(client: AsyncClient) -> None:
    await create_project(client, "linked")
    ids = await seed_plan(client, "linked")

    resp = await client.get(
        "/api/v1/projects/linked/docs/links",
        params=[
            ("ref", "event:purchase"),
            ("ref", "event-type:checkout"),
            ("ref", "field:checkout/amount"),
            ("ref", "field:amount"),
            ("ref", "field:other/amount"),
            ("ref", "event:gone"),
        ],
    )
    assert resp.status_code == 200, resp.text
    by_raw = {item["raw"]: item for item in resp.json()}

    assert by_raw["[[event:purchase]]"]["status"] == "resolved"
    assert by_raw["[[event:purchase]]"]["route_path"] == (
        f"/o/default/p/linked/monitoring/event/{ids['event_id']}"
    )
    assert by_raw["[[event-type:checkout]]"]["route_path"] == "/o/default/p/linked/events/checkout"
    assert by_raw["[[field:checkout/amount]]"]["route_path"] == (
        f"/o/default/p/linked/event-types/{ids['event_type_id']}"
    )
    assert by_raw["[[field:checkout/amount]]"]["entity_id"] == ids["field_id"]
    assert by_raw["[[field:amount]]"]["status"] == "resolved"
    assert by_raw["[[field:other/amount]]"]["status"] == "broken"
    assert by_raw["[[event:gone]]"] == {
        "kind": "event",
        "target": "gone",
        "qualifier": None,
        "raw": "[[event:gone]]",
        "status": "broken",
        "route_path": None,
        "entity_id": None,
        "candidates": 0,
    }


async def test_malformed_ref_is_422(client: AsyncClient) -> None:
    await create_project(client, "badref")
    resp = await client.get("/api/v1/projects/badref/docs/links", params={"ref": "metric:x"})
    assert resp.status_code == 422


async def test_write_reports_broken_links_and_read_resolves_them(client: AsyncClient) -> None:
    await create_project(client, "warned")
    await seed_plan(client, "warned")

    written = await put_doc(
        client,
        "warned",
        "guides/buying.md",
        "Uses [[event:purchase]] and [[event:refund]].\n",
    )

    assert written["warnings"] == [
        "Broken link [[event:refund]]: no event named 'refund' on the main plan"
    ]
    statuses = {link["target"]: link["status"] for link in written["links"]}
    assert statuses == {"purchase": "resolved", "refund": "broken"}


async def test_backlinks_list_project_and_organization_notes(client: AsyncClient) -> None:
    await create_project(client, "backed")
    await seed_plan(client, "backed")
    await put_doc(client, "backed", "a.md", "---\ntitle: Alpha\n---\n[[field:checkout/amount]]")
    await put_doc(client, "backed", "b.md", "bare [[field:amount]]", scope="organization")
    await put_doc(client, "backed", "c.md", "other [[field:other/amount]]")
    await put_doc(client, "backed", "d.md", "[[event:purchase]]")

    resp = await client.get(
        "/api/v1/projects/backed/docs/backlinks",
        params={"kind": "field", "name": "amount", "qualifier": "checkout"},
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [(item["scope"], item["path"], item["link_raw"]) for item in items] == [
        ("project", "a.md", "[[field:checkout/amount]]"),
        ("organization", "b.md", "[[field:amount]]"),
    ]
    assert items[0]["title"] == "Alpha"

    events = await client.get(
        "/api/v1/projects/backed/docs/backlinks", params={"kind": "event", "name": "purchase"}
    )
    assert [item["path"] for item in events.json()["items"]] == ["d.md"]
