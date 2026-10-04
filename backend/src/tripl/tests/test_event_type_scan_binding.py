"""Event types, the scan binding that guards them, and the lookup.

Every behaviour below shipped with no test at all.

``scan_configs.event_type_id`` is ``ON DELETE SET NULL``, so
deleting a bound event type never failed: it silently unbound every scan that
named it, and an unbound config with no ``event_type_column`` discovers nothing
from the data, so it went on listing and running and collecting zero events.
Both doors that can delete an event type now refuse first — the CRUD delete
(``event_type_service.delete_event_type``) and a branch merge that removed the
type (``plan_branch_merge_service._apply_merge``) — through the one predicate in
``scan_config_lookup.scan_configs_binding_event_types``. The 409 they share is
``event_type_binding_conflict_detail``, which has a singular and a plural
wording; both are exercised here.

That predicate is scoped to the project asking, like every other query in
``scan_config_lookup``. Its result is RENDERED — the 409 names each blocking
scan — so an unscoped answer would show one project's operator a scan belonging
to another project and block a delete they have no way to unblock: the remedy
the sentence offers is not reachable from the project they are in.

A scan resolves its types against MAIN's plan,
so a shadow candidate always carries a main event type id. Writing that id onto
a row authored on a working branch is what ``create_event`` now refuses
outright, which would have taken the whole branch-accept flow with it.
``reconciliation_service._event_type_on_branch`` translates the DETECTED id onto
the accept branch by name, and answers 422 when the branch has no such type.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import update

from tripl.models.data_source import DataSource
from tripl.models.scan_config import ScanConfig
from tripl.models.shadow_event_candidate import SHADOW_STATUS_NEW, ShadowEventCandidate
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_plan_branches import _approve_and_merge, _create_branch

# A first-seen stamp no test in this file writes at, so "observed long ago" can
# never be confused with something this suite created a second ago.
_LONG_AGO = datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)


# --- helpers ------------------------------------------------------------------


async def _project(client: AsyncClient, slug: str) -> uuid.UUID:
    resp = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def _event_type(client: AsyncClient, slug: str, name: str) -> str:
    resp = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": name, "display_name": name.title()},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _event_type_names(client: AsyncClient, slug: str, query: str = "") -> list[str]:
    """Which event types the project still has, SORTED.

    The list route orders by ``(order, created_at)`` and every type here is
    created with the default order 0, while ``created_at`` is a
    ``server_default`` — ``CURRENT_TIMESTAMP`` on sqlite, one-second resolution.
    Two types created in the same test can tie, and what these tests assert is
    membership, never sequence.
    """
    resp = await client.get(f"/api/v1/projects/{slug}/event-types{query}")
    assert resp.status_code == 200, resp.text
    return sorted(et["name"] for et in resp.json())


async def _branch_event_type(client: AsyncClient, slug: str, branch_id: str, name: str) -> str:
    """The branch's OWN copy of an event type — a deep copy carries a new id."""
    resp = await client.get(f"/api/v1/projects/{slug}/event-types?branch={branch_id}")
    assert resp.status_code == 200, resp.text
    return str(next(et["id"] for et in resp.json() if et["name"] == name))


async def _scan_config(
    project_id: uuid.UUID,
    *,
    name: str,
    event_type_id: str | None = None,
) -> uuid.UUID:
    """A scan config written straight to the row, with its own data source.

    ``uq_scan_config_ds_name`` is ``(data_source_id, name)``, so a fresh source
    per config keeps the names in this file free to be whatever reads best in
    the 409 body being asserted.
    """
    async with TestSessionLocal() as session:
        data_source = DataSource(
            name=f"wh-{uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=9000,
            database_name="db",
            username="u",
            password_encrypted="",
        )
        session.add(data_source)
        await session.flush()
        config = ScanConfig(
            project_id=project_id,
            data_source_id=data_source.id,
            event_type_id=uuid.UUID(event_type_id) if event_type_id else None,
            name=name,
            base_query="SELECT * FROM events",
            time_column="time",
        )
        session.add(config)
        await session.commit()
        return config.id


