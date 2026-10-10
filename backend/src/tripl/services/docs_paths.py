"""Docs catalog path rules and limits (F22, GH #299).

A note's path is also its file name on export, so the rules are the ones that
keep an export unpackable anywhere: relative, normalised, no traversal, no
characters a common filesystem refuses, and unique per scope without regard to
case (macOS and Windows would otherwise merge two notes into one file).

Folders are implicit — they are path prefixes — so a folder "path" here is a
prefix normalised the same way, minus the ``.md`` rule.

The limits are echoed by ``GET /docs`` (``limits``) and in
``website/docs/use/docs-catalog.md``; change them together.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

DocScope = Literal["project", "organization"]

#: One note's raw content, frontmatter included, in UTF-8 bytes.
MAX_FILE_BYTES = 256 * 1024
#: The frontmatter block alone.
MAX_FRONTMATTER_BYTES = 16 * 1024
MAX_TITLE_CHARS = 300
MAX_DESCRIPTION_CHARS = 2000
MAX_TAGS = 30
MAX_TAG_CHARS = 64
MAX_LINKS_PER_FILE = 500

MAX_PATH_CHARS = 512
MAX_PATH_SEGMENTS = 10
MAX_FILES_PER_SCOPE = 5000

MAX_BUNDLE_FILES = 2000
MAX_BUNDLE_BYTES = 20 * 1024 * 1024
MAX_ZIP_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_ZIP_COMPRESSION_RATIO = 100

_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()+-]{0,127}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SLASH_RUNS = re.compile(r"/{2,}")


class DocPathError(ValueError):
    """A path the catalog refuses; the message is safe to show (422)."""


def _normalize(raw: str, *, what: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise DocPathError(f"{what} is empty")
    if _CONTROL.search(raw):
        raise DocPathError(f"{what} contains a control character")
    if "\\" in raw:
        raise DocPathError(f"{what} contains a backslash; use '/' between folders")
    value = unicodedata.normalize("NFC", raw.strip())
    if value.startswith("/"):
        raise DocPathError(f"{what} must be relative (no leading '/')")
    value = _SLASH_RUNS.sub("/", value)
    while value.startswith("./"):
        value = value[2:]
    return value


def _check_segments(value: str, *, what: str) -> None:
    if len(value) > MAX_PATH_CHARS:
        raise DocPathError(f"{what} is longer than {MAX_PATH_CHARS} characters")
    segments = value.split("/")
    if len(segments) > MAX_PATH_SEGMENTS:
        raise DocPathError(f"{what} is nested deeper than {MAX_PATH_SEGMENTS} levels")
    for segment in segments:
        if segment == "":
            raise DocPathError(f"{what} has an empty folder name")
        if segment in {".", ".."}:
            raise DocPathError(f"{what} may not contain '.' or '..'")
        if not _SEGMENT.match(segment):
            raise DocPathError(
                f"'{segment}' in {what.lower()} is not allowed: names start with a letter "
                "or digit and use only letters, digits, spaces and . _ ( ) + -"
                " (at most 128 characters)"
            )


def normalize_doc_path(raw: str) -> str:
    """The canonical form of a note path, or :class:`DocPathError`."""
    value = _normalize(raw, what="Path")
    _check_segments(value, what="Path")
    if not value.lower().endswith(".md"):
        raise DocPathError("Path must end in .md")
    return value


def normalize_prefix(raw: str) -> str:
    """The canonical form of a folder prefix (no trailing '/'), or :class:`DocPathError`."""
    value = _normalize(raw, what="Folder")
    value = value.rstrip("/")
    if not value:
        raise DocPathError("Folder is empty")
    _check_segments(value, what="Folder")
    return value


def path_key(path: str) -> str:
    """The case-insensitive identity of a path within its scope."""
    return path.lower()


def is_under(path: str, prefix: str) -> bool:
    """Whether ``path`` lies inside the folder ``prefix`` (case-insensitively)."""
    return path_key(path).startswith(path_key(prefix) + "/")


def file_stem(path: str) -> str:
    """The file name without its folder and ``.md``, as a title of last resort."""
    name = path.rsplit("/", 1)[-1]
    return name[:-3] if name.lower().endswith(".md") else name


def content_bytes(content: str) -> int:
    return len(content.encode("utf-8"))
