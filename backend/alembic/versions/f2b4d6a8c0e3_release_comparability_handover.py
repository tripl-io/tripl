"""release comparabilities: the hand-over is labelled

While the newest active release ramps with nothing found, the release-regression
pass reports its predecessor's comparison instead (point 5 of
``tripl.core.analyzers.release_regression``). The verdict row used to carry
only that comparison's pair, so the Release regressions panel called the
predecessor the latest release while the By-version chart named the newest.

* ``release_comparabilities.latest_active_version`` (nullable string): the
  newest active release, which ``version`` is too except during a hand-over.
  NULL only when no two releases were eligible (``no_baseline``).
* ``release_comparabilities.latest_release_share`` (nullable float): that
  release's share of all traffic over its comparison window, quoted by the
  hand-over label. NULL when no comparison was made.
* ``release_comparabilities.handed_over`` (boolean, false): the row describes
  the predecessor's comparison standing in for the newest release.

Existing rows predate the hand-over, so their ``version`` IS the newest release
and is copied into ``latest_active_version``; every recalculation rewrites the
rows anyway.

Revision ID: f2b4d6a8c0e3
Revises: e3a5c7e9b1d4
Create Date: 2026-10-17 15:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2b4d6a8c0e3"
down_revision: str | None = "e3a5c7e9b1d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "release_comparabilities",
        sa.Column("latest_active_version", sa.String(500), nullable=True),
    )
    op.add_column(
        "release_comparabilities",
        sa.Column("latest_release_share", sa.Float(), nullable=True),
    )
    op.add_column(
        "release_comparabilities",
        sa.Column("handed_over", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.execute("UPDATE release_comparabilities SET latest_active_version = version")


def downgrade() -> None:
    op.drop_column("release_comparabilities", "handed_over")
    op.drop_column("release_comparabilities", "latest_release_share")
    op.drop_column("release_comparabilities", "latest_active_version")
