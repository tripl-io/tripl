"""``tripl annotate`` — post a deploy or release marker onto a project's charts.

One word, by the grammar in ``commands/__init__.py``: it does not act on a class
of objects an operator browses, it records one fact ("we deployed web
2026.09.25 at 14:02") from a CI step. Listing and deleting annotations stay in
the app.

It always sends ``source="api"``. ``manual`` is what the app's own form sends and
``release`` is reserved to the metrics worker, which draws those markers itself
when a scan sees a new app version become active; the API refuses it from a
client with a 422.

A **200** is not an error. The API de-duplicates ``api`` annotations by
``(project, label)`` over 24 hours (``manual`` ones never) and answers 200 with
the row that already exists, so a retried
deploy job never draws the marker twice. The command exits 0 either way and
says which of the two happened, in the human line and as ``deduplicated`` in the
``--json`` document.

No prompt and no ``--yes``: a marker on a chart is additive and cheap to delete,
and this is a command whose natural home is an unattended pipeline — the same
reasoning ``scans run`` follows. ``--dry-run`` is still here, because every
mutation has one.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from tripl_cli.api import chart_annotations as annotations_api
from tripl_cli.commands import (
    add_json,
    add_project,
    add_timeout,
    bounded_datetime,
    bounded_text,
    require_single_project,
)
from tripl_cli.commands._write import add_write_flags, request_document
from tripl_cli.config import Config, require_base_url
from tripl_cli.diagnostics.collect import Reader, instance_of
from tripl_cli.errors import EXIT_OK, TriplConfigError
from tripl_cli.model import JsonDict, MutationOutcome, Run, as_dict
from tripl_cli.render import render_header, render_mutation
from tripl_cli.report import mutation_document
from tripl_cli.runner import run_async

URL_SCHEMES = ("http", "https")


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    # Not `parent`: that one spells the instance URL `--url` as well as
    # `--base-url`, and here `--url` is the release link. Imported here rather
    # than at module scope because `tripl_cli.cli` imports this package.
    from tripl_cli.cli import build_global_parser

    connection = build_global_parser(instance_url_flags=("--base-url",))
    parser = subparsers.add_parser(
        "annotate",
        parents=[connection],
        help="post a deploy or release marker onto a project's charts (needs a tk_w_ key)",
        description=(
            "Create a project-level chart annotation with source 'api', e.g. from a deploy "
            "job. Needs a tk_w_ key backed by an editor or owner. Does NOT prompt. The same "
            "label posted again within 24 hours is de-duplicated: the API answers 200 with "
            "the existing annotation, and this command says so and still exits 0."
        ),
    )
    parser.add_argument(
        "label",
        metavar="<label>",
        type=bounded_text("<label>", 1, annotations_api.LABEL_MAX_LENGTH),
        help=f"the text drawn on the chart, 1-{annotations_api.LABEL_MAX_LENGTH} characters",
    )
    add_project(parser, single=True)
    parser.add_argument(
        "--url",
        dest="link",
        metavar="URL",
        type=http_url("--url", annotations_api.URL_MAX_LENGTH),
        help="release or pull-request link the chart tooltip opens (http or https)",
    )
    parser.add_argument(
        "--at",
        dest="at",
        metavar="TIMESTAMP",
        type=bounded_datetime("--at"),
        help="when it happened, RFC 3339; a naive value is read as UTC (default: now)",
    )
    parser.add_argument(
        "--description",
        dest="description",
        metavar="TEXT",
        type=bounded_text("--description", 1, annotations_api.DESCRIPTION_MAX_LENGTH),
        help="longer text shown in the tooltip",
    )
    parser.add_argument(
        "--scope-type",
        dest="scope_type",
        choices=annotations_api.SCOPE_TYPES,
        help="draw it only on charts of this scope (needs --scope-ref)",
    )
    parser.add_argument(
        "--scope-ref",
        dest="scope_ref",
        metavar="REF",
        type=bounded_text("--scope-ref", 1, annotations_api.SCOPE_REF_MAX_LENGTH),
        help="the event type, event or metric id the scope names (needs --scope-type)",
    )
    add_write_flags(parser, prompts=False)
    add_json(parser)
    add_timeout(parser)
    parser.set_defaults(handler=run)


def http_url(flag: str, max_length: int) -> Callable[[str], str]:
    """An absolute http(s) URL, refused at parse time otherwise.

    The API enforces the same rule; checking it here means a ``--url`` pasted
    without its scheme costs no request and names the flag, rather than coming
    back as a 422 about a body field the operator never typed.
    """

    def _parse(raw: str) -> str:
        value = raw.strip()
        if len(value) > max_length:
            raise argparse.ArgumentTypeError(
                f"{flag} must be at most {max_length} characters, got {len(value)}"
            )
        parts = urlsplit(value)
        if parts.scheme.lower() not in URL_SCHEMES or not parts.netloc:
            raise argparse.ArgumentTypeError(
                f"{flag} must be an absolute http or https URL, got {raw!r}"
            )
        return value

    return _parse


def _scope(args: argparse.Namespace) -> tuple[str | None, str | None]:
    """Both or neither: a scope type with no ref would match no chart at all."""
    scope_type: str | None = args.scope_type
    scope_ref: str | None = args.scope_ref
    if (scope_type is None) != (scope_ref is None):
        raise TriplConfigError(
            "--scope-type and --scope-ref go together: give both, or neither for a "
            "project-level annotation. Nothing was sent."
        )
    return scope_type, scope_ref


def run(args: argparse.Namespace, config: Config) -> int:
    slug = require_single_project(args)
    scope_type, scope_ref = _scope(args)
    as_json: bool = bool(args.as_json)
    dry_run: bool = bool(args.dry_run)
    base_url = require_base_url(config)
    started = time.monotonic()
    generated_at = datetime.now(UTC)
    at: datetime = args.at if args.at is not None else generated_at
    request = annotations_api.create_annotation(
        slug,
        label=str(args.label),
        at=at,
        url=args.link,
        description=args.description,
        scope_type=scope_type,
        scope_ref=scope_ref,
    )

    async def body(client: httpx.AsyncClient) -> tuple[Reader, JsonDict | None, int | None]:
        reader = Reader(client, base_url)
        if dry_run:
            return reader, None, None
        result = as_dict(await reader.send(request))
        return reader, result, reader.last_status_code

    reader, result, status = run_async(config, body, timeout=float(args.timeout))
    outcome = MutationOutcome(
        command="annotate",
        run=Run(
            instance=instance_of(config, base_url, "unknown"),
            generated_at=generated_at,
            duration_ms=int((time.monotonic() - started) * 1000),
            requests=reader.requests,
        ),
        request=request_document(request),
        project=slug,
        dry_run=dry_run,
        deduplicated=_deduplicated(status),
        result=result,
    )
    _emit(outcome, base_url=base_url, config=config, as_json=as_json)
    return EXIT_OK


def _deduplicated(status: int | None) -> bool | None:
    """201 is new, 200 is the existing row, and no status means nothing was sent."""
    if status is None:
        return None
    return status == annotations_api.STATUS_DEDUPLICATED


def _emit(outcome: MutationOutcome, *, base_url: str, config: Config, as_json: bool) -> None:
    human = sys.stderr if as_json else sys.stdout
    print(
        render_header(outcome.command, base_url, config.sources.get("base_url", "unknown")),
        file=human,
    )
    print(file=human)
    print(render_mutation(outcome), file=human)
    if as_json:
        json.dump(mutation_document(outcome), sys.stdout)
        sys.stdout.write("\n")
