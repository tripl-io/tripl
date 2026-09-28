"""Links to notes, people and the project's other entities (F24 part 2, GH #308).

Parsing of every kind, resolution and routes, rename -> broken with relink
suggestions, note links that survive a move, and note links that never reveal a
note the reader cannot see (neither in the link nor in "Linked from").
"""

import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy import update

from tripl.models.event import Event
from tripl.models.metric_definition import MetricDefinition
from tripl.models.variable import Variable
from tripl.services._docs_link_similar import (
    MAX_SUGGESTED_REFS,
    NamePool,
    SuggestionBudget,
    closest,
    levenshtein,
    similarity,
)
from tripl.services.docs_links import (
    extract_links,
    parse_ref,
    raw_link,
    rewrite_links_as_text,
)
from tripl.tests._docs_helpers import create_project, get_doc, put_doc, seed_plan
from tripl.tests._docs_sharing_helpers import (
    BASE,
    Crew,
    crew,  # noqa: F401 - the fixture
    put,
    read,
    share,
    user_id,
)
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_alembic_revisions import _load_migration

NOTE_ID = "3f2a9c1e-5b7d-4e8f-9a0b-1c2d3e4f5a6b"
RULE_ID = "7a8b9c0d-1e2f-4a3b-8c4d-5e6f7a8b9c0d"
USER_ID = "0f1e2d3c-4b5a-4968-8776-655443322110"


# ── Parsing ───────────────────────────────────────────────────────────────────


def test_every_kind_is_parsed_and_code_is_ignored() -> None:
    body = (
        f"[[doc:{NOTE_ID.upper()}#setup-steps|the setup]] and [[doc:guides/intro.md]]\n"
        "[[variable:plan_tier]] [[metric:weekly_revenue|Revenue]]\n"
        f"[[alert-rule:{RULE_ID}]] [[branch:feature-onboarding]] [[scan:nightly scan]]\n"
        f"[[data-source:warehouse]] ping [[user:{USER_ID}]]\n"
        "`[[metric:in_code]]`\n"
        "```\n[[user:not-a-mention]]\n```\n"
        "[[widget:unknown]] stays text.\n"
    )
    links = extract_links(body)

    assert [(link.kind, link.target, link.qualifier, link.label) for link in links] == [
        ("doc", NOTE_ID, "setup-steps", "the setup"),
        ("doc", "guides/intro.md", None, None),
        ("variable", "plan_tier", None, None),
        ("metric", "weekly_revenue", None, "Revenue"),
        ("alert_rule", RULE_ID, None, None),
        ("branch", "feature-onboarding", None, None),
        ("scan", "nightly scan", None, None),
        ("data_source", "warehouse", None, None),
        ("user", USER_ID, None, None),
    ]
    # An id is stored in its canonical (lower-case) form.
    assert links[0].target == NOTE_ID.lower()


def test_canonical_text_and_the_index_body() -> None:
    assert raw_link("doc", NOTE_ID, "setup") == f"[[doc:{NOTE_ID}#setup]]"
    assert raw_link("alert_rule", RULE_ID, None) == f"[[alert-rule:{RULE_ID}]]"
    assert raw_link("data_source", "warehouse", None) == "[[data-source:warehouse]]"
    # Ids never reach the search index; a label does, and names do.
    assert (
        rewrite_links_as_text(
            f"see [[doc:{NOTE_ID}|the guide]] ask [[user:{USER_ID}]] on [[metric:weekly_revenue]]"
        )
        == "see the guide ask  on weekly_revenue"
    )


def test_parse_ref_takes_every_kind() -> None:
    assert parse_ref(f"doc:{NOTE_ID}#setup") == ("doc", NOTE_ID, "setup")
    assert parse_ref("data-source:warehouse") == ("data_source", "warehouse", None)
    assert parse_ref(f"user:{USER_ID.upper()}") == ("user", USER_ID, None)


def test_relink_suggestions_rank_close_names() -> None:
    assert set(closest("signup", ["sign_up", "purchase", "signup_started", "logout"])) == {
        "sign_up",
        "signup_started",
    }
    assert closest("refund", ["purchase", "checkout"]) == []
    assert len(closest("event", [f"event_{i}" for i in range(10)])) == 3
    assert levenshtein("kitten", "sitting") == 3
    assert similarity("abc", "abc") == 1.0


