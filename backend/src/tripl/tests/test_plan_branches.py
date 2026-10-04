import uuid

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import event as sa_event
from sqlalchemy import select

from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_meta_value import EventMetaValue
from tripl.models.event_photo import EventPhoto
from tripl.models.event_photo_comment import EventPhotoComment
from tripl.models.event_tag import EventTag
from tripl.models.event_type import EventType
from tripl.models.event_type_owner import EventTypeOwner
from tripl.models.event_type_relation import EventTypeRelation
from tripl.models.field_definition import FieldDefinition
from tripl.models.meta_field_definition import MetaFieldDefinition
from tripl.models.plan_branch import PlanBranch
from tripl.models.plan_branch_approval import PlanBranchApproval
from tripl.models.plan_branch_reviewer import PlanBranchReviewer
from tripl.models.plan_revision import PlanRevision
from tripl.models.project import Project
from tripl.models.project_branch_settings import ProjectBranchSettings
from tripl.models.project_tracker_config import ProjectTrackerConfig
from tripl.models.user import User
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import VariableEventValueOverride
from tripl.models.variable_value import VariableValue
from tripl.schemas.plan_branch import PlanBranchCreate
from tripl.services._plan_branch_renames import (
    pair_renames,
    rekey_in_place,
    snapshot_rename_pairs,
)
from tripl.services.plan_branch_merge_service import _RENAME_STAGING_PREFIX
from tripl.services.plan_revision_service import (
    build_plan_snapshot,
    plan_snapshot_hash,
)
from tripl.tests._members import add_member_by_slug
from tripl.tests.conftest import TestSessionLocal, engine
from tripl.worker.tasks import implementation_tickets as impl_tasks


async def _create_branch(client: AsyncClient, slug: str, name: str = "feature") -> str:
    resp = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": name})
    assert resp.status_code == 201
    return resp.json()["id"]


async def _transition(client: AsyncClient, slug: str, branch_id: str, action: str) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{slug}/branches/{branch_id}/transition",
        json={"action": action},
    )
    return resp.json() if resp.status_code == 200 else {"_status": resp.status_code}


async def _seed_plan(client: AsyncClient, slug: str) -> str:
    """Create a project with one event type (+field) and one event on main."""
    project = await client.post(
        "/api/v1/projects",
        json={"name": slug, "slug": slug, "description": ""},
    )
    assert project.status_code == 201
    et = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "track", "display_name": "Track"},
    )
    assert et.status_code == 201
    et_id = et.json()["id"]
    field = await client.post(
        f"/api/v1/projects/{slug}/event-types/{et_id}/fields",
        json={"name": "name", "display_name": "Name", "field_type": "string"},
    )
    assert field.status_code == 201
    event = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": et_id, "name": "purchase:success"},
    )
    assert event.status_code == 201
    return et_id


@pytest.mark.asyncio
async def test_main_branch_is_auto_created(client: AsyncClient) -> None:
    await _seed_plan(client, "branch-main")
    resp = await client.get("/api/v1/projects/branch-main/branches")
    assert resp.status_code == 200
    body = resp.json()
    mains = [b for b in body["items"] if b["kind"] == "main"]
    assert len(mains) == 1
    assert mains[0]["name"] == "main"


