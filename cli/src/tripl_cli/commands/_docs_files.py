"""The local half of ``tripl docs pull`` and ``push``: a folder of ``.md`` files.

Kept apart from the command module because every rule here is about the
operator's DISK, not about the API. That is where the dangerous edges are: a
bundle path that climbs out of the target folder, a symlink that points a write
at ``~/.ssh``, a ``.git`` directory swept into an upload.

The rules, in one place:

* ``push`` walks without following symlinks, and never uploads one. Hidden files
  and directories (``.git``, ``.DS_Store``) are skipped, and so is anything that
  is not ``*.md``. An agent-skill folder keeps ``scripts/`` and ``assets/`` next
  to ``SKILL.md``. Those are reported as skipped, and they never fail the push.
* A file over the per-file limit, or one that is not UTF-8, is a PROBLEM: the
  push refuses before it sends anything, and names every such file at once.
* ``pull`` validates EVERY target path before it writes the first byte, so a
  bad bundle cannot leave a half-written folder behind. It never deletes a local
  file, and it never writes through an existing symlink.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from tripl_cli.api import docs as docs_api
from tripl_cli.errors import TriplConfigError, TriplError
from tripl_cli.model import JsonDict

MARKDOWN_SUFFIX = ".md"
BYTE_ORDER_MARK = "﻿"

SKIP_HIDDEN_DIRECTORY = "hidden directory"
SKIP_HIDDEN_FILE = "hidden file"
SKIP_SYMLINK = "symbolic link"
SKIP_NOT_MARKDOWN = "not a Markdown file"
SKIP_NOT_REGULAR = "not a regular file"

ACTION_WRITTEN = "written"
ACTION_OVERWRITTEN = "overwritten"
ACTION_UNCHANGED = "unchanged"


@dataclass(frozen=True)
class LocalFile:
    path: str
    content: str
    size_bytes: int


@dataclass(frozen=True)
class Skipped:
    path: str
    reason: str


@dataclass(frozen=True)
class Folder:
    """What a push would upload, what it leaves behind, and what stops it."""

    files: tuple[LocalFile, ...]
    skipped: tuple[Skipped, ...]
    problems: tuple[str, ...]

    @property
    def total_bytes(self) -> int:
        return sum(item.size_bytes for item in self.files)


def read_folder(root: Path, *, keep_root: bool) -> Folder:
    """Every ``*.md`` under ``root``, as bundle paths relative to it.

    ``keep_root`` prefixes each path with the folder's own name, so
    ``tripl docs push ./checkout-skill --keep-root`` lands ``SKILL.md`` at
    ``checkout-skill/SKILL.md`` instead of at the root of the scope.

    A leading byte-order mark is dropped. The API reads frontmatter only when
    ``---`` is the first byte, and an editor that writes a BOM would otherwise
    turn every field of it into body text without a word.
    """
    if not root.is_dir():
        raise TriplConfigError(f"{root} is not a directory. Nothing was sent.")
    prefix = f"{root.resolve().name}/" if keep_root else ""
    files: list[LocalFile] = []
    skipped: list[Skipped] = []
    problems: list[str] = []
    for directory, subdirectories, filenames in root.walk(follow_symlinks=False):
        relative_dir = directory.relative_to(root)
        for name in sorted(subdirectories):
            if name.startswith("."):
                skipped.append(
                    Skipped(f"{(relative_dir / name).as_posix()}/", SKIP_HIDDEN_DIRECTORY)
                )
        # Pruned in place, which is what stops the walk descending into them.
        subdirectories[:] = sorted(name for name in subdirectories if not name.startswith("."))
        for name in sorted(filenames):
            full = directory / name
            relative = (relative_dir / name).as_posix()
            reason = _skip_reason(full, name)
            if reason is not None:
                skipped.append(Skipped(relative, reason))
                continue
            size = full.stat().st_size
            if size > docs_api.MAX_FILE_BYTES:
                problems.append(
                    f"{relative}: {size} bytes, over the {docs_api.MAX_FILE_BYTES}-byte limit"
                )
                continue
            try:
                text = full.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                problems.append(f"{relative}: not UTF-8 text")
                continue
            text = text.removeprefix(BYTE_ORDER_MARK)
            files.append(LocalFile(f"{prefix}{relative}", text, len(text.encode("utf-8"))))
    files.sort(key=lambda item: item.path)
    return Folder(tuple(files), tuple(skipped), tuple(problems))


def _skip_reason(full: Path, name: str) -> str | None:
    # Symlink first: `is_file()` follows the link, so asking it anything else
    # first would read through a link the walk promised not to follow.
    if full.is_symlink():
        return SKIP_SYMLINK
    if name.startswith("."):
        return SKIP_HIDDEN_FILE
    if not name.lower().endswith(MARKDOWN_SUFFIX):
        return SKIP_NOT_MARKDOWN
    if not full.is_file():
        return SKIP_NOT_REGULAR
    return None


def refuse_non_empty(root: Path, *, force: bool) -> None:
    """``pull`` into a folder that already holds something needs ``--force``.

    Checked before the export is requested, so the refusal costs no request.
    """
    if root.exists() and not root.is_dir():
        raise TriplConfigError(f"{root} exists and is not a directory. Nothing was written.")
    if force or not root.exists():
        return
    if any(root.iterdir()):
        raise TriplConfigError(
            f"{root} is not empty. Re-run with --force to write into it: files the export "
            "carries are overwritten, and nothing else is touched. Nothing was written."
        )


def safe_target(root: Path, path: str) -> Path:
    """Where one bundle path lands under ``root``, or a refusal.

    The API validates paths on the way in. This checks them again on the way
    out anyway, because this command writes to the operator's disk and must not
    trust the instance with that. An absolute path, a backslash, a ``..`` or a
    ``.`` segment, a drive letter or a NUL is refused, and so is anything that
    resolves outside ``root`` through a symlinked folder already there.
    """
    parts = path.split("/")
    unsafe = (
        not path
        or path.startswith("/")
        or "\\" in path
        or "\x00" in path
        or ":" in parts[0]
        or any(part in ("", ".", "..") for part in parts)
    )
    refusal = TriplError(
        f"the export carries an unsafe path {path!r}; refusing to write outside {root}. "
        "Nothing was written."
    )
    if unsafe:
        raise refusal
    target = root.joinpath(*parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise refusal
    return target


def write_bundle(root: Path, files: Sequence[tuple[str, str]]) -> list[JsonDict]:
    """Write every ``(path, content)`` under ``root``; one row per file.

    Every target is validated before the first write, so one bad path cannot
    leave half a folder behind. Content is written as UTF-8 bytes, which keeps
    the line endings the note was stored with on every platform.
    """
    targets = [(safe_target(root, path), path, content) for path, content in files]
    for target, path, _ in targets:
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise TriplError(
                f"{target} is a symlink or not a regular file; refusing to write {path!r} "
                "through it. Nothing was written."
            )
    rows: list[JsonDict] = []
    for target, path, content in targets:
        data = content.encode("utf-8")
        if target.exists():
            action = ACTION_UNCHANGED if target.read_bytes() == data else ACTION_OVERWRITTEN
        else:
            action = ACTION_WRITTEN
        if action != ACTION_UNCHANGED:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        rows.append(
            {
                "path": path,
                "local_path": str(target),
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "action": action,
            }
        )
    return rows
