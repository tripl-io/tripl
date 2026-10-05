"""The plan policy hook (``Extension.plan_policy_violations``) at its three phases.

Community answers no violations, so each phase is pinned with a fake extension:
``POST /plan/validate`` reports a call's violation as a ``policy_violation``
finding, a blocking violation refuses a merge with 409 ``policy_violations``
after the project's own gates, and a ``direct_edit`` violation refuses a write
to main while the same write on a branch, and that branch's merge, go through.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.core.plan_policy import PlanPolicyContext, PolicyViolation, refusal_detail
from tripl.extensions import Extension, override_extensions
from tripl.tests._members import add_member_by_slug


class _Policy(Extension):
    """Records every context; answers what each phase is told to."""

    name = "policy"

    def __init__(
        self,
        *,
        validate: Sequence[PolicyViolation] = (),
        merge: Sequence[PolicyViolation] = (),
        protect_main: bool = False,
    ) -> None:
        self.contexts: list[PlanPolicyContext] = []
        self.validate = list(validate)
        self.merge = list(merge)
        self.protect_main = protect_main

    async def plan_policy_violations(
        self, session: AsyncSession, context: PlanPolicyContext
    ) -> Sequence[PolicyViolation]:
        self.contexts.append(context)
        if context.phase == "validate":
            return self.validate
        if context.phase == "merge":
            return self.merge
        if self.protect_main:
            return [PolicyViolation(rule="protected_main", message="Main is protected.")]
        return ()


async def _seed(client: AsyncClient, slug: str) -> str:
    project = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert project.status_code == 201
    et = await client.post(
        f"/api/v1/projects/{slug}/event-types", json={"name": "track", "display_name": "Track"}
    )
    assert et.status_code == 201
    event = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": et.json()["id"], "name": "purchase"},
    )
    assert event.status_code == 201
    return str(et.json()["id"])


async def _branch(client: AsyncClient, slug: str) -> str:
    created = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "feature"})
    assert created.status_code == 201
    return str(created.json()["id"])


async def _approve(client: AsyncClient, slug: str, branch_id: str) -> None:
    for action in ("submit", "approve"):
        moved = await client.post(
            f"/api/v1/projects/{slug}/branches/{branch_id}/transition", json={"action": action}
        )
        assert moved.status_code == 200, moved.text


def test_refusal_detail_lists_every_violation() -> None:
    approver = uuid.uuid4()
    detail = refusal_detail(
        [
            PolicyViolation(rule="a", message="First."),
            PolicyViolation(rule="b", message="Second.", approver_ids=(approver,)),
        ]
    )
    assert detail["message"] == "Blocked by 2 plan rules: First. Second."
    assert [v["rule"] for v in detail["policy_violations"]] == ["a", "b"]
    assert detail["policy_violations"][1]["approver_ids"] == [str(approver)]


@pytest.mark.asyncio
async def test_validate_reports_policy_violations_on_the_named_call(client: AsyncClient) -> None:
    await _seed(client, "pol-validate")
    policy = _Policy(
        validate=[
            PolicyViolation(rule="forbidden", message="No email.", item_ref="b", field="email"),
            PolicyViolation(
                rule="style", message="Prefer snake.", severity="warning", item_ref="a"
            ),
            PolicyViolation(rule="orphan", message="Names no call."),
        ]
    )
    with override_extensions([policy]):
        resp = await client.post(
            "/api/v1/projects/pol-validate/plan/validate",
            json={
                "items": [
                    {"ref": "a", "event_type": "track", "name": "purchase"},
                    {
                        "ref": "b",
                        "event_type": "track",
                        "name": "purchase",
                        "properties": {"email": "x"},
                        "fields": {"plan": "pro"},
                        "complete": True,
                    },
                ]
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    a, b = body["items"]
    assert a["status"] == "warning"
    assert {"code": "policy_violation", "severity": "warning", "rule": "style"}.items() <= next(
        f for f in a["findings"] if f["code"] == "policy_violation"
    ).items()
    assert b["status"] == "error"
    policy_finding = next(f for f in b["findings"] if f["code"] == "policy_violation")
    assert policy_finding["field"] == "email"
    assert policy_finding["rule"] == "forbidden"
    assert body["summary"]["errors"] == 1

    (context,) = policy.contexts
    assert context.phase == "validate"
    assert context.project_slug == "pol-validate"
    assert context.branch_id is not None  # main, resolved
    call_b = context.calls[1]
    assert call_b.ref == "b"
    assert call_b.event_type == "track"
    assert set(call_b.field_names) == {"plan", "email"}
    assert call_b.complete is True


@pytest.mark.asyncio
async def test_validate_without_extensions_reports_no_policy_finding(
    client: AsyncClient,
) -> None:
    await _seed(client, "pol-none")
    with override_extensions([]):
        resp = await client.post(
            "/api/v1/projects/pol-none/plan/validate",
            json={"items": [{"ref": "a", "event_type": "track", "name": "purchase"}]},
        )
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["status"] == "ok"
    assert all(f["rule"] is None for f in item["findings"])


@pytest.mark.asyncio
async def test_a_blocking_policy_refuses_the_merge(client: AsyncClient) -> None:
    await _seed(client, "pol-merge")
    branch_id = await _branch(client, "pol-merge")
    await _approve(client, "pol-merge", branch_id)
    approver = uuid.uuid4()
    blocking = _Policy(
        merge=[
            PolicyViolation(
                rule="sensitive_approval",
                message="A sensitive field needs the privacy group's approval.",
                entity_type="field",
                entity="email",
                approver_ids=(approver,),
            ),
            PolicyViolation(rule="hint", message="Only a warning.", severity="warning"),
        ]
    )
    with override_extensions([blocking]):
        refused = await client.post(f"/api/v1/projects/pol-merge/branches/{branch_id}/merge")
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert [v["rule"] for v in detail["policy_violations"]] == ["sensitive_approval"]
    assert detail["policy_violations"][0]["approver_ids"] == [str(approver)]
    assert detail["message"].startswith("Blocked by 1 plan rule:")

    (context,) = blocking.contexts
    assert context.phase == "merge"
    assert str(context.branch_id) == branch_id
    assert context.base_snapshot is not None and context.branch_snapshot is not None
    assert context.author_id is not None
    assert context.author_id in context.approver_ids  # the self-approval, still fresh

    # Warnings alone never block.
    with override_extensions([_Policy(merge=[PolicyViolation("hint", "w", severity="warning")])]):
        merged = await client.post(f"/api/v1/projects/pol-merge/branches/{branch_id}/merge")
    assert merged.status_code == 200, merged.text
    assert merged.json()["status"] == "merged"


@pytest.mark.asyncio
async def test_a_protected_main_refuses_direct_edits_but_takes_a_branch_merge(
    client: AsyncClient,
) -> None:
    et_id = await _seed(client, "pol-main")
    branch_id = await _branch(client, "pol-main")
    policy = _Policy(protect_main=True)
    body = {"event_type_id": et_id, "name": "signup"}
    with override_extensions([policy]):
        refused = await client.post("/api/v1/projects/pol-main/events", json=body)
        assert refused.status_code == 409, refused.text
        payload = refused.json()
        assert payload["detail"].startswith("Blocked by 1 plan rule: Main is protected.")
        assert payload["policy_violations"][0]["rule"] == "protected_main"

        # Reads of main are never asked about.
        listed = await client.get("/api/v1/projects/pol-main/events")
        assert listed.status_code == 200

        branch_types = await client.get(f"/api/v1/projects/pol-main/event-types?branch={branch_id}")
        branch_et = branch_types.json()[0]["id"]
        on_branch = await client.post(
            f"/api/v1/projects/pol-main/events?branch={branch_id}",
            json={"event_type_id": branch_et, "name": "signup"},
        )
        assert on_branch.status_code == 201, on_branch.text
        await _approve(client, "pol-main", branch_id)
        merged = await client.post(f"/api/v1/projects/pol-main/branches/{branch_id}/merge")
        assert merged.status_code == 200, merged.text

    phases = [c.phase for c in policy.contexts]
    assert phases.count("direct_edit") == 1
    assert "merge" in phases
    direct = next(c for c in policy.contexts if c.phase == "direct_edit")
    assert direct.branch_id is None
    assert direct.actor_id is not None

    names = {
        e["name"] for e in (await client.get("/api/v1/projects/pol-main/events")).json()["items"]
    }
    assert "signup" in names


@pytest.mark.asyncio
async def test_a_viewer_gets_the_write_gate_403_not_the_policy_409(client: AsyncClient) -> None:
    """Authorization answers first: told to use a branch is no help to a caller
    who may not write at all."""
    et_id = await _seed(client, "pol-viewer")
    await client.post("/api/v1/auth/logout")
    registered = await client.post(
        "/api/v1/auth/register",
        json={"email": "pol-viewer@example.com", "password": "Password123!", "name": "V"},
    )
    assert registered.status_code == 201, registered.text
    await add_member_by_slug("pol-viewer", "pol-viewer@example.com", "viewer")
    with override_extensions([_Policy(protect_main=True)]):
        refused = await client.post(
            "/api/v1/projects/pol-viewer/events", json={"event_type_id": et_id, "name": "x"}
        )
    assert refused.status_code == 403, refused.text


@pytest.mark.asyncio
async def test_main_writes_pass_when_no_policy_blocks(client: AsyncClient) -> None:
    et_id = await _seed(client, "pol-open")
    with override_extensions([_Policy()]):
        created = await client.post(
            "/api/v1/projects/pol-open/events", json={"event_type_id": et_id, "name": "open"}
        )
    assert created.status_code == 201, created.text
