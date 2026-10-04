"""Row-level warnings on a branch diff that are not differences.

A diff says what changed between the base and the branch. It cannot say that
an ADDED event is broken in a way the merge will not fix — and on production
that was the case for every event authored on a branch before the naming rule
reached branches: no ``source_name``, a free-text ``name``, and
a merge that would land a row the scan can never match. The reviewer approving
the branch is the last person who can catch it, so the diff tells them.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.core.name_template import apply_name_format, resolve_dotted_keys
from tripl.models.event_type import EventType
from tripl.schemas.plan_revision import PlanDiffEntry
from tripl.services.scan_config_lookup import (
    governing_name_format,
    load_governing_scan_configs_by_type,
)


async def attach_identity_warnings(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    entries: list[PlanDiffEntry],
    branch_snapshot: dict[str, Any],
) -> None:
    """Warn on every added/changed event that has no scan identity but should.

    Three shapes, in the order an operator can act on them: the rule's
    placeholders are not all filled; the identity the rule derives is already
    held by another event on the branch; or the row was simply authored before
    the rule reached branches and carries the identity nowhere.
    """
    candidates = [
        entry
        for entry in entries
        if entry.entity_type == "event"
        and entry.kind in ("added", "changed")
        and entry.after is not None
        and entry.after.get("source_name") is None
    ]
    if not candidates:
        return
    type_rows = (
        await session.execute(
            select(EventType.id, EventType.name).where(
                EventType.project_id == project_id, EventType.branch_id == branch_id
            )
        )
    ).all()
    configs_by_type = await load_governing_scan_configs_by_type(
        session, project_id=project_id, event_type_ids=[type_id for type_id, _ in type_rows]
    )
    format_by_type_name = {
        name: governing_name_format(configs_by_type.get(type_id, [])) for type_id, name in type_rows
    }
    held: dict[tuple[str, str], str] = {}
    for event in branch_snapshot.get("events", []):
        identity = event.get("source_name") or event.get("name")
        if identity:
            held.setdefault((event.get("event_type_name", ""), identity), event.get("name", ""))

    for entry in candidates:
        after = entry.after or {}
        type_name = str(after.get("event_type_name", ""))
        fmt = format_by_type_name.get(type_name)
        if not fmt:
            continue
        values_by_field = {
            str(member.get("field_name")): str(member.get("value"))
            for member in after.get("field_values", [])
            if isinstance(member, dict) and member.get("value")
        }
        identity, missing = apply_name_format(fmt, resolve_dotted_keys(fmt, values_by_field))
        if missing:
            entry.warnings.append(
                f"No scan identity: the naming rule '{fmt}' needs "
                f"{', '.join(sorted(set(missing)))}. Fill those fields so the event "
                "merges with its scanned counterpart."
            )
        elif held.get((type_name, identity), entry.name) != entry.name:
            entry.warnings.append(
                f"No scan identity: '{identity}' is already held by "
                f"'{held[(type_name, identity)]}' on this branch."
            )
        else:
            entry.warnings.append(
                f"No scan identity: this event was authored without one; the naming rule "
                f"'{fmt}' derives '{identity}'. Recreate it so the rule stamps the identity."
            )
            # Claim the derived identity for the rows that follow: two label-named
            # rows deriving the same identity would otherwise both read as merely
            # "authored without one", and the reviewer would never learn that the
            # second can never be stamped while the first exists.
            held[(type_name, identity)] = entry.name
