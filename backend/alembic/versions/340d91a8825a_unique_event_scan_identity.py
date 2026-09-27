"""one event per scan identity

Revision ID: 340d91a8825a
Revises: b3f91a27c045
Create Date: 2026-09-03 10:00:00.000000

``events.source_name`` is the identity a scan routes a row to: ``generate_events``
looks the derived name up in a map keyed by it and inserts only on a miss, and
``create_event`` refuses a second row for it with a 409. Nothing in the schema
backed either. ``ix_events_source_identity`` was a plain index, and production
holds what a plain index permits: acme-web carries two pageview pairs
(35091aae/039c0792 and a1b96770/1c4be1a5) created 94 ms apart on 2026-06-15,
one of each pair updated for months while its twin froze at creation. Which twin
a scan updated was whatever row the database returned first, so volumes and
last-seen times could migrate between the two as row order shifted.

This revision replaces that index with ``uq_event_scan_identity`` on
``(event_type_id, source_name)`` — and that IS the whole key. An event type
lives on exactly one branch of one project (``uq_event_type_project_name`` is
per branch, and a branch copy mints its own types), so ``event_type_id`` already
carries project and branch, and the constraint scopes an identity the way every
reader does: per project, per branch, per type. NULL stays free — Postgres
treats NULLs as distinct in a unique key — so events authored outside a scan
rule coexist with no identity until a scan adopts a name for one of them.

Repair before DDL, and repair by renaming, not by deleting. A populated
database already holds the twins this forbids, and the owner ruled out both
deleting and merging them: every loser keeps its row, its name and its history.
NULLing the loser's identity is no repair either. It reads as "authored outside
a scan rule", which a scan-created twin is not; it leaves an operator nothing to
search for; and it only makes the collision dormant. ``index_events_by_identity``
declines to adopt a name another row already holds, so the NULLed row sits quiet
until the winner is ever deleted and then takes the identity back — and before
this revision, when adoption was unconditional, it would have taken it back on
the very next run. So each loser is moved
to an identity no scan can derive, ``<source_name> #duplicate-<id>`` (unique by
construction; cut at 450 characters so it fits ``String(500)``), and tagged
``duplicate-identity`` so an operator can list the dead twins with the events
tag filter and retire them by hand.

The winner per identity is the row traffic most recently landed on —
``last_seen_at DESC NULLS LAST, created_at ASC, id ASC`` — the rule
``generate_events`` was already ordering its dedup load by, so the row the scan
has been deterministically routing to is the one that keeps the identity.

Not claimed: the mechanism that produced the twins. ``scan_jobs`` history does
not reach 2026-06-15; the most likely shape is two scan configs of one project
minting the same identity in parallel, which the worker now survives through a
savepoint (``insert_event_claiming_identity``). Also not claimed: any test of
the repair against real rows — CI's round trip runs on an EMPTY database, so
the UPDATE executes there against zero rows and proves only that it parses.
(tripl-8tdl)
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "340d91a8825a"
down_revision: str | None = "b3f91a27c045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT_NAME = "uq_event_scan_identity"
_OLD_INDEX_NAME = "ix_events_source_identity"


def upgrade() -> None:
    # Rename-then-narrow. One statement because the loser set exists only
    # before the rename: once suffixed, a loser is unique under its partition
    # and no re-ranking finds it, so the UPDATE hands its ids straight to the
    # tag INSERT. Postgres-only (a data-modifying CTE, ``gen_random_uuid()``)
    # with no dialect guard, like e5c7a91b3d02: the chain never runs on SQLite
    # here — tests build their schema from the models.
    #
    # ``WHERE source_name IS NOT NULL`` is load-bearing: NULLs are distinct
    # under the constraint, so they need no repair, and without it every NULL
    # row past the first would be RETURNED and tagged while its identity stayed
    # NULL (``left(NULL, 450) || ...`` is NULL).
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                row_number() OVER (
                    PARTITION BY event_type_id, source_name
                    ORDER BY last_seen_at DESC NULLS LAST, created_at ASC, id ASC
                ) AS rn
            FROM events
            WHERE source_name IS NOT NULL
        ),
        losers AS (
            UPDATE events e
            SET source_name = left(e.source_name, 450) || ' #duplicate-' || e.id::text
            FROM ranked r
            WHERE e.id = r.id
              AND r.rn > 1
            RETURNING e.id
        )
        INSERT INTO event_tags (id, event_id, name)
        SELECT gen_random_uuid(), id, 'duplicate-identity'
        FROM losers
        ON CONFLICT DO NOTHING
        """
    )
    op.drop_index(_OLD_INDEX_NAME, table_name="events")
    op.create_unique_constraint(_CONSTRAINT_NAME, "events", ["event_type_id", "source_name"])


def downgrade() -> None:
    # DDL only. The suffixed identities and the tags stay: a downgrade that
    # cannot restore data is a documented no-op in this repository, never a
    # raise (d4f5e6a7b8c9) — and the renamed rows are valid under the plain
    # index this restores, so the chain unwinds cleanly past them.
    op.drop_constraint(_CONSTRAINT_NAME, "events", type_="unique")
    op.create_index(_OLD_INDEX_NAME, "events", ["project_id", "event_type_id", "source_name"])
