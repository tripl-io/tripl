"""Docs catalog import and export: a folder of ``.md`` files (F22, GH #299).

An export is the scope's notes, raw content and all, so a round trip is
lossless — unknown frontmatter keys included, which is what lets an agent-skill
folder (``SKILL.md`` plus ``references/*.md``) go in and come back out intact.

Import is Markdown only. A non-``.md`` entry (scripts, images, assets) is never
an error: it is reported in ``skipped`` and left out. Everything else is
validated up front — paths, sizes, frontmatter, case-insensitive duplicates and
the per-scope cap — and any error aborts the whole import (422) unless it is a
dry run, which reports them. ``mirror`` also deletes the notes the bundle does
not carry.

Zip uploads are checked before anything is extracted: the upload size, the
declared size of every entry, the total, and each entry's compression ratio.
Reads are bounded as well, so an entry that lies about its size still cannot
inflate past the limit.
"""

from __future__ import annotations

import io
import stat
import zipfile
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.doc_file import DocFile
from tripl.models.organization import Organization
from tripl.models.project import Project
from tripl.schemas.docs import (
    DocBundle,
    DocBundleFile,
    DocImportError,
    DocImportMode,
    DocImportResult,
    DocImportSkipped,
)
from tripl.services import _docs_store as store
from tripl.services import audit_service
from tripl.services.docs_access import DocCaller, require_doc_writer, require_org_bulk_delete
from tripl.services.docs_frontmatter import DocContentError, ParsedDoc, parse_frontmatter
from tripl.services.docs_paths import (
    MAX_BUNDLE_BYTES,
    MAX_BUNDLE_FILES,
    MAX_FILE_BYTES,
    MAX_FILES_PER_SCOPE,
    MAX_ZIP_COMPRESSION_RATIO,
    MAX_ZIP_UPLOAD_BYTES,
    DocPathError,
    DocScope,
    content_bytes,
    normalize_doc_path,
    path_key,
)
from tripl.services.docs_service import _resolve_project

BUNDLE_FORMAT = "tripl-docs/v1"
#: Entries of any kind a zip may list, so a million empty entries cannot make
#: even the pre-extraction scan expensive.
MAX_ZIP_ENTRIES = 4 * MAX_BUNDLE_FILES


def _is_markdown(name: str) -> bool:
    return name.lower().endswith(".md")


async def export_bundle(session: AsyncSession, slug: str, scope: DocScope) -> DocBundle:
    project = await _resolve_project(session, slug)
    docs = list(
        await session.scalars(
            select(DocFile).where(store.scope_filter(project, scope)).order_by(DocFile.path_key)
        )
    )
    organization_slug: str | None = None
    organization = await session.get(Organization, project.organization_id)
    if organization is not None:
        organization_slug = organization.slug
    return DocBundle(
        format=BUNDLE_FORMAT,
        scope=scope,
        project_slug=project.slug,
        organization_slug=organization_slug,
        exported_at=datetime.now(UTC),
        files=[
            DocBundleFile(path=doc.path, content=doc.content, sha256=doc.content_sha256)
            for doc in docs
        ],
    )


async def export_zip(session: AsyncSession, slug: str, scope: DocScope) -> tuple[bytes, str]:
    """``(zip bytes, file name)``; the name is the project's or organization's slug."""
    bundle = await export_bundle(session, slug, scope)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in sorted(bundle.files, key=lambda item: item.path):
            archive.writestr(item.path, item.content.encode("utf-8"))
    owner = bundle.project_slug if scope == "project" else (bundle.organization_slug or "org")
    return buffer.getvalue(), f"{owner}-docs.zip"


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def _strip_wrapper(names: list[str]) -> str | None:
    """The single top-level folder every entry shares, if there is one."""
    tops = {name.split("/", 1)[0] for name in names}
    if len(tops) != 1 or any("/" not in name for name in names):
        return None
    return next(iter(tops))