def test_a_name_pool_is_indexed_once_and_matches_closest() -> None:
    names = ["sign_up", "purchase", "signup_started", "logout", "", "sign_up"]
    pool = NamePool(names)
    assert len(pool) == 4
    for wanted in ("signup", "purchased", "log_out", "refund", ""):
        assert pool.closest(wanted) == closest(wanted, names), wanted
    # The stored name itself is never offered back.
    assert "purchase" not in pool.closest("purchase")


def test_a_large_pool_only_ranks_names_that_share_a_trigram() -> None:
    pool = NamePool(f"event_{index:05d}" for index in range(20_000))
    assert pool.closest("zzz_unrelated") == []
    assert len(pool.closest("event_00042")) == 3


def test_the_suggestion_budget_runs_out() -> None:
    budget = SuggestionBudget(2)
    assert [budget.take() for _ in range(4)] == [True, True, False, False]


def test_the_migration_follows_note_sharing() -> None:
    migration = _load_migration("doc_link_kinds", "d5f7a9b1c3e6_doc_link_kinds.py")
    assert migration.down_revision == "c4e6a8b0d2f5"


# ── Resolution ────────────────────────────────────────────────────────────────


async def _ok(resp: Any, *codes: int) -> Any:
    assert resp.status_code in (codes or (200, 201)), resp.text
    return resp.json() if resp.content else None


async def seed_entities(client: AsyncClient, slug: str) -> dict[str, str]:
    """Plan, data source, scan, metric, variable, branch and alert rule of ``slug``."""
    base = f"/api/v1/projects/{slug}"
    ids = await seed_plan(client, slug)
    source = await _ok(
        await client.post(
            "/api/v1/data-sources",
            json={
                "name": f"{slug}-warehouse",
                "db_type": "clickhouse",
                "host": "localhost",
                "port": 1,
                "database_name": "analytics",
                "username": "default",
                "password": "synthetic-secret",
            },
        )
    )
    scan = await _ok(
        await client.post(
            f"{base}/scans",
            json={"data_source_id": source["id"], "name": "nightly scan", "base_query": "SELECT 1"},
        )
    )
    metric = await _ok(
        await client.post(
            f"{base}/metrics",
            json={
                "kind": "sql",
                "name": "weekly_revenue",
                "display_name": "Weekly revenue",
                "data_source_id": source["id"],
                "interval": "1d",
                "config": {"metric_sql": "SELECT 1 AS value, now() AS t", "time_column": "t"},
            },
        )
    )
    variable = await _ok(await client.post(f"{base}/variables", json={"name": "plan_tier"}))
    branch = await _ok(await client.post(f"{base}/branches", json={"name": "feature-onboarding"}))
    destination = await _ok(
        await client.post(
            f"{base}/alert-destinations",
            json={
                "type": "slack",
                "name": "Growth alerts",
                "webhook_url": f"https://hooks.slack.com/services/T1/B1/{uuid.uuid4().hex[:8]}",
            },
        )
    )
    rule = await _ok(
        await client.post(
            f"{base}/alert-destinations/{destination['id']}/rules", json={"name": "Checkout drop"}
        )
    )
    return {
        **ids,
        "data_source_id": source["id"],
        "data_source": source["name"],
        "scan_id": scan["id"],
        "metric_id": metric["id"],
        "variable_id": variable["id"],
        "branch_id": branch["id"],
        "rule_id": rule["id"],
    }


