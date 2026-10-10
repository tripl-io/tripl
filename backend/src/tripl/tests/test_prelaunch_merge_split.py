"""``_apply_merge`` is an orchestrator over one arm per entity kind.

The arms moved out of ``plan_branch_merge_service`` into the
``_plan_branch_merge_*`` modules without changing what a merge does; the merge
suites are what pin that. Two things the split itself introduced are pinned
here, without a database:

* the order the arms run in, and what each one is handed. The order is part of
  the behaviour — an arm reads the ids and names the earlier ones wrote, and
  flush order decides which constraint a collision trips — so a reshuffle has
  to fail here rather than surface as a rare 409 on a real merge;
* that "Update from main" still parks renames through the merge's own pass.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tripl.services import (
    _plan_branch_merge_variables,
    _plan_branch_update_apply,
    plan_branch_merge_service,
)
from tripl.services._plan_branch_merge_state import MergeContext

_ARMS = (
    "apply_event_types",
    "apply_field_definitions",
    "apply_meta_fields",
    "apply_variables",
    "apply_events",
    "apply_photos",
    "_hand_over_event_discussions",
    "apply_value_overrides",
    "apply_relations",
)

_BLOB = ("local", "photos/a.png", None)


class _Session:
    """Answers the one read ``_apply_merge`` makes itself: the origin-ids flag."""

    async def scalar(self, _statement: Any) -> bool:
        return True


@pytest.mark.asyncio
async def test_the_arms_run_in_order_each_handed_what_the_earlier_ones_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    results: dict[str, object] = {}

    def arm(name: str) -> Any:
        async def run(*args: Any, **kwargs: Any) -> object:
            calls.append((name, args, kwargs))
            if name == "apply_photos":
                args[0].released_blobs.add(_BLOB)
            results[name] = object()
            return results[name]

        return run

    for name in _ARMS:
        monkeypatch.setattr(plan_branch_merge_service, name, arm(name))

    twins: dict[uuid.UUID, uuid.UUID | None] = {uuid.uuid4(): None}
    snapshot: dict[str, Any] = {"events": []}

    async def event_thread_twins(*_args: Any, **_kwargs: Any) -> Any:
        calls.append(("_event_thread_twins", (), {}))
        return twins

    async def build_plan_snapshot(_session: Any, _project_id: Any, *, branch_id: Any) -> Any:
        calls.append(("build_plan_snapshot", (branch_id,), {}))
        return snapshot

    monkeypatch.setattr(plan_branch_merge_service, "_event_thread_twins", event_thread_twins)
    monkeypatch.setattr(plan_branch_merge_service, "build_plan_snapshot", build_plan_snapshot)

    session = _Session()
    project_id, main_branch_id, branch_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    released = await plan_branch_merge_service._apply_merge(
        session, project_id, main_branch_id, branch_id
    )

    # The twins are read before anything writes, and the branch's snapshot too.
    assert [name for name, _, _ in calls] == ["_event_thread_twins", "build_plan_snapshot", *_ARMS]
    by_name = {name: (args, kwargs) for name, args, kwargs in calls}
    assert by_name["build_plan_snapshot"][0] == (branch_id,)

    ctx = by_name["apply_event_types"][0][0]
    assert isinstance(ctx, MergeContext)
    assert (ctx.session, ctx.project_id, ctx.main_branch_id, ctx.branch_id) == (
        session,
        project_id,
        main_branch_id,
        branch_id,
    )
    assert ctx.resolutions == {}
    assert ctx.base_payload == {}
    assert ctx.branch_origins_complete is True
    assert ctx.branch_snapshot_payload is snapshot

    event_types = results["apply_event_types"]
    fields = results["apply_field_definitions"]
    meta_fields = results["apply_meta_fields"]
    variables = results["apply_variables"]
    events = results["apply_events"]
    assert by_name["apply_field_definitions"] == ((ctx, event_types), {})
    assert by_name["apply_meta_fields"] == ((ctx,), {})
    assert by_name["apply_variables"] == ((ctx,), {})
    assert by_name["apply_events"] == ((ctx, event_types, fields, meta_fields), {})
    assert by_name["apply_photos"] == ((ctx, events), {})
    assert by_name["_hand_over_event_discussions"] == (
        (session,),
        {"thread_twins": twins, "events": events},
    )
    assert by_name["apply_value_overrides"] == ((ctx, variables, events), {})
    assert by_name["apply_relations"] == ((ctx, event_types, fields), {})

    # What any arm adds to the shared set is what the merge releases after commit.
    assert released == frozenset({_BLOB})


def test_update_from_main_parks_renames_through_the_merges_own_pass() -> None:
    """A rename cycle settles on a branch exactly as it does on main.

    "Update from main" imports the parking pass rather than keeping one of its
    own, so the two cannot drift; it has to stay the merge's own function.
    """
    assert (
        _plan_branch_update_apply.rename_variables_with_parking
        is _plan_branch_merge_variables._rename_main_variables
    )
