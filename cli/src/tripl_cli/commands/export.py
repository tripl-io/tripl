"""``tripl export`` — the plan as a JSON Schema bundle, or as the ``tripl codegen`` model.

One word: it reads a project's plan as a whole. ``--format jsonschema`` (the
default) asks ``GET …/plan/export`` for one JSON Schema (draft 2020-12) per
event — required fields, enums from allowed values, patterns and bounds from
field contracts — and writes, under ``--out DIR``::

    DIR/bundle.json                          the whole bundle, as served
    DIR/<event type>/<identity>.schema.json  one schema per event

``--format codegen_model`` writes ``DIR/codegen_model.json``, the input of
``tripl codegen --model`` (offline generation, custom template development).
Without ``--out`` the export is printed to stdout instead.

File names come from plan strings, so they are sanitised to
``[A-Za-z0-9._-]`` and can never climb out of ``DIR``; two identities that
sanitise alike get ``-2``, ``-3``. JSON is written with sorted keys and a
trailing newline, so a committed export diffs cleanly between revisions.

Exit codes: 0 written; 1 the instance failed; 2 a bad invocation.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from tripl_cli.api import plan_export
from tripl_cli.commands import add_json, add_timeout
from tripl_cli.commands._plan import add_branch
from tripl_cli.commands.codegen import (
    add_check_config,
    add_project_flag,
    branch_document,
    check_config_of,
    display_path,
    fetch_export,
    project_of,
)
from tripl_cli.config import Config
from tripl_cli.errors import EXIT_OK, TriplConfigError, TriplError
from tripl_cli.model import JsonDict, as_dict
from tripl_cli.render import render_header
from tripl_cli.report import export_document

BUNDLE_FILE = "bundle.json"
MODEL_FILE = "codegen_model.json"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser(
        "export",
        parents=[parent],
        help="export the plan as JSON Schema (one schema per event), or as the codegen model",
        description=(
            "Export the tracking plan: --format jsonschema writes one JSON Schema per event "
            "plus the whole bundle; --format codegen_model writes the model `tripl codegen "
            "--model` reads. Without --out, the export is printed to stdout."
        ),
    )
    parser.add_argument(
        "--format",
        dest="export_format",
        choices=plan_export.FORMATS,
        required=True,
        # Required rather than defaulted, so a future format is never picked by default.
        help="what to export: jsonschema (one schema per event) or codegen_model",
    )
    parser.add_argument(
        "--out",
        dest="out",
        metavar="DIR",
        type=Path,
        help="write into DIR (created if missing) instead of printing to stdout",
    )
    add_check_config(parser)
    add_project_flag(parser)
    add_branch(parser)
    add_json(parser)
    add_timeout(parser)
    parser.set_defaults(handler=run)


def safe_name(text: str) -> str:
    """A plan string as one path segment: never empty, ``.``/``..`` or a separator."""
    name = _UNSAFE.sub("_", text).strip("._") or "_"
    return name[:120]


def dumps(document: Any) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def schema_files(bundle: JsonDict, out: Path) -> dict[Path, Any]:
    """``{path: schema}`` for every schema in a ``jsonschema`` bundle."""
    files: dict[Path, Any] = {}
    taken: set[Path] = set()
    for key in sorted(as_dict(bundle.get("schemas"))):
        schema = as_dict(bundle.get("schemas"))[key]
        event_type, _, identity = str(key).partition("/")
        directory = out / safe_name(event_type)
        stem = safe_name(identity or event_type)
        path = directory / f"{stem}.schema.json"
        number = 2
        while path in taken:
            path = directory / f"{stem}-{number}.schema.json"
            number += 1
        taken.add(path)
        files[path] = schema
    return files


def _write(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    except OSError as exc:
        raise TriplError(f"cannot write {path}: {exc}") from None


def run(args: argparse.Namespace, config: Config) -> int:
    out: Path | None = args.out
    if args.as_json and out is None:
        raise TriplConfigError(
            "--json puts its report on stdout, where the export would go too; add --out DIR."
        )
    check_config = check_config_of(args)
    project = project_of(args, check_config, "export")
    selector: str | None = args.branch
    if selector is None and check_config is not None:
        selector = check_config.branch
    fmt: str = args.export_format
    payload, branch, run_facts = fetch_export(config, project, selector, fmt, timeout=args.timeout)
    revision = payload.get("revision")
    revision_text = None if revision is None or isinstance(revision, dict) else str(revision)
    schemas = len(as_dict(payload.get("schemas"))) if fmt == plan_export.FORMAT_JSONSCHEMA else 0

    if out is None:
        sys.stdout.write(dumps(payload))
        return EXIT_OK
    written: list[Path] = []
    if fmt == plan_export.FORMAT_JSONSCHEMA:
        _write(out / BUNDLE_FILE, dumps(payload))
        written.append(out / BUNDLE_FILE)
        for path, schema in schema_files(payload, out).items():
            _write(path, dumps(schema))
            written.append(path)
    else:
        _write(out / MODEL_FILE, dumps(payload))
        written.append(out / MODEL_FILE)

    human = sys.stderr if args.as_json else sys.stdout
    instance = run_facts.instance
    print(render_header("export", instance.base_url, instance.base_url_source), file=human)
    print(file=human)
    jsonschema = fmt == plan_export.FORMAT_JSONSCHEMA
    what = f"{schemas} event schemas" if jsonschema else "the codegen model"
    print(
        f"Exported {what} of {project} ({branch.name or 'main'}, revision "
        f"{revision_text or 'unknown'}) to {out}: {len(written)} files.",
        file=human,
    )
    if args.as_json:
        document = export_document(
            run_facts,
            project=project,
            branch=branch_document(branch),
            export_format=fmt,
            revision=revision_text,
            files=[display_path(path) for path in written],
            schemas=schemas,
        )
        json.dump(document, sys.stdout)
        sys.stdout.write("\n")
    return EXIT_OK