async def test_every_kind_resolves_to_its_page(client: AsyncClient) -> None:
    await create_project(client, "atlas")
    ids = await seed_entities(client, "atlas")
    target = await put_doc(client, "atlas", "guides/setup.md", "# Setup guide\n\n## Steps\n")
    owner_id = await user_id("test@example.com")
    note = await put_doc(
        client,
        "atlas",
        "index.md",
        (
            f"[[doc:{target['id']}#steps]] [[variable:plan_tier]] [[metric:weekly_revenue]]\n"
            f"[[alert-rule:{ids['rule_id']}]] [[branch:feature-onboarding]] "
            f"[[scan:nightly scan]] [[data-source:{ids['data_source']}]] [[user:{owner_id}]]\n"
        ),
    )
    assert note["warnings"] == []
    by_kind = {link["kind"]: link for link in note["links"]}
    project = "/o/default/p/atlas"
    expected = {
        "doc": (f"{project}/docs/project/guides/setup.md#steps", "Setup guide", target["id"]),
        "variable": (f"{project}/variables/{ids['variable_id']}", "plan_tier", ids["variable_id"]),
        "metric": (
            f"{project}/monitoring/metric/{ids['metric_id']}",
            "Weekly revenue",
            ids["metric_id"],
        ),
        "alert_rule": (f"{project}/monitors/{ids['rule_id']}", "Checkout drop", ids["rule_id"]),
        "branch": (
            f"{project}/branches/{ids['branch_id']}",
            "feature-onboarding",
            ids["branch_id"],
        ),
        "scan": (f"{project}/scans/{ids['scan_id']}", "nightly scan", ids["scan_id"]),
        "data_source": (
            f"/settings/data-sources/{ids['data_source_id']}",
            ids["data_source"],
            ids["data_source_id"],
        ),
        "user": (None, None, str(owner_id)),
    }
    for kind, (route, label, entity_id) in expected.items():
        link = by_kind[kind]
        assert link["status"] == "resolved", (kind, link)
        assert link["route_path"] == route, kind
        assert link["entity_id"] == entity_id, kind
        if label is not None:
            assert link["label"] == label, kind
    assert by_kind["user"]["label"].startswith("@")
    assert by_kind["doc"]["detail"] == "Project notes · guides/setup.md"

    # The editor's live preview resolves the same references.
    preview = await client.get(
        "/api/v1/projects/atlas/docs/links",
        params=[("ref", "metric:weekly_revenue"), ("ref", f"alert-rule:{ids['rule_id']}")],
    )
    assert [item["status"] for item in preview.json()] == ["resolved", "resolved"]


async def test_links_by_id_that_do_not_resolve(client: AsyncClient) -> None:
    await create_project(client, "loose")
    written = await put_doc(
        client,
        "loose",
        "index.md",
        f"[[doc:{NOTE_ID}]] [[alert-rule:not-an-id]] [[user:{USER_ID}]] [[doc:missing/page.md]]",
    )
    by_kind = {(link["kind"], link["target"]): link for link in written["links"]}
    # A missing note reads like a hidden one: unavailable, with no reason.
    assert (by_kind[("doc", NOTE_ID)]["status"], by_kind[("doc", NOTE_ID)]["reason"]) == (
        "unavailable",
        None,
    )
    assert by_kind[("alert_rule", "not-an-id")]["reason"] == "invalid_id"
    assert by_kind[("user", USER_ID)]["reason"] == "not_a_member"
    path_form = by_kind[("doc", "missing/page.md")]
    assert (path_form["status"], path_form["reason"], path_form["suggestions"]) == (
        "broken",
        "path_form",
        [],
    )
    assert len(written["warnings"]) == 4
    assert (
        f"Link [[doc:{NOTE_ID}]] points at a note that is unavailable "
        "(deleted, or not visible to you)"
    ) in written["warnings"]


async def test_only_the_first_broken_links_get_suggestions(client: AsyncClient) -> None:
    await create_project(client, "many")
    await seed_plan(client, "many")
    count = MAX_SUGGESTED_REFS + 5
    body = " ".join(f"[[event:purchase_{index:02d}]]" for index in range(count))
    note = await put_doc(client, "many", "index.md", body)
    links = note["links"]
    assert [link["status"] for link in links] == ["broken"] * count
    assert all(link["suggestions"] == ["purchase"] for link in links[:MAX_SUGGESTED_REFS])
    assert all(link["suggestions"] == [] for link in links[MAX_SUGGESTED_REFS:])


