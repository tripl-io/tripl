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

Who may see a note (F24, GH #308) is ``visibility``: ``level`` (everyone at the
note's level, the F22 behaviour), ``restricted`` (its author plus the users and
organization groups in :class:`~tripl.models.doc_share.DocShare`) or
``private`` (its author only). While ``visibility_inherited`` is true the note
takes the setting of the nearest folder that has one
(:class:`~tripl.models.doc_share.DocFolderSetting`), and ``visibility`` is kept
at ``level``. The effective rule is computed in ONE place,
``services.docs_access``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
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
    true,
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
        CheckConstraint(
            "visibility IN ('private', 'restricted', 'level')",
            name="ck_doc_files_visibility",
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
    visibility: Mapped[str] = mapped_column(String(16), default="level", server_default="level")
    visibility_inherited: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())


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


#: Every ``DocLink.kind`` (F22 plan links; F24 part 2 notes, mentions and the
#: project's other entities). Mirrors ``schemas.docs.DocLinkKind``.
DOC_LINK_KINDS: tuple[str, ...] = (
    "event",
    "event_type",
    "field",
    "doc",
    "variable",
    "metric",
    "alert_rule",
    "branch",
    "scan",
    "data_source",
    "user",
)
DOC_LINK_KIND_CHECK = "kind IN ({})".format(", ".join(f"'{kind}'" for kind in DOC_LINK_KINDS))


class DocLink(UUIDMixin, Base):
    """A ``[[kind:target]]`` reference found in a note's body.

    Plan entities (event, event type, field, variable, metric, branch, scan,
    data source) are kept by NAME and resolved on read, so a rename shows up as
    a broken link. A note (``doc``), an alert rule and a mentioned user are kept
    by id (``target`` is the UUID's canonical text), so they survive renames and
    moves. ``qualifier`` is a field's event type, or a note link's heading
    anchor.
    """

    __tablename__ = "doc_links"
    __table_args__ = (
        CheckConstraint(
            DOC_LINK_KIND_CHECK,
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