def parse_zip_upload(
    data: bytes, *, keep_root: bool = False
) -> tuple[list[DocBundleFile], list[DocImportSkipped], list[DocImportError]]:
    """The Markdown files of an uploaded zip, with what was skipped and what failed."""
    if len(data) > MAX_ZIP_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Zip upload is larger than {MAX_ZIP_UPLOAD_BYTES // (1024 * 1024)} MiB",
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=422, detail="Upload is not a valid zip file") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise HTTPException(
                status_code=413, detail=f"Zip holds more than {MAX_ZIP_ENTRIES} entries"
            )
        skipped: list[DocImportSkipped] = []
        errors: list[DocImportError] = []
        candidates: list[zipfile.ZipInfo] = []
        for info in infos:
            name = info.filename
            if info.is_dir():
                continue
            if name.startswith("__MACOSX/") or name.rsplit("/", 1)[-1] == ".DS_Store":
                skipped.append(DocImportSkipped(path=name, reason="macOS metadata"))
            elif _is_symlink(info):
                skipped.append(DocImportSkipped(path=name, reason="symbolic link"))
            elif not _is_markdown(name):
                skipped.append(DocImportSkipped(path=name, reason="not a Markdown (.md) file"))
            else:
                candidates.append(info)
        if len(candidates) > MAX_BUNDLE_FILES:
            raise HTTPException(
                status_code=413, detail=f"Zip holds more than {MAX_BUNDLE_FILES} Markdown files"
            )
        if sum(info.file_size for info in candidates) > MAX_BUNDLE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Zip unpacks to more than {MAX_BUNDLE_BYTES // (1024 * 1024)} MiB",
            )
        wrapper = None if keep_root else _strip_wrapper([info.filename for info in candidates])
        files: list[DocBundleFile] = []
        for info in candidates:
            path = info.filename[len(wrapper) + 1 :] if wrapper else info.filename
            if info.file_size > MAX_FILE_BYTES:
                errors.append(
                    DocImportError(path=path, detail=f"larger than {MAX_FILE_BYTES // 1024} KiB")
                )
                continue
            if (
                info.compress_size
                and info.file_size / info.compress_size > MAX_ZIP_COMPRESSION_RATIO
            ):
                errors.append(DocImportError(path=path, detail="compression ratio is too high"))
                continue
            with archive.open(info) as handle:
                raw = handle.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                errors.append(
                    DocImportError(path=path, detail=f"larger than {MAX_FILE_BYTES // 1024} KiB")
                )
                continue
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                errors.append(DocImportError(path=path, detail="not valid UTF-8 text"))
                continue
            files.append(DocBundleFile(path=path, content=content.removeprefix("﻿")))
    return files, skipped, errors


def _validate(item: DocBundleFile, seen: dict[str, str]) -> tuple[str, ParsedDoc] | DocImportError:
    try:
        path = normalize_doc_path(item.path)
    except DocPathError as exc:
        return DocImportError(path=item.path, detail=str(exc))
    if path_key(path) in seen:
        return DocImportError(
            path=item.path,
            detail=f"same path as {seen[path_key(path)]} (paths are case-insensitive)",
        )
    seen[path_key(path)] = item.path
    if content_bytes(item.content) > MAX_FILE_BYTES:
        return DocImportError(path=path, detail=f"larger than {MAX_FILE_BYTES // 1024} KiB")
    if item.sha256 is not None and item.sha256.lower() != store.sha256(item.content):
        return DocImportError(path=path, detail="sha256 does not match the content")
    try:
        return path, parse_frontmatter(item.content, path)
    except DocContentError as exc:
        return DocImportError(path=path, detail=str(exc))


async def check_import_allowed(
    session: AsyncSession, slug: str, caller: DocCaller, scope: DocScope, mode: DocImportMode
) -> Project:
    """The authorization half of an import, answered before any upload is read.

    Resolves the path's project (its organization owns the organization notes)
    and returns it. A mirror deletes the notes the bundle leaves out, so for
    organization notes it takes the bulk-delete rule (org owner/admin, browser
    session only).
    """
    project = await _resolve_project(session, slug)
    if mode == "mirror":
        await require_org_bulk_delete(session, caller, scope, project.organization_id, "mirror")
    else:
        await require_doc_writer(session, caller, scope, project.organization_id)
    return project


