"""@mentions in docs catalog notes (F24 part 2, GH #308).

The editor's people picker inserts ``[[user:<id>]]``, rendered ``@Name``. When a
note is SAVED through the editor or the API (``PUT /docs/file``), every person
mentioned in the new body but not in the previous one is told, once, through the
F06 notification center (kind ``mention``, about the note).

Only people who could open the link are told:

* a member of the note's project organization (a mention of anyone else is a
  broken link);
* who may READ the note (``docs_access.readers_among``): mentioning someone in
  a private note never tells them the note exists;
* and, as for every notification, a member of the path's project
  (``notification_service.notify``); the actor never notifies themselves.

An import, a mirror, a restore and a move never notify: they replay text, they
do not write a new mention.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.doc_file import DocFile
from tripl.models.organization import OrganizationMember
from tripl.models.project import Project
from tripl.models.user import User
from tripl.services import notification_announce, notification_service
from tripl.services.docs_access import readers_among
from tripl.services.docs_frontmatter import split_frontmatter
from tripl.services.docs_links import canonical_id, extract_links, rewrite_links_as_text
from tripl.services.mentions import MAX_MENTIONS_PER_COMMENT, excerpt
from tripl.services.project_links import project_link

#: What a notification about a note is about (``notifications.entity_type``).
DOC_ENTITY = "doc"


def mentioned_user_ids(body: str) -> list[uuid.UUID]:
    """Distinct ``[[user:<id>]]`` ids outside code, in order of first appearance."""
    seen: dict[uuid.UUID, None] = {}
    for link in extract_links(body):
        if link.kind != "user":
            continue
        value = canonical_id(link.target)
        if value is None:
            continue
        seen.setdefault(uuid.UUID(value), None)
        if len(seen) >= MAX_MENTIONS_PER_COMMENT:
            break
    return list(seen)


def _body_of(content: str) -> str:
    return split_frontmatter(content)[1]


def new_mentions(before: str | None, after: str) -> list[uuid.UUID]:
    """The people mentioned in ``after`` who were not mentioned in ``before`` (full contents)."""
    already = set(mentioned_user_ids(_body_of(before))) if before else set()
    return [user_id for user_id in mentioned_user_ids(_body_of(after)) if user_id not in already]


async def _org_members(
    session: AsyncSession, organization_id: uuid.UUID, user_ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    rows = await session.scalars(
        select(OrganizationMember.user_id).where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id.in_(user_ids),
        )
    )
    return set(rows.all())


async def announce(
    session: AsyncSession,
    project: Project,
    doc: DocFile,
    *,
    before: str | None,
    actor: User,
) -> None:
    """Tell the newly mentioned readers of ``doc``; best effort, never fails the save."""
    candidates = new_mentions(before, doc.content)
    if not candidates:
        return

    async def notify() -> None:
        members = await _org_members(session, project.organization_id, candidates)
        recipients = await readers_among(session, doc.id, members)
        if not recipients:
            return
        scope = "project" if doc.project_id is not None else "organization"
        who = await notification_announce.actor_label(session, actor.id)
        await notification_service.notify(
            session,
            project_id=project.id,
            kind="mention",
            entity_type=DOC_ENTITY,
            entity_id=doc.id,
            title=f"{who} mentioned you in {doc.title or doc.path}",
            url=await project_link(session, project.id, f"/docs/{scope}/{doc.path}"),
            body=excerpt(rewrite_links_as_text(_body_of(doc.content))),
            actor_user_id=actor.id,
            user_ids=sorted(recipients, key=str),
            honour_mute=False,
        )

    await notification_announce.best_effort(session, "doc mentions", notify)