async def _rebind(config_id: uuid.UUID, event_type_id: str) -> None:
    """Point a scan at another event type — the first remedy the 409 offers.

    Written to the row rather than through ``PATCH /scans/{id}`` because what
    the guard reads is the column, and the remedy is not what is under test.
    """
    async with TestSessionLocal() as session:
        await session.execute(
            update(ScanConfig)
            .where(ScanConfig.id == config_id)
            .values(event_type_id=uuid.UUID(event_type_id))
        )
        await session.commit()


async def _candidate(
    project_id: uuid.UUID,
    config_id: uuid.UUID,
    *,
    event_type_id: str,
    event_name: str,
) -> uuid.UUID:
    """A shadow candidate as the collector writes one: a MAIN event type id.

    A scan resolves its types against main's plan and ``ShadowEventCandidate``
    has no branch column, so this is the only shape the accept route ever sees.
    """
    async with TestSessionLocal() as session:
        candidate = ShadowEventCandidate(
            project_id=project_id,
            scan_config_id=config_id,
            event_type_id=uuid.UUID(event_type_id),
            event_name=event_name,
            observed_count=42,
            first_seen_at=_LONG_AGO,
            last_seen_at=datetime.now(UTC),
            status=SHADOW_STATUS_NEW,
        )
        session.add(candidate)
        await session.commit()
        return candidate.id


# --- the CRUD delete door -------------------------------------


@pytest.mark.asyncio
async def test_deleting_an_event_type_one_scan_binds_is_refused(client: AsyncClient) -> None:
    """The singular wording of the shared 409, and the type surviving it.

    RED on a revert: drop the ``scan_configs_binding_event_types`` guard from
    ``delete_event_type`` and the FK's ``ON DELETE SET NULL`` lets the request
    answer 204 — so ``== 409`` fails, the detail assertions have no body to run
    against, and ``checkout`` is gone from the list below. Revert only the
    singular arms of ``event_type_binding_conflict_detail`` (render the plural
    wording unconditionally) and the request is still 409 but reads "2 scans" /
    "those scans", so the three wording assertions fail on their own.
    """
    slug = "b7r-delete-bound"
    project_id = await _project(client, slug)
    checkout = await _event_type(client, slug, "checkout")
    signup = await _event_type(client, slug, "signup")
    billing = await _event_type(client, slug, "billing")
    config_id = await _scan_config(project_id, name="Checkout scan", event_type_id=checkout)

    refused = await client.delete(f"/api/v1/projects/{slug}/event-types/{checkout}")

    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert "This event type cannot be deleted." in detail, detail
    assert "collected into by 1 scan: 'Checkout scan'" in detail, detail
    assert "that scan would collect into nothing" in detail, detail
    assert "Point the scan at another event type" in detail, detail
    assert "then delete the event type" in detail, detail
    assert await _event_type_names(client, slug) == ["billing", "checkout", "signup"]

    # Not vacuous, twice over: the guard is about THIS type's binding, not "the
    # project has a scan" — an unbound type deletes untouched — and the bound
    # one deletes as soon as the scan is pointed at another event type, which is
    # the first remedy the 409 names.
    unbound = await client.delete(f"/api/v1/projects/{slug}/event-types/{signup}")
    assert unbound.status_code == 204, unbound.text
    await _rebind(config_id, billing)
    cleared = await client.delete(f"/api/v1/projects/{slug}/event-types/{checkout}")
    assert cleared.status_code == 204, cleared.text
    assert await _event_type_names(client, slug) == ["billing"]


@pytest.mark.asyncio
async def test_the_refusal_names_every_scan_and_agrees_with_its_own_count(
    client: AsyncClient,
) -> None:
    """The plural wording, which no other test produced.

    RED on a revert: make ``event_type_binding_conflict_detail`` render the
    singular arms unconditionally and the body reads "1 scan", "that scan would
    collect into nothing" and "Point the scan at another event type" while
    naming two of them — every assertion below fails, and the sentence goes back
    to instructing the reader about one scan after listing two.
    Drop the guard entirely and the request is 204.
    """
    slug = "b7r-delete-bound-plural"
    project_id = await _project(client, slug)
    checkout = await _event_type(client, slug, "checkout")
    # Ordered by name in the query, so the body's list is stable to assert on.
    await _scan_config(project_id, name="Android checkout", event_type_id=checkout)
    await _scan_config(project_id, name="Web checkout", event_type_id=checkout)

    refused = await client.delete(f"/api/v1/projects/{slug}/event-types/{checkout}")

    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert "collected into by 2 scans: 'Android checkout'; 'Web checkout'" in detail, detail
    assert "those scans would collect into nothing" in detail, detail
    assert "Point those scans at another event type" in detail, detail
    assert "give them an Event type column" in detail, detail
    assert await _event_type_names(client, slug) == ["checkout"]


