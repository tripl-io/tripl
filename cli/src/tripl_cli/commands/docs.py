"""``tripl docs`` — the project's docs catalog: list, print, pull and push Markdown notes.

``<plural-noun> <verb>`` by the grammar in ``commands/__init__.py``: the notes
are a class of objects an operator browses. Two scopes live behind every verb:
the project's own notes and the notes of its ORGANIZATION, which every project
of that organization shows. ``--scope`` picks one.

Three verbs read (``ls``, ``cat``, ``pull``) and need a ``tk_r_`` key. ``push``
writes, and follows the write-safety rules in ``_write.py``. It has
``--dry-run``, which sends nothing, and on a terminal it asks first. The
question it asks is not a guess: before asking it posts the same bundle with
``dry_run=true``, which changes nothing, and shows how many notes would be
created, updated and deleted. With ``--yes`` there is no preview and no
question, and one request goes out.

``pull`` writes to the local disk only. It never deletes a local file, and it
refuses a non-empty folder without ``--force``. ``push`` never deletes a note
either, unless you pass ``--mirror``: then every note of the scope that the
folder does not carry is deleted, which is why ``--mirror`` is spelled out in
the question.

Deliberately absent: moving, renaming and deleting a single note, and the
revision history. Those stay in the app, where the diff and the restore button
live. ``push --mirror`` is the one way to delete from here, and it deletes only
what the folder no longer has.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from tripl_cli.api import ApiRequest
from tripl_cli.api import docs as docs_api
from tripl_cli.commands import (
    add_json,
    add_project,
    add_timeout,
    group_help,
    require_single_project,
)
from tripl_cli.commands._docs_files import Folder, read_folder, refuse_non_empty, write_bundle
from tripl_cli.commands._plan import MAIN, begin, emit
from tripl_cli.commands._write import add_write_flags, confirm, emit_mutation, request_document
from tripl_cli.config import Config, require_base_url
from tripl_cli.diagnostics.collect import Reader, instance_of
from tripl_cli.errors import EXIT_OK, TriplConfigError, TriplError
from tripl_cli.model import JsonDict, MutationOutcome, PlanRead, Run, as_dict, as_list, text_of
from tripl_cli.render import columns, plural, render_plan_read
from tripl_cli.runner import run_async

SCOPE_ALL = "all"
DEFAULT_SCOPE = "project"
SCOPE_LABELS = {"project": "project notes", "organization": "organization notes"}
# The import result's lists, in the order the human summary names them.
RESULT_LISTS = ("created", "updated", "unchanged", "deleted")
# What a refusal after the preview says: the dry-run import went out, and it
# changed nothing.
NOTHING_CHANGED = "Nothing was changed."
PULL_ACTIONS = ("written", "overwritten", "unchanged")


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser(
        "docs",
        parents=[parent],
        help="list, print, pull and push a project's Markdown notes",
        description=(
            "Read and sync the docs catalog: Markdown notes for people and agents, kept per "
            "project and per organization. `ls`, `cat` and `pull` need a tk_r_ key; `push` "
            "needs a tk_w_ key backed by an editor or owner, and asks first."
        ),
    )
    verbs = parser.add_subparsers(dest="docs_command", metavar="<verb>")
    _register_ls(verbs, parent)
    _register_cat(verbs, parent)
    _register_pull(verbs, parent)
    _register_push(verbs, parent)
    parser.set_defaults(handler=group_help(parser))


def _add_scope(parser: argparse.ArgumentParser, *, allow_all: bool) -> None:
    choices = (*docs_api.SCOPES, SCOPE_ALL) if allow_all else docs_api.SCOPES
    default = SCOPE_ALL if allow_all else DEFAULT_SCOPE
    parser.add_argument(
        "--scope",
        dest="scope",
        choices=choices,
        default=default,
        help=f"which notes: {'|'.join(choices)} (default: {default})",
    )


def _add_common(parser: argparse.ArgumentParser, *, allow_all: bool = False) -> None:
    add_project(parser, single=True)
    _add_scope(parser, allow_all=allow_all)


def _register_ls(
    verbs: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    parser = verbs.add_parser(
        "ls",
        parents=[parent],
        help="list the notes, project and organization",
        description=(
            "List every note with its scope, path, title, audience and revision. Folders are "
            "the paths themselves; there are no empty ones."
        ),
    )
    _add_common(parser, allow_all=True)
    parser.add_argument(
        "--audience",
        dest="audience",
        choices=docs_api.AUDIENCES,
        help="keep notes written for this reader; a note marked `both` matches human and agent",
    )
    add_json(parser)
    add_timeout(parser)
    parser.set_defaults(handler=run_ls)


def _register_cat(
    verbs: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    parser = verbs.add_parser(
        "cat",
        parents=[parent],
        help="print one note's raw Markdown",
        description=(
            "Print one note exactly as stored, frontmatter included, and nothing else on "
            "stdout, so `tripl docs cat x.md > x.md` round-trips. Broken [[links]] are "
            "reported on stderr. Without --lang it prints the project's agent default "
            "language when that translation is up to date, else the original."
        ),
    )
    parser.add_argument("path", metavar="<path>", help="the note's path, e.g. guides/setup.md")
    parser.add_argument(
        "--lang",
        metavar="<code>",
        default=None,
        help="a stored translation to print (e.g. en), or 'original'",
    )
    _add_common(parser)
    add_json(parser)
    add_timeout(parser)
    parser.set_defaults(handler=run_cat)


def _register_pull(
    verbs: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    parser = verbs.add_parser(
        "pull",
        parents=[parent],
        help="write every note of one scope into a local folder",
        description=(
            "Export one scope and write each note to <dir>/<path>. A non-empty <dir> needs "
            "--force, which overwrites the files the export carries and touches nothing "
            "else. Local files are never deleted."
        ),
    )
    parser.add_argument("directory", metavar="<dir>", type=Path, help="folder to write into")
    _add_common(parser)
    parser.add_argument(
        "--force",
        dest="force",
        action="store_true",
        help="write into a non-empty folder, overwriting the files the export carries",
    )
    add_json(parser)
    add_timeout(parser)
    parser.set_defaults(handler=run_pull)


def _register_push(
    verbs: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    parser = verbs.add_parser(
        "push",
        parents=[parent],
        help="import a local folder of .md files into one scope (needs a tk_w_ key)",
        description=(
            "Upload every *.md under <dir> as one import. Other files, hidden files and "
            "symlinks are skipped and listed. Existing notes at the same path are updated; "
            "--mirror also DELETES every note of the scope the folder does not carry. Asks "
            "first, after a preview, unless --yes."
        ),
    )
    parser.add_argument("directory", metavar="<dir>", type=Path, help="folder to upload")
    _add_common(parser)
    parser.add_argument(
        "--mirror",
        dest="mirror",
        action="store_true",
        help="also delete every note of the scope that the folder does not carry",
    )
    parser.add_argument(
        "--keep-root",
        dest="keep_root",
        action="store_true",
        help="prefix every path with the folder's own name instead of uploading its contents",
    )
    add_write_flags(parser, prompts=True)
    add_json(parser)
    add_timeout(parser)
    parser.set_defaults(handler=run_push)


# --- ls -----------------------------------------------------------------------


def run_ls(args: argparse.Namespace, config: Config) -> int:
    slug = require_single_project(args)
    scope: str = args.scope
    audience: str | None = args.audience
    context = begin(config)

    async def body(client: httpx.AsyncClient) -> PlanRead:
        reader = context.reader(client)
        payload = await reader.send(docs_api.list_docs(slug))
        rows = docs_api.filter_by_audience(
            docs_api.tree_docs(payload, None if scope == SCOPE_ALL else scope), audience
        )
        # No total, offset or limit: the tree route pages nothing, so the rows
        # ARE the whole answer, the same statement `plan types` makes.
        return context.read(
            reader, command="docs ls", kind="doc", project=slug, branch=MAIN, items=rows
        )

    read = run_async(config, body, timeout=float(args.timeout))
    emit(
        read,
        context,
        as_json=bool(args.as_json),
        human=render_plan_read(read, _doc_rows(read.items), empty="no notes"),
    )
    return EXIT_OK


def _doc_rows(docs: tuple[JsonDict, ...]) -> list[list[str]]:
    """scope, path, audience, revision, title."""
    return [
        [
            text_of(doc, "scope") or "unknown",
            text_of(doc, "path") or "(no path)",
            text_of(doc, "audience") or "both",
            f"r{doc.get('revision', '?')}",
            " ".join((text_of(doc, "title") or "-").split()),
        ]
        for doc in docs
    ]


# --- cat ----------------------------------------------------------------------


def run_cat(args: argparse.Namespace, config: Config) -> int:
    slug = require_single_project(args)
    scope: str = args.scope
    path: str = str(args.path)
    as_json = bool(args.as_json)
    context = begin(config)

    async def body(client: httpx.AsyncClient) -> PlanRead:
        reader = context.reader(client)
        payload = as_dict(await reader.send(docs_api.read_doc(slug, scope, path, args.lang)))
        return context.read(
            reader, command="docs cat", kind="doc_file", project=slug, branch=MAIN, items=[payload]
        )

    read = run_async(config, body, timeout=float(args.timeout))
    doc = read.items[0] if read.items else {}
    warnings = [*_link_warnings(doc), *_language_notes(doc)]
    if as_json:
        summary = f"{scope} {text_of(doc, 'path') or path} r{doc.get('revision', '?')}"
        emit(read, context, as_json=True, human="\n".join([summary, *warnings]))
        return EXIT_OK
    # Nothing but the note on stdout: no header, no footer, no added newline.
    sys.stdout.write(text_of(doc, "content") or "")
    sys.stdout.flush()
    for line in warnings:
        print(line, file=sys.stderr)
    return EXIT_OK


def _language_notes(doc: JsonDict) -> list[str]:
    """What the read got, language-wise, when it is not simply what was asked for."""
    lang = text_of(doc, "lang")
    requested = text_of(doc, "requested_lang")
    fallback = text_of(doc, "translation_fallback")
    if lang and doc.get("translation_outdated"):
        return [f"note: the {lang} translation is behind the original"]
    if requested and fallback:
        return [f"note: printed the original; the {requested} translation is {fallback}"]
    return []


def _link_warnings(doc: JsonDict) -> list[str]:
    """One stderr line per link the API could not resolve to exactly one target."""
    lines: list[str] = []
    for link in as_list(doc.get("links")):
        status = text_of(link, "status")
        if status in ("broken", "ambiguous"):
            lines.append(f"warning: {status} link {text_of(link, 'raw') or '(unknown)'}")
    return lines


# --- pull ---------------------------------------------------------------------


def run_pull(args: argparse.Namespace, config: Config) -> int:
    slug = require_single_project(args)
    scope: str = args.scope
    root: Path = args.directory
    refuse_non_empty(root, force=bool(args.force))
    context = begin(config)

    async def body(client: httpx.AsyncClient) -> tuple[Reader, list[tuple[str, str]]]:
        reader = context.reader(client)
        payload = await reader.send(docs_api.export_docs(slug, scope))
        # An empty note is legal, and `text_of` reads "" as absent: both are "".
        files = [
            (text_of(item, "path") or "", text_of(item, "content") or "")
            for item in docs_api.bundle_files(payload)
        ]
        return reader, files

    reader, files = run_async(config, body, timeout=float(args.timeout))
    rows = write_bundle(root, files)
    read = context.read(
        reader, command="docs pull", kind="pulled_file", project=slug, branch=MAIN, items=rows
    )
    counts = {action: sum(1 for row in rows if row["action"] == action) for action in PULL_ACTIONS}
    table = [f"  {line}" for line in columns([[row["action"], row["path"]] for row in rows])]
    footer = f"{plural(len(rows), 'note')}: " + ", ".join(
        f"{count} {action}" for action, count in counts.items()
    )
    human = "\n".join(
        [f"{slug} {SCOPE_LABELS[scope]} -> {root}", *(table or ["  (no notes)"]), "", footer]
    )
    emit(read, context, as_json=bool(args.as_json), human=human)
    return EXIT_OK


# --- push ---------------------------------------------------------------------


def run_push(args: argparse.Namespace, config: Config) -> int:
    slug = require_single_project(args)
    scope: str = args.scope
    root: Path = args.directory
    mode = docs_api.MODE_MIRROR if args.mirror else docs_api.MODE_MERGE
    dry_run = bool(args.dry_run)
    assume_yes = bool(args.assume_yes)
    as_json = bool(args.as_json)
    folder = _checked_folder(
        root, keep_root=bool(args.keep_root), mirror=mode == docs_api.MODE_MIRROR
    )
    base_url = require_base_url(config)
    started = time.monotonic()
    generated_at = datetime.now(UTC)
    pairs = [(item.path, item.content) for item in folder.files]
    request = docs_api.import_docs(slug, scope, pairs, mode=mode)
    question = _question(folder, slug=slug, scope=scope, mode=mode, root=root)
    interactive = not dry_run and not assume_yes
    if interactive and not sys.stdin.isatty():
        # Refused before any request, preview included: a pipeline gets no
        # half-answer, only the flag it needs.
        confirm(question, assume_yes=False)

    async def body(client: httpx.AsyncClient) -> tuple[Reader, JsonDict | None]:
        reader = Reader(client, base_url)
        if dry_run:
            return reader, None
        if interactive:
            preview_request = docs_api.import_docs(slug, scope, pairs, mode=mode, dry_run=True)
            preview = as_dict(await reader.send(preview_request))
            _refuse_on_errors(preview)
            print(_summary(preview, prefix="preview"), file=sys.stderr)
            # The preview went out, so "Nothing was sent." would be false. What
            # is true is that a dry run changes nothing.
            confirm(
                f"{question} {_counts_sentence(preview)}",
                assume_yes=False,
                consequence=NOTHING_CHANGED,
            )
        return reader, as_dict(await reader.send(request))

    reader, result = run_async(config, body, timeout=float(args.timeout))
    if result is not None:
        _refuse_on_errors(result)
    outcome = MutationOutcome(
        command="docs push",
        run=Run(
            instance=instance_of(config, base_url, "unknown"),
            generated_at=generated_at,
            duration_ms=int((time.monotonic() - started) * 1000),
            requests=reader.requests,
        ),
        request=_printable(request),
        project=slug,
        dry_run=dry_run,
        action=mode,
        result=result,
    )
    emit_mutation(
        outcome,
        base_url=base_url,
        config=config,
        as_json=as_json,
        human=_render_push(outcome, folder, scope=scope),
    )
    return EXIT_OK


def _checked_folder(root: Path, *, keep_root: bool, mirror: bool) -> Folder:
    """The folder, or exit 2 naming every file that stops it. Nothing is sent either way."""
    folder = read_folder(root, keep_root=keep_root)
    problems = list(folder.problems)
    if len(folder.files) > docs_api.MAX_BUNDLE_FILES:
        problems.append(
            f"{len(folder.files)} .md files, over the {docs_api.MAX_BUNDLE_FILES}-file limit"
        )
    if folder.total_bytes > docs_api.MAX_BUNDLE_BYTES:
        problems.append(
            f"{folder.total_bytes} bytes in total, over the {docs_api.MAX_BUNDLE_BYTES}-byte limit"
        )
    if problems:
        raise TriplConfigError(
            f"{root} cannot be pushed:\n  " + "\n  ".join(problems) + "\nNothing was sent."
        )
    if not folder.files:
        # Under --mirror an empty upload would delete every note of the scope,
        # and without it an empty upload does nothing. Neither is what was meant.
        what = (
            "--mirror would delete every note of the scope"
            if mirror
            else "there is nothing to push"
        )
        raise TriplConfigError(f"{root} holds no .md files; {what}. Nothing was sent.")
    return folder


def _question(folder: Folder, *, slug: str, scope: str, mode: str, root: Path) -> str:
    files = plural(len(folder.files), "Markdown file")
    base = f"Push {files} from {root} into the {SCOPE_LABELS[scope]} of {slug}"
    if mode == docs_api.MODE_MIRROR:
        return f"{base}, and DELETE every note there that the folder does not carry?"
    return f"{base}?"


def _names(result: JsonDict, key: str) -> list[str]:
    value = result.get(key)
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _counts_sentence(result: JsonDict) -> str:
    return ", ".join(f"{len(_names(result, key))} {key}" for key in RESULT_LISTS) + "."


def _summary(result: JsonDict, *, prefix: str) -> str:
    lines = [f"{prefix}: {_counts_sentence(result)}"]
    for key in ("created", "updated", "deleted"):
        lines.extend(f"  {key:<8} {name}" for name in _names(result, key))
    return "\n".join(lines)


def _refuse_on_errors(result: JsonDict) -> None:
    """A result carrying errors changed nothing, and must not exit 0."""
    errors = as_list(result.get("errors"))
    if errors:
        detail = "\n  ".join(
            f"{text_of(error, 'path') or '?'}: {text_of(error, 'detail') or '?'}"
            for error in errors
        )
        raise TriplError(f"the import was refused:\n  {detail}\n{NOTHING_CHANGED}")


def _printable(request: ApiRequest) -> JsonDict:
    """``request_document``, with each file's content replaced by its size.

    The body can be twenty megabytes of Markdown. The dry-run line and the
    ``--json`` document say which files would be sent, not what is in them.
    """
    document = request_document(request)
    body = as_dict(request.json_body)
    files: list[Any] = [
        {
            "path": text_of(item, "path"),
            "size_bytes": len((text_of(item, "content") or "").encode("utf-8")),
        }
        for item in as_list(body.get("files"))
    ]
    document["body"] = {**body, "files": files}
    return document


def _render_push(outcome: MutationOutcome, folder: Folder, *, scope: str) -> str:
    lines = [f"  skipped  {item.path} ({item.reason})" for item in folder.skipped]
    if outcome.dry_run:
        request = outcome.request
        head = (
            f"dry run: would send {request['method']} {request['path']} with "
            f"{plural(len(folder.files), 'file')} ({folder.total_bytes} bytes), "
            f"mode {outcome.action}"
        )
        return "\n".join([head, *lines, "Nothing was sent."])
    result = outcome.result or {}
    server_skipped = [
        f"  skipped  {text_of(item, 'path') or '?'} ({text_of(item, 'reason') or 'skipped'})"
        for item in as_list(result.get("skipped"))
    ]
    head = f"{outcome.project}: pushed to {SCOPE_LABELS[scope]} ({outcome.action})"
    return "\n".join([_summary(result, prefix=head), *lines, *server_skipped])