@pytest.mark.asyncio
async def test_create_branch_deep_copies_and_isolates(client: AsyncClient) -> None:
    await _seed_plan(client, "branch-copy")

    created = await client.post(
        "/api/v1/projects/branch-copy/branches",
        json={"name": "feature-x", "description": "wip"},
    )
    assert created.status_code == 201
    branch = created.json()
    assert branch["kind"] == "working"
    assert branch["status"] == "draft"
    assert branch["base_revision_id"] is not None
    branch_id = uuid.UUID(branch["id"])

    # The main editing API still sees exactly the one main event type — branch
    # copies do not leak into the live plan.
    main_ets = await client.get("/api/v1/projects/branch-copy/event-types")
    assert main_ets.status_code == 200
    assert len(main_ets.json()) == 1

    async with TestSessionLocal() as session:
        ets = (
            (await session.execute(select(EventType).where(EventType.name == "track")))
            .scalars()
            .all()
        )
        # One on main, one deep-copied onto the branch — distinct ids, same name.
        assert len(ets) == 2
        branch_et = next(et for et in ets if et.branch_id == branch_id)
        main_et = next(et for et in ets if et.branch_id != branch_id)
        assert branch_et.id != main_et.id

        # The branch carries its own field + event copies, FK-remapped to the
        # branch's event type.
        branch_fields = (
            (
                await session.execute(
                    select(FieldDefinition).where(FieldDefinition.event_type_id == branch_et.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(branch_fields) == 1
        assert branch_fields[0].name == "name"

        branch_events = (
            (await session.execute(select(Event).where(Event.branch_id == branch_id)))
            .scalars()
            .all()
        )
        assert len(branch_events) == 1
        assert branch_events[0].name == "purchase:success"
        assert branch_events[0].event_type_id == branch_et.id


@pytest.mark.asyncio
async def test_delete_branch_removes_copies_keeps_main(client: AsyncClient) -> None:
    await _seed_plan(client, "branch-del")
    created = await client.post(
        "/api/v1/projects/branch-del/branches",
        json={"name": "throwaway"},
    )
    assert created.status_code == 201
    branch_id = created.json()["id"]

    deleted = await client.delete(f"/api/v1/projects/branch-del/branches/{branch_id}")
    assert deleted.status_code == 204

    # The branch row is gone. Its copied entities are removed by the
    # branch_id ON DELETE CASCADE in Postgres; SQLite (test DB) doesn't enforce
    # FK cascades, so we assert on the parent + the main-scoped list instead.
    async with TestSessionLocal() as session:
        assert await session.get(PlanBranch, uuid.UUID(branch_id)) is None

    # Main plan is unaffected (its list filters to the main branch).
    main_ets = await client.get("/api/v1/projects/branch-del/event-types")
    assert main_ets.status_code == 200
    assert len(main_ets.json()) == 1


@pytest.mark.asyncio
async def test_main_branch_cannot_be_deleted(client: AsyncClient) -> None:
    await _seed_plan(client, "branch-guard")
    branches = await client.get("/api/v1/projects/branch-guard/branches")
    main = next(b for b in branches.json()["items"] if b["kind"] == "main")

    resp = await client.delete(f"/api/v1/projects/branch-guard/branches/{main['id']}")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_duplicate_branch_name_rejected(client: AsyncClient) -> None:
    await _seed_plan(client, "branch-dup")
    first = await client.post(
        "/api/v1/projects/branch-dup/branches",
        json={"name": "dupe"},
    )
    assert first.status_code == 201
    second = await client.post(
        "/api/v1/projects/branch-dup/branches",
        json={"name": "dupe"},
    )
    assert second.status_code == 409

    # 'main' is reserved.
    reserved = await client.post(
        "/api/v1/projects/branch-dup/branches",
        json={"name": "main"},
    )
    assert reserved.status_code == 422


@pytest.mark.parametrize(
    "name",
    ["feature-x", "checkout/paywall-copy", "PROJ-4770", "v1.2_rc", "a" * 64, "  padded  "],
)
def test_branch_name_rule_accepts_ref_like_names(name: str) -> None:
    assert PlanBranchCreate(name=name).name == name.strip()


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("Bad name with spaces!!", "cannot contain spaces"),
        ("-leading-dash", "start with a letter or number"),
        ("emoji-\N{ROCKET}", "start with a letter or number"),
        ("a" * 65, "at most 64 characters"),
    ],
)
def test_branch_name_rule_refuses_what_the_dialog_refuses(name: str, message: str) -> None:
    """The API enforces the rule the create dialog explains.

    It used to live only in branchMeta.ts, so a direct POST still created
    "Bad name with spaces!!".
    """
    with pytest.raises(ValidationError) as exc:
        PlanBranchCreate(name=name)
    assert message in str(exc.value)


@pytest.mark.asyncio
async def test_branch_name_rule_is_a_422_on_create(client: AsyncClient) -> None:
    await _seed_plan(client, "branch-name-rule")
    resp = await client.post(
        "/api/v1/projects/branch-name-rule/branches",
        json={"name": "Bad name with spaces!!"},
    )
    assert resp.status_code == 422
    assert "cannot contain spaces" in resp.text


@pytest.mark.asyncio
async def test_branch_transition_state_machine(client: AsyncClient) -> None:
    await _seed_plan(client, "branch-wf")
    branch_id = await _create_branch(client, "branch-wf")

    # draft → approve is illegal (must submit first)
    bad = await client.post(
        f"/api/v1/projects/branch-wf/branches/{branch_id}/transition",
        json={"action": "approve"},
    )
    assert bad.status_code == 409

    detail = await _transition(client, "branch-wf", branch_id, "submit")
    assert detail["status"] == "ready_for_review"

    detail = await _transition(client, "branch-wf", branch_id, "approve")
    assert detail["status"] == "approved"
    assert len(detail["approvals"]) == 1

    detail = await _transition(client, "branch-wf", branch_id, "request_changes")
    assert detail["status"] == "changes_requested"
    # request_changes is one of the approval-clearing actions.
    assert detail["approvals"] == []

    detail = await _transition(client, "branch-wf", branch_id, "submit")
    assert detail["status"] == "ready_for_review"

    detail = await _transition(client, "branch-wf", branch_id, "close")
    assert detail["status"] == "closed"


@pytest.mark.asyncio
async def test_approval_persisted_then_cleared_in_db(client: AsyncClient) -> None:
    await _seed_plan(client, "branch-appr")
    branch_id = await _create_branch(client, "branch-appr")

    await _transition(client, "branch-appr", branch_id, "submit")
    await _transition(client, "branch-appr", branch_id, "approve")

    async with TestSessionLocal() as session:
        approvals = (
            (
                await session.execute(
                    select(PlanBranchApproval).where(
                        PlanBranchApproval.branch_id == uuid.UUID(branch_id)
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(approvals) == 1

    await _transition(client, "branch-appr", branch_id, "request_changes")

    async with TestSessionLocal() as session:
        remaining = (
            (
                await session.execute(
                    select(PlanBranchApproval).where(
                        PlanBranchApproval.branch_id == uuid.UUID(branch_id)
                    )
                )
            )
            .scalars()
            .all()
        )
        assert remaining == []


@pytest.mark.asyncio
async def test_main_branch_rejects_transitions(client: AsyncClient) -> None:
    await _seed_plan(client, "branch-main-reject")
    branches = await client.get("/api/v1/projects/branch-main-reject/branches")
    main_id = next(b for b in branches.json()["items"] if b["kind"] == "main")["id"]
    resp = await client.post(
        f"/api/v1/projects/branch-main-reject/branches/{main_id}/transition",
        json={"action": "submit"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_reviewers_add_list_remove(client: AsyncClient) -> None:
    await _seed_plan(client, "branch-rev")
    branch_id = await _create_branch(client, "branch-rev")

    # Seed a second user (reviewer candidate) directly — the auth endpoints would
    # log us out as the new user, which is not what we want here.
    reviewer_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        session.add(
            User(
                id=reviewer_id,
                email="reviewer@example.com",
                password_hash="!seed",
            )
        )
        await session.commit()
    # A reviewer must be a member of the project.
    await add_member_by_slug("branch-rev", "reviewer@example.com", "editor")

    added = await client.post(
        f"/api/v1/projects/branch-rev/branches/{branch_id}/reviewers",
        json={"user_id": str(reviewer_id)},
    )
    assert added.status_code == 201
    assert added.json()["user_id"] == str(reviewer_id)

    detail = await client.get(f"/api/v1/projects/branch-rev/branches/{branch_id}")
    assert any(r["user_id"] == str(reviewer_id) for r in detail.json()["reviewers"])

    removed = await client.delete(
        f"/api/v1/projects/branch-rev/branches/{branch_id}/reviewers/{reviewer_id}"
    )
    assert removed.status_code == 204

    detail2 = await client.get(f"/api/v1/projects/branch-rev/branches/{branch_id}")
    assert detail2.json()["reviewers"] == []


async def _touch_branch_event_type(branch_id: str, name: str = "track") -> None:
    """Mutate a branch's deep-copy of an event type so it diffs from the merge
    base — making it 'touched' for owner-gating / auto-assign."""
    async with TestSessionLocal() as session:
        branch_et = (
            (
                await session.execute(
                    select(EventType).where(
                        EventType.name == name, EventType.branch_id == uuid.UUID(branch_id)
                    )
                )
            )
            .scalars()
            .first()
        )
        branch_et.description = "touched on branch"
        await session.commit()


@pytest.mark.asyncio
async def test_submit_auto_assigns_touched_owners_as_reviewers(client: AsyncClient) -> None:
    """Submitting a branch surfaces the owners of every touched event type as
    expected reviewers — and is idempotent across a re-submit."""
    et_id = await _seed_plan(client, "branch-autorev")
    branch_id = await _create_branch(client, "branch-autorev")

    owner_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        session.add(
            User(
                id=owner_id,
                email="owner-rev@example.com",
                password_hash="!seed",
            )
        )
        # Flush the user before the owner row that FKs to it — the unit of work
        # has no ORM relationship ordering them, so a single commit can insert
        # the owner first and trip the FK under SQLite (matches Postgres).
        await session.flush()
        # Own the LIVE (main) "track" event type — owners attach to main only.
        session.add(EventTypeOwner(event_type_id=uuid.UUID(et_id), user_id=owner_id))
        await session.commit()
    await add_member_by_slug("branch-autorev", "owner-rev@example.com", "editor")
    await _touch_branch_event_type(branch_id)

    detail = await _transition(client, "branch-autorev", branch_id, "submit")
    assert detail["status"] == "ready_for_review"
    assert str(owner_id) in {r["user_id"] for r in detail["reviewers"]}

    # request_changes clears approvals but keeps reviewers; re-submitting must not
    # duplicate the owner (the (branch_id, user_id) unique key + pre-read dedup).
    await _transition(client, "branch-autorev", branch_id, "request_changes")
    detail2 = await _transition(client, "branch-autorev", branch_id, "submit")
    owner_rows = [r for r in detail2["reviewers"] if r["user_id"] == str(owner_id)]
    assert len(owner_rows) == 1

    async with TestSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(PlanBranchReviewer).where(
                        PlanBranchReviewer.branch_id == uuid.UUID(branch_id),
                        PlanBranchReviewer.user_id == owner_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_submit_skips_branch_author_as_reviewer(client: AsyncClient) -> None:
    """The branch author is never auto-assigned to review their own branch, even
    when they own a touched event type (self-review is skipped)."""
    et_id = await _seed_plan(client, "branch-autorev-self")
    branch_id = await _create_branch(client, "branch-autorev-self")

    async with TestSessionLocal() as session:
        author = (
            (await session.execute(select(User).where(User.email == "test@example.com")))
            .scalars()
            .first()
        )
        author_id = author.id
        session.add(EventTypeOwner(event_type_id=uuid.UUID(et_id), user_id=author_id))
        await session.commit()
    await _touch_branch_event_type(branch_id)

    detail = await _transition(client, "branch-autorev-self", branch_id, "submit")
    assert detail["status"] == "ready_for_review"
    assert all(r["user_id"] != str(author_id) for r in detail["reviewers"])


@pytest.mark.asyncio
async def test_branch_comments_threaded(client: AsyncClient) -> None:
    await _seed_plan(client, "branch-cmt")
    branch_id = await _create_branch(client, "branch-cmt")

    root = await client.post(
        f"/api/v1/projects/branch-cmt/branches/{branch_id}/comments",
        json={"body": "Looks good but…"},
    )
    assert root.status_code == 201
    root_id = root.json()["id"]
    assert root.json()["parent_id"] is None

    reply = await client.post(
        f"/api/v1/projects/branch-cmt/branches/{branch_id}/comments",
        json={"body": "fixed", "parent_id": root_id},
    )
    assert reply.status_code == 201
    assert reply.json()["parent_id"] == root_id

    listed = await client.get(f"/api/v1/projects/branch-cmt/branches/{branch_id}/comments")
    bodies = [c["body"] for c in listed.json()]
    assert bodies == ["Looks good but…", "fixed"]

    deleted = await client.delete(
        f"/api/v1/projects/branch-cmt/branches/{branch_id}/comments/{root_id}"
    )
    assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_empty_body_comment_rejected(client: AsyncClient) -> None:
    await _seed_plan(client, "branch-cmt-empty")
    branch_id = await _create_branch(client, "branch-cmt-empty")
    resp = await client.post(
        f"/api/v1/projects/branch-cmt-empty/branches/{branch_id}/comments",
        json={"body": "   "},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_diff_only_reports_branch_changes_when_main_advances(client: AsyncClient) -> None:
    await _seed_plan(client, "branch-diff")
    branch_id = await _create_branch(client, "branch-diff")

    initial = await client.get(f"/api/v1/projects/branch-diff/branches/{branch_id}/diff")
    assert initial.status_code == 200
    body = initial.json()
    # Right after deep-copy the branch mirrors main: no entries, not behind base.
    assert body["entries"] == []
    assert body["summary"] == {"added": 0, "removed": 0, "changed": 0, "housekeeping": 0}
    assert body["behind_base"] is False

    async with TestSessionLocal() as session:
        branch_event = (
            await session.execute(
                select(Event).where(
                    Event.branch_id == uuid.UUID(branch_id),
                    Event.name == "purchase:success",
                )
            )
        ).scalar_one()
        branch_event_id = branch_event.id
        branch_event.description = "edited on branch"
        await session.commit()

    main_event_types = await client.get("/api/v1/projects/branch-diff/event-types")
    track_id = next(et["id"] for et in main_event_types.json() if et["name"] == "track")
    added_on_main = await client.post(
        "/api/v1/projects/branch-diff/events",
        json={"event_type_id": track_id, "name": "main:added-after-branch"},
    )
    assert added_on_main.status_code == 201

    after = await client.get(f"/api/v1/projects/branch-diff/branches/{branch_id}/diff")
    assert after.status_code == 200
    after_body = after.json()
    assert after_body["behind_base"] is True
    assert after_body["summary"] == {"added": 0, "removed": 0, "changed": 1, "housekeeping": 0}
    assert len(after_body["entries"]) == 1
    entry = after_body["entries"][0]
    assert entry["entity_type"] == "event"
    assert entry["kind"] == "changed"
    assert entry["name"] == "purchase:success"
    assert entry["parent"] == "track"
    assert entry["changes"] == ["description: '' → 'edited on branch'"]
    # Structured field-level diff mirrors the human-readable string. A scalar
    # field carries no per-member breakdown — before/after already say it all.
    assert entry["field_changes"] == [
        {"field": "description", "before": "", "after": "edited on branch", "items": []}
    ]
    # The row links to the event it describes, and WHICH copy it names is the
    # whole contract: `changed` carries the BRANCH-side row, because the entry
    # is a deep link to /edit and only the branch copy is editable here. The
    # inequality is what makes this non-vacuous — the deep copy mints a fresh
    # uuid, so the two ids genuinely differ.
    main_events = await client.get("/api/v1/projects/branch-diff/events")
    main_event_id = next(
        e["id"] for e in main_events.json()["items"] if e["name"] == "purchase:success"
    )
    assert entry["entity_id"] == str(branch_event_id)
    assert entry["entity_id"] != main_event_id
    # Full before/after state carries the raw values, with DB ids / ordering stripped.
    assert entry["before"]["description"] == ""
    assert entry["after"]["description"] == "edited on branch"
    assert entry["before"]["name"] == "purchase:success"
    assert entry["after"]["event_type_name"] == "track"
    for state in (entry["before"], entry["after"]):
        assert "id" not in state
        assert "order" not in state
        assert "event_type_id" not in state


@pytest.mark.asyncio
async def test_branch_list_can_carry_diff_counts(client: AsyncClient) -> None:
    """``?include_diff_counts=true`` answers the list's ahead/behind badge.

    The Branches tab used to need one ``/branches/{id}/diff`` per feature branch
    just for those two numbers, and every one of those calls rebuilt main's plan
    snapshot. The list now derives them for the whole page from a
    single main snapshot; they must agree with the diff endpoint exactly.
    """
    slug = "branch-list-counts"
    await _seed_plan(client, slug)
    edited_id = await _create_branch(client, slug, name="edited")
    untouched_id = await _create_branch(client, slug, name="untouched")

    # Off by default: no extra snapshots for callers that only want the rows.
    plain = await client.get(f"/api/v1/projects/{slug}/branches")
    assert plain.status_code == 200
    assert all(item["ahead"] is None for item in plain.json()["items"])
    assert all(item["behind_base"] is None for item in plain.json()["items"])

    async with TestSessionLocal() as session:
        branch_event = (
            await session.execute(
                select(Event).where(
                    Event.branch_id == uuid.UUID(edited_id),
                    Event.name == "purchase:success",
                )
            )
        ).scalar_one()
        branch_event.description = "edited on branch"
        await session.commit()

    main_event_types = await client.get(f"/api/v1/projects/{slug}/event-types")
    track_id = next(et["id"] for et in main_event_types.json() if et["name"] == "track")
    advanced = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": track_id, "name": "main:added-after-branch"},
    )
    assert advanced.status_code == 201

    listed = await client.get(f"/api/v1/projects/{slug}/branches?include_diff_counts=true")
    assert listed.status_code == 200
    by_id = {item["id"]: item for item in listed.json()["items"]}

    assert by_id[edited_id]["ahead"] == 1
    assert by_id[edited_id]["behind_base"] is True
    assert by_id[untouched_id]["ahead"] == 0
    assert by_id[untouched_id]["behind_base"] is True

    # main is not a feature branch, so it carries no counts either way.
    main_row = next(item for item in listed.json()["items"] if item["kind"] == "main")
    assert main_row["ahead"] is None
    assert main_row["behind_base"] is None

    for branch_id in (edited_id, untouched_id):
        diff = await client.get(f"/api/v1/projects/{slug}/branches/{branch_id}/diff")
        assert diff.status_code == 200
        summary = diff.json()["summary"]
        assert by_id[branch_id]["ahead"] == sum(summary.values())
        assert by_id[branch_id]["behind_base"] == diff.json()["behind_base"]


@pytest.mark.asyncio
async def test_diff_entries_carry_before_after_state(client: AsyncClient) -> None:
    """Added/removed entries expose one-sided full state for the detail view.

    The branch page renders the entity's full state (branch side for added,
    base side for removed) alongside the field-level diff, so the diff endpoint
    must carry ``before``/``after`` snapshots — not just field names.
    """
    await _seed_plan(client, "branch-diff-state")
    branch_id = await _create_branch(client, "branch-diff-state")

    # Add a brand-new event on the branch (using the branch's own event-type id,
    # since branch copies are FK-remapped away from main).
    branch_ets = await client.get(
        f"/api/v1/projects/branch-diff-state/event-types?branch={branch_id}"
    )
    branch_et_id = next(et["id"] for et in branch_ets.json() if et["name"] == "track")
    added = await client.post(
        f"/api/v1/projects/branch-diff-state/events?branch={branch_id}",
        json={"event_type_id": branch_et_id, "name": "checkout:started"},
    )
    assert added.status_code == 201

    # Remove the deep-copied seed event from the branch.
    branch_events = await client.get(
        f"/api/v1/projects/branch-diff-state/events?branch={branch_id}"
    )
    seed_event = next(e for e in branch_events.json()["items"] if e["name"] == "purchase:success")
    deleted = await client.delete(
        f"/api/v1/projects/branch-diff-state/events/{seed_event['id']}?branch={branch_id}"
    )
    assert deleted.status_code == 204

    diff = await client.get(f"/api/v1/projects/branch-diff-state/branches/{branch_id}/diff")
    assert diff.status_code == 200
    entries = {e["name"]: e for e in diff.json()["entries"]}

    added_entry = entries["checkout:started"]
    assert added_entry["kind"] == "added"
    assert added_entry["before"] is None
    assert added_entry["after"]["name"] == "checkout:started"
    assert added_entry["after"]["event_type_name"] == "track"
    assert added_entry["field_changes"] == []
    assert "id" not in added_entry["after"]

    # `added` exists only on the branch, so its id is the branch row's.
    assert added_entry["entity_id"] == added.json()["id"]

    removed_entry = entries["purchase:success"]
    assert removed_entry["kind"] == "removed"
    assert removed_entry["after"] is None
    assert removed_entry["before"]["name"] == "purchase:success"
    assert "id" not in removed_entry["before"]
    # `removed` is the mirror image and the one worth pinning: the entry comes
    # from the BASE payload, which `create_branch` built from main, so the id
    # is MAIN's row — never the branch copy that was just deleted, which no
    # longer exists to link to.
    main_events = await client.get("/api/v1/projects/branch-diff-state/events")
    main_event_id = next(
        e["id"] for e in main_events.json()["items"] if e["name"] == "purchase:success"
    )
    assert removed_entry["entity_id"] == main_event_id
    assert removed_entry["entity_id"] != seed_event["id"]


@pytest.mark.asyncio
async def test_diff_rejects_main_branch(client: AsyncClient) -> None:
    await _seed_plan(client, "branch-diff-main")
    branches = await client.get("/api/v1/projects/branch-diff-main/branches")
    main_id = next(b for b in branches.json()["items"] if b["kind"] == "main")["id"]
    resp = await client.get(f"/api/v1/projects/branch-diff-main/branches/{main_id}/diff")
    assert resp.status_code == 400


# --- Phase 4: merge engine ------------------------------------------------


async def _approve_and_merge(client: AsyncClient, slug: str, branch_id: str):
    await _transition(client, slug, branch_id, "submit")
    await _transition(client, slug, branch_id, "approve")
    return await client.post(f"/api/v1/projects/{slug}/branches/{branch_id}/merge")


@pytest.mark.asyncio
async def test_merge_requires_approved_status(client: AsyncClient) -> None:
    await _seed_plan(client, "merge-gate")
    branch_id = await _create_branch(client, "merge-gate")
    resp = await client.post(f"/api/v1/projects/merge-gate/branches/{branch_id}/merge")
    assert resp.status_code == 409  # status is draft, not approved


@pytest.mark.asyncio
async def test_second_merge_of_the_same_branch_is_rejected(client: AsyncClient) -> None:
    """Applying an approved branch twice would duplicate every add."""
    await _seed_plan(client, "merge-twice")
    branch_id = await _create_branch(client, "merge-twice")

    first = await _approve_and_merge(client, "merge-twice", branch_id)
    assert first.status_code == 200

    second = await client.post(f"/api/v1/projects/merge-twice/branches/{branch_id}/merge")
    assert second.status_code == 400


@pytest.mark.asyncio
async def test_merge_lock_rereads_status_instead_of_trusting_the_identity_map(
    client: AsyncClient,
) -> None:
    """The status gate must read the ROW, not a cached instance.

    This is the half of the double-merge race that a row lock alone does not
    fix: the loser wakes up holding a `PlanBranch` it loaded before the winner
    committed. Without ``populate_existing`` the re-check reads the stale
    ``approved`` off the identity map and applies the branch a second time.
    """
    from sqlalchemy import update

    from tripl.models.plan_branch import BranchStatus, PlanBranch
    from tripl.services.plan_branch_merge_service import _lock_branch_for_merge
    from tripl.tests.conftest import TestSessionLocal

    await _seed_plan(client, "merge-stale")
    branch_id = await _create_branch(client, "merge-stale")

    async with TestSessionLocal() as session:
        cached = await session.get(PlanBranch, uuid.UUID(branch_id))
        assert cached is not None
        project_id = cached.project_id
        assert cached.status != BranchStatus.merged.value

        # Someone else's merge lands. `synchronize_session=False` is what makes
        # this a faithful stand-in for a concurrent request: the row changes
        # while THIS session's identity map keeps the value it already read,
        # exactly as it would if the write came from another connection.
        await session.execute(
            update(PlanBranch)
            .where(PlanBranch.id == uuid.UUID(branch_id))
            .values(status=BranchStatus.merged.value)
            .execution_options(synchronize_session=False)
        )
        assert cached.status != BranchStatus.merged.value

        locked = await _lock_branch_for_merge(session, project_id, uuid.UUID(branch_id))

        assert locked.status == BranchStatus.merged.value


@pytest.mark.asyncio
async def test_merge_rejects_main_branch(client: AsyncClient) -> None:
    await _seed_plan(client, "merge-main")
    branches = await client.get("/api/v1/projects/merge-main/branches")
    main_id = next(b for b in branches.json()["items"] if b["kind"] == "main")["id"]
    resp = await client.post(f"/api/v1/projects/merge-main/branches/{main_id}/merge")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_merge_no_op_marks_branch_merged_and_writes_revision(
    client: AsyncClient,
) -> None:
    """A branch identical to main still merges cleanly — status flips, a
    post-merge PlanRevision is recorded."""
    await _seed_plan(client, "merge-noop")
    branch_id = await _create_branch(client, "merge-noop")

    # Count revisions before merge (base snapshot + N from earlier ops).
    pre = await client.get("/api/v1/projects/merge-noop/revisions")
    pre_total = pre.json()["total"]

    resp = await _approve_and_merge(client, "merge-noop", branch_id)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "merged"
    assert body["merged_at"] is not None
    assert body["merged_by"] is not None

    post = await client.get("/api/v1/projects/merge-noop/revisions")
    assert post.json()["total"] == pre_total + 1


@pytest.mark.asyncio
async def test_merge_enqueues_implementation_ticket_when_tracker_enabled(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A merge that touches an event enqueues a ticket task covering the main
    event ids — but only when the project has an enabled tracker config."""
    await _seed_plan(client, "merge-ticket")
    async with TestSessionLocal() as session:
        project = (
            await session.execute(select(Project).where(Project.slug == "merge-ticket"))
        ).scalar_one()
        session.add(
            ProjectTrackerConfig(
                project_id=project.id,
                enabled=True,
                tracker_type="jira",
                base_url="https://example.atlassian.net",
                project_key="ENG",
                auth_email="alice@example.com",
                api_token_encrypted="tok",
                issue_type="Task",
            )
        )
        await session.commit()

    branch_id = await _create_branch(client, "merge-ticket")
    # Change the branch's copy of the event so the merge carries a real diff.
    async with TestSessionLocal() as session:
        branch_event = (
            (await session.execute(select(Event).where(Event.branch_id == uuid.UUID(branch_id))))
            .scalars()
            .first()
        )
        branch_event.description = "implemented via branch"
        await session.commit()

    captured: dict[str, object] = {}

    class _FakeTask:
        def delay(self, *args: object) -> None:
            captured["args"] = args

    monkeypatch.setattr(impl_tasks, "create_implementation_ticket", _FakeTask())

    resp = await _approve_and_merge(client, "merge-ticket", branch_id)
    assert resp.status_code == 200, resp.text

    assert "args" in captured, "expected the ticket task to be enqueued"
    project_arg, branch_arg, event_ids_arg, summary_arg = captured["args"]
    assert branch_arg == branch_id
    assert isinstance(event_ids_arg, list) and len(event_ids_arg) == 1
    assert "branch 'feature'" in summary_arg

    # The covered id is a MAIN-branch event id (resolvable on the live plan).
    async with TestSessionLocal() as session:
        main_branch = (
            await session.execute(
                select(PlanBranch).where(
                    PlanBranch.project_id == uuid.UUID(project_arg),
                    PlanBranch.name == "main",
                )
            )
        ).scalar_one()
        main_event = (
            await session.execute(
                select(Event).where(
                    Event.id == uuid.UUID(event_ids_arg[0]),
                    Event.branch_id == main_branch.id,
                )
            )
        ).scalar_one_or_none()
        assert main_event is not None


@pytest.mark.asyncio
async def test_merge_skips_ticket_when_tracker_disabled(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No tracker config → no enqueue, even when the merge touches events."""
    await _seed_plan(client, "merge-noticket")
    branch_id = await _create_branch(client, "merge-noticket")
    async with TestSessionLocal() as session:
        branch_event = (
            (await session.execute(select(Event).where(Event.branch_id == uuid.UUID(branch_id))))
            .scalars()
            .first()
        )
        branch_event.description = "changed"
        await session.commit()

    called = {"delay": False}

    class _FakeTask:
        def delay(self, *args: object) -> None:
            called["delay"] = True

    monkeypatch.setattr(impl_tasks, "create_implementation_ticket", _FakeTask())

    resp = await _approve_and_merge(client, "merge-noticket", branch_id)
    assert resp.status_code == 200, resp.text
    assert called["delay"] is False


@pytest.mark.asyncio
async def test_merge_refreshes_main_search_index(client: AsyncClient) -> None:
    """Merging refreshes main's search index immediately — merged content is
    searchable without waiting for the next scan cycle or CRUD edit."""
    await _seed_plan(client, "merge-search")
    # Seed main's index BEFORE the merge, so only a post-merge refresh can put
    # the token below into it. This used to be a search GET, which seeded the
    # index only because the read path built an empty branch's index for it —
    # the side effect that was since removed. Asking for the reindex outright says
    # what the setup needs instead of relying on a read path's side effect;
    # nothing the test asserts about the merge changed.
    seeded = await client.post("/api/v1/projects/merge-search/search/reindex")
    assert seeded.status_code == 200

    branch_id = await _create_branch(client, "merge-search")
    # Direct DB edit (bypasses CRUD reindex) so the token can ONLY enter main's
    # index through the merge's own refresh.
    async with TestSessionLocal() as session:
        branch_event = (
            (await session.execute(select(Event).where(Event.branch_id == uuid.UUID(branch_id))))
            .scalars()
            .first()
        )
        branch_event.description = "zebrasearchtoken description"
        await session.commit()

    resp = await _approve_and_merge(client, "merge-search", branch_id)
    assert resp.status_code == 200, resp.text

    found = await client.get(
        "/api/v1/projects/merge-search/search", params={"q": "zebrasearchtoken"}
    )
    assert found.status_code == 200
    hits = found.json()["items"]
    assert any(
        item["entity_type"] == "event" and item["title"] == "purchase:success" for item in hits
    ), hits


@pytest.mark.asyncio
async def test_merge_preserves_main_event_type_id(client: AsyncClient) -> None:
    """Branch modifies an existing event type; the live main row keeps its id
    (so runtime rows linked to it survive)."""
    await _seed_plan(client, "merge-id")

    # Pre-merge: capture the live main event_type id.
    async with TestSessionLocal() as session:
        main_branch_id = (
            (await session.execute(select(PlanBranch).where(PlanBranch.name == "main")))
            .scalars()
            .first()
            .id
        )
        original_et = (
            (
                await session.execute(
                    select(EventType).where(
                        EventType.name == "track", EventType.branch_id == main_branch_id
                    )
                )
            )
            .scalars()
            .first()
        )
        original_et_id = original_et.id

    branch_id = await _create_branch(client, "merge-id")

    # Mutate the branch's copy via ORM (the ?branch= editing API lands in Phase 5).
    async with TestSessionLocal() as session:
        branch_et = (
            (
                await session.execute(
                    select(EventType).where(
                        EventType.name == "track",
                        EventType.branch_id == uuid.UUID(branch_id),
                    )
                )
            )
            .scalars()
            .first()
        )
        branch_et.color = "#ff0000"
        branch_et.description = "renamed on branch"
        await session.commit()

    resp = await _approve_and_merge(client, "merge-id", branch_id)
    assert resp.status_code == 200

    async with TestSessionLocal() as session:
        survived = (
            (
                await session.execute(
                    select(EventType).where(
                        EventType.name == "track", EventType.branch_id == main_branch_id
                    )
                )
            )
            .scalars()
            .first()
        )
        # Same id => attached metrics/photos/alerts survive the merge.
        assert survived.id == original_et_id
        assert survived.color == "#ff0000"
        assert survived.description == "renamed on branch"


@pytest.mark.asyncio
async def test_merge_adds_and_removes_event_types(client: AsyncClient) -> None:
    await _seed_plan(client, "merge-add-rm")
    branch_id = await _create_branch(client, "merge-add-rm")

    async with TestSessionLocal() as session:
        # Branch removes "track" and adds "checkout".
        branch_uuid = uuid.UUID(branch_id)
        branch_track = (
            (
                await session.execute(
                    select(EventType).where(
                        EventType.name == "track", EventType.branch_id == branch_uuid
                    )
                )
            )
            .scalars()
            .first()
        )
        # Branch events also reference branch_track — drop the matching event too.
        branch_track_event = (
            (
                await session.execute(
                    select(Event).where(
                        Event.branch_id == branch_uuid, Event.name == "purchase:success"
                    )
                )
            )
            .scalars()
            .first()
        )
        await session.delete(branch_track_event)
        await session.delete(branch_track)
        # Add a new event type on the branch.
        session.add(
            EventType(
                id=uuid.uuid4(),
                project_id=branch_track.project_id,
                branch_id=branch_uuid,
                name="checkout",
                display_name="Checkout",
                description="",
                color="#00ff00",
                order=1,
            )
        )
        await session.commit()

    resp = await _approve_and_merge(client, "merge-add-rm", branch_id)
    assert resp.status_code == 200

    main_ets = await client.get("/api/v1/projects/merge-add-rm/event-types")
    names = {et["name"] for et in main_ets.json()}
    assert "track" not in names
    assert "checkout" in names


@pytest.mark.asyncio
async def test_merge_preserves_main_only_event_addition(client: AsyncClient) -> None:
    await _seed_plan(client, "merge-behind")
    branch_id = await _create_branch(client, "merge-behind")

    main_event_types = await client.get("/api/v1/projects/merge-behind/event-types")
    track_id = next(et["id"] for et in main_event_types.json() if et["name"] == "track")
    added_on_main = await client.post(
        "/api/v1/projects/merge-behind/events",
        json={"event_type_id": track_id, "name": "main:added-after-branch"},
    )
    assert added_on_main.status_code == 201

    merged = await _approve_and_merge(client, "merge-behind", branch_id)
    assert merged.status_code == 200, merged.text
    events = (await client.get("/api/v1/projects/merge-behind/events")).json()["items"]
    assert {event["name"] for event in events} == {
        "purchase:success",
        "main:added-after-branch",
    }


@pytest.mark.asyncio
async def test_merge_preserves_main_only_event_edit(client: AsyncClient) -> None:
    await _seed_plan(client, "merge-behind-change")
    branch_id = await _create_branch(client, "merge-behind-change")

    events = await client.get("/api/v1/projects/merge-behind-change/events")
    event_id = events.json()["items"][0]["id"]
    changed_on_main = await client.patch(
        f"/api/v1/projects/merge-behind-change/events/{event_id}",
        json={"description": "edited on main"},
    )
    assert changed_on_main.status_code == 200

    merged = await _approve_and_merge(client, "merge-behind-change", branch_id)
    assert merged.status_code == 200, merged.text
    event = (await client.get("/api/v1/projects/merge-behind-change/events")).json()["items"][0]
    assert event["description"] == "edited on main"


@pytest.mark.asyncio
async def test_merge_rejects_legacy_branch_without_complete_base_snapshot(
    client: AsyncClient,
) -> None:
    await _seed_plan(client, "merge-legacy")
    branch_id = await _create_branch(client, "merge-legacy")
    async with TestSessionLocal() as session:
        branch = await session.get(PlanBranch, uuid.UUID(branch_id))
        assert branch is not None
        branch.base_revision_id = None
        await session.commit()

    merged = await _approve_and_merge(client, "merge-legacy", branch_id)
    assert merged.status_code == 409
    assert merged.json()["detail"]["incomplete_base_snapshot"] is True


@pytest.mark.asyncio
async def test_merge_preserves_main_only_event_tag_during_unrelated_branch_edit(
    client: AsyncClient,
) -> None:
    slug = "merge-main-tag"
    await _seed_plan(client, slug)
    branch_id = await _create_branch(client, slug)

    async with TestSessionLocal() as session:
        main_event = (
            (
                await session.execute(
                    select(Event).where(
                        Event.name == "purchase:success",
                        Event.branch_id != uuid.UUID(branch_id),
                    )
                )
            )
            .scalars()
            .one()
        )
        branch_event = (
            (
                await session.execute(
                    select(Event).where(
                        Event.name == "purchase:success",
                        Event.branch_id == uuid.UUID(branch_id),
                    )
                )
            )
            .scalars()
            .one()
        )
        session.add(EventTag(event_id=main_event.id, name="main-only"))
        branch_event.description = "unrelated branch edit"
        await session.commit()
        main_event_id = main_event.id

    merged = await _approve_and_merge(client, slug, branch_id)
    assert merged.status_code == 200, merged.text

    async with TestSessionLocal() as session:
        tags = (
            (await session.execute(select(EventTag).where(EventTag.event_id == main_event_id)))
            .scalars()
            .all()
        )
        assert [tag.name for tag in tags] == ["main-only"]


@pytest.mark.asyncio
async def test_branch_tag_change_invalidates_approval_hash(client: AsyncClient) -> None:
    slug = "branch-tag-hash"
    await _seed_plan(client, slug)
    branch_id = await _create_branch(client, slug)
    await _transition(client, slug, branch_id, "submit")
    await _transition(client, slug, branch_id, "approve")

    async with TestSessionLocal() as session:
        branch_event = (
            (await session.execute(select(Event).where(Event.branch_id == uuid.UUID(branch_id))))
            .scalars()
            .one()
        )
        session.add(EventTag(event_id=branch_event.id, name="after-approval"))
        await session.commit()

    merged = await client.post(f"/api/v1/projects/{slug}/branches/{branch_id}/merge")
    assert merged.status_code == 409
    assert merged.json()["detail"]["insufficient_approvals"]["stale"] == 1


@pytest.mark.asyncio
async def test_snapshot_v2_captures_all_merge_relevant_event_child_state(
    client: AsyncClient,
) -> None:
    slug = "snapshot-v2-children"
    await _seed_plan(client, slug)
    meta = await client.post(
        f"/api/v1/projects/{slug}/meta-fields",
        json={"name": "owner_team", "display_name": "Owner team", "field_type": "string"},
    )
    assert meta.status_code == 201

    async with TestSessionLocal() as session:
        project = (
            (await session.execute(select(Project).where(Project.slug == slug))).scalars().one()
        )
        event = (
            (await session.execute(select(Event).where(Event.project_id == project.id)))
            .scalars()
            .one()
        )
        field = (
            (
                await session.execute(
                    select(FieldDefinition).where(
                        FieldDefinition.event_type_id == event.event_type_id
                    )
                )
            )
            .scalars()
            .one()
        )
        meta_field = await session.get(MetaFieldDefinition, uuid.UUID(meta.json()["id"]))
        assert meta_field is not None
        event.source_name = "purchase_source"
        event.reviewed = True
        event.metric_breakdown_columns = ["platform"]
        session.add(
            EventFieldValue(
                event_id=event.id,
                field_definition_id=field.id,
                value="${variant}",
                is_authored=True,
            )
        )
        session.add(
            EventMetaValue(
                event_id=event.id,
                meta_field_definition_id=meta_field.id,
                value="growth",
            )
        )
        session.add(EventTag(event_id=event.id, name="critical"))
        photo = EventPhoto(
            project_id=project.id,
            event_id=event.id,
            original_filename="Spec",
            content_type="",
            size_bytes=0,
            kind="figma",
            external_url="https://www.figma.com/file/snapshot/Spec",
            sort_order=0,
        )
        session.add(photo)
        await session.flush()
        session.add(EventPhotoComment(photo_id=photo.id, body="ship it"))
        await session.commit()

        snapshot = await build_plan_snapshot(session, project.id, branch_id=event.branch_id)

    assert snapshot["snapshot_version"] == 2
    event_state = snapshot["events"][0]
    assert event_state["source_name"] == "purchase_source"
    assert event_state["reviewed"] is True
    assert event_state["metric_breakdown_columns"] == ["platform"]
    assert event_state["field_values"] == [
        {"field_name": "name", "value": "${variant}", "is_authored": True}
    ]
    assert event_state["meta_values"] == [{"meta_field_name": "owner_team", "value": "growth"}]
    assert event_state["tags"] == ["critical"]
    comment_state = event_state["photos"][0]["comments"][0]
    assert comment_state["user_fingerprint"] is None
    assert len(comment_state["body_fingerprint"]) == 64
    assert comment_state["replies"] == []
    assert "body" not in comment_state
    assert "storage_key" not in event_state["photos"][0]


@pytest.mark.asyncio
async def test_parent_delete_conflicts_with_main_child_addition(client: AsyncClient) -> None:
    slug = "merge-parent-child-conflict"
    event_type_id = await _seed_plan(client, slug)
    branch_id = await _create_branch(client, slug)

    added = await client.post(
        f"/api/v1/projects/{slug}/event-types/{event_type_id}/fields",
        json={"name": "main_only", "display_name": "Main only", "field_type": "string"},
    )
    assert added.status_code == 201

    async with TestSessionLocal() as session:
        branch_type = (
            (
                await session.execute(
                    select(EventType).where(EventType.branch_id == uuid.UUID(branch_id))
                )
            )
            .scalars()
            .one()
        )
        branch_event = (
            (await session.execute(select(Event).where(Event.branch_id == uuid.UUID(branch_id))))
            .scalars()
            .one()
        )
        await session.delete(branch_event)
        await session.delete(branch_type)
        await session.commit()

    merged = await _approve_and_merge(client, slug, branch_id)
    assert merged.status_code == 409
    assert merged.json()["detail"]["conflicts"] == [{"entity_type": "event_type", "name": "track"}]


@pytest.mark.asyncio
async def test_field_delete_conflicts_with_main_dependent_value(client: AsyncClient) -> None:
    slug = "merge-field-value-conflict"
    await _seed_plan(client, slug)
    branch_id = await _create_branch(client, slug)

    async with TestSessionLocal() as session:
        main_event = (
            (await session.execute(select(Event).where(Event.branch_id != uuid.UUID(branch_id))))
            .scalars()
            .one()
        )
        main_field = (
            (
                await session.execute(
                    select(FieldDefinition).where(
                        FieldDefinition.event_type_id == main_event.event_type_id
                    )
                )
            )
            .scalars()
            .one()
        )
        branch_type = (
            (
                await session.execute(
                    select(EventType).where(EventType.branch_id == uuid.UUID(branch_id))
                )
            )
            .scalars()
            .one()
        )
        branch_field = (
            (
                await session.execute(
                    select(FieldDefinition).where(FieldDefinition.event_type_id == branch_type.id)
                )
            )
            .scalars()
            .one()
        )
        session.add(
            EventFieldValue(
                event_id=main_event.id,
                field_definition_id=main_field.id,
                value="main-only",
            )
        )
        await session.delete(branch_field)
        await session.commit()

    merged = await _approve_and_merge(client, slug, branch_id)
    assert merged.status_code == 409
    assert {tuple(conflict.values()) for conflict in merged.json()["detail"]["conflicts"]} >= {
        ("field_definition", "track.name")
    }


@pytest.mark.asyncio
async def test_field_delete_conflicts_with_main_dependent_relation(client: AsyncClient) -> None:
    slug = "merge-field-relation-conflict"
    event_type_id = await _seed_plan(client, slug)
    second = await client.post(
        f"/api/v1/projects/{slug}/event-types/{event_type_id}/fields",
        json={"name": "target", "display_name": "Target", "field_type": "string"},
    )
    assert second.status_code == 201
    branch_id = await _create_branch(client, slug)

    async with TestSessionLocal() as session:
        main_type = await session.get(EventType, uuid.UUID(event_type_id))
        assert main_type is not None
        main_fields = {
            field.name: field
            for field in (
                (
                    await session.execute(
                        select(FieldDefinition).where(FieldDefinition.event_type_id == main_type.id)
                    )
                )
                .scalars()
                .all()
            )
        }
        project = await session.get(Project, main_type.project_id)
        assert project is not None
        session.add(
            EventTypeRelation(
                project_id=project.id,
                branch_id=main_type.branch_id,
                source_event_type_id=main_type.id,
                target_event_type_id=main_type.id,
                source_field_id=main_fields["name"].id,
                target_field_id=main_fields["target"].id,
                relation_type="one_to_one",
                description="main-only",
            )
        )
        branch_type = (
            (
                await session.execute(
                    select(EventType).where(EventType.branch_id == uuid.UUID(branch_id))
                )
            )
            .scalars()
            .one()
        )
        branch_field = (
            (
                await session.execute(
                    select(FieldDefinition).where(
                        FieldDefinition.event_type_id == branch_type.id,
                        FieldDefinition.name == "name",
                    )
                )
            )
            .scalars()
            .one()
        )
        await session.delete(branch_field)
        await session.commit()

    merged = await _approve_and_merge(client, slug, branch_id)
    assert merged.status_code == 409
    assert {tuple(conflict.values()) for conflict in merged.json()["detail"]["conflicts"]} >= {
        ("field_definition", "track.name")
    }


@pytest.mark.asyncio
async def test_meta_field_delete_conflicts_with_main_dependent_value(client: AsyncClient) -> None:
    slug = "merge-meta-value-conflict"
    await _seed_plan(client, slug)
    created = await client.post(
        f"/api/v1/projects/{slug}/meta-fields",
        json={"name": "team", "display_name": "Team", "field_type": "string"},
    )
    assert created.status_code == 201
    branch_id = await _create_branch(client, slug)

    async with TestSessionLocal() as session:
        main_event = (
            (await session.execute(select(Event).where(Event.branch_id != uuid.UUID(branch_id))))
            .scalars()
            .one()
        )
        main_meta = await session.get(MetaFieldDefinition, uuid.UUID(created.json()["id"]))
        assert main_meta is not None
        branch_meta = (
            (
                await session.execute(
                    select(MetaFieldDefinition).where(
                        MetaFieldDefinition.branch_id == uuid.UUID(branch_id)
                    )
                )
            )
            .scalars()
            .one()
        )
        session.add(
            EventMetaValue(
                event_id=main_event.id,
                meta_field_definition_id=main_meta.id,
                value="main-only",
            )
        )
        await session.delete(branch_meta)
        await session.commit()

    merged = await _approve_and_merge(client, slug, branch_id)
    assert merged.status_code == 409
    assert {tuple(conflict.values()) for conflict in merged.json()["detail"]["conflicts"]} >= {
        ("meta_field", "team")
    }


@pytest.mark.asyncio
async def test_snapshot_override_keys_include_event_type_for_duplicate_names(
    client: AsyncClient,
) -> None:
    slug = "snapshot-duplicate-event-names"
    await _seed_plan(client, slug)
    second_type = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "screen", "display_name": "Screen"},
    )
    assert second_type.status_code == 201
    second_event = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": second_type.json()["id"], "name": "purchase:success"},
    )
    assert second_event.status_code == 201
    variable = await client.post(f"/api/v1/projects/{slug}/variables", json={"name": "variant"})
    assert variable.status_code == 201
    events = (await client.get(f"/api/v1/projects/{slug}/events")).json()["items"]
    for event in events:
        response = await client.put(
            f"/api/v1/projects/{slug}/variables/{variable.json()['id']}/event-overrides/"
            f"{event['id']}",
            json={"values": [event["event_type_id"]]},
        )
        assert response.status_code == 200

    branch_id = await _create_branch(client, slug)
    async with TestSessionLocal() as session:
        branch = await session.get(PlanBranch, uuid.UUID(branch_id))
        assert branch is not None and branch.base_revision_id is not None
        base = await session.get(PlanRevision, branch.base_revision_id)
        assert base is not None
        branch_snapshot = await build_plan_snapshot(session, branch.project_id, branch_id=branch.id)

    assert [(event["event_type_name"], event["name"]) for event in branch_snapshot["events"]] == [
        ("screen", "purchase:success"),
        ("track", "purchase:success"),
    ]
    base_overrides = base.payload["variables"][0]["event_value_overrides"]
    branch_overrides = branch_snapshot["variables"][0]["event_value_overrides"]
    assert branch_overrides == base_overrides
    assert [(row["event_type_name"], row["event_name"]) for row in branch_overrides] == [
        ("screen", "purchase:success"),
        ("track", "purchase:success"),
    ]


@pytest.mark.asyncio
async def test_merge_preserves_main_only_event_deletion(client: AsyncClient) -> None:
    slug = "merge-main-delete"
    await _seed_plan(client, slug)
    branch_id = await _create_branch(client, slug)
    event_id = (await client.get(f"/api/v1/projects/{slug}/events")).json()["items"][0]["id"]
    deleted = await client.delete(f"/api/v1/projects/{slug}/events/{event_id}")
    assert deleted.status_code == 204

    merged = await _approve_and_merge(client, slug, branch_id)
    assert merged.status_code == 200, merged.text
    assert (await client.get(f"/api/v1/projects/{slug}/events")).json()["items"] == []


@pytest.mark.asyncio
async def test_merge_3way_auto_merges_non_overlapping_field_changes(
    client: AsyncClient,
) -> None:
    """Main and branch edit different fields of the same event type — true
    3-way merge keeps both sides without a conflict."""
    await _seed_plan(client, "merge-3way")
    branch_id = await _create_branch(client, "merge-3way")

    main_ets = await client.get("/api/v1/projects/merge-3way/event-types")
    main_track_id = next(et["id"] for et in main_ets.json() if et["name"] == "track")
    await client.patch(
        f"/api/v1/projects/merge-3way/event-types/{main_track_id}",
        json={"color": "#0000ff"},
    )

    async with TestSessionLocal() as session:
        branch_track = (
            (
                await session.execute(
                    select(EventType).where(
                        EventType.name == "track",
                        EventType.branch_id == uuid.UUID(branch_id),
                    )
                )
            )
            .scalars()
            .first()
        )
        branch_track.description = "changed on branch"
        await session.commit()

    resp = await _approve_and_merge(client, "merge-3way", branch_id)
    assert resp.status_code == 200, resp.text

    main_after = await client.get(f"/api/v1/projects/merge-3way/event-types/{main_track_id}")
    body = main_after.json()
    # Main's color edit survives; branch's description edit lands too.
    assert body["color"] == "#0000ff"
    assert body["description"] == "changed on branch"


@pytest.mark.asyncio
async def test_merge_blocks_when_both_sides_retitle_the_same_event(
    client: AsyncClient,
) -> None:
    """`title` is authored text, so two people can write it differently.

    It reached the event and the diff's key list without reaching the merge's,
    so a title edited on both sides merged silently with one side winning and
    nothing reported.
    """
    slug = "merge-title-conflict"
    await _seed_plan(client, slug)
    branch_id = await _create_branch(client, slug)

    main_event_id = (await client.get(f"/api/v1/projects/{slug}/events")).json()["items"][0]["id"]
    on_main = await client.patch(
        f"/api/v1/projects/{slug}/events/{main_event_id}",
        json={"title": "Purchase completed"},
    )
    assert on_main.status_code == 200

    branch_events = await client.get(f"/api/v1/projects/{slug}/events?branch={branch_id}")
    branch_event_id = branch_events.json()["items"][0]["id"]
    on_branch = await client.patch(
        f"/api/v1/projects/{slug}/events/{branch_event_id}?branch={branch_id}",
        json={"title": "Checkout finished"},
    )
    assert on_branch.status_code == 200

    resp = await _approve_and_merge(client, slug, branch_id)
    assert resp.status_code == 409, resp.text

    # Main keeps its own text while the conflict stands — the merge changed
    # nothing rather than picking a winner.
    main_after = await client.get(f"/api/v1/projects/{slug}/events/{main_event_id}")
    assert main_after.json()["title"] == "Purchase completed"


@pytest.mark.asyncio
async def test_merge_blocks_on_same_field_conflict_until_resolved(
    client: AsyncClient,
) -> None:
    """Both sides edit the *same* event_type field with different values →
    409 with unresolved_field_conflicts; once a resolution is saved, merge
    proceeds with the chosen value."""
    await _seed_plan(client, "merge-fconflict")
    branch_id = await _create_branch(client, "merge-fconflict")

    main_ets = await client.get("/api/v1/projects/merge-fconflict/event-types")
    main_track_id = next(et["id"] for et in main_ets.json() if et["name"] == "track")
    await client.patch(
        f"/api/v1/projects/merge-fconflict/event-types/{main_track_id}",
        json={"color": "#aaaaaa"},
    )

    async with TestSessionLocal() as session:
        branch_track = (
            (
                await session.execute(
                    select(EventType).where(
                        EventType.name == "track",
                        EventType.branch_id == uuid.UUID(branch_id),
                    )
                )
            )
            .scalars()
            .first()
        )
        branch_track.color = "#bbbbbb"
        await session.commit()

    first = await _approve_and_merge(client, "merge-fconflict", branch_id)
    assert first.status_code == 409
    unresolved = first.json()["detail"]["unresolved_field_conflicts"]
    assert any(
        c["entity_type"] == "event_type" and c["name"] == "track" and c["field"] == "color"
        for c in unresolved
    )

    # Inspect the rich conflicts payload and pick "ours" for the color field.
    conflicts = await client.get(f"/api/v1/projects/merge-fconflict/branches/{branch_id}/conflicts")
    assert conflicts.status_code == 200
    body = conflicts.json()
    assert body["unresolved_count"] == 1
    entity = body["entities"][0]
    assert entity["fields"][0]["field"] == "color"
    assert entity["fields"][0]["ours"] == "#aaaaaa"
    assert entity["fields"][0]["theirs"] == "#bbbbbb"

    save = await client.post(
        f"/api/v1/projects/merge-fconflict/branches/{branch_id}/resolutions",
        json={
            "entity_type": "event_type",
            "entity_name": "track",
            "field_name": "color",
            "choice": "ours",
        },
    )
    assert save.status_code == 201

    # Branch is now in 'merged' state? No — it was approved → merge failed →
    # status is still approved. Just call merge again; transitions stay intact.
    merge = await client.post(f"/api/v1/projects/merge-fconflict/branches/{branch_id}/merge")
    assert merge.status_code == 200, merge.text

    main_after = await client.get(f"/api/v1/projects/merge-fconflict/event-types/{main_track_id}")
    # "ours" wins for color — main's value survives the merge.
    assert main_after.json()["color"] == "#aaaaaa"


@pytest.mark.asyncio
async def test_resolutions_cleared_on_branch_reopen(client: AsyncClient) -> None:
    """Reopening the branch (back to draft) drops any captured resolutions —
    the reviewer must re-pick against the new base/ours/theirs."""
    await _seed_plan(client, "merge-clear-res")
    branch_id = await _create_branch(client, "merge-clear-res")

    async with TestSessionLocal() as session:
        branch_track = (
            (
                await session.execute(
                    select(EventType).where(
                        EventType.name == "track",
                        EventType.branch_id == uuid.UUID(branch_id),
                    )
                )
            )
            .scalars()
            .first()
        )
        branch_track.color = "#cccccc"
        await session.commit()

    await client.patch(
        f"/api/v1/projects/merge-clear-res/event-types/"
        f"{(await client.get('/api/v1/projects/merge-clear-res/event-types')).json()[0]['id']}",
        json={"color": "#dddddd"},
    )

    save = await client.post(
        f"/api/v1/projects/merge-clear-res/branches/{branch_id}/resolutions",
        json={
            "entity_type": "event_type",
            "entity_name": "track",
            "field_name": "color",
            "choice": "theirs",
        },
    )
    assert save.status_code == 201

    # Push the branch back to draft via reopen — transition path used by the
    # UI when changes are requested.
    await client.post(
        f"/api/v1/projects/merge-clear-res/branches/{branch_id}/transition",
        json={"action": "submit"},
    )
    await client.post(
        f"/api/v1/projects/merge-clear-res/branches/{branch_id}/transition",
        json={"action": "approve"},
    )
    await client.post(
        f"/api/v1/projects/merge-clear-res/branches/{branch_id}/transition",
        json={"action": "reopen"},
    )

    conflicts = await client.get(f"/api/v1/projects/merge-clear-res/branches/{branch_id}/conflicts")
    assert conflicts.status_code == 200
    body = conflicts.json()
    # Resolutions are gone — every conflict is unresolved again.
    assert body["unresolved_count"] == 1
    for entity in body["entities"]:
        for field in entity["fields"]:
            assert field["choice"] is None


# --- event_photos branching --------------------------------------------------


async def _attach_main_figma(
    client: AsyncClient, slug: str, event_id: str, url: str, title: str
) -> str:
    resp = await client.post(
        f"/api/v1/projects/{slug}/events/{event_id}/photos/figma",
        json={"url": url, "title": title},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_branch_create_copies_event_photos_and_comments(
    client: AsyncClient,
) -> None:
    """Creating a branch deep-copies photos + threaded comments onto the
    branch's events with fresh ids and FK remap."""
    await _seed_plan(client, "branch-photos")
    events = await client.get("/api/v1/projects/branch-photos/events")
    main_event_id = events.json()["items"][0]["id"]
    main_photo_id = await _attach_main_figma(
        client,
        "branch-photos",
        main_event_id,
        "https://www.figma.com/file/abc/Spec",
        "Spec",
    )
    # One top-level comment + one reply.
    base = f"/api/v1/projects/branch-photos/events/{main_event_id}/photos/{main_photo_id}/comments"
    top = await client.post(base, json={"body": "looks good"})
    assert top.status_code == 201
    reply = await client.post(base, json={"body": "thanks!", "parent_id": top.json()["id"]})
    assert reply.status_code == 201

    branch_id = await _create_branch(client, "branch-photos", "feature-photos")

    async with TestSessionLocal() as session:
        branch_event = (
            (await session.execute(select(Event).where(Event.branch_id == uuid.UUID(branch_id))))
            .scalars()
            .first()
        )
        assert branch_event is not None
        assert branch_event.id != uuid.UUID(main_event_id)

        branch_photos = (
            (
                await session.execute(
                    select(EventPhoto).where(EventPhoto.event_id == branch_event.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(branch_photos) == 1
        branch_photo = branch_photos[0]
        # Fresh id but the same blob target (storage_key/external_url reused).
        assert branch_photo.id != uuid.UUID(main_photo_id)
        assert branch_photo.external_url == "https://www.figma.com/file/abc/Spec"
        assert branch_photo.kind == "figma"

        branch_comments = (
            (
                await session.execute(
                    select(EventPhotoComment).where(EventPhotoComment.photo_id == branch_photo.id)
                )
            )
            .scalars()
            .all()
        )
        assert {c.body for c in branch_comments} == {"looks good", "thanks!"}
        # The reply's parent_id was remapped to the branch's top-level comment.
        top_branch = next(c for c in branch_comments if c.parent_id is None)
        reply_branch = next(c for c in branch_comments if c.parent_id is not None)
        assert reply_branch.parent_id == top_branch.id


@pytest.mark.asyncio
async def test_merge_carries_branch_photo_changes_to_main(client: AsyncClient) -> None:
    """Branch removes a main photo and attaches a new one; merge replaces
    main's photo set with the branch's set (reachable as the source of truth)."""
    await _seed_plan(client, "merge-photos")
    events = await client.get("/api/v1/projects/merge-photos/events")
    main_event_id = events.json()["items"][0]["id"]
    await _attach_main_figma(
        client,
        "merge-photos",
        main_event_id,
        "https://www.figma.com/file/old/Old",
        "Old",
    )

    branch_id = await _create_branch(client, "merge-photos", "feature-art")

    async with TestSessionLocal() as session:
        branch_event = (
            (await session.execute(select(Event).where(Event.branch_id == uuid.UUID(branch_id))))
            .scalars()
            .first()
        )
        branch_event_id = branch_event.id
        # Drop the deep-copied photo from the branch.
        branch_photos = (
            (
                await session.execute(
                    select(EventPhoto).where(EventPhoto.event_id == branch_event_id)
                )
            )
            .scalars()
            .all()
        )
        for ph in branch_photos:
            await session.delete(ph)
        await session.commit()

    # Attach a new figma row directly on the branch event.
    new_url = "https://www.figma.com/file/new/New"
    await _attach_main_figma(client, "merge-photos", str(branch_event_id), new_url, "New")

    resp = await _approve_and_merge(client, "merge-photos", branch_id)
    assert resp.status_code == 200

    # On main, the live event now carries the branch's photo set: one figma row
    # with the new URL, the old one gone.
    listed = await client.get(f"/api/v1/projects/merge-photos/events/{main_event_id}/photos")
    assert listed.status_code == 200
    urls = [row["external_url"] for row in listed.json()]
    assert urls == [new_url]


@pytest.mark.asyncio
async def test_merge_keeps_comments_written_on_main_and_the_photo_id(
    client: AsyncClient,
) -> None:
    """Regression: a merge used to replace main's whole photo
    canvas whenever the branch's snapshot subtree differed — and comments are
    nested INSIDE each photo there, so one comment written on the branch was
    enough. Main's photo rows were deleted, ``event_photo_comments.photo_id``
    cascaded, and everything said on main since the branch was cut went with
    them. The photo ids churned too, so any link into the canvas broke.
    """
    slug = "merge-photo-comments"
    await _seed_plan(client, slug)
    events = await client.get(f"/api/v1/projects/{slug}/events")
    main_event_id = events.json()["items"][0]["id"]
    main_photo_id = await _attach_main_figma(
        client,
        slug,
        main_event_id,
        "https://www.figma.com/file/abc/Spec",
        "Spec",
    )
    main_comments_url = (
        f"/api/v1/projects/{slug}/events/{main_event_id}/photos/{main_photo_id}/comments"
    )
    before = await client.post(main_comments_url, json={"body": "before the branch"})
    assert before.status_code == 201

    branch_id = await _create_branch(client, slug, "feature-art")

    async with TestSessionLocal() as session:
        branch_event = (
            (await session.execute(select(Event).where(Event.branch_id == uuid.UUID(branch_id))))
            .scalars()
            .first()
        )
        assert branch_event is not None
        branch_event_id = branch_event.id
        branch_photo = (
            (
                await session.execute(
                    select(EventPhoto).where(EventPhoto.event_id == branch_event_id)
                )
            )
            .scalars()
            .one()
        )
        branch_photo_id = branch_photo.id
        branch_copy_of_before = (
            (
                await session.execute(
                    select(EventPhotoComment).where(
                        EventPhotoComment.photo_id == branch_photo_id,
                        EventPhotoComment.body == "before the branch",
                    )
                )
            )
            .scalars()
            .one()
        )

    branch_comments_url = (
        f"/api/v1/projects/{slug}/events/{branch_event_id}/photos/{branch_photo_id}/comments"
    )
    on_branch = await client.post(
        branch_comments_url,
        json={"body": "answered on the branch", "parent_id": str(branch_copy_of_before.id)},
    )
    assert on_branch.status_code == 201
    # Meanwhile, someone reading main says something there. This is the comment
    # the merge used to destroy.
    on_main = await client.post(main_comments_url, json={"body": "said on main"})
    assert on_main.status_code == 201

    resp = await _approve_and_merge(client, slug, branch_id)
    assert resp.status_code == 200

    listed = await client.get(f"/api/v1/projects/{slug}/events/{main_event_id}/photos")
    assert [row["id"] for row in listed.json()] == [main_photo_id]

    async with TestSessionLocal() as session:
        rows = list(
            (
                await session.execute(
                    select(EventPhotoComment).where(
                        EventPhotoComment.photo_id == uuid.UUID(main_photo_id)
                    )
                )
            )
            .scalars()
            .all()
        )
    # Both sides survive, and the branch's copy of a comment main already holds
    # is recognised as the same comment rather than added a second time.
    assert sorted(row.body for row in rows) == [
        "answered on the branch",
        "before the branch",
        "said on main",
    ]
    root = next(row for row in rows if row.body == "before the branch")
    reply = next(row for row in rows if row.body == "answered on the branch")
    # The reply came over from the branch and found main's own comment to hang
    # under, not the branch copy it was written against.
    assert reply.parent_id == root.id
    assert root.id == uuid.UUID(before.json()["id"])


# --- ?branch= router param threading ----------------------------------------


@pytest.mark.asyncio
async def test_router_branch_param_lists_branch_event_types(client: AsyncClient) -> None:
    """GET /event-types?branch=<id> returns the branch's deep copy, not main's."""
    await _seed_plan(client, "branch-route-list")
    branch_id = await _create_branch(client, "branch-route-list", "feature-A")

    # Editing on the branch adds a second event type; main keeps its one.
    create_resp = await client.post(
        f"/api/v1/projects/branch-route-list/event-types?branch={branch_id}",
        json={"name": "checkout", "display_name": "Checkout"},
    )
    assert create_resp.status_code == 201

    main_list = (await client.get("/api/v1/projects/branch-route-list/event-types")).json()
    assert {et["name"] for et in main_list} == {"track"}

    branch_list = (
        await client.get(f"/api/v1/projects/branch-route-list/event-types?branch={branch_id}")
    ).json()
    assert {et["name"] for et in branch_list} == {"track", "checkout"}


@pytest.mark.asyncio
async def test_router_branch_param_threads_variables(client: AsyncClient) -> None:
    """Variables CRUD scoped to a branch leaves main untouched."""
    await _seed_plan(client, "branch-route-vars")
    branch_id = await _create_branch(client, "branch-route-vars", "feature-vars")

    create_resp = await client.post(
        f"/api/v1/projects/branch-route-vars/variables?branch={branch_id}",
        json={"name": "spot_id", "variable_type": "string"},
    )
    assert create_resp.status_code == 201

    main_vars = (await client.get("/api/v1/projects/branch-route-vars/variables")).json()
    assert main_vars == {"items": [], "total": 0}

    branch_vars = (
        await client.get(f"/api/v1/projects/branch-route-vars/variables?branch={branch_id}")
    ).json()
    assert [v["name"] for v in branch_vars["items"]] == ["spot_id"]


@pytest.mark.asyncio
async def test_router_branch_param_invalid_uuid_returns_400(client: AsyncClient) -> None:
    """Malformed ?branch= value yields 400 — the dep rejects before service runs."""
    await _seed_plan(client, "branch-route-bad")
    resp = await client.get("/api/v1/projects/branch-route-bad/event-types?branch=not-a-uuid")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_router_branch_param_cross_project_404(client: AsyncClient) -> None:
    """A real branch id belonging to another project is rejected as 404."""
    await _seed_plan(client, "branch-route-iso-a")
    await _seed_plan(client, "branch-route-iso-b")
    other_branch_id = await _create_branch(client, "branch-route-iso-b", "feature-b")
    resp = await client.get(
        f"/api/v1/projects/branch-route-iso-a/event-types?branch={other_branch_id}"
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_router_branch_param_threads_meta_fields(client: AsyncClient) -> None:
    """Meta-field create on branch is isolated from main."""
    await _seed_plan(client, "branch-route-meta")
    branch_id = await _create_branch(client, "branch-route-meta", "feature-meta")

    create_resp = await client.post(
        f"/api/v1/projects/branch-route-meta/meta-fields?branch={branch_id}",
        json={"name": "owner", "display_name": "Owner", "field_type": "string"},
    )
    assert create_resp.status_code == 201

    main_mf = (await client.get("/api/v1/projects/branch-route-meta/meta-fields")).json()
    assert main_mf == []
    branch_mf = (
        await client.get(f"/api/v1/projects/branch-route-meta/meta-fields?branch={branch_id}")
    ).json()
    assert [mf["name"] for mf in branch_mf] == ["owner"]


# --- event merge: source_name preservation (scan dedup integrity) -----------


@pytest.mark.asyncio
async def test_merge_preserves_event_source_name(client: AsyncClient) -> None:
    """Merge must carry an event's ``source_name`` from branch to main on BOTH
    the update-existing and create-new paths.

    Scan dedup (worker event_generator) keys on ``source_name``; if a merged
    event lands with ``source_name`` unset it gets backfilled to ``name`` and
    re-duplicates as a shadow row, detaching metrics. This guards that data
    integrity invariant.
    """
    await _seed_plan(client, "merge-source-name")

    main_branch_id_str = None
    async with TestSessionLocal() as session:
        main_branch = (
            (await session.execute(select(PlanBranch).where(PlanBranch.name == "main")))
            .scalars()
            .first()
        )
        main_branch_id_str = main_branch.id
        # Give the live main event a runtime-distinct source_name (as the scan
        # worker would have stamped it: differs from the editable display name).
        main_event = (
            (
                await session.execute(
                    select(Event).where(
                        Event.branch_id == main_branch.id, Event.name == "purchase:success"
                    )
                )
            )
            .scalars()
            .first()
        )
        main_event.source_name = "purchase_success_raw"
        await session.commit()

    branch_id = await _create_branch(client, "merge-source-name")
    branch_uuid = uuid.UUID(branch_id)

    async with TestSessionLocal() as session:
        # update-existing path: stamp the branch event's source_name (mirrors a
        # scan having run against the branch) and mutate it so the merge writes
        # back to the existing main row.
        branch_event = (
            (
                await session.execute(
                    select(Event).where(
                        Event.branch_id == branch_uuid, Event.name == "purchase:success"
                    )
                )
            )
            .scalars()
            .first()
        )
        branch_event.source_name = "purchase_success_raw"
        branch_event.description = "edited on branch"

        # create-new path: add a brand-new event on the branch with its own
        # source_name that diverges from its display name.
        branch_et = (
            (
                await session.execute(
                    select(EventType).where(
                        EventType.name == "track", EventType.branch_id == branch_uuid
                    )
                )
            )
            .scalars()
            .first()
        )
        session.add(
            Event(
                id=uuid.uuid4(),
                project_id=branch_et.project_id,
                branch_id=branch_uuid,
                event_type_id=branch_et.id,
                name="signup:done",
                source_name="signup_done_raw",
            )
        )
        await session.commit()

    resp = await _approve_and_merge(client, "merge-source-name", branch_id)
    assert resp.status_code == 200, resp.text

    async with TestSessionLocal() as session:
        merged = {
            e.name: e
            for e in (
                await session.execute(select(Event).where(Event.branch_id == main_branch_id_str))
            )
            .scalars()
            .all()
        }
        # update-existing path kept the source_name (not backfilled to name),
        # so scan dedup re-attaches metrics instead of spawning a shadow row.
        assert merged["purchase:success"].source_name == "purchase_success_raw"
        # create-new path carried the source_name across too.
        assert merged["signup:done"].source_name == "signup_done_raw"


@pytest.mark.asyncio
async def test_deep_copy_carries_event_source_name_so_merge_preserves_it(
    client: AsyncClient,
) -> None:
    """Deep-copy must carry an event's ``source_name`` onto the branch copy.

    If the deep-copy left the branch event's ``source_name`` as ``None`` and no
    scan re-stamped it, merge's update-existing path would copy that ``None``
    back over main's good value — re-introducing the orphaned-metrics/shadow-row
    bug. Here the branch event's ``source_name`` is deliberately NOT touched after
    branch creation (no scan), so the only way main keeps its value is if the
    deep-copy carried it.
    """
    await _seed_plan(client, "merge-deepcopy-srcname")

    main_branch_id_str = None
    async with TestSessionLocal() as session:
        main_branch = (
            (await session.execute(select(PlanBranch).where(PlanBranch.name == "main")))
            .scalars()
            .first()
        )
        main_branch_id_str = main_branch.id
        main_event = (
            (
                await session.execute(
                    select(Event).where(
                        Event.branch_id == main_branch.id,
                        Event.name == "purchase:success",
                    )
                )
            )
            .scalars()
            .first()
        )
        main_event.source_name = "purchase_success_raw"
        await session.commit()

    branch_id = await _create_branch(client, "merge-deepcopy-srcname")
    branch_uuid = uuid.UUID(branch_id)

    # The deep-copy must have carried source_name onto the branch event copy.
    async with TestSessionLocal() as session:
        branch_event = (
            (
                await session.execute(
                    select(Event).where(
                        Event.branch_id == branch_uuid,
                        Event.name == "purchase:success",
                    )
                )
            )
            .scalars()
            .first()
        )
        assert branch_event.source_name == "purchase_success_raw"
        # Mutate something unrelated so the merge hits the update-existing path
        # without re-stamping source_name (i.e. as if no scan ran on the branch).
        branch_event.description = "edited on branch"
        await session.commit()

    resp = await _approve_and_merge(client, "merge-deepcopy-srcname", branch_id)
    assert resp.status_code == 200, resp.text

    async with TestSessionLocal() as session:
        merged_main_event = (
            (
                await session.execute(
                    select(Event).where(
                        Event.branch_id == main_branch_id_str,
                        Event.name == "purchase:success",
                    )
                )
            )
            .scalars()
            .first()
        )
        # Merge must not have written None over main's good source_name.
        assert merged_main_event.source_name == "purchase_success_raw"


# --- merge: a branch rename is a rename, not a delete plus an add


async def _main_branch_id() -> uuid.UUID:
    async with TestSessionLocal() as session:
        main_branch = (
            (await session.execute(select(PlanBranch).where(PlanBranch.name == "main")))
            .scalars()
            .first()
        )
        return main_branch.id


@pytest.mark.asyncio
async def test_merge_of_a_renamed_event_keeps_its_id_and_its_variable_values(
    client: AsyncClient,
) -> None:
    """A branch rename must move main's event, not replace it.

    Upsert-by-natural-key keys events on (event type, name), and Event has no
    display_name — its machine name is the displayed one — so renaming one used
    to read as a removal plus an unrelated addition. Main's row was deleted, a
    fresh uuid inserted, and the FK cascade took ``variable_values`` with it.
    Nothing diffed the loss: VariableValue is not in the plan snapshot.
    """
    await _seed_plan(client, "merge-rename-event")
    main_branch_id = await _main_branch_id()

    async with TestSessionLocal() as session:
        main_event = (
            (
                await session.execute(
                    select(Event).where(
                        Event.branch_id == main_branch_id, Event.name == "purchase:success"
                    )
                )
            )
            .scalars()
            .first()
        )
        # The scan identity a rename does not touch, stamped as a scan would.
        main_event.source_name = "purchase_success_raw"
        main_event_id = main_event.id
        project_id = main_event.project_id
        field = (
            (
                await session.execute(
                    select(FieldDefinition).where(
                        FieldDefinition.event_type_id == main_event.event_type_id
                    )
                )
            )
            .scalars()
            .first()
        )
        variable = Variable(
            id=uuid.uuid4(),
            project_id=project_id,
            branch_id=main_branch_id,
            name="cart_total",
            source_name="cart_total",
        )
        session.add(variable)
        session.add(
            VariableValue(
                id=uuid.uuid4(),
                project_id=project_id,
                branch_id=main_branch_id,
                variable_id=variable.id,
                event_id=main_event_id,
                field_definition_id=field.id,
                source_column="properties.cart_total",
                observed_count=7,
                values=["1999"],
            )
        )
        await session.commit()

    branch_id = await _create_branch(client, "merge-rename-event")
    branch_uuid = uuid.UUID(branch_id)

    async with TestSessionLocal() as session:
        branch_event = (
            (
                await session.execute(
                    select(Event).where(
                        Event.branch_id == branch_uuid, Event.name == "purchase:success"
                    )
                )
            )
            .scalars()
            .first()
        )
        branch_event.name = "purchase:completed"
        await session.commit()

    resp = await _approve_and_merge(client, "merge-rename-event", branch_id)
    assert resp.status_code == 200, resp.text

    async with TestSessionLocal() as session:
        main_event_rows = (
            (await session.execute(select(Event).where(Event.branch_id == main_branch_id)))
            .scalars()
            .all()
        )
        main_events = {e.name: e for e in main_event_rows}
        assert "purchase:success" not in main_events
        renamed = main_events["purchase:completed"]
        assert renamed.id == main_event_id
        assert renamed.source_name == "purchase_success_raw"

        contexts = (
            (
                await session.execute(
                    select(VariableValue).where(VariableValue.branch_id == main_branch_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(contexts) == 1
        assert contexts[0].event_id == main_event_id
        assert contexts[0].observed_count == 7
        assert contexts[0].values == ["1999"]


@pytest.mark.asyncio
async def test_merge_of_a_renamed_variable_succeeds_and_keeps_its_contexts(
    client: AsyncClient,
) -> None:
    """A renamed branch variable used to fail the merge outright.

    ``uq_variable_project_source_name`` is unique on (project, branch,
    source_name) and no rename writes source_name, so the insert arm added a
    main variable carrying main's own source_name while the delete of the row it
    replaced was still pending in the same flush — SQLAlchemy runs a mapper's
    saves before its deletes — and the merge raised IntegrityError. Renaming in
    place removes both the failure and the id churn under ``variable_values``.
    """
    await _seed_plan(client, "merge-rename-variable")
    main_branch_id = await _main_branch_id()

    async with TestSessionLocal() as session:
        main_event = (
            (
                await session.execute(
                    select(Event).where(
                        Event.branch_id == main_branch_id, Event.name == "purchase:success"
                    )
                )
            )
            .scalars()
            .first()
        )
        project_id = main_event.project_id
        field = (
            (
                await session.execute(
                    select(FieldDefinition).where(
                        FieldDefinition.event_type_id == main_event.event_type_id
                    )
                )
            )
            .scalars()
            .first()
        )
        variable = Variable(
            id=uuid.uuid4(),
            project_id=project_id,
            branch_id=main_branch_id,
            name="cart_total",
            source_name="cart_total_raw",
        )
        session.add(variable)
        main_variable_id = variable.id
        session.add(
            VariableValue(
                id=uuid.uuid4(),
                project_id=project_id,
                branch_id=main_branch_id,
                variable_id=main_variable_id,
                event_id=main_event.id,
                field_definition_id=field.id,
                source_column="properties.cart_total",
                observed_count=3,
                values=["1999"],
            )
        )
        await session.commit()

    branch_id = await _create_branch(client, "merge-rename-variable")
    branch_uuid = uuid.UUID(branch_id)

    async with TestSessionLocal() as session:
        branch_variable = (
            (
                await session.execute(
                    select(Variable).where(
                        Variable.branch_id == branch_uuid, Variable.name == "cart_total"
                    )
                )
            )
            .scalars()
            .first()
        )
        # The deep copy carries main's source_name onto the branch row, which is
        # what makes the collision below reachable.
        assert branch_variable.source_name == "cart_total_raw"
        branch_variable.name = "basket_total"
        await session.commit()

    resp = await _approve_and_merge(client, "merge-rename-variable", branch_id)
    assert resp.status_code == 200, resp.text

    async with TestSessionLocal() as session:
        main_variables = (
            (await session.execute(select(Variable).where(Variable.branch_id == main_branch_id)))
            .scalars()
            .all()
        )
        assert len(main_variables) == 1
        assert main_variables[0].id == main_variable_id
        assert main_variables[0].name == "basket_total"
        assert main_variables[0].source_name == "cart_total_raw"

        contexts = (
            (
                await session.execute(
                    select(VariableValue).where(VariableValue.branch_id == main_branch_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(contexts) == 1
        assert contexts[0].variable_id == main_variable_id
        assert contexts[0].observed_count == 3


async def _seed_main_variables(slug: str, sources_by_name: dict[str, str]) -> dict[str, uuid.UUID]:
    """Put one main variable per ``name -> source_name``; return source_name -> id.

    Keying the result by source_name is the point of these tests: it is the one
    identity a rename does not move, so it is what the assertions can hold on to
    while every name shifts underneath.
    """
    async with TestSessionLocal() as session:
        project = (
            (await session.execute(select(Project).where(Project.slug == slug))).scalars().first()
        )
        main_branch = (
            (
                await session.execute(
                    select(PlanBranch).where(
                        PlanBranch.project_id == project.id, PlanBranch.name == "main"
                    )
                )
            )
            .scalars()
            .first()
        )
        for name, source_name in sources_by_name.items():
            session.add(
                Variable(
                    id=uuid.uuid4(),
                    project_id=project.id,
                    branch_id=main_branch.id,
                    name=name,
                    source_name=source_name,
                )
            )
        await session.commit()
        rows = (
            (await session.execute(select(Variable).where(Variable.branch_id == main_branch.id)))
            .scalars()
            .all()
        )
        return {v.source_name: v.id for v in rows}


async def _rename_branch_variables(branch_id: uuid.UUID, names_by_source: dict[str, str]) -> None:
    """Give each branch variable the name keyed by its source_name.

    A permutation of names needs a parking value even here: the merge trips over
    ``uq_variable_project_name`` precisely because it is non-deferrable, and the
    same constraint refuses an in-place exchange in the branch. That the setup
    has to dodge it is itself the evidence the merge could not.
    """
    async with TestSessionLocal() as session:
        rows = {
            v.source_name: v
            for v in (
                await session.execute(select(Variable).where(Variable.branch_id == branch_id))
            )
            .scalars()
            .all()
        }
        for variable in rows.values():
            variable.name = f"parked_{uuid.uuid4().hex}"
        await session.flush()
        for source_name, new_name in names_by_source.items():
            rows[source_name].name = new_name
        await session.commit()


async def _main_variables_by_source(main_branch_id: uuid.UUID) -> dict[str, Variable]:
    async with TestSessionLocal() as session:
        return {
            v.source_name: v
            for v in (
                await session.execute(select(Variable).where(Variable.branch_id == main_branch_id))
            )
            .scalars()
            .all()
        }


@pytest.mark.asyncio
async def test_merge_of_two_variables_whose_names_were_swapped(client: AsyncClient) -> None:
    """A swap is two renames, and a name-keyed diff cannot see either of them.

    ``update_variable`` writes ``name`` and never ``source_name`` — ``VariableUpdate``
    has no such field — while the branch deep copy carries main's ``source_name``
    onto the branch row. So a branch can legitimately hold two variables whose
    NAMES are swapped relative to their SOURCE_NAMES, and by name nothing was
    added and nothing removed: the pairing had no would-delete and no
    would-insert to work from, and paired nothing. The upsert then matched each
    branch row to the main row already wearing its new name and wrote that row's
    ``source_name`` onto it, inside one flush, against a non-deferrable
    ``UNIQUE (project_id, branch_id, source_name)``. The IntegrityError reached
    the client as a bare 500 and the branch stayed unmergeable until someone
    renamed a row by hand.
    """
    await _seed_plan(client, "merge-swap-variables")
    main_ids = await _seed_main_variables(
        "merge-swap-variables",
        {"cart_total": "cart_total_raw", "cart_count": "cart_count_raw"},
    )

    branch_id = await _create_branch(client, "merge-swap-variables")
    await _rename_branch_variables(
        uuid.UUID(branch_id),
        {"cart_total_raw": "cart_count", "cart_count_raw": "cart_total"},
    )

    resp = await _approve_and_merge(client, "merge-swap-variables", branch_id)
    assert resp.status_code == 200, resp.text

    merged = await _main_variables_by_source(await _main_branch_id())
    assert len(merged) == 2
    # The names moved, the scan identities stayed put, and each identity is still
    # on the row id its ``variable_values`` hang off.
    assert merged["cart_total_raw"].name == "cart_count"
    assert merged["cart_total_raw"].id == main_ids["cart_total_raw"]
    assert merged["cart_count_raw"].name == "cart_total"
    assert merged["cart_count_raw"].id == main_ids["cart_count_raw"]


@pytest.mark.asyncio
async def test_merge_of_three_variables_rotated_through_each_others_names(
    client: AsyncClient,
) -> None:
    """The longer form of the same shape, and the one a swap-only fix would miss.

    A rotation leaves the branch's set of names identical to main's exactly as a
    swap does, so it paired nothing either; unlike a swap it cannot be settled by
    "when two rows collide, exchange them". The moves have to be applied as the
    permutation they are — through a parking name — or one of the three UPDATEs
    always lands on a name another main row still holds.
    """
    await _seed_plan(client, "merge-rotate-variables")
    main_ids = await _seed_main_variables(
        "merge-rotate-variables",
        {"variant": "variant_raw", "bucket": "bucket_raw", "cohort": "cohort_raw"},
    )

    branch_id = await _create_branch(client, "merge-rotate-variables")
    await _rename_branch_variables(
        uuid.UUID(branch_id),
        {"variant_raw": "bucket", "bucket_raw": "cohort", "cohort_raw": "variant"},
    )

    resp = await _approve_and_merge(client, "merge-rotate-variables", branch_id)
    assert resp.status_code == 200, resp.text

    merged = await _main_variables_by_source(await _main_branch_id())
    assert {source: v.name for source, v in merged.items()} == {
        "variant_raw": "bucket",
        "bucket_raw": "cohort",
        "cohort_raw": "variant",
    }
    assert {source: v.id for source, v in merged.items()} == main_ids
    # The parking name exists only between two flushes inside the merge's own
    # transaction; a row still wearing one means the second pass never ran.
    assert all(not v.name.startswith(_RENAME_STAGING_PREFIX) for v in merged.values())


async def _attach_variable_values(slug: str, variable_ids: dict[str, uuid.UUID]) -> None:
    """Hang one ``variable_values`` row off each of main's variables.

    These are the rows the FK cascade takes when a merge deletes a Variable
    instead of moving it, and ``build_plan_snapshot`` does not carry them — so
    counting them afterwards is the only way a test can see the loss at all.
    """
    async with TestSessionLocal() as session:
        project = (
            (await session.execute(select(Project).where(Project.slug == slug))).scalars().first()
        )
        main_branch = (
            (
                await session.execute(
                    select(PlanBranch).where(
                        PlanBranch.project_id == project.id, PlanBranch.name == "main"
                    )
                )
            )
            .scalars()
            .first()
        )
        event = (
            (
                await session.execute(
                    select(Event).where(
                        Event.project_id == project.id, Event.branch_id == main_branch.id
                    )
                )
            )
            .scalars()
            .first()
        )
        field = (
            (
                await session.execute(
                    select(FieldDefinition).where(
                        FieldDefinition.event_type_id == event.event_type_id
                    )
                )
            )
            .scalars()
            .first()
        )
        for source_name, variable_id in variable_ids.items():
            session.add(
                VariableValue(
                    id=uuid.uuid4(),
                    project_id=project.id,
                    branch_id=main_branch.id,
                    variable_id=variable_id,
                    event_id=event.id,
                    field_definition_id=field.id,
                    source_column=f"properties.{source_name}",
                    observed_count=3,
                    values=["1999"],
                )
            )
        await session.commit()


@pytest.mark.asyncio
async def test_merge_moving_a_rename_onto_a_deleted_variables_name_is_a_409(
    client: AsyncClient,
) -> None:
    """The ambiguous shape must fail whole rather than half-succeed.

    Base and main both hold ``cart_total``/``cart_total_raw`` and a
    ``cart_count`` with NO ``source_name``. The branch DELETES ``cart_count`` and
    RENAMES ``cart_total`` into the name it vacated. With an identity on the
    occupant that shape now merges (pinned in
    ``test_variable_regressions``); without one nothing proves the branch's
    ``cart_count`` is not the occupant edited, so ``pair_renames`` still drops
    the move, the merge's two arms disagree about who owns ``cart_total_raw``,
    and SQLAlchemy — which runs a mapper's saves ahead of its deletes inside one
    flush — turns that into an IntegrityError. Losing nothing while telling the
    user to rename the clashing entity is the answer.

    Two separate failures hide behind this 409 and both are asserted here.

    Run the removals BEFORE the upsert and the merge succeeds instead: main's
    ``cart_total`` is deleted with its ``variable_values``, and the row the user
    DELETED survives wearing the kept row's scan identity — which is what the
    next scan matches warehouse data on. No diff shows either half.

    Log ``branch.id`` rather than a plain local in the handler and the 409 is
    never built at all: the failed flush has already rolled back to the root
    transaction and expired every state in the identity map, so reading the
    primary key back is implicit IO on the sync Session from async code and the
    client gets the bare 500 the handler exists to replace.
    """
    await _seed_plan(client, "merge-delete-then-rename")
    main_ids = await _seed_main_variables(
        "merge-delete-then-rename",
        {"cart_total": "cart_total_raw", "cart_count": ""},
    )
    await _attach_variable_values("merge-delete-then-rename", main_ids)
    main_branch_id = await _main_branch_id()

    branch_id = await _create_branch(client, "merge-delete-then-rename")
    async with TestSessionLocal() as session:
        branch_rows = {
            v.source_name: v
            for v in (
                await session.execute(
                    select(Variable).where(Variable.branch_id == uuid.UUID(branch_id))
                )
            )
            .scalars()
            .all()
        }
        await session.delete(branch_rows[""])
        # The delete has to land before the rename: inside the branch the same
        # non-deferrable ``uq_variable_project_name`` refuses both at once, which
        # is the constraint the merge is about to run into from the other side.
        await session.flush()
        branch_rows["cart_total_raw"].name = "cart_count"
        await session.commit()

    resp = await _approve_and_merge(client, "merge-delete-then-rename", branch_id)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["merge_constraint_violation"] is True

    merged = await _main_variables_by_source(main_branch_id)
    # Nothing moved and nothing went: both rows, both names, both ids.
    assert {source: v.name for source, v in merged.items()} == {
        "cart_total_raw": "cart_total",
        "": "cart_count",
    }
    assert {source: v.id for source, v in merged.items()} == main_ids

    async with TestSessionLocal() as session:
        values = (
            (
                await session.execute(
                    select(VariableValue).where(VariableValue.branch_id == main_branch_id)
                )
            )
            .scalars()
            .all()
        )
    # The observed values are the half a snapshot diff would never have shown.
    assert {v.variable_id for v in values} == set(main_ids.values())


@pytest.mark.asyncio
async def test_merge_moving_a_rename_onto_a_deleted_events_name_lands_row_by_row(
    client: AsyncClient,
) -> None:
    """The event twin of the variable case above, pinned by ``uq_event_scan_identity``.

    Base and main both hold ``purchase:success``/``purchase_success_raw`` and
    ``purchase:completed``/``purchase_completed_raw`` under ``track``. The branch
    DELETES ``purchase:completed`` and RENAMES ``purchase:success`` into the name
    it vacated. ``pair_renames`` proposes that move and drops it — main's own
    ``purchase:completed`` is not itself moving away — so the upsert, keyed by
    name, writes ``purchase_success_raw`` onto main's ``purchase:completed``
    while main's ``purchase:success`` still holds it and only goes in the
    removal arm afterwards. Until events had a constraint that flush SUCCEEDED:
    the row the user kept was deleted with everything hanging off it, the row
    the user deleted survived wearing the kept row's scan identity, and no
    snapshot diff showed either half. ``uq_event_scan_identity`` then turned
    the two arms' disagreement into a 409 that lost nothing.

    The branch copies now name the main rows they came from (``origin_id``),
    so there is nothing left to disagree about: the copy of ``purchase:success``
    was renamed, the copy of ``purchase:completed`` was deleted, and the merge
    does exactly that to exactly those rows — the kept row keeps its id and its
    identity under the new name, the deleted one goes.
    """
    slug = "merge-delete-then-rename-event"
    et_id = await _seed_plan(client, slug)
    second = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": et_id, "name": "purchase:completed"},
    )
    assert second.status_code == 201, second.text
    main_branch_id = await _main_branch_id()

    async with TestSessionLocal() as session:
        main_rows = {
            e.name: e
            for e in (
                (await session.execute(select(Event).where(Event.branch_id == main_branch_id)))
                .scalars()
                .all()
            )
        }
        main_rows["purchase:success"].source_name = "purchase_success_raw"
        main_rows["purchase:completed"].source_name = "purchase_completed_raw"
        await session.commit()
        main_ids = {name: e.id for name, e in main_rows.items()}

    branch_id = await _create_branch(client, slug)
    branch_uuid = uuid.UUID(branch_id)
    async with TestSessionLocal() as session:
        branch_ids = {
            e.name: e.id
            for e in (
                (await session.execute(select(Event).where(Event.branch_id == branch_uuid)))
                .scalars()
                .all()
            )
        }

    deleted = await client.delete(
        f"/api/v1/projects/{slug}/events/{branch_ids['purchase:completed']}?branch={branch_id}"
    )
    assert deleted.status_code == 204, deleted.text
    renamed = await client.patch(
        f"/api/v1/projects/{slug}/events/{branch_ids['purchase:success']}?branch={branch_id}",
        json={"name": "purchase:completed"},
    )
    assert renamed.status_code == 200, renamed.text

    resp = await _approve_and_merge(client, slug, branch_id)
    assert resp.status_code == 200, resp.text

    async with TestSessionLocal() as session:
        merged = (
            (await session.execute(select(Event).where(Event.branch_id == main_branch_id)))
            .scalars()
            .all()
        )
    # The kept row, renamed in place with its identity; the deleted row gone.
    assert [(e.id, e.name, e.source_name) for e in merged] == [
        (main_ids["purchase:success"], "purchase:completed", "purchase_success_raw")
    ]


@pytest.mark.asyncio
async def test_merge_still_removes_a_deleted_event_beside_an_unrelated_addition(
    client: AsyncClient,
) -> None:
    """Pairing must not fuse a real removal with a real addition.

    Both rows carry a source_name and sit in the same event type, so this is the
    exact shape a rename presents — minus the shared identity that proves the two
    are one row. The removal has to stay a removal and the addition an addition.
    """
    await _seed_plan(client, "merge-remove-event")
    main_branch_id = await _main_branch_id()

    async with TestSessionLocal() as session:
        main_event = (
            (
                await session.execute(
                    select(Event).where(
                        Event.branch_id == main_branch_id, Event.name == "purchase:success"
                    )
                )
            )
            .scalars()
            .first()
        )
        main_event.source_name = "purchase_success_raw"
        await session.commit()

    branch_id = await _create_branch(client, "merge-remove-event")
    branch_uuid = uuid.UUID(branch_id)

    async with TestSessionLocal() as session:
        branch_et = (
            (
                await session.execute(
                    select(EventType).where(
                        EventType.name == "track", EventType.branch_id == branch_uuid
                    )
                )
            )
            .scalars()
            .first()
        )
        branch_event = (
            (
                await session.execute(
                    select(Event).where(
                        Event.branch_id == branch_uuid, Event.name == "purchase:success"
                    )
                )
            )
            .scalars()
            .first()
        )
        await session.delete(branch_event)
        session.add(
            Event(
                id=uuid.uuid4(),
                project_id=branch_et.project_id,
                branch_id=branch_uuid,
                event_type_id=branch_et.id,
                name="signup:done",
                source_name="signup_done_raw",
            )
        )
        await session.commit()

    resp = await _approve_and_merge(client, "merge-remove-event", branch_id)
    assert resp.status_code == 200, resp.text

    async with TestSessionLocal() as session:
        main_event_rows = (
            (await session.execute(select(Event).where(Event.branch_id == main_branch_id)))
            .scalars()
            .all()
        )
        assert sorted(e.name for e in main_event_rows) == ["signup:done"]


# --- pair_renames: the shapes that must stay a delete plus an add ------------


def test_pair_renames_matches_the_would_insert_sharing_a_would_deletes_source_name() -> None:
    renames = pair_renames(
        {("track", "purchase:success"): "purchase_success_raw"},
        {("track", "purchase:success"): "purchase_success_raw"},
        {("track", "purchase:completed"): "purchase_success_raw"},
    )
    assert renames == {("track", "purchase:success"): ("track", "purchase:completed")}


def test_pair_renames_leaves_rows_without_a_source_name_unpaired() -> None:
    """source_name is nullable by design, so absence identifies nothing."""
    renames = pair_renames(
        {("track", "purchase:success"): None},
        {("track", "purchase:success"): None},
        {("track", "purchase:completed"): None},
    )
    assert renames == {}


def test_pair_renames_leaves_a_source_name_shared_by_two_candidates_unpaired() -> None:
    """An identity two rows claim on one side identifies neither of them.

    Live rows cannot do this any more — ``uq_variable_project_source_name`` and
    ``uq_event_scan_identity`` make an identity singular per branch and per type
     — so these shapes now come only from a base payload snapshotted
    before the event constraint existed. ``pair_renames`` is pure over three
    mappings and cannot tell which side is live, and a payload is data, so the
    rule holds on every side.
    """
    two_could_have_been_renamed = pair_renames(
        {("track", "one"): "shared_raw", ("track", "two"): "shared_raw"},
        {("track", "one"): "shared_raw", ("track", "two"): "shared_raw"},
        {("track", "three"): "shared_raw"},
    )
    assert two_could_have_been_renamed == {}

    two_could_be_the_new_name = pair_renames(
        {("track", "one"): "shared_raw"},
        {("track", "one"): "shared_raw"},
        {("track", "two"): "shared_raw", ("track", "three"): "shared_raw"},
    )
    assert two_could_be_the_new_name == {}


def test_pair_renames_leaves_a_would_insert_with_no_would_delete_unpaired() -> None:
    """A genuinely added event has to keep merging as an addition."""
    assert pair_renames({}, {}, {("track", "signup:done"): "signup_done_raw"}) == {}


def test_pair_renames_reads_a_move_to_another_event_type_as_what_it_is() -> None:
    """The renameable component is the last one; the scope above it may not move."""
    renames = pair_renames(
        {("track", "purchase:success"): "purchase_success_raw"},
        {("track", "purchase:success"): "purchase_success_raw"},
        {("identify", "purchase:success"): "purchase_success_raw"},
    )
    assert renames == {}


# --- pair_renames: the cycles a name-keyed diff could not see ---


def test_pair_renames_pairs_a_two_row_swap_as_the_two_renames_it_is() -> None:
    """No name enters or leaves the branch, so nothing looks moved by name."""
    unchanged = {("variant",): "variant_raw", ("bucket",): "bucket_raw"}
    renames = pair_renames(
        unchanged,
        unchanged,
        {("bucket",): "variant_raw", ("variant",): "bucket_raw"},
    )
    assert renames == {("variant",): ("bucket",), ("bucket",): ("variant",)}


def test_pair_renames_pairs_a_rotation_of_three() -> None:
    """The result is a permutation, which is why callers re-key all at once."""
    unchanged = {("a",): "a_raw", ("b",): "b_raw", ("c",): "c_raw"}
    renames = pair_renames(
        unchanged,
        unchanged,
        {("b",): "a_raw", ("c",): "b_raw", ("a",): "c_raw"},
    )
    assert renames == {("a",): ("b",), ("b",): ("c",), ("c",): ("a",)}


def test_pair_renames_refuses_a_move_onto_a_name_a_staying_row_still_holds() -> None:
    """Main grew its own 'bucket' while the branch renamed 'variant' into it.

    Honouring the rename would put two main rows on one name, so this stays a
    delete plus an insert and conflict detection keeps its say. An occupied
    destination is only harmless when its occupant is itself moving away, which
    is exactly what makes the swap above legal and this illegal.
    """
    renames = pair_renames(
        {("variant",): "variant_raw"},
        {("variant",): "variant_raw", ("bucket",): "bucket_raw"},
        {("bucket",): "variant_raw"},
    )
    assert renames == {}


def test_pair_renames_leaves_a_row_missing_from_the_base_unpaired() -> None:
    """A rename moves a row that existed when the branch was cut, not any row."""
    renames = pair_renames(
        {},
        {("variant",): "variant_raw"},
        {("bucket",): "variant_raw"},
    )
    assert renames == {}


def test_pair_renames_ignores_a_main_row_the_base_never_had_when_reading_identities() -> None:
    """A duplicate main grew after the cut must not veto a real rename.

    When this was written Event carried a plain index on ``source_name``, so two
    live main events could share one, and here only the older of the two was
    there when the branch was cut. That newer row can never BE the rename
    source, because the pairing refuses an old key the base does not hold;
    letting it make the identity look ambiguous therefore costs the pair and
    nothing else. The rename then falls back to a delete plus an insert, and the
    cascade takes ``variable_values``, their drift rows and ``event_changes``
    with it.

    ``uq_event_scan_identity`` has since made the shape impossible for live main
    rows. The test stays because what it pins is the ORDER of the
    two steps — narrow main to the base, then count — which the function owes
    every caller regardless of where its mappings came from.
    """
    renames = pair_renames(
        {("track", "purchase"): "purchase_raw"},
        {("track", "purchase"): "purchase_raw", ("track", "purchase_copy"): "purchase_raw"},
        {("track", "checkout"): "purchase_raw"},
    )
    assert renames == {("track", "purchase"): ("track", "checkout")}


def test_pair_renames_still_refuses_two_base_rows_sharing_one_identity() -> None:
    """Narrowing main to the base is not the same as trusting main.

    Both candidates were already there when the branch was cut, so either could
    be the row the branch renamed. A guess renames the wrong one, which is worse
    than the deletion the pairing set out to avoid.

    A base naming one identity twice under one type is now only a snapshot taken
    before ``uq_event_scan_identity`` existed; such a payload is
    data, not a constraint, and this is the rule that keeps it from being
    guessed at.
    """
    both = {("track", "purchase"): "purchase_raw", ("track", "purchase_2"): "purchase_raw"}
    assert pair_renames(both, both, {("track", "checkout"): "purchase_raw"}) == {}


def test_rekey_in_place_moves_a_whole_cycle_without_fusing_two_rows() -> None:
    """One pair at a time, the first move's row is read straight back as the second's."""
    rows = {"variant": "row_for_variant", "bucket": "row_for_bucket"}
    rekey_in_place(rows, {"variant": "bucket", "bucket": "variant"})
    assert rows == {"bucket": "row_for_variant", "variant": "row_for_bucket"}


# --- merge policy: min approvals + self-approval guard ---------


async def _seed_second_user(email: str, slug: str) -> uuid.UUID:
    """Insert an extra user directly — the auth endpoints would switch the
    client's session to the new user, which these tests don't want. The user is
    made an editor member of ``slug``: approvers are project members."""
    user_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        session.add(User(id=user_id, email=email, password_hash="!seed"))
        await session.commit()
    await add_member_by_slug(slug, email, "editor")
    return user_id


async def _add_approval(branch_id: str, user_id: uuid.UUID) -> None:
    """Insert an approval stamped fresh for the branch's CURRENT content —
    mirrors what the approve transition records for another user."""
    async with TestSessionLocal() as session:
        branch = await session.get(PlanBranch, uuid.UUID(branch_id))
        assert branch is not None
        payload = await build_plan_snapshot(session, branch.project_id, branch_id=branch.id)
        session.add(
            PlanBranchApproval(
                branch_id=uuid.UUID(branch_id),
                user_id=user_id,
                plan_hash=plan_snapshot_hash(payload),
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_branch_settings_defaults_and_update(client: AsyncClient) -> None:
    await _seed_plan(client, "branch-policy")

    resp = await client.get("/api/v1/projects/branch-policy/branch-settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["min_approvals"] == 1
    assert body["block_self_approval"] is False

    updated = await client.patch(
        "/api/v1/projects/branch-policy/branch-settings",
        json={"min_approvals": 2, "block_self_approval": True},
    )
    assert updated.status_code == 200
    assert updated.json()["min_approvals"] == 2
    assert updated.json()["block_self_approval"] is True

    # Partial update keeps the other field.
    partial = await client.patch(
        "/api/v1/projects/branch-policy/branch-settings",
        json={"min_approvals": 3},
    )
    assert partial.status_code == 200
    assert partial.json()["block_self_approval"] is True

    bad = await client.patch(
        "/api/v1/projects/branch-policy/branch-settings",
        json={"min_approvals": -1},
    )
    assert bad.status_code == 422

    # Explicit JSON null is not a valid value for a NOT NULL column — it's
    # treated as "field omitted", not as a write.
    nulled = await client.patch(
        "/api/v1/projects/branch-policy/branch-settings",
        json={"min_approvals": None},
    )
    assert nulled.status_code == 200
    assert nulled.json()["min_approvals"] == 3


@pytest.mark.asyncio
async def test_branch_settings_get_does_not_write(client: AsyncClient) -> None:
    """GET must stay read-only: defaults come back without a row being created."""
    await _seed_plan(client, "branch-policy-ro")

    resp = await client.get("/api/v1/projects/branch-policy-ro/branch-settings")
    assert resp.status_code == 200
    assert resp.json()["min_approvals"] == 1
    assert resp.json()["id"] is None

    async with TestSessionLocal() as session:
        rows = (await session.execute(select(ProjectBranchSettings))).scalars().all()
        assert all(str(r.project_id) != resp.json()["project_id"] for r in rows)


@pytest.mark.asyncio
async def test_approve_stacks_from_approved_status(client: AsyncClient) -> None:
    """approve is legal from 'approved' so later reviewers can add approvals
    toward the quota; a repeat approve by the same user stays idempotent."""
    await _seed_plan(client, "branch-stack")
    branch_id = await _create_branch(client, "branch-stack")
    await _transition(client, "branch-stack", branch_id, "submit")

    first = await _transition(client, "branch-stack", branch_id, "approve")
    assert first["status"] == "approved"
    assert len(first["approvals"]) == 1

    again = await _transition(client, "branch-stack", branch_id, "approve")
    assert again["status"] == "approved"
    assert len(again["approvals"]) == 1  # same user — upsert, not a duplicate


@pytest.mark.asyncio
async def test_merge_blocked_until_min_approvals_met(client: AsyncClient) -> None:
    await _seed_plan(client, "branch-minappr")
    patched = await client.patch(
        "/api/v1/projects/branch-minappr/branch-settings",
        json={"min_approvals": 2},
    )
    assert patched.status_code == 200

    branch_id = await _create_branch(client, "branch-minappr")
    await _transition(client, "branch-minappr", branch_id, "submit")
    detail = await _transition(client, "branch-minappr", branch_id, "approve")
    assert detail["status"] == "approved"

    blocked = await client.post(f"/api/v1/projects/branch-minappr/branches/{branch_id}/merge")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["insufficient_approvals"] == {
        "required": 2,
        "current": 1,
        "stale": 0,
    }

    second_user = await _seed_second_user("approver2@example.com", "branch-minappr")
    await _add_approval(branch_id, second_user)

    merged = await client.post(f"/api/v1/projects/branch-minappr/branches/{branch_id}/merge")
    assert merged.status_code == 200
    assert merged.json()["status"] == "merged"


@pytest.mark.asyncio
async def test_merge_allows_zero_min_approvals(client: AsyncClient) -> None:
    """min_approvals=0 disables the quota — approved status alone gates merge."""
    await _seed_plan(client, "branch-zeroappr")
    await client.patch(
        "/api/v1/projects/branch-zeroappr/branch-settings",
        json={"min_approvals": 0},
    )
    branch_id = await _create_branch(client, "branch-zeroappr")
    resp = await _approve_and_merge(client, "branch-zeroappr", branch_id)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_self_approval_blocked_by_policy(client: AsyncClient) -> None:
    await _seed_plan(client, "branch-selfappr")
    await client.patch(
        "/api/v1/projects/branch-selfappr/branch-settings",
        json={"block_self_approval": True},
    )
    branch_id = await _create_branch(client, "branch-selfappr")
    await _transition(client, "branch-selfappr", branch_id, "submit")

    denied = await client.post(
        f"/api/v1/projects/branch-selfappr/branches/{branch_id}/transition",
        json={"action": "approve"},
    )
    assert denied.status_code == 403

    # The branch stays in review — the rejected approve must not flip status.
    detail = await client.get(f"/api/v1/projects/branch-selfappr/branches/{branch_id}")
    assert detail.json()["status"] == "ready_for_review"
    assert detail.json()["approvals"] == []


@pytest.mark.asyncio
async def test_stale_approval_blocks_merge_until_reapproved(client: AsyncClient) -> None:
    """Editing branch content after an approval voids it: the
    merge gate only counts approvals stamped for the current content."""
    await _seed_plan(client, "branch-stale")
    branch_id = await _create_branch(client, "branch-stale")
    await _transition(client, "branch-stale", branch_id, "submit")
    detail = await _transition(client, "branch-stale", branch_id, "approve")
    assert detail["status"] == "approved"

    # Author rewrites the branch AFTER the approval was recorded.
    await _touch_branch_event_type(branch_id)

    blocked = await client.post(f"/api/v1/projects/branch-stale/branches/{branch_id}/merge")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["insufficient_approvals"] == {
        "required": 1,
        "current": 0,
        "stale": 1,
    }

    # A fresh approve restamps the content hash and unblocks the merge.
    reapproved = await _transition(client, "branch-stale", branch_id, "approve")
    assert reapproved["status"] == "approved"
    merged = await client.post(f"/api/v1/projects/branch-stale/branches/{branch_id}/merge")
    assert merged.status_code == 200, merged.text


@pytest.mark.asyncio
async def test_photo_comment_does_not_void_an_approval(client: AsyncClient) -> None:
    """Discussion is not a plan change.

    A comment typed under a spec screenshot used to change the branch's
    snapshot hash and void every approval on it, so merge answered
    ``current=0, stale=1`` while author and reviewer both correctly insisted
    nobody had touched the plan.

    The complement is asserted too, because it is the line this fix draws:
    attaching a screenshot DOES change what a reviewer is being asked to
    approve, so it still voids the review.
    """
    await _seed_plan(client, "branch-photo-talk")
    events = await client.get("/api/v1/projects/branch-photo-talk/events")
    main_event_id = events.json()["items"][0]["id"]
    # TWO photos, deliberately: the snapshot orders them by canonical JSON of
    # the whole photo dict, in which "comments" sorts first, so a single photo
    # would not exercise the case where a new comment REORDERS the list and
    # changes the hash even after the field itself is stripped.
    for url, title in (
        ("https://www.figma.com/file/abc/Spec", "Spec"),
        ("https://www.figma.com/file/bcd/Flow", "Flow"),
    ):
        await _attach_main_figma(client, "branch-photo-talk", main_event_id, url, title)

    branch_id = await _create_branch(client, "branch-photo-talk")
    await _transition(client, "branch-photo-talk", branch_id, "submit")
    await _transition(client, "branch-photo-talk", branch_id, "approve")

    # The branch holds its own deep copies of the event and both photos.
    async with TestSessionLocal() as session:
        branch_event = (
            (await session.execute(select(Event).where(Event.branch_id == uuid.UUID(branch_id))))
            .scalars()
            .first()
        )
        assert branch_event is not None
        branch_photos = (
            (
                await session.execute(
                    select(EventPhoto)
                    .where(EventPhoto.event_id == branch_event.id)
                    .order_by(EventPhoto.external_url)
                )
            )
            .scalars()
            .all()
        )
        assert len(branch_photos) == 2
        branch_event_id = str(branch_event.id)
        # The .../abc/... photo, i.e. the one that sorts FIRST while both have
        # empty comment lists — `external_url` is the first key that differs.
        # Commenting on it pushes it behind the other under the unstripped sort
        # key, so this is the photo whose comment actually reorders the list.
        branch_photo_id = str(branch_photos[0].id)

    posted = await client.post(
        f"/api/v1/projects/branch-photo-talk/events/{branch_event_id}"
        f"/photos/{branch_photo_id}/comments",
        json={"body": "is this the final copy?"},
    )
    assert posted.status_code == 201, posted.text

    detail = await client.get(f"/api/v1/projects/branch-photo-talk/branches/{branch_id}")
    assert [a["stale"] for a in detail.json()["approvals"]] == [False]

    # Attaching another screenshot is a change to what is under review, and
    # must still void it.
    await _attach_main_figma(
        client,
        "branch-photo-talk",
        branch_event_id,
        "https://www.figma.com/file/def/Second",
        "Second",
    )
    after_upload = await client.get(f"/api/v1/projects/branch-photo-talk/branches/{branch_id}")
    assert [a["stale"] for a in after_upload.json()["approvals"]] == [True]

    await _transition(client, "branch-photo-talk", branch_id, "approve")
    merged = await client.post(f"/api/v1/projects/branch-photo-talk/branches/{branch_id}/merge")
    assert merged.status_code == 200, merged.text


@pytest.mark.asyncio
async def test_branch_detail_reports_approval_staleness(client: AsyncClient) -> None:
    """The detail response carries the freshness the merge gate scores on.

    Without it a client can only count rows, which is how a branch with one
    voided approval showed a green "Approvals 1/1" and then failed to merge
    with ``current=0`` — the contradiction that made the Approve button look
    broken when it was working correctly.
    """
    await _seed_plan(client, "branch-stale-detail")
    branch_id = await _create_branch(client, "branch-stale-detail")
    await _transition(client, "branch-stale-detail", branch_id, "submit")
    approved = await _transition(client, "branch-stale-detail", branch_id, "approve")
    assert [a["stale"] for a in approved["approvals"]] == [False]

    # Same edit as the merge-gate test above: the approval now describes content
    # the branch has moved past.
    await _touch_branch_event_type(branch_id)

    detail = await client.get(f"/api/v1/projects/branch-stale-detail/branches/{branch_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    # Status still reads "approved" — staleness is the only signal that the
    # review no longer covers the branch.
    assert body["status"] == "approved"
    assert [a["stale"] for a in body["approvals"]] == [True]

    # Re-approving clears it, matching what the merge gate then allows.
    refreshed = await _transition(client, "branch-stale-detail", branch_id, "approve")
    assert [a["stale"] for a in refreshed["approvals"]] == [False]


@pytest.mark.asyncio
async def test_merge_discards_author_approval_when_self_approval_blocked(
    client: AsyncClient,
) -> None:
    """An author approval recorded while the policy was off must not satisfy
    the merge gate after block_self_approval flips on."""
    await _seed_plan(client, "branch-selfappr-merge")
    branch_id = await _create_branch(client, "branch-selfappr-merge")
    await _transition(client, "branch-selfappr-merge", branch_id, "submit")
    detail = await _transition(client, "branch-selfappr-merge", branch_id, "approve")
    assert detail["status"] == "approved"

    await client.patch(
        "/api/v1/projects/branch-selfappr-merge/branch-settings",
        json={"block_self_approval": True},
    )

    blocked = await client.post(
        f"/api/v1/projects/branch-selfappr-merge/branches/{branch_id}/merge"
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["insufficient_approvals"] == {
        "required": 1,
        "current": 0,
        "stale": 0,
    }

    reviewer = await _seed_second_user("approver3@example.com", "branch-selfappr-merge")
    await _add_approval(branch_id, reviewer)

    merged = await client.post(f"/api/v1/projects/branch-selfappr-merge/branches/{branch_id}/merge")
    assert merged.status_code == 200


# --- variables: documented values / bindings / overrides round-trip


@pytest.mark.asyncio
async def test_branch_round_trip_carries_variable_values_bindings_and_overrides(
    client: AsyncClient,
) -> None:
    """Deep-copy, diff and merge must all carry the user-owned variable fields.

    Covers all four branch-machinery touchpoints: branch creation deep-copies
    allowed_values/bindings and override rows (with id remap), the snapshot/diff
    surfaces changes to them, and merge writes them back to main (overrides
    remapped to main event ids).
    """
    slug = "branch-var-docs"
    await _seed_plan(client, slug)

    async with TestSessionLocal() as session:
        project = (
            (await session.execute(select(Project).where(Project.slug == slug))).scalars().one()
        )
        main_event = (
            (
                await session.execute(
                    select(Event).where(
                        Event.project_id == project.id, Event.name == "purchase:success"
                    )
                )
            )
            .scalars()
            .one()
        )
        event_id = str(main_event.id)

    created = await client.post(
        f"/api/v1/projects/{slug}/variables",
        json={
            "name": "variant",
            "allowed_values": ["a", "b"],
            "bindings": ["page_data.extra.variant"],
        },
    )
    assert created.status_code == 201
    main_var_id = uuid.UUID(created.json()["id"])
    put = await client.put(
        f"/api/v1/projects/{slug}/variables/{main_var_id}/event-overrides/{event_id}",
        json={"values": ["a"]},
    )
    assert put.status_code == 200

    branch_id = await _create_branch(client, slug)
    branch_uuid = uuid.UUID(branch_id)

    # Deep-copy carried fields + override (remapped to the branch's copies).
    async with TestSessionLocal() as session:
        branch_var = (
            (
                await session.execute(
                    select(Variable).where(
                        Variable.branch_id == branch_uuid, Variable.name == "variant"
                    )
                )
            )
            .scalars()
            .one()
        )
        assert branch_var.allowed_values == ["a", "b"]
        assert branch_var.bindings == ["page_data.extra.variant"]
        branch_override = (
            (
                await session.execute(
                    select(VariableEventValueOverride).where(
                        VariableEventValueOverride.branch_id == branch_uuid
                    )
                )
            )
            .scalars()
            .one()
        )
        assert branch_override.variable_id == branch_var.id
        assert branch_override.values == ["a"]

    # Edit documented values on the branch through the API (?branch= scoping).
    upd = await client.patch(
        f"/api/v1/projects/{slug}/variables/{branch_var.id}?branch={branch_id}",
        json={"allowed_values": ["a", "b", "c"], "bindings": ["page_data.extra.variant"]},
    )
    assert upd.status_code == 200
    put2 = await client.put(
        f"/api/v1/projects/{slug}/variables/{branch_var.id}/event-overrides/"
        f"{branch_override.event_id}?branch={branch_id}",
        json={"values": ["a", "c"]},
    )
    assert put2.status_code == 200

    # Diff must surface the changed variable with the new change keys.
    diff = await client.get(f"/api/v1/projects/{slug}/branches/{branch_id}/diff")
    assert diff.status_code == 200
    var_entries = [e for e in diff.json()["entries"] if e["entity_type"] == "variable"]
    assert len(var_entries) == 1
    assert var_entries[0]["kind"] == "changed"
    changes = var_entries[0]["changes"]
    assert any(c.startswith("allowed_values") for c in changes)
    assert any(c.startswith("event_value_overrides") for c in changes)

    resp = await _approve_and_merge(client, slug, branch_id)
    assert resp.status_code == 200, resp.text

    # Merge wrote branch values back to main; override remapped to main event.
    async with TestSessionLocal() as session:
        merged_var = (
            (await session.execute(select(Variable).where(Variable.id == main_var_id)))
            .scalars()
            .one()
        )
        assert merged_var.allowed_values == ["a", "b", "c"]
        assert merged_var.bindings == ["page_data.extra.variant"]
        main_override = (
            (
                await session.execute(
                    select(VariableEventValueOverride).where(
                        VariableEventValueOverride.branch_id == merged_var.branch_id
                    )
                )
            )
            .scalars()
            .one()
        )
        assert main_override.variable_id == main_var_id
        assert main_override.event_id == uuid.UUID(event_id)
        assert main_override.values == ["a", "c"]


@pytest.mark.asyncio
async def test_branch_round_trip_preserves_field_value_authorship(
    client: AsyncClient,
) -> None:
    """Deep-copy and merge must carry is_authored so scans keep skipping the value.

    If either copy path recreated EventFieldValue rows without the flag, a
    hand-authored value would silently become unauthored after branching or
    merging — and the next scan would overwrite it.
    """
    slug = "branch-fv-authored"
    await _seed_plan(client, slug)

    async with TestSessionLocal() as session:
        project = (
            (await session.execute(select(Project).where(Project.slug == slug))).scalars().one()
        )
        main_event = (
            (
                await session.execute(
                    select(Event).where(
                        Event.project_id == project.id, Event.name == "purchase:success"
                    )
                )
            )
            .scalars()
            .one()
        )
        field = (
            (
                await session.execute(
                    select(FieldDefinition).where(
                        FieldDefinition.event_type_id == main_event.event_type_id
                    )
                )
            )
            .scalars()
            .one()
        )
        session.add(
            EventFieldValue(
                event_id=main_event.id,
                field_definition_id=field.id,
                value="${variant}",
                is_authored=True,
            )
        )
        await session.commit()
        main_event_id = main_event.id

    branch_id = await _create_branch(client, slug)
    branch_uuid = uuid.UUID(branch_id)

    async with TestSessionLocal() as session:
        branch_event = (
            (
                await session.execute(
                    select(Event).where(
                        Event.branch_id == branch_uuid, Event.name == "purchase:success"
                    )
                )
            )
            .scalars()
            .one()
        )
        branch_fv = (
            (
                await session.execute(
                    select(EventFieldValue).where(EventFieldValue.event_id == branch_event.id)
                )
            )
            .scalars()
            .one()
        )
        # Deep-copy carried the authored flag onto the branch copy.
        assert branch_fv.is_authored is True
        # Touch something unrelated so merge hits the update-existing path.
        branch_event.description = "edited on branch"
        await session.commit()

    resp = await _approve_and_merge(client, slug, branch_id)
    assert resp.status_code == 200, resp.text

    async with TestSessionLocal() as session:
        merged_fv = (
            (
                await session.execute(
                    select(EventFieldValue).where(EventFieldValue.event_id == main_event_id)
                )
            )
            .scalars()
            .one()
        )
        # Merge rebuilt main's field values from the branch — flag preserved.
        assert merged_fv.value == "${variant}"
        assert merged_fv.is_authored is True


@pytest.mark.asyncio
async def test_branch_round_trip_carries_variable_exclusion_flag(client: AsyncClient) -> None:
    slug = "branch-var-excl"
    await _seed_plan(client, slug)
    created = await client.post(f"/api/v1/projects/{slug}/variables", json={"name": "junk_var"})
    main_var_id = uuid.UUID(created.json()["id"])

    branch_id = await _create_branch(client, slug)
    branch_uuid = uuid.UUID(branch_id)

    async with TestSessionLocal() as session:
        branch_var = (
            (
                await session.execute(
                    select(Variable).where(
                        Variable.branch_id == branch_uuid, Variable.name == "junk_var"
                    )
                )
            )
            .scalars()
            .one()
        )
        # Deep-copy carried the (false) flag; exclude on the branch.
        assert branch_var.excluded_from_scans is False

    resp = await client.patch(
        f"/api/v1/projects/{slug}/variables/{branch_var.id}?branch={branch_id}",
        json={"excluded_from_scans": True},
    )
    assert resp.status_code == 200

    # Diff surfaces the exclusion as a variable change.
    diff = await client.get(f"/api/v1/projects/{slug}/branches/{branch_id}/diff")
    var_entries = [e for e in diff.json()["entries"] if e["entity_type"] == "variable"]
    assert len(var_entries) == 1
    assert any(c.startswith("excluded_from_scans") for c in var_entries[0]["changes"])

    merged = await _approve_and_merge(client, slug, branch_id)
    assert merged.status_code == 200, merged.text

    async with TestSessionLocal() as session:
        main_var = await session.get(Variable, main_var_id)
        assert main_var.excluded_from_scans is True


# --- reverting the removed half of a rename ---------------------


@pytest.mark.asyncio
async def test_revert_of_a_renamed_variable_moves_the_name_back(client: AsyncClient) -> None:
    """Reverting the "removed" entry of a rename must not 500, and must not insert.

    The diff keys variables by name, so renaming one on a branch reads as a
    removal of the old name plus an addition of the new one. Rebuilding the
    removed row from the base snapshot puts a SECOND row on the branch carrying
    ``source_name``, which ``uq_variable_project_source_name`` rejects at commit
    — and with no handler anywhere the reviewer got a bare 500.

    The row is moved, not replaced, so its id survives; the observed values,
    per-event overrides and drift history hanging off that id survive with it.
    """
    slug = "revert-var-rename"
    await _seed_plan(client, slug)
    created = await client.post(f"/api/v1/projects/{slug}/variables", json={"name": "plan_tier"})
    assert created.status_code == 201

    async with TestSessionLocal() as session:
        main_var = (
            (await session.execute(select(Variable).where(Variable.name == "plan_tier")))
            .scalars()
            .one()
        )
        # As a scan would have stamped it: the identity a rename cannot move.
        main_var.source_name = "plan_tier_raw"
        await session.commit()

    branch_id = await _create_branch(client, slug)
    branch_uuid = uuid.UUID(branch_id)

    async with TestSessionLocal() as session:
        branch_var = (
            (
                await session.execute(
                    select(Variable).where(
                        Variable.branch_id == branch_uuid, Variable.name == "plan_tier"
                    )
                )
            )
            .scalars()
            .one()
        )
        branch_var_id = branch_var.id
        # The deep copy carried the identity across, which is what makes the row
        # recognisable as the same one after the rename.
        assert branch_var.source_name == "plan_tier_raw"

    renamed = await client.patch(
        f"/api/v1/projects/{slug}/variables/{branch_var_id}?branch={branch_id}",
        json={"name": "subscription_tier"},
    )
    assert renamed.status_code == 200, renamed.text

    diff = await client.get(f"/api/v1/projects/{slug}/branches/{branch_id}/diff")
    split = {
        (e["kind"], e["name"]) for e in diff.json()["entries"] if e["entity_type"] == "variable"
    }
    assert ("removed", "plan_tier") in split
    assert ("added", "subscription_tier") in split

    reverted = await client.post(
        f"/api/v1/projects/{slug}/branches/{branch_id}/revert",
        json={"entity_type": "variable", "name": "plan_tier"},
    )
    assert reverted.status_code == 200, reverted.text
    # The rename is gone from the diff entirely — not replaced by a removal of
    # the new name, which is what a delete-and-rebuild would have left behind.
    assert [e for e in reverted.json()["entries"] if e["entity_type"] == "variable"] == []

    async with TestSessionLocal() as session:
        branch_vars = (
            (await session.execute(select(Variable).where(Variable.branch_id == branch_uuid)))
            .scalars()
            .all()
        )
        assert [(v.id, v.name, v.source_name) for v in branch_vars] == [
            (branch_var_id, "plan_tier", "plan_tier_raw")
        ]


@pytest.mark.asyncio
async def test_revert_of_a_renamed_event_does_not_duplicate_the_scan_identity(
    client: AsyncClient,
) -> None:
    """The same revert on an event, which once had nothing in the database saying no.

    Events carried only an index on ``(project, event_type, source_name)``, so
    rebuilding the removed half of a rename succeeded and left the branch with
    two events claiming one scan identity: the next scan matched warehouse data
    onto whichever it found, and ``pair_renames`` refused that identity for ever
    after because two candidates make the pairing a guess. No error, no diff
    entry, nothing to see — which is why it is tested.
    ``uq_event_scan_identity`` now refuses that second row outright,
    so without the rename reading this revert would 409 instead of corrupting;
    the assertion is unchanged because the rename is still the answer that keeps
    the row and everything hanging off it.
    """
    slug = "revert-event-rename"
    await _seed_plan(client, slug)

    async with TestSessionLocal() as session:
        main_event = (
            (await session.execute(select(Event).where(Event.name == "purchase:success")))
            .scalars()
            .one()
        )
        main_event.source_name = "purchase_success_raw"
        await session.commit()

    branch_id = await _create_branch(client, slug)
    branch_uuid = uuid.UUID(branch_id)

    async with TestSessionLocal() as session:
        branch_event = (
            (
                await session.execute(
                    select(Event).where(
                        Event.branch_id == branch_uuid, Event.name == "purchase:success"
                    )
                )
            )
            .scalars()
            .one()
        )
        branch_event_id = branch_event.id

    renamed = await client.patch(
        f"/api/v1/projects/{slug}/events/{branch_event_id}?branch={branch_id}",
        json={"name": "purchase:completed"},
    )
    assert renamed.status_code == 200, renamed.text

    reverted = await client.post(
        f"/api/v1/projects/{slug}/branches/{branch_id}/revert",
        json={"entity_type": "event", "name": "purchase:success", "parent": "track"},
    )
    assert reverted.status_code == 200, reverted.text

    async with TestSessionLocal() as session:
        branch_events = (
            (await session.execute(select(Event).where(Event.branch_id == branch_uuid)))
            .scalars()
            .all()
        )
        # One row, and the SAME row: a second one here is the silent corruption.
        assert [(e.id, e.name, e.source_name) for e in branch_events] == [
            (branch_event_id, "purchase:success", "purchase_success_raw")
        ]


async def _stored_event_values(
    branch_id: uuid.UUID, event_name: str
) -> tuple[list[str], list[str]]:
    """One branch's copy of an event, as the values actually stored on it.

    Read out of the database rather than off a response body, because what a
    revert has to put right is the stored text and a 200 says nothing about it.
    """
    async with TestSessionLocal() as session:
        event = (
            (
                await session.execute(
                    select(Event).where(Event.branch_id == branch_id, Event.name == event_name)
                )
            )
            .scalars()
            .one()
        )
        field_values = (
            (
                await session.execute(
                    select(EventFieldValue.value).where(EventFieldValue.event_id == event.id)
                )
            )
            .scalars()
            .all()
        )
        meta_values = (
            (
                await session.execute(
                    select(EventMetaValue.value).where(EventMetaValue.event_id == event.id)
                )
            )
            .scalars()
            .all()
        )
    return sorted(field_values), sorted(meta_values)


@pytest.mark.asyncio
async def test_reverting_a_variable_rename_puts_the_token_back_in_the_values(
    client: AsyncClient,
) -> None:
    """Undoing a rename has to undo the reference rewrite the rename performed.

    Saving a rename rewrites every ``${old_name}`` on the branch to
    ``${new_name}``, in both value tables (``variable_service``). The
    rename-aware revert wrote only the row's ``name``, so the variable came back
    answering to the base name while every field and meta value on the branch
    still said ``${new_name}`` — a token no variable answers to, which
    ``event_service._attach_template_warnings`` renders as "Unknown variable
    token" on each affected event and which a merge then carries to main. The
    dialog offering the revert promises the opposite: that the variable's
    documented values and history are untouched.
    """
    slug = "revert-var-rename-tokens"
    et_id = await _seed_plan(client, slug)
    tier_field = await client.post(
        f"/api/v1/projects/{slug}/event-types/{et_id}/fields",
        json={"name": "tier", "display_name": "Tier", "field_type": "string"},
    )
    assert tier_field.status_code == 201
    meta_field = await client.post(
        f"/api/v1/projects/{slug}/meta-fields",
        json={"name": "owner_note", "display_name": "Owner note", "field_type": "string"},
    )
    assert meta_field.status_code == 201
    created = await client.post(f"/api/v1/projects/{slug}/variables", json={"name": "plan_tier"})
    assert created.status_code == 201
    event = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": et_id,
            "name": "checkout:start",
            "field_values": [
                {"field_definition_id": tier_field.json()["id"], "value": "tier=${plan_tier}"}
            ],
            "meta_values": [
                {
                    "meta_field_definition_id": meta_field.json()["id"],
                    "value": "owned while ${plan_tier} is set",
                }
            ],
        },
    )
    assert event.status_code == 201, event.text

    main_branch = await _main_branch_id()
    async with TestSessionLocal() as session:
        main_var = (
            (await session.execute(select(Variable).where(Variable.name == "plan_tier")))
            .scalars()
            .one()
        )
        # As a scan would have stamped it: the identity that makes the branch row
        # recognisable as the same row once its name has moved.
        main_var.source_name = "plan_tier_raw"
        await session.commit()

    branch_id = await _create_branch(client, slug)
    branch_uuid = uuid.UUID(branch_id)
    async with TestSessionLocal() as session:
        branch_var_id = (
            (
                await session.execute(
                    select(Variable).where(
                        Variable.branch_id == branch_uuid, Variable.name == "plan_tier"
                    )
                )
            )
            .scalars()
            .one()
        ).id

    renamed = await client.patch(
        f"/api/v1/projects/{slug}/variables/{branch_var_id}?branch={branch_id}",
        json={"name": "subscription_tier"},
    )
    assert renamed.status_code == 200, renamed.text
    # The state the revert has to undo, asserted so that a change to the SAVE
    # side cannot quietly empty the assertions below.
    assert await _stored_event_values(branch_uuid, "checkout:start") == (
        ["tier=${subscription_tier}"],
        ["owned while ${subscription_tier} is set"],
    )

    reverted = await client.post(
        f"/api/v1/projects/{slug}/branches/{branch_id}/revert",
        json={"entity_type": "variable", "name": "plan_tier"},
    )
    assert reverted.status_code == 200, reverted.text
    # Nothing of the rename survives in the diff. Undoing only the name left the
    # EVENT carrying a ``changed`` entry for its field and meta values — a change
    # nobody made, on its way to main.
    assert [
        entry
        for entry in reverted.json()["entries"]
        if entry["entity_type"] in ("variable", "event")
    ] == []

    async with TestSessionLocal() as session:
        branch_vars = (
            (await session.execute(select(Variable).where(Variable.branch_id == branch_uuid)))
            .scalars()
            .all()
        )
        # Still the same row, moved and not rebuilt.
        assert [(v.id, v.name) for v in branch_vars] == [(branch_var_id, "plan_tier")]

    # The half the revert used to leave behind: BOTH value tables name the
    # restored variable again, so nothing on the branch renders as an unknown
    # token and a merge carries no broken template to main.
    assert await _stored_event_values(branch_uuid, "checkout:start") == (
        ["tier=${plan_tier}"],
        ["owned while ${plan_tier} is set"],
    )
    # And main never moved either way, which is what makes the rewrite — in both
    # directions — a branch-scoped write.
    assert await _stored_event_values(main_branch, "checkout:start") == (
        ["tier=${plan_tier}"],
        ["owned while ${plan_tier} is set"],
    )


async def _name_one_identity_twice_in_the_base(
    branch_id: uuid.UUID, *, event_type_name: str, event_names: tuple[str, str], identity: str
) -> None:
    """Rewrite a branch's stored base snapshot so two events under one type claim one identity.

    Live rows cannot say this any more — ``uq_event_scan_identity`` refuses the
    second — but a ``PlanRevision`` payload is data, and one snapshotted before
    the constraint existed can still say it. The column is plain
    JSON with no mutation tracking, so the payload is replaced, not edited.
    """
    async with TestSessionLocal() as session:
        branch = await session.get(PlanBranch, branch_id)
        assert branch is not None and branch.base_revision_id is not None
        revision = await session.get(PlanRevision, branch.base_revision_id)
        assert revision is not None
        events = [
            {**item, "source_name": identity}
            if item["event_type_name"] == event_type_name and item["name"] in event_names
            else item
            for item in revision.payload["events"]
        ]
        assert sum(1 for item in events if item.get("source_name") == identity) == 2
        revision.payload = {**revision.payload, "events": events}
        await session.commit()


@pytest.mark.asyncio
async def test_revert_is_not_a_rename_when_the_base_named_one_identity_twice(
    client: AsyncClient,
) -> None:
    """Ambiguity on the BASE side must stop the rename reading too.

    ``_row_renamed_from`` counted candidates on the branch and never asked what
    the base held. When that was fixed main could legitimately hold
    two events sharing one scan identity under one type — create
    ``checkout:start``, rename it, let the scan mint ``checkout:start`` again.
    ``uq_event_scan_identity`` has since made that impossible for live rows,
    so the shape is now reachable only through a base snapshot
    taken before the constraint existed; a ``PlanRevision`` payload is data, not
    a constraint, which is why the guard stays and why this seed writes the
    duplicate into the stored payload and leaves the live rows unique.

    Without the guard: exactly one branch row carries the identity, the
    branch-side count sees nothing wrong, and the revert renames the LIVE
    ``purchase:success`` to the deleted one's name — the deleted event never
    comes back and an unrelated one silently loses its name. With it, the
    removal is read as a removal and rebuilt from the snapshot, and the
    snapshot's copy wears the identity the live row holds, so the constraint
    refuses the rebuild and ``revert_change`` answers 409 with the branch left
    exactly as it was. Before the constraint that rebuild landed as a second row
    on one identity; a refused revert that names what to rename first is the
    honest replacement.
    """
    slug = "revert-event-shared-identity"
    et_id = await _seed_plan(client, slug)
    second = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": et_id, "name": "purchase:completed"},
    )
    assert second.status_code == 201

    async with TestSessionLocal() as session:
        main_event = (
            (await session.execute(select(Event).where(Event.name == "purchase:success")))
            .scalars()
            .one()
        )
        main_event.source_name = "purchase_raw"
        await session.commit()

    branch_id = await _create_branch(client, slug)
    branch_uuid = uuid.UUID(branch_id)
    await _name_one_identity_twice_in_the_base(
        branch_uuid,
        event_type_name="track",
        event_names=("purchase:success", "purchase:completed"),
        identity="purchase_raw",
    )
    async with TestSessionLocal() as session:
        branch_ids_by_name = {
            e.name: e.id
            for e in (
                (await session.execute(select(Event).where(Event.branch_id == branch_uuid)))
                .scalars()
                .all()
            )
        }

    deleted = await client.delete(
        f"/api/v1/projects/{slug}/events/{branch_ids_by_name['purchase:completed']}"
        f"?branch={branch_id}"
    )
    assert deleted.status_code == 204, deleted.text

    reverted = await client.post(
        f"/api/v1/projects/{slug}/branches/{branch_id}/revert",
        json={"entity_type": "event", "name": "purchase:completed", "parent": "track"},
    )
    assert reverted.status_code == 409, reverted.text
    detail = reverted.json()["detail"]
    assert "purchase:completed" in detail
    assert "scan identity" in detail

    async with TestSessionLocal() as session:
        rows = (
            (await session.execute(select(Event).where(Event.branch_id == branch_uuid)))
            .scalars()
            .all()
        )
    # The branch is exactly as it was: the row that was never deleted keeps its
    # name, its id and its identity, and nothing was rebuilt beside it. Reading
    # the removal as a rename left this one row wearing the wrong name.
    assert [(e.id, e.name, e.source_name) for e in rows] == [
        (branch_ids_by_name["purchase:success"], "purchase:success", "purchase_raw")
    ]


@pytest.mark.asyncio
async def test_building_a_plan_snapshot_does_not_hydrate_variable_contexts(
    client: AsyncClient,
) -> None:
    """The snapshot names no observed context, so it must not load any.

    ``Variable.value_contexts`` is ``lazy="selectin"`` and each context then
    selectin-loads its FieldDefinition, so a bare ``select(Variable)`` here
    pulled the project's entire context table into memory to serialise eight
    columns. A snapshot is built on every branch DIFF, not only on
    a merge, so this is a request path.
    """
    slug = "snapshot-no-contexts"
    await _seed_plan(client, slug)
    created = await client.post(f"/api/v1/projects/{slug}/variables", json={"name": "plan_tier"})
    assert created.status_code == 201
    variable_id = uuid.UUID(created.json()["id"])
    project_id = uuid.UUID(created.json()["project_id"])

    async with TestSessionLocal() as session, session.begin():
        event = (
            (await session.execute(select(Event).where(Event.project_id == project_id)))
            .scalars()
            .one()
        )
        field = (
            (await session.execute(select(FieldDefinition).where(FieldDefinition.name == "name")))
            .scalars()
            .one()
        )
        session.add(
            VariableValue(
                project_id=project_id,
                branch_id=event.branch_id,
                variable_id=variable_id,
                event_id=event.id,
                field_definition_id=field.id,
                source_column="name",
                value_kind="low",
                observed_count=1,
                values=["a"],
            )
        )

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(" ".join(statement.split()))

    # A fresh session, so nothing an earlier load left in the identity map can
    # satisfy a loader without a query.
    async with TestSessionLocal() as session:
        sa_event.listen(engine.sync_engine, "before_cursor_execute", _record)
        try:
            payload = await build_plan_snapshot(session, project_id)
        finally:
            sa_event.remove(engine.sync_engine, "before_cursor_execute", _record)

    assert [v["name"] for v in payload["variables"]] == ["plan_tier"]
    # Not one read of the table, in either shape the selectin loader emits. The
    # readers that DO want contexts issue their own select(VariableValue).
    assert [s for s in statements if "variable_values" in s] == [], statements


# --- the diff states which renames a merge will pair ------------


def test_snapshot_rename_pairs_reports_only_the_renames_the_diff_split() -> None:
    """``snapshot_rename_pairs`` joins up the two entries a rename is split into.

    Pure by construction — three snapshot payloads in, the pairing out — because
    it must answer exactly what ``pair_renames`` answers and no more, and that
    needs no database to check.
    """
    base = {
        "variables": [{"name": "plan_tier", "source_name": "plan_tier_raw"}],
        "events": [
            {"event_type_name": "track", "name": "purchase:success", "source_name": "purchase_raw"}
        ],
    }
    branch = {
        "variables": [{"name": "subscription_tier", "source_name": "plan_tier_raw"}],
        "events": [
            {
                "event_type_name": "track",
                "name": "purchase:completed",
                "source_name": "purchase_raw",
            }
        ],
    }
    assert [pair.model_dump() for pair in snapshot_rename_pairs(base, base, branch)] == [
        {
            "entity_type": "event",
            "parent": "track",
            "removed_name": "purchase:success",
            "added_name": "purchase:completed",
        },
        {
            "entity_type": "variable",
            "parent": None,
            "removed_name": "plan_tier",
            "added_name": "subscription_tier",
        },
    ]


def test_snapshot_rename_pairs_is_silent_where_the_diff_shows_no_removal() -> None:
    """A swap is a pair the merge makes and the diff never splits.

    Both names are present on both sides, so the diff reports two ``changed``
    entries and there is no removal or addition to join up. Naming the pair
    anyway would point the UI at entries that are not in the response.
    """
    base = {
        "variables": [
            {"name": "plan_tier", "source_name": "s1"},
            {"name": "billing_tier", "source_name": "s2"},
        ]
    }
    branch = {
        "variables": [
            {"name": "billing_tier", "source_name": "s1"},
            {"name": "plan_tier", "source_name": "s2"},
        ]
    }
    assert pair_renames(
        {("plan_tier",): "s1", ("billing_tier",): "s2"},
        {("plan_tier",): "s1", ("billing_tier",): "s2"},
        {("billing_tier",): "s1", ("plan_tier",): "s2"},
    ) == {("plan_tier",): ("billing_tier",), ("billing_tier",): ("plan_tier",)}
    assert snapshot_rename_pairs(base, base, branch) == []


def test_snapshot_rename_pairs_follows_main_not_the_base() -> None:
    """The pairing the MERGE will make, which the base-vs-branch diff cannot see.

    Main independently grew a row on the name the branch's rename is moving to,
    so the merge refuses the move and performs a removal plus an addition. The
    diff still shows those two entries; reporting them as one rename would
    promise the reviewer an id — and the observed values hanging off it — that
    the merge is about to delete.
    """
    base = {"variables": [{"name": "plan_tier", "source_name": "plan_tier_raw"}]}
    main = {
        "variables": [
            {"name": "plan_tier", "source_name": "plan_tier_raw"},
            {"name": "subscription_tier", "source_name": "unrelated_raw"},
        ]
    }
    branch = {"variables": [{"name": "subscription_tier", "source_name": "plan_tier_raw"}]}

    assert snapshot_rename_pairs(base, main, branch) == []
    # Same branch, main untouched: now it is a rename the merge will pair.
    paired = snapshot_rename_pairs(base, base, branch)
    assert [pair.removed_name for pair in paired] == ["plan_tier"]


async def _event_id_by_name(client: AsyncClient, slug: str, name: str, branch: str = "") -> str:
    query = f"?branch={branch}" if branch else ""
    listed = await client.get(f"/api/v1/projects/{slug}/events{query}")
    return next(e["id"] for e in listed.json()["items"] if e["name"] == name)


@pytest.mark.asyncio
async def test_the_branch_copy_points_at_its_own_successor_not_at_mains(
    client: AsyncClient,
) -> None:
    """A self-FK is the one reference a deep copy can get wrong two ways.

    Left alone it would point at a MAIN row from a branch row — the only such
    reference in the whole copy — and built one-pass it would silently NULL any
    successor authored before the event it replaces, because the events SELECT
    is unordered.
    """
    slug = "branch-superseded"
    et_id = await _seed_plan(client, slug)
    # The successor is created SECOND here and FIRST in the sibling test below,
    # so between them both orderings are covered.
    successor = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": et_id, "name": "purchase:completed"},
    )
    assert successor.status_code == 201
    main_old_id = await _event_id_by_name(client, slug, "purchase:success")
    await client.patch(
        f"/api/v1/projects/{slug}/events/{main_old_id}",
        json={"status": "deprecated", "superseded_by_event_id": successor.json()["id"]},
    )

    branch_id = await _create_branch(client, slug)

    branch_old_id = await _event_id_by_name(client, slug, "purchase:success", branch_id)
    branch_new_id = await _event_id_by_name(client, slug, "purchase:completed", branch_id)
    assert branch_old_id != main_old_id

    copied = await client.get(f"/api/v1/projects/{slug}/events/{branch_old_id}?branch={branch_id}")
    assert copied.status_code == 200
    # Points INTO the branch, not back at main.
    assert copied.json()["superseded_by_event_id"] == branch_new_id
    assert copied.json()["superseded_by_event_id"] != successor.json()["id"]


@pytest.mark.asyncio
async def test_a_successor_authored_before_its_predecessor_survives_the_copy(
    client: AsyncClient,
) -> None:
    """The forward reference. `_seed_plan` creates `purchase:success` first, so
    naming an event created LATER as the successor of one created EARLIER puts
    the pointer ahead of its target in an unordered SELECT."""
    slug = "branch-superseded-forward"
    et_id = await _seed_plan(client, slug)
    successor = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": et_id, "name": "purchase:completed"},
    )
    main_old_id = await _event_id_by_name(client, slug, "purchase:success")
    # The EARLIER row names the LATER one.
    await client.patch(
        f"/api/v1/projects/{slug}/events/{main_old_id}",
        json={"superseded_by_event_id": successor.json()["id"]},
    )

    branch_id = await _create_branch(client, slug)
    branch_old_id = await _event_id_by_name(client, slug, "purchase:success", branch_id)
    branch_new_id = await _event_id_by_name(client, slug, "purchase:completed", branch_id)

    copied = await client.get(f"/api/v1/projects/{slug}/events/{branch_old_id}?branch={branch_id}")
    assert copied.json()["superseded_by_event_id"] == branch_new_id


@pytest.mark.asyncio
async def test_merging_a_successor_translates_the_pointer_onto_mains_own_rows(
    client: AsyncClient,
) -> None:
    """The merge must never copy this column: on a branch row it holds a BRANCH
    id, and writing that onto main would point one project's catalog at rows
    that only exist on a branch."""
    slug = "branch-superseded-merge"
    await _seed_plan(client, slug)
    main_old_id = await _event_id_by_name(client, slug, "purchase:success")
    branch_id = await _create_branch(client, slug)

    branch_et = await client.get(f"/api/v1/projects/{slug}/event-types?branch={branch_id}")
    branch_et_id = next(et["id"] for et in branch_et.json() if et["name"] == "track")
    # Both the successor and the pointer are authored on the branch, so main
    # learns the whole fact at merge time.
    created = await client.post(
        f"/api/v1/projects/{slug}/events?branch={branch_id}",
        json={"event_type_id": branch_et_id, "name": "purchase:completed"},
    )
    assert created.status_code == 201
    branch_old_id = await _event_id_by_name(client, slug, "purchase:success", branch_id)
    await client.patch(
        f"/api/v1/projects/{slug}/events/{branch_old_id}?branch={branch_id}",
        json={"status": "deprecated", "superseded_by_event_id": created.json()["id"]},
    )
    merged = await _approve_and_merge(client, slug, branch_id)
    assert merged.status_code == 200, merged.text

    main_new_id = await _event_id_by_name(client, slug, "purchase:completed")
    after = await client.get(f"/api/v1/projects/{slug}/events/{main_old_id}")
    assert after.json()["status"] == "deprecated"
    # Main's own row for the successor, which the merge created moments earlier.
    assert after.json()["superseded_by_event_id"] == main_new_id
    assert after.json()["superseded_by_event_id"] != created.json()["id"]


@pytest.mark.asyncio
async def test_a_renamed_event_splits_into_entries_pointing_at_opposite_sides(
    client: AsyncClient,
) -> None:
    """The case where following an entry's own id is a trap.

    A rename shows up as removed + added, and the two entries name DIFFERENT
    rows: the removed one carries main's id, because it comes from the base
    payload, and main's row still exists under the old name; the added one
    carries the branch copy — which is the same physical row the removal is
    talking about, renamed. A client that deep-links the removed entry lands on
    main's untouched event, not on the edit the reviewer is looking at, so the
    pairing has to be resolved rather than either id followed blindly.
    """
    slug = "branch-diff-rename-ids"
    await _seed_plan(client, slug)
    main_events = await client.get(f"/api/v1/projects/{slug}/events")
    main_event_id = next(
        e["id"] for e in main_events.json()["items"] if e["name"] == "purchase:success"
    )
    # `pair_renames` joins the two sides on their SCAN identity, which the API
    # never accepts — the same ORM stamp the variable rename test above needs,
    # and it has to land before the branch is cut so the deep copy carries it.
    async with TestSessionLocal() as session:
        main_event = await session.get(Event, uuid.UUID(main_event_id))
        assert main_event is not None
        main_event.source_name = "purchase_success_raw"
        await session.commit()
    branch_id = await _create_branch(client, slug)

    branch_events = await client.get(f"/api/v1/projects/{slug}/events?branch={branch_id}")
    branch_event_id = next(
        e["id"] for e in branch_events.json()["items"] if e["name"] == "purchase:success"
    )
    assert branch_event_id != main_event_id

    renamed = await client.patch(
        f"/api/v1/projects/{slug}/events/{branch_event_id}?branch={branch_id}",
        json={"name": "purchase:completed"},
    )
    assert renamed.status_code == 200

    diff = await client.get(f"/api/v1/projects/{slug}/branches/{branch_id}/diff")
    assert diff.status_code == 200
    body = diff.json()
    # The pairing REPORTS the rename; it does not collapse the two entries.
    # The pairing joins on scan identity, so it reports the rename here only
    # because `source_name` was stamped above; a hand-named event has none and
    # the two entries stand alone. Either way the pairing REPORTS — it never
    # collapses the entries.
    assert body["renames"] == [
        {
            "entity_type": "event",
            "parent": "track",
            "removed_name": "purchase:success",
            "added_name": "purchase:completed",
        }
    ]
    entries = {e["name"]: e for e in body["entries"]}
    removed = entries["purchase:success"]
    added = entries["purchase:completed"]
    assert removed["kind"] == "removed"
    assert added["kind"] == "added"
    assert removed["entity_id"] == main_event_id
    assert added["entity_id"] == branch_event_id
    # The inequality IS the hazard: the removed row names a live event on main,
    # not the branch copy that was renamed.
    assert removed["entity_id"] != branch_event_id


@pytest.mark.asyncio
async def test_branch_diff_names_the_rename_the_merge_will_pair(client: AsyncClient) -> None:
    """The endpoint carries the pairing, not just the pure function.

    ``snapshot_rename_pairs`` is unit-tested above against three payloads. This
    asserts the wiring: that ``diff_branch`` actually calls it with the base,
    main and branch snapshots, and that the pair survives serialisation. Without
    the call the field is a well-typed empty list and the UI is back to guessing.
    """
    slug = "diff-rename-pair"
    await _seed_plan(client, slug)
    created = await client.post(f"/api/v1/projects/{slug}/variables", json={"name": "plan_tier"})
    assert created.status_code == 201
    # Only a SCAN stamps ``source_name``; the API never accepts it, and it is the
    # identity the pairing joins on, so the row has to be given one by hand.
    async with TestSessionLocal() as session:
        var = (
            (await session.execute(select(Variable).where(Variable.name == "plan_tier")))
            .scalars()
            .one()
        )
        var.source_name = "plan_tier_raw"
        await session.commit()

    branch_id = await _create_branch(client, slug)
    async with TestSessionLocal() as session:
        branch_var = (
            (
                await session.execute(
                    select(Variable).where(
                        Variable.branch_id == uuid.UUID(branch_id),
                        Variable.name == "plan_tier",
                    )
                )
            )
            .scalars()
            .one()
        )
        branch_var_id = branch_var.id

    renamed = await client.patch(
        f"/api/v1/projects/{slug}/variables/{branch_var_id}?branch={branch_id}",
        json={"name": "subscription_tier"},
    )
    assert renamed.status_code == 200, renamed.text

    diff = await client.get(f"/api/v1/projects/{slug}/branches/{branch_id}/diff")
    assert diff.status_code == 200, diff.text
    body = diff.json()
    assert body["renames"] == [
        {
            "entity_type": "variable",
            "parent": None,
            "removed_name": "plan_tier",
            "added_name": "subscription_tier",
        }
    ]
    # The entries themselves are unchanged: the pairing REPORTS, it does not
    # rewrite the diff the reviewer reads.
    kinds = {(entry["kind"], entry["name"]) for entry in body["entries"]}
    assert ("removed", "plan_tier") in kinds
    assert ("added", "subscription_tier") in kinds


@pytest.mark.asyncio
async def test_diff_keeps_scan_housekeeping_out_of_the_reviewers_counts(
    client: AsyncClient,
) -> None:
    """A retired scan-minted variable is not one of the author's removals.

    On production a branch with two authored events read "+3 added −7 removed":
    the seven were variables a scan had minted from user-typed city names and
    later retired — scan provenance, nothing bound, nothing documented. The
    entries stay in the diff with a reason, and the counts a reviewer reads
    (``summary`` here, ``ahead`` on the list) count only the rest. A removal
    main has ALSO made since the branch was cut is housekeeping too: the merge
    has nothing to do for it.
    """
    from tripl.core.analyzers._event_generator_variables import SCAN_PROVENANCE_DESCRIPTION

    slug = "branch-housekeeping"
    await _seed_plan(client, slug)
    junk = await client.post(
        f"/api/v1/projects/{slug}/variables",
        json={"name": "property_adana", "description": SCAN_PROVENANCE_DESCRIPTION},
    )
    assert junk.status_code == 201, junk.text
    documented = await client.post(
        f"/api/v1/projects/{slug}/variables",
        json={
            "name": "property_city",
            "description": SCAN_PROVENANCE_DESCRIPTION,
            "allowed_values": ["adana", "ankara"],
        },
    )
    assert documented.status_code == 201, documented.text
    branch_id = await _create_branch(client, slug)

    branch_vars = (await client.get(f"/api/v1/projects/{slug}/variables?branch={branch_id}")).json()
    branch_rows = branch_vars["items"] if isinstance(branch_vars, dict) else branch_vars
    for row in branch_rows:
        gone = await client.delete(
            f"/api/v1/projects/{slug}/variables/{row['id']}?branch={branch_id}"
        )
        assert gone.status_code == 204, gone.text
    branch_ets = await client.get(f"/api/v1/projects/{slug}/event-types?branch={branch_id}")
    branch_et_id = next(et["id"] for et in branch_ets.json() if et["name"] == "track")
    added = await client.post(
        f"/api/v1/projects/{slug}/events?branch={branch_id}",
        json={"event_type_id": branch_et_id, "name": "checkout:started"},
    )
    assert added.status_code == 201, added.text

    diff = (await client.get(f"/api/v1/projects/{slug}/branches/{branch_id}/diff")).json()
    by_name = {e["name"]: e for e in diff["entries"]}
    assert by_name["property_adana"]["housekeeping"] == "unused scan variable retired"
    assert by_name["property_city"]["housekeeping"] is None, "documented values are a mark"
    assert by_name["checkout:started"]["housekeeping"] is None
    assert diff["summary"] == {"added": 1, "removed": 1, "changed": 0, "housekeeping": 1}

    # Main retires the documented one too: the branch no longer removes anything.
    gone_on_main = await client.delete(
        f"/api/v1/projects/{slug}/variables/{documented.json()['id']}"
    )
    assert gone_on_main.status_code == 204, gone_on_main.text
    diff = (await client.get(f"/api/v1/projects/{slug}/branches/{branch_id}/diff")).json()
    by_name = {e["name"]: e for e in diff["entries"]}
    assert by_name["property_city"]["housekeeping"] == "already removed on main"
    assert by_name["property_adana"]["housekeeping"] == "unused scan variable retired"
    assert diff["summary"] == {"added": 1, "removed": 0, "changed": 0, "housekeeping": 2}
    assert diff["behind_base"] is True

    # Once main retires the junk one as well, "already removed on main" is the
    # stronger statement — the merge does nothing for it — and wins.
    gone_junk = await client.delete(f"/api/v1/projects/{slug}/variables/{junk.json()['id']}")
    assert gone_junk.status_code == 204, gone_junk.text
    diff = (await client.get(f"/api/v1/projects/{slug}/branches/{branch_id}/diff")).json()
    by_name = {e["name"]: e for e in diff["entries"]}
    assert by_name["property_adana"]["housekeeping"] == "already removed on main"
    assert diff["summary"] == {"added": 1, "removed": 0, "changed": 0, "housekeeping": 2}

    listed = (await client.get(f"/api/v1/projects/{slug}/branches?include_diff_counts=true")).json()
    row = next(b for b in listed["items"] if b["id"] == branch_id)
    assert row["ahead"] == 1
