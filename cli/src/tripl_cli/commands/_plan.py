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
import os
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
from tripl_cli.errors import TriplConfigError
from tripl_cli.model import (
    JsonDict,
    PlanRead,
    Run,
    as_dict,
    names_by_id,
    page_items,
    text_of,
)
from tripl_cli.render import plan_branch_note, render_header
from tripl_cli.report import plan_read_document


@dataclass(frozen=True)
class Branch:
    """Which revision a read was answered from. Both ``None`` means main.

    Not an id alone: the operator typed a name, and a UUID echoed back at them
    in the output and the JSON is not an answer to "which branch did this read".
    """

    id: str | None = None
    name: str | None = None
    # The working branch that was asked for, by its name (as typed when no
    # branch has it), and its review status (None when no branch has it). Both
    # stay None when no branch was asked for, or the one asked for is main.
    # ``id`` None with ``requested`` set means the read fell back to main.
    requested: str | None = None
    status: str | None = None

    @property
    def note(self) -> str | None:
        """The line a run prints about the branch it asked for, or None for nothing to say."""
        return self.describe()

    def describe(self, *, fallback_flag: bool = True) -> str | None:
        """``note``, for a command with (the default) or without ``--branch-if-exists``."""
        if self.requested is None:
            return None
        return plan_branch_note(
            self.requested, self.status, used_main=self.id is None, fallback_flag=fallback_flag
        )

    @property
    def document(self) -> JsonDict | None:
        """``plan_branch`` in a JSON document: what was asked for and what was read."""
        if self.requested is None:
            return None
        return {
            "requested": self.requested,
            "status": self.status,
            "used": "main" if self.id is None else "branch",
        }


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


# A working branch in one of these states no longer changes: the instance still
# answers reads of it, from the copy it held when it was merged or closed.
ENDED_STATUSES = ("merged", "closed")
KIND_MAIN = "main"

# Read by ``tripl check`` and ``tripl codegen`` as a ``--branch-if-exists`` given
# by the environment, so a pipeline can export its git branch name once. Not a
# ``TRIPL_CHECK_*`` name: those are the CI templates' own job variables, which
# the CLI never reads.
ENV_PLAN_BRANCH = "TRIPL_PLAN_BRANCH"

FLAG_BRANCH = "--branch"
FLAG_BRANCH_IF_EXISTS = "--branch-if-exists"


@dataclass(frozen=True)
class BranchRequest:
    """The plan branch a run asks for, and what to do when it cannot be used.

    ``fallback`` false is ``--branch``: the branch must exist. True is
    ``--branch-if-exists``: no such branch, or one already merged or closed,
    means the live main plan. ``source`` names where the request came from, for
    the messages that refuse it.
    """

    selector: str
    fallback: bool
    source: str
    # Set when the environment variable took the place of another branch named
    # in the check config: the line that says so, for stderr.
    replaced: str | None = None


def add_branch_if_exists(parser: argparse.ArgumentParser) -> None:
    """``--branch-if-exists REF``, on the commands a pipeline runs for every git branch.

    Only ``tripl check`` and ``tripl codegen``: a pipeline cannot know whether a
    plan branch is named like its git branch, and ``--branch`` exits 2 when none
    is. An operator browsing the plan names a branch they know exists.
    """
    parser.add_argument(
        FLAG_BRANCH_IF_EXISTS,
        dest="branch_if_exists",
        metavar="REF",
        help=(
            "read this plan branch when it exists and is still open, else the live main "
            "plan; name or id, matched exactly (for CI: pass the git branch name)"
        ),
    )


def branch_request(
    args: argparse.Namespace,
    *,
    config_branch: str | None = None,
    config_branch_if_exists: str | None = None,
    environment: bool = True,
) -> BranchRequest | None:
    """Which plan branch this run asks for, or None for main.

    Highest first: ``--branch`` or ``--branch-if-exists`` (both is a refusal),
    then ``TRIPL_PLAN_BRANCH``, then ``branch`` or ``branch_if_exists`` in the
    check config. ``environment`` false is for a command that has no
    ``--branch-if-exists`` and so does not read the variable.

    An empty ``--branch-if-exists`` or variable names no branch: it is what a
    pipeline passes when the variable it reads the git branch from is not set.
    """
    flag: str | None = getattr(args, "branch", None)
    flag_if_exists: str | None = getattr(args, "branch_if_exists", None)
    if flag is not None and flag_if_exists is not None:
        raise TriplConfigError(
            f"{FLAG_BRANCH} and {FLAG_BRANCH_IF_EXISTS} both name the plan branch; pick one "
            f"({FLAG_BRANCH} fails when the branch does not exist, {FLAG_BRANCH_IF_EXISTS} "
            "reads main then)."
        )
    if flag is not None:
        return BranchRequest(flag, fallback=False, source=FLAG_BRANCH)
    if flag_if_exists is not None and flag_if_exists.strip():
        return BranchRequest(flag_if_exists.strip(), fallback=True, source=FLAG_BRANCH_IF_EXISTS)
    configured: BranchRequest | None = None
    if config_branch is not None:
        configured = BranchRequest(
            config_branch, fallback=False, source="`branch` in the check config"
        )
    elif config_branch_if_exists is not None:
        configured = BranchRequest(
            config_branch_if_exists,
            fallback=True,
            source="`branch_if_exists` in the check config",
        )
    exported = os.environ.get(ENV_PLAN_BRANCH, "").strip() if environment else ""
    if not exported:
        return configured
    replaced = None
    if configured is not None and configured.selector != exported:
        replaced = (
            f"{ENV_PLAN_BRANCH} names plan branch {exported}, so {configured.source} "
            f"({configured.selector}) is not used."
        )
    return BranchRequest(exported, fallback=True, source=ENV_PLAN_BRANCH, replaced=replaced)


async def resolve_branch(
    reader: Reader, slug: str, selector: str | None, *, fallback: bool = False
) -> Branch:
    """``--branch`` -> the id the API wants, or main. One extra request, only when asked.

    ``None`` in, ``Branch()`` out, and every builder then omits ``?branch=``
    entirely — which is how the API itself spells main (see ``api/branches.py``).

    The branch's review status comes back with it, so a caller can say that a
    merged or closed branch was read: the instance answers such a read from the
    copy the branch held when it ended, which is not what main holds now.

    With ``fallback`` (``--branch-if-exists``) a selector that matches no branch,
    or one that is merged or closed, resolves to main, with what was asked for
    kept on the result. A selector that matches the main branch itself is main,
    with nothing to report. Two matches are still a refusal.
    """
    if selector is None:
        return MAIN
    listing = page_items(as_dict(await reader.send(branches_api.list_branches(slug))))
    named = names_by_id(listing)
    if fallback and selector not in named and selector not in named.values():
        return Branch(requested=selector)
    branch_id, name = resolve_one(named, selector, what="branch")
    row = next(item for item in listing if text_of(item, "id") == branch_id)
    # Main-ness first: the main branch is stored with status "merged", which
    # says nothing about it.
    if text_of(row, "kind") == KIND_MAIN:
        return MAIN if fallback else Branch(id=branch_id, name=name)
    status = text_of(row, "status") or None
    if fallback and status in ENDED_STATUSES:
        return Branch(requested=name, status=status)
    return Branch(id=branch_id, name=name, requested=name, status=status)


async def resolve_request(reader: Reader, slug: str, request: BranchRequest | None) -> Branch:
    """``resolve_branch`` for a request from ``branch_request``."""
    if request is None:
        return MAIN
    return await resolve_branch(reader, slug, request.selector, fallback=request.fallback)


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
