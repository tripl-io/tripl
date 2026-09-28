"""Note sharing for the docs catalog (F24, GH #308).

* :class:`DocShare` — one user or one organization group a ``restricted`` note
  is shared with, at ``view`` or ``edit``.
* :class:`DocFolderSetting` — a visibility set on a folder (a path prefix; folders
  are still implicit, so a setting can outlive the notes under it). A note whose
  ``visibility_inherited`` is true takes the setting of its nearest folder.
* :class:`DocFolderShare` — the users and groups of a ``restricted`` folder.

A share names EITHER a user OR a group (``ck_*_one_principal``), each through a
real foreign key, so deleting a user or a group deletes its shares. Sharing
never grants project or organization access: a note is only ever read through a
project route that already admitted the caller as a member, and group
memberships are dropped with the organization membership. The rules themselves
live in ``services.docs_access``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin, UUIDMixin

_PROJECT_SCOPE = text("project_id IS NOT NULL")
_ORGANIZATION_SCOPE = text("organization_id IS NOT NULL")

DOC_VISIBILITIES = ("private", "restricted", "level")
DOC_SHARE_PERMISSIONS = ("view", "edit")


class DocShare(UUIDMixin, Base):
    """One principal a note is shared with."""

    __tablename__ = "doc_shares"
    __table_args__ = (
        CheckConstraint(
            "(user_id IS NULL) <> (group_id IS NULL)", name="ck_doc_shares_one_principal"
        ),
        CheckConstraint("permission IN ('view', 'edit')", name="ck_doc_shares_permission"),
        UniqueConstraint("doc_file_id", "user_id", name="uq_doc_shares_user"),
        UniqueConstraint("doc_file_id", "group_id", name="uq_doc_shares_group"),
        Index("ix_doc_shares_user_id", "user_id"),
        Index("ix_doc_shares_group_id", "group_id"),
    )

    doc_file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("doc_files.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, default=None
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organization_groups.id", ondelete="CASCADE"), nullable=True, default=None
    )
    permission: Mapped[str] = mapped_column(String(8), default="view")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DocFolderSetting(UUIDMixin, TimestampMixin, Base):
    """The visibility of a folder, inherited by the notes under it."""

    __tablename__ = "doc_folder_settings"
    __table_args__ = (
        CheckConstraint(
            "(project_id IS NULL) <> (organization_id IS NULL)",
            name="ck_doc_folder_settings_one_scope",
        ),
        CheckConstraint(
            "visibility IN ('private', 'restricted', 'level')",
            name="ck_doc_folder_settings_visibility",
        ),
        Index(
            "uq_doc_folder_settings_project_path",
            "project_id",
            "path_key",
            unique=True,
            postgresql_where=_PROJECT_SCOPE,
            sqlite_where=_PROJECT_SCOPE,
        ),
        Index(
            "uq_doc_folder_settings_org_path",
            "organization_id",
            "path_key",
            unique=True,
            postgresql_where=_ORGANIZATION_SCOPE,
            sqlite_where=_ORGANIZATION_SCOPE,
        ),
        Index("ix_doc_folder_settings_org", "organization_id"),
    )

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, default=None
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, default=None
    )
    path: Mapped[str] = mapped_column(String(512))
    path_key: Mapped[str] = mapped_column(String(512))
    visibility: Mapped[str] = mapped_column(String(16), default="level")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, default=None
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, default=None
    )


class DocFolderShare(UUIDMixin, Base):
    """One principal a folder is shared with."""

    __tablename__ = "doc_folder_shares"
    __table_args__ = (
        CheckConstraint(
            "(user_id IS NULL) <> (group_id IS NULL)",
            name="ck_doc_folder_shares_one_principal",
        ),
        CheckConstraint("permission IN ('view', 'edit')", name="ck_doc_folder_shares_permission"),
        UniqueConstraint("folder_id", "user_id", name="uq_doc_folder_shares_user"),
        UniqueConstraint("folder_id", "group_id", name="uq_doc_folder_shares_group"),
        Index("ix_doc_folder_shares_user_id", "user_id"),
        Index("ix_doc_folder_shares_group_id", "group_id"),
    )

    folder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("doc_folder_settings.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, default=None
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organization_groups.id", ondelete="CASCADE"), nullable=True, default=None
    )
    permission: Mapped[str] = mapped_column(String(8), default="view")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
