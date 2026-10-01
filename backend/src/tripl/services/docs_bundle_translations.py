"""Translations in docs import and export: ``<note>.<lang>.md`` next to ``<note>.md``.

An export writes each translation with text as a file named after its note
plus the language, ``guides/setup.de.md``, and marks it in the JSON bundle with
``translation_of`` and ``lang``. A translation whose file name is already a
note's path is left out: the note wins.

An import reads a file as a translation when the bundle says so, or when its
name is ``<stem>.<tag>.md``, ``<stem>.md`` is a note (in the bundle or in the
scope) and no note lives at the file's own path. So ``notes.v2.md`` stays a
note, and so does ``release.en.md`` when there is no ``release.md``. An
imported translation is taken to match the original as imported: its
``source_revision`` is the note's revision after the import.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.doc_file import DocFile
from tripl.models.doc_translation import DocTranslation
from tripl.schemas.docs import DocBundleFile, DocImportError
from tripl.services._docs_translation_rows import apply_text, effective_status
from tripl.services.docs_frontmatter import DocContentError, parse_frontmatter
from tripl.services.docs_paths import DocPathError, content_bytes, normalize_doc_path, path_key
from tripl.services.docs_translate_text import normalize_lang
from tripl.services.docs_translations import MAX_TRANSLATION_BYTES

_TRANSLATION_NAME = re.compile(
    r"^(?P<stem>.+)\.(?P<lang>[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*)\.md$"
)


def translation_path(path: str, lang: str) -> str:
    """``guides/setup.md`` in ``de`` -> ``guides/setup.de.md``."""
    stem = path[:-3] if path.lower().endswith(".md") else path
    return f"{stem}.{lang}.md"


async def export_files(session: AsyncSession, docs: list[DocFile]) -> list[DocBundleFile]:
    """A file per translation with text of ``docs``, skipping a name a note already has."""
    if not docs:
        return []
    by_id = {doc.id: doc for doc in docs}
    taken = {doc.path_key for doc in docs}
    rows = await session.scalars(
        select(DocTranslation)
        .where(DocTranslation.doc_file_id.in_(list(by_id)), DocTranslation.revision > 0)
        .order_by(DocTranslation.lang)
    )
    files: list[DocBundleFile] = []
    for row in rows:
        doc = by_id[row.doc_file_id]
        name = translation_path(doc.path, row.lang)
        if path_key(name) in taken:
            continue
        files.append(
            DocBundleFile(
                path=name,
                content=row.content,
                sha256=row.content_sha256,
                translation_of=doc.path,
                lang=row.lang,
            )
        )
    return files


@dataclass(frozen=True)
class BundleTranslation:
    item: DocBundleFile
    #: The note's ``path_key``.
    note_key: str
    lang: str


def _key(path: str) -> str:
    try:
        return path_key(normalize_doc_path(path))
    except DocPathError:
        return path_key(path)


def split(
    files: list[DocBundleFile], existing_keys: set[str]
) -> tuple[list[DocBundleFile], list[BundleTranslation], list[DocImportError]]:
    """``(notes, translations, errors)`` of a bundle's files (see the module docstring)."""
    explicit = [item for item in files if item.translation_of or item.lang]
    rest = [item for item in files if not (item.translation_of or item.lang)]
    named: dict[int, re.Match[str]] = {}
    for index, item in enumerate(rest):
        match = _TRANSLATION_NAME.match(item.path)
        if match and normalize_lang(match.group("lang")) is not None:
            named[index] = match
    note_keys = set(existing_keys) | {
        _key(item.path) for index, item in enumerate(rest) if index not in named
    }
    notes: list[DocBundleFile] = []
    found: list[BundleTranslation] = []
    errors: list[DocImportError] = []
    for index, item in enumerate(rest):
        match = named.get(index)
        base = _key(f"{match.group('stem')}.md") if match else ""
        if match is None or _key(item.path) in existing_keys or base not in note_keys:
            notes.append(item)
            continue
        found.append(BundleTranslation(item, base, normalize_lang(match.group("lang")) or ""))
    for item in explicit:
        lang = normalize_lang(item.lang or "")
        if not item.translation_of or lang is None:
            errors.append(
                DocImportError(
                    path=item.path, detail="a translation needs translation_of and a lang code"
                )
            )
            continue
        base = _key(item.translation_of)
        if base not in note_keys:
            errors.append(
                DocImportError(path=item.path, detail=f"no note {item.translation_of} to translate")
            )
            continue
        found.append(BundleTranslation(item, base, lang))
    seen: set[tuple[str, str]] = set()
    for translation in found:
        error = _check(translation, seen)
        if error is not None:
            errors.append(error)
    return notes, found, errors