async def import_zip(
    session: AsyncSession,
    slug: str,
    scope: DocScope,
    data: bytes,
    *,
    keep_root: bool,
    mode: DocImportMode,
    dry_run: bool,
    caller: DocCaller,
) -> DocImportResult:
    await check_import_allowed(session, slug, caller, scope, mode)
    files, skipped, errors = parse_zip_upload(data, keep_root=keep_root)
    return await import_bundle(
        session,
        slug,
        scope,
        files,
        mode=mode,
        dry_run=dry_run,
        caller=caller,
        skipped=skipped,
        errors=errors,
    )


async def import_bundle(
    session: AsyncSession,
    slug: str,
    scope: DocScope,
    files: list[DocBundleFile],
    *,
    mode: DocImportMode,
    dry_run: bool,
    caller: DocCaller,
    skipped: list[DocImportSkipped] | None = None,
    errors: list[DocImportError] | None = None,
) -> DocImportResult:
    project = await check_import_allowed(session, slug, caller, scope, mode)
    user = caller.user
    if sum(content_bytes(item.content) for item in files) > MAX_BUNDLE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Bundle is larger than {MAX_BUNDLE_BYTES // (1024 * 1024)} MiB",
        )
    result = DocImportResult(
        scope=scope,
        mode=mode,
        dry_run=dry_run,
        skipped=list(skipped or []),
        errors=list(errors or []),
    )
    existing = {
        doc.path_key: doc
        for doc in await session.scalars(select(DocFile).where(store.scope_filter(project, scope)))
    }
    seen: dict[str, str] = {}
    plan: list[tuple[str, DocBundleFile, ParsedDoc, DocFile | None]] = []
    for item in files:
        if not _is_markdown(item.path):
            result.skipped.append(
                DocImportSkipped(path=item.path, reason="not a Markdown (.md) file")
            )
            continue
        checked = _validate(item, seen)
        if isinstance(checked, DocImportError):
            result.errors.append(checked)
            continue
        path, parsed = checked
        doc = existing.get(path_key(path))
        if doc is None:
            result.created.append(path)
        elif doc.content == item.content and doc.path == path:
            result.unchanged.append(path)
            continue
        else:
            result.updated.append(path)
        plan.append((path, item, parsed, doc))
    stale = [doc for key, doc in existing.items() if key not in seen] if mode == "mirror" else []
    result.deleted = sorted(doc.path for doc in stale)
    if len(existing) + len(result.created) - len(stale) > MAX_FILES_PER_SCOPE:
        result.errors.append(
            DocImportError(path="*", detail=f"a scope holds at most {MAX_FILES_PER_SCOPE} docs")
        )
    if result.errors and not dry_run:
        raise HTTPException(
            status_code=422,
            detail={"errors": [error.model_dump() for error in result.errors]},
        )
    if dry_run or not (plan or stale):
        return result

    plan_docs: list[DocFile] = []
    for path, item, parsed, found in plan:
        doc = found if found is not None else store.new_doc(project, scope, path)
        plan_docs.append(doc)
        if found is not None and found.path != path:
            doc.path = path
        await store.apply_content(
            session,
            doc,
            content=item.content,
            parsed=parsed,
            action="import",
            user=user,
            message="Imported",
        )
    written_ids = [doc.id for doc in plan_docs] + [doc.id for doc in stale]
    deleted_records = [store.deleted_record(doc) for doc in stale]
    for doc in stale:
        await session.delete(doc)
    await session.flush()
    await audit_service.record(
        session,
        user=user,
        action="doc.import",
        target_type="doc",
        target_id=None,
        target_name=f"{scope} notes",
        project=project,
        payload={
            "scope": scope,
            "mode": mode,
            "counts": {
                "created": len(result.created),
                "updated": len(result.updated),
                "unchanged": len(result.unchanged),
                "deleted": len(result.deleted),
                "skipped": len(result.skipped),
            },
            # Every note a mirror removed, with what it held: revisions go
            # with the note, so this row is the record that it existed.
            "deleted": deleted_records,
        },
        commit=False,
    )
    try:
        await session.flush()
        await store.reindex_after_write(session, project, scope, written_ids)
    except IntegrityError as exc:
        await session.rollback()
        raise store.concurrent_write() from exc
    return result