@pytest.mark.asyncio
async def test_another_projects_scan_neither_blocks_the_delete_nor_names_itself(
    client: AsyncClient,
) -> None:
    """The project filter on ``scan_configs_binding_event_types``.

    ``scan_configs.event_type_id`` is a plain single-column FK and
    ``scan_service`` validates only ``data_source_id`` against the project, so
    nothing stops a row in one project from binding another project's event
    type — by a pasted uuid or by a project-scoped key whose fence only checks
    the path's slug. Unscoped, that row blocks this delete forever and NAMES
    ITSELF in the body, pointing the reader at a scan they cannot see or edit
    and at a remedy they cannot perform.

    RED on a revert: drop ``ScanConfig.project_id == project_id`` from the
    helper's ``where`` and the foreign row still matches ``event_type_id IN
    (...)``, so this delete answers 409 with "'Foreign scan'" in the detail —
    both assertions below fail.
    """
    slug = "b7r-scope-owner"
    other_slug = "b7r-scope-foreign"
    await _project(client, slug)
    other_project_id = await _project(client, other_slug)
    checkout = await _event_type(client, slug, "checkout")
    # The invalid row: another project's scan, bound to this project's type.
    await _scan_config(other_project_id, name="Foreign scan", event_type_id=checkout)

    deleted = await client.delete(f"/api/v1/projects/{slug}/event-types/{checkout}")

    assert deleted.status_code == 204, deleted.text
    assert await _event_type_names(client, slug) == []


# --- the merge door -------------------------------------------


@pytest.mark.asyncio
async def test_merging_a_branch_that_removed_a_bound_event_type_is_refused(
    client: AsyncClient,
) -> None:
    """The second door onto the same FK, with the same predicate and 409 body.

    A merge applies the branch's deletions to main, so a branch that dropped its
    copy of ``checkout`` empties every scan bound to main's copy — the identical
    outcome the CRUD delete refuses, reached without anyone pressing Delete on
    an event type.

    RED on a revert: remove the ``removed_main_ets`` binding guard from
    ``_apply_merge`` and the merge answers 200, deletes main's ``checkout`` and
    leaves 'Checkout scan' bound to nothing — so ``== 409`` fails and so does
    the assertion that main still holds the type.
    """
    slug = "b7r-merge-bound"
    project_id = await _project(client, slug)
    checkout = await _event_type(client, slug, "checkout")
    signup = await _event_type(client, slug, "signup")
    config_id = await _scan_config(project_id, name="Checkout scan", event_type_id=checkout)

    branch_id = await _create_branch(client, slug)
    branch_checkout = await _branch_event_type(client, slug, branch_id, "checkout")
    # The branch removes it. Its own copy is bound by nothing, so this delete is
    # not the one under test and has to succeed.
    dropped = await client.delete(
        f"/api/v1/projects/{slug}/event-types/{branch_checkout}?branch={branch_id}"
    )
    assert dropped.status_code == 204, dropped.text

    refused = await _approve_and_merge(client, slug, branch_id)

    assert refused.status_code == 409, refused.text
    detail = str(refused.json()["detail"])
    assert "Cannot merge this branch: merging deletes 'checkout' from main." in detail, detail
    assert "collected into by 1 scan: 'Checkout scan'" in detail, detail
    assert "then merge the branch" in detail, detail
    assert await _event_type_names(client, slug) == ["checkout", "signup"]

    # Not vacuous: the same approved branch merges once the scan is pointed at
    # another event type, and main loses 'checkout' exactly as the branch asked.
    await _rebind(config_id, signup)
    merged = await client.post(f"/api/v1/projects/{slug}/branches/{branch_id}/merge")
    assert merged.status_code == 200, merged.text
    assert await _event_type_names(client, slug) == ["signup"]


# --- accepting a candidate on a branch --------