def _check(translation: BundleTranslation, seen: set[tuple[str, str]]) -> DocImportError | None:
    item = translation.item
    pair = (translation.note_key, translation.lang)
    if pair in seen:
        return DocImportError(path=item.path, detail="a second translation of the same note")
    seen.add(pair)
    if content_bytes(item.content) > MAX_TRANSLATION_BYTES:
        return DocImportError(
            path=item.path, detail=f"larger than {MAX_TRANSLATION_BYTES // 1024} KiB"
        )
    try:
        parse_frontmatter(item.content, item.path)
    except DocContentError as exc:
        return DocImportError(path=item.path, detail=str(exc))
    return None


def check_editable(
    translations: list[BundleTranslation], editable_keys: set[str]
) -> list[DocImportError]:
    """A translation of a note the caller may not edit is an error (before anything is written)."""
    return [
        DocImportError(path=translation.item.path, detail="its note is not yours to edit")
        for translation in translations
        if translation.note_key not in editable_keys
    ]


async def apply(
    session: AsyncSession,
    translations: list[BundleTranslation],
    docs_by_key: dict[str, DocFile],
    *,
    user_id: uuid.UUID,
    mirror: bool,
) -> tuple[list[str], list[str]]:
    """Write the bundle's translations; ``mirror`` also drops the ones it lacks.

    ``docs_by_key`` are the notes the caller may edit that remain after the
    import, by ``path_key``. Returns ``(written, deleted)``; a translation
    being made right now is left alone either way.
    """
    by_id = {doc.id: doc for doc in docs_by_key.values()}
    wanted = {
        (docs_by_key[translation.note_key].id, translation.lang): translation
        for translation in translations
        if translation.note_key in docs_by_key
    }
    rows = (
        list(
            await session.scalars(
                select(DocTranslation).where(DocTranslation.doc_file_id.in_(list(by_id)))
            )
        )
        if by_id
        else []
    )
    existing = {(row.doc_file_id, row.lang): row for row in rows}
    written: list[str] = []
    for (doc_id, lang), translation in wanted.items():
        doc = by_id[doc_id]
        row = existing.get((doc_id, lang))
        if row is not None and effective_status(row) == "pending":
            continue
        if row is not None and row.revision > 0 and row.content == translation.item.content:
            continue
        if row is None:
            now = datetime.now(UTC)
            row = DocTranslation(
                id=uuid.uuid4(),
                doc_file_id=doc_id,
                lang=lang,
                status="ready",
                content="",
                content_sha256="",
                source_revision=doc.revision,
                revision=0,
                machine=False,
                error="",
                requested_by=user_id,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        apply_text(
            session.add,
            row,
            content=translation.item.content,
            source_revision=doc.revision,
            action="edit",
            user_id=user_id,
        )
        written.append(translation_path(doc.path, lang))
    deleted: list[str] = []
    if mirror:
        for (doc_id, lang), row in existing.items():
            if (doc_id, lang) in wanted or effective_status(row) == "pending":
                continue
            deleted.append(translation_path(by_id[doc_id].path, lang))
            await session.delete(row)
    return sorted(written), sorted(deleted)
