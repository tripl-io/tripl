"""Per-organization photo storage (F20 PR11)

* ``photo_storage_configs``: each version of an organization's OWN storage
  (backend, bucket, flags, encrypted credential JSON), identified per
  organization by a digest of those values and written once.
* ``event_photos.storage_org_id``: the organization whose upload wrote the
  blob, under its ``orgs/{org_id}/`` key prefix.
* ``event_photos.storage_config_id``: the version of that organization's own
  storage the blob was written with; NULL is the operator's store named by
  ``storage_backend``.

Backfill: none needed. Every existing row keeps NULL in both columns, which
reads as exactly what it was — the operator's store, a legacy ``events/...``
key, found through ``storage_backend``.

Revision ID: f6c8a0b2d4e7
Revises: d4e8f1a2b3c5
Create Date: 2026-09-28 22:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6c8a0b2d4e7"
down_revision: str | None = "d4e8f1a2b3c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "photo_storage_configs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("backend", sa.String(20), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "organization_id", "config_hash", name="uq_photo_storage_configs_org_hash"
        ),
    )
    op.create_index(
        "ix_photo_storage_configs_organization_id", "photo_storage_configs", ["organization_id"]
    )
    op.add_column("event_photos", sa.Column("storage_org_id", sa.Uuid(), nullable=True))
    op.add_column("event_photos", sa.Column("storage_config_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_event_photos_storage_org_id_organizations",
        "event_photos",
        "organizations",
        ["storage_org_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_event_photos_storage_config_id_photo_storage_configs",
        "event_photos",
        "photo_storage_configs",
        ["storage_config_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_event_photos_storage_config_id", "event_photos", ["storage_config_id"])


def downgrade() -> None:
    op.drop_index("ix_event_photos_storage_config_id", table_name="event_photos")
    op.drop_constraint(
        "fk_event_photos_storage_config_id_photo_storage_configs",
        "event_photos",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_event_photos_storage_org_id_organizations", "event_photos", type_="foreignkey"
    )
    op.drop_column("event_photos", "storage_config_id")
    op.drop_column("event_photos", "storage_org_id")
    op.drop_index("ix_photo_storage_configs_organization_id", table_name="photo_storage_configs")
    op.drop_table("photo_storage_configs")