@pytest.mark.asyncio
async def test_accepting_a_candidate_on_a_branch_uses_the_branchs_own_event_type(
    client: AsyncClient,
) -> None:
    """The detected type id is MAIN's; the row has to carry the branch's.

    ``uq_event_scan_identity`` is ``(event_type_id, source_name)`` and a type
    lives on exactly one branch, so a branch row holding main's type id holds
    MAIN's scan identity. ``create_event`` refuses that outright now,
    which is what would have taken the branch accept with it.

    RED on a revert: drop the ``_event_type_on_branch`` call from
    ``accept_shadow_event`` and the candidate's main id goes straight into
    ``EventCreate``, so the accept answers 422 "not on this project's branch" —
    ``== 200`` fails, and so does the event-type assertion below it.
    """
    slug = "b7r-branch-accept"
    project_id = await _project(client, slug)
    main_pv = await _event_type(client, slug, "pv")
    config_id = await _scan_config(project_id, name="Pageview scan", event_type_id=main_pv)
    branch_id = await _create_branch(client, slug)
    branch_pv = await _branch_event_type(client, slug, branch_id, "pv")
    assert branch_pv != main_pv, "the deep copy must carry its own id"

    candidate_id = await _candidate(
        project_id, config_id, event_type_id=main_pv, event_name="screen | checkout"
    )

    accepted = await client.post(
        f"/api/v1/projects/{slug}/reconciliation/shadow-events/{candidate_id}/accept"
        f"?branch={branch_id}",
        json={"name": "Checkout Screen"},
    )

    assert accepted.status_code == 200, accepted.text
    listed = await client.get(f"/api/v1/projects/{slug}/events?branch={branch_id}")
    assert listed.status_code == 200, listed.text
    rows = listed.json()["items"]
    assert [row["name"] for row in rows] == ["Checkout Screen"]
    # The substance: the branch's own copy, not the id the scan detected.
    assert rows[0]["event_type_id"] == branch_pv
    assert rows[0]["source_name"] == "screen | checkout"
    # And the accept landed on the branch alone — main's plan is untouched.
    main_rows = await client.get(f"/api/v1/projects/{slug}/events")
    assert main_rows.json()["items"] == []


@pytest.mark.asyncio
async def test_accepting_onto_a_branch_that_deleted_the_event_type_says_so(
    client: AsyncClient,
) -> None:
    """422 rather than a silent fallback to the id the scan detected.

    The branch was deep-copied from main, so a missing counterpart means the
    branch DELETED that event type — and accepting a candidate onto a type the
    branch says is gone is not a thing the operator asked for.

    RED on a revert: drop the ``_event_type_on_branch`` call and this accept is
    still refused, but by ``create_event``'s branch guard reading "not on this
    project's branch". The status code alone therefore proves nothing here,
    which is why the assertion is on the DETAIL: only the translation step can
    say "does not exist on this branch" and tell the reader the two ways out.
    """
    slug = "b7r-branch-accept-gone"
    project_id = await _project(client, slug)
    main_pv = await _event_type(client, slug, "pv")
    config_id = await _scan_config(project_id, name="Pageview scan", event_type_id=main_pv)
    branch_id = await _create_branch(client, slug)
    branch_pv = await _branch_event_type(client, slug, branch_id, "pv")
    dropped = await client.delete(
        f"/api/v1/projects/{slug}/event-types/{branch_pv}?branch={branch_id}"
    )
    assert dropped.status_code == 204, dropped.text

    candidate_id = await _candidate(
        project_id, config_id, event_type_id=main_pv, event_name="screen | checkout"
    )
    accept_url = f"/api/v1/projects/{slug}/reconciliation/shadow-events/{candidate_id}/accept"

    refused = await client.post(f"{accept_url}?branch={branch_id}", json={"name": "Checkout"})

    assert refused.status_code == 422, refused.text
    detail = refused.json()["detail"]
    assert "Event type 'pv' does not exist on this branch." in detail, detail
    assert "accept it on main" in detail, detail

    # Not vacuous: the very same candidate accepts on main, where the type the
    # scan detected is the type that exists.
    accepted = await client.post(accept_url, json={"name": "Checkout"})
    assert accepted.status_code == 200, accepted.text
    listed = await client.get(f"/api/v1/projects/{slug}/events")
    assert [row["event_type_id"] for row in listed.json()["items"]] == [main_pv]