async def test_a_rename_breaks_a_name_link_and_suggests_the_new_name(client: AsyncClient) -> None:
    await create_project(client, "renamed")
    ids = await seed_entities(client, "renamed")
    await put_doc(
        client,
        "renamed",
        "index.md",
        "[[event:purchase]] [[metric:weekly_revenue]] [[variable:plan_tier]]",
    )
    async with TestSessionLocal() as session:
        await session.execute(
            update(Event).where(Event.id == uuid.UUID(ids["event_id"])).values(name="purchases")
        )
        await session.execute(
            update(MetricDefinition)
            .where(MetricDefinition.id == uuid.UUID(ids["metric_id"]))
            .values(name="weekly_revenue_v2")
        )
        await session.execute(
            update(Variable)
            .where(Variable.id == uuid.UUID(ids["variable_id"]))
            .values(name="plan_tiers")
        )
        await session.commit()

    note = await get_doc(client, "renamed", "index.md")
    by_kind = {link["kind"]: link for link in note["links"]}
    assert by_kind["event"]["status"] == "broken"
    assert by_kind["event"]["suggestions"] == ["purchases"]
    assert by_kind["metric"]["suggestions"] == ["weekly_revenue_v2"]
    assert by_kind["variable"]["suggestions"] == ["plan_tiers"]
    assert by_kind["metric"]["route_path"] is None

    # The save names the fix too.
    again = await put_doc(
        client, "renamed", "index.md", "[[event:purchase]] and more", base_revision=1
    )
    assert again["warnings"] == [
        "Broken link [[event:purchase]]: no event named 'purchase' on the main plan; "
        "did you mean 'purchases'?"
    ]


async def test_a_note_link_survives_a_move_and_a_new_title(client: AsyncClient) -> None:
    await create_project(client, "moving")
    target = await put_doc(client, "moving", "drafts/plan.md", "# Draft plan\n")
    await put_doc(client, "moving", "index.md", f"See [[doc:{target['id']}#goals|the plan]].")

    moved = await client.post(
        "/api/v1/projects/moving/docs/move",
        json={"scope": "project", "from_path": "drafts/plan.md", "to_path": "plans/q3.md"},
    )
    assert moved.status_code == 200, moved.text
    await put_doc(client, "moving", "plans/q3.md", "# Q3 plan\n", base_revision=2)

    link = (await get_doc(client, "moving", "index.md"))["links"][0]
    assert link["status"] == "resolved"
    assert link["label"] == "Q3 plan"
    assert link["route_path"] == "/o/default/p/moving/docs/project/plans/q3.md#goals"

    page = await get_doc(client, "moving", "plans/q3.md")
    assert [item["path"] for item in page["linked_from"]] == ["index.md"]
    linked_from = await client.get(
        "/api/v1/projects/moving/docs/backlinks", params={"kind": "doc", "name": target["id"]}
    )
    assert linked_from.status_code == 200, linked_from.text
    assert [(item["path"], item["link_raw"]) for item in linked_from.json()["items"]] == [
        ("index.md", f"[[doc:{target['id']}#goals]]")
    ]


async def test_a_typed_path_is_saved_as_a_link_by_id(client: AsyncClient) -> None:
    await create_project(client, "typed")
    target = await put_doc(client, "typed", "guides/intro.md", "# Intro\n")
    org_note = await put_doc(client, "typed", "shared/style.md", "# Style\n", scope="organization")

    written = await put_doc(
        client,
        "typed",
        "index.md",
        "---\ntitle: Index\n---\n"
        "[[doc:guides/intro.md#start|read this]] and [[doc:shared/style]]\n"
        "`[[doc:guides/intro.md]]` stays as typed, [[doc:nowhere.md]] too.\n",
    )
    assert written["content"] == (
        "---\ntitle: Index\n---\n"
        f"[[doc:{target['id']}#start|read this]] and [[doc:{org_note['id']}]]\n"
        "`[[doc:guides/intro.md]]` stays as typed, [[doc:nowhere.md]] too.\n"
    )
    statuses = {link["target"]: link["status"] for link in written["links"]}
    assert statuses == {
        target["id"]: "resolved",
        org_note["id"]: "resolved",
        "nowhere.md": "broken",
    }


async def test_backlinks_reach_every_kind(client: AsyncClient) -> None:
    await create_project(client, "backs")
    ids = await seed_entities(client, "backs")
    owner_id = await user_id("test@example.com")
    await put_doc(
        client,
        "backs",
        "a.md",
        f"[[metric:weekly_revenue]] [[variable:plan_tier]] [[alert-rule:{ids['rule_id']}]] "
        f"[[user:{owner_id}]] [[branch:feature-onboarding]] [[scan:nightly scan]] "
        f"[[data-source:{ids['data_source']}]]",
    )
    queries = {
        "metric": "weekly_revenue",
        "variable": "plan_tier",
        "alert_rule": ids["rule_id"].upper(),
        "user": str(owner_id),
        "branch": "feature-onboarding",
        "scan": "nightly scan",
        "data_source": ids["data_source"],
    }
    for kind, name in queries.items():
        resp = await client.get(
            "/api/v1/projects/backs/docs/backlinks", params={"kind": kind, "name": name}
        )
        assert resp.status_code == 200, resp.text
        assert [item["path"] for item in resp.json()["items"]] == ["a.md"], kind


