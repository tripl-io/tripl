"""docs catalog links to notes, people and more plan entities (F24 part 2, #308)

* ``doc_links.kind`` accepts ``doc``, ``variable``, ``metric``, ``alert_rule``,
  ``branch``, ``scan``, ``data_source`` and ``user`` beside the F22 plan kinds.
  A note, an alert rule and a mentioned user are stored by id (the UUID's text in
  ``target``); the rest by name, like the F22 kinds.
* ``notifications.entity_type`` accepts ``doc``: an @mention in a note notifies
  about the note.

Downgrade deletes the link rows and notifications of the new kinds (the
previous release has no reader for them) and restores both checks.

Revision ID: d5f7a9b1c3e6
Revises: c4e6a8b0d2f5
Create Date: 2026-10-12 10:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5f7a9b1c3e6"
down_revision: str | None = "c4e6a8b0d2f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_KINDS = "'event', 'event_type', 'field'"
_NEW_KINDS = (
    "'event', 'event_type', 'field', 'doc', 'variable', 'metric', 'alert_rule', "
    "'branch', 'scan', 'data_source', 'user'"
)
_OLD_ENTITY_TYPES = "'event', 'event_type', 'metric', 'branch'"
_NEW_ENTITY_TYPES = "'event', 'event_type', 'metric', 'branch', 'doc'"


def _swap(table: str, name: str, column: str, values: str) -> None:
    with op.batch_alter_table(table) as batch:
        batch.drop_constraint(name, type_="check")
        batch.create_check_constraint(name, f"{column} IN ({values})")


def upgrade() -> None:
    _swap("doc_links", "ck_doc_links_kind", "kind", _NEW_KINDS)
    _swap("notifications", "ck_notification_entity_type", "entity_type", _NEW_ENTITY_TYPES)


def downgrade() -> None:
    op.execute(sa.text(f"DELETE FROM doc_links WHERE kind NOT IN ({_OLD_KINDS})"))
    op.execute(sa.text("DELETE FROM notifications WHERE entity_type = 'doc'"))
    _swap("doc_links", "ck_doc_links_kind", "kind", _OLD_KINDS)
    _swap("notifications", "ck_notification_entity_type", "entity_type", _OLD_ENTITY_TYPES)
