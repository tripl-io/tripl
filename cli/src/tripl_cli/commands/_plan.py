"""What every ``tripl events`` and ``tripl plan`` verb does identically.

Seven verbs read seven resources and print seven tables. Everything AROUND that
— resolving ``--branch``, opening a counting reader, stamping the run, choosing
which stream the human lines go to, and writing at most one JSON document — is
the same seven times, and was already spelled six times across ``scans.py`` and
``drifts.py`` before these landed. It is spelled once here.

The ``--branch`` rule is the load-bearing part. Every plan read is answered from
ONE revision, and which one is decided by a query parameter that
``backend/openapi.json`` does not document — ``deps.get_branch_id_override``
reads ``?branch=`` off the raw query string, so the contract test cannot see it
and cannot protect it. That makes it the one parameter here worth stating
plainly wherever it appears: absent means the live main plan, and a branch is
named by name or id through the same matcher ``<scan>`` uses, so neither can
silently resolve to something the operator did not ask for.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from tripl_cli.api import branches as branches_api
from tripl_cli.commands._resolve import resolve_one
from tripl_cli.config import Config, require_base_url
from tripl_cli.diagnostics.collect import Reader, instance_of
from tripl_cli.model import JsonDict, PlanRead, Run, as_dict, names_by_id, page_items
from tripl_cli.render import render_header
from tripl_cli.report import plan_read_document


@dataclass(frozen=True)
class Branch:
    """Which revision a read was answered from. Both ``None`` means main.

    Not an id alone: the operator typed a name, and a UUID echoed back at them
    in the output and the JSON is not an answer to "which branch did this read".
    """

    id: str | None = None
    name: str | None = None


# The live plan, spelled the way the API spells it: by naming no branch at all.
# A shared instance rather than a `Branch()` at each call site, so "this read was
# answered from main" is one object every verb points at.
MAIN = Branch()


def add_branch(parser: argparse.ArgumentParser) -> None:
    """``--branch REF`` on every verb whose route resolves a plan revision.

    NOT on ``tripl plan branches``: the branch listing is a property of the
    project, not of a revision, and a flag that quietly did nothing would be
    read as one that did something.
    """
    parser.add_argument(
        "--branch",
        dest="branch",
        metavar="REF",
        help=(
            "read this plan branch instead of the live main plan; "
            "name or id, matched exactly (see `tripl plan branches`)"
        ),
    )


async def resolve_branch(reader: Reader, slug: str, selector: str | None) -> Branch:
    """``--branch`` -> the id the API wants, or main. One extra request, only when asked.

    ``None`` in, ``Branch()`` out, and every builder then omits ``?branch=``
    entirely — which is how the API itself spells main (see ``api/branches.py``).
    """
    if selector is None:
        return MAIN
    listing = page_items(as_dict(await reader.send(branches_api.list_branches(slug))))
    branch_id, name = resolve_one(names_by_id(listing), selector, what="branch")
    return Branch(id=branch_id, name=name)


@dataclass(frozen=True)
class ReadContext:
    """The connection settings and the clock, resolved once per invocation.

    ``base_url`` is demanded here, before the event loop opens, so "you have not
    configured a URL" stays exit 2 with no socket touched — the same separation
    ``runner.run_async`` enforces for the API key.
    """

    config: Config
    base_url: str
    started: float
    generated_at: datetime

    def reader(self, client: httpx.AsyncClient) -> Reader:
        return Reader(client, self.base_url)

    def read(
        self,
        reader: Reader,
        *,
        command: str,
        kind: str,
        project: str,
        branch: Branch,
        items: Sequence[JsonDict],
        total: int | None = None,
        offset: int | None = None,
        limit: int | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> PlanRead:
        return PlanRead(
            run=Run(
                instance=instance_of(self.config, self.base_url, "unknown"),
                generated_at=self.generated_at,
                duration_ms=int((time.monotonic() - self.started) * 1000),
                requests=reader.requests,
            ),
            command=command,
            kind=kind,
            project=project,
            items=tuple(items),
            branch_id=branch.id,
            branch_name=branch.name,
            total=total,
            offset=offset,
            limit=limit,
            meta=dict(meta or {}),
        )


def begin(config: Config) -> ReadContext:
    return ReadContext(
        config=config,
        base_url=require_base_url(config),
        started=time.monotonic(),
        generated_at=datetime.now(UTC),
    )


def emit(read: PlanRead, context: ReadContext, *, as_json: bool, human: str) -> None:
    """The header, the body, and at most one JSON document. One stream rule, one place.

    Under ``--json`` every human line goes to stderr — including the header —
    so stdout carries the document and nothing else. Seven verbs, one
    implementation of that promise.
    """
    stream = sys.stderr if as_json else sys.stdout
    source = context.config.sources.get("base_url", "unknown")
    print(render_header(read.command, context.base_url, source), file=stream)
    print(file=stream)
    print(human, file=stream)
    if as_json:
        json.dump(plan_read_document(read), sys.stdout)
        sys.stdout.write("\n")