# ── Visibility ────────────────────────────────────────────────────────────────


async def test_a_link_to_a_hidden_note_names_nothing(crew: Crew) -> None:  # noqa: F811
    secret = await put(crew.alice, "drafts/secret.md", "# Launch codename\n")
    await share(crew.alice, "drafts/secret.md", "private")
    public = await put(crew.alice, "public.md", "# Public page\n")
    await put(crew.alice, "index.md", f"[[doc:{secret['id']}]] and [[doc:{public['id']}]]")
    # A private note of Alice's links to the public page too.
    await put(crew.alice, "drafts/diary.md", f"[[doc:{public['id']}]]")
    await share(crew.alice, "drafts/diary.md", "private")

    mine = {link["target"]: link for link in (await read(crew.alice, "index.md"))["links"]}
    assert mine[secret["id"]]["status"] == "resolved"
    assert mine[secret["id"]]["label"] == "Launch codename"

    theirs = await read(crew.bob, "index.md")
    hidden = next(link for link in theirs["links"] if link["target"] == secret["id"])
    assert hidden == {
        "kind": "doc",
        "target": secret["id"],
        "qualifier": None,
        "raw": f"[[doc:{secret['id']}]]",
        "status": "unavailable",
        "route_path": None,
        "entity_id": None,
        "candidates": 0,
        "label": None,
        "detail": None,
        "reason": None,
        "suggestions": [],
    }
    # A note that does not exist answers the same, so the API is no existence oracle.
    missing_id = str(uuid.uuid4())
    probe_links = await crew.bob.get(
        f"{BASE}/links", params=[("ref", f"doc:{secret['id']}"), ("ref", f"doc:{missing_id}")]
    )
    assert probe_links.status_code == 200, probe_links.text
    hidden_probe, missing_probe = probe_links.json()
    strip = ("target", "raw")
    assert {k: v for k, v in hidden_probe.items() if k not in strip} == {
        k: v for k, v in missing_probe.items() if k not in strip
    }
    assert "Launch codename" not in str(theirs["links"])
    assert "drafts/secret.md" not in str(theirs["links"])

    # "Linked from": only notes the reader can read.
    params = {"kind": "doc", "name": public["id"]}
    alice_from = await crew.alice.get(f"{BASE}/backlinks", params=params)
    bob_from = await crew.bob.get(f"{BASE}/backlinks", params=params)
    assert [item["path"] for item in alice_from.json()["items"]] == ["drafts/diary.md", "index.md"]
    assert [item["path"] for item in bob_from.json()["items"]] == ["index.md"]
    assert [item["path"] for item in (await read(crew.bob, "public.md"))["linked_from"]] == [
        "index.md"
    ]
    assert [item["path"] for item in (await read(crew.alice, "public.md"))["linked_from"]] == [
        "drafts/diary.md",
        "index.md",
    ]

    # Nor does a hidden note's own id say who links to it.
    probe = await crew.bob.get(f"{BASE}/backlinks", params={"kind": "doc", "name": secret["id"]})
    assert probe.status_code == 200
    assert probe.json()["items"] == []


async def test_a_typed_path_to_a_hidden_note_stays_broken(crew: Crew) -> None:  # noqa: F811
    await put(crew.alice, "drafts/secret.md", "# Secret\n")
    await share(crew.alice, "drafts/secret.md", "private")

    written = await put(crew.bob, "notes.md", "[[doc:drafts/secret.md]]")
    assert written["content"] == "[[doc:drafts/secret.md]]"
    link = written["links"][0]
    assert (link["status"], link["reason"], link["suggestions"]) == ("broken", "path_form", [])
    assert link["label"] is None

    # Alice can read the note at that path: she is offered it, by its title.
    theirs = (await read(crew.alice, "notes.md"))["links"][0]
    secret = await read(crew.alice, "drafts/secret.md")
    assert (theirs["status"], theirs["reason"], theirs["suggestions"], theirs["label"]) == (
        "broken",
        "path_form",
        [secret["id"]],
        "Secret",
    )
