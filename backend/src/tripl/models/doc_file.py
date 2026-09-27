"""The docs catalog: Markdown notes for people and AI agents (F22, GH #299).

Two scopes share one table. A note belongs either to a PROJECT or to an
ORGANIZATION (never both, never neither: ``ck_doc_files_one_scope``). A
project's catalog shows its own notes and its organization's.

Folders are implicit: they are the prefixes of the stored paths, so there are no
folder rows and no empty folders, and an export is an exact mirror of a
directory of ``.md`` files. Uniqueness is case-insensitive per scope, on
``path_key`` (``lower(path)``, written by the service), so an export always
unpacks on a case-insensitive filesystem.

The catalog is NOT branch-aware: there is one version of each file. History is
kept in :class:`DocRevision`, one full copy of the content per revision (the diff
is computed on read). :class:`DocLink` holds the ``[[event:...]]`` style
references found in the body, by NAME; whether a name resolves is computed live
against the main plan, so nothing here goes stale when an event appears or
disappears.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin, UUIDMixin

_PROJECT_SCOPE = text("project_id IS NOT NULL")
_ORGANIZATION_SCOPE = text("organization_id IS NOT NULL")


class DocFile(UUIDMixin, TimestampMixin, Base):
    """One Markdown note, stored verbatim (frontmatter included)."""

    __tablename__ = "doc_files"
    __table_args__ = (
        CheckConstraint(
            "(project_id IS NULL) <> (organization_id IS NULL)",
            name="ck_doc_files_one_scope",
        ),
        CheckConstraint(
            "audience IN ('human', 'agent', 'both')",
            name="ck_doc_files_audience",
        ),
        Index(
            "uq_doc_files_project_path",
            "project_id",
            "path_key",
            unique=True,
            postgresql_where=_PROJECT_SCOPE,
            sqlite_where=_PROJECT_SCOPE,
        ),
        Index(
            "uq_doc_files_org_path",
            "organization_id",
            "path_key",
            unique=True,
            postgresql_where=_ORGANIZATION_SCOPE,
            sqlite_where=_ORGANIZATION_SCOPE,
        ),
        Index("ix_doc_files_org", "organization_id"),
    )

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, default=None
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, default=None
    )
    path: Mapped[str] = mapped_column(String(512))
    path_key: Mapped[str] = mapped_column(String(512))
    content: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    audience: Mapped[str] = mapped_column(String(8), default="both")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, default=None
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, default=None
    )


class DocRevision(UUIDMixin, Base):
    """One saved state of a note: the full content at that point, and who wrote it."""

    __tablename__ = "doc_revisions"
    __table_args__ = (
        UniqueConstraint("doc_file_id", "number", name="uq_doc_revisions_file_number"),
        CheckConstraint(
            "action IN ('create', 'update', 'move', 'restore', 'import')",
            name="ck_doc_revisions_action",
        ),
    )

    doc_file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("doc_files.id", ondelete="CASCADE"), index=True
    )
    number: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(16))
    path: Mapped[str] = mapped_column(String(512))
    content: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(String(500), default="")
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    restored_from_number: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)


class DocLink(UUIDMixin, Base):
    """A ``[[kind:name]]`` reference found in a note's body, by name."""

    __tablename__ = "doc_links"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('event', 'event_type', 'field')",
            name="ck_doc_links_kind",
        ),
        Index("ix_doc_links_target", "kind", "target"),
    )

    doc_file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("doc_files.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))
    target: Mapped[str] = mapped_column(String(500))
    qualifier: Mapped[str | None] = mapped_column(String(500), nullable=True, default=None)
