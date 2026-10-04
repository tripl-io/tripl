"""``tripl plan`` — read the tracking plan: types, fields, properties, branches, search.

READ ONLY. Every write route behind these reads either edits the live plan or
needs a branch to land on, and both decisions stay with a human in the tripl UI
(the same line ``api/branches.py`` already draws for merge, revert and branch
transitions).

WHY ONE GROUP, and why its verbs are nouns. The repository's grammar is
"a command acting on a class of objects is ``<plural-noun> <verb>``", which
``events list`` obeys exactly. ``plan types`` does not: ``types`` is a kind, not
an action. The alternative was one top-level group per REST collection —
``event-types list``, ``variables list``, ``branches list``, ``search`` — which
is four more entries in ``tripl --help`` for four reads, and that is the same
objection that rejected ``list-scans``. An operator does not arrive with "I want
to query the event-types collection"; they arrive with "what does this plan look
like", and this is one group per QUESTION rather than one per collection.

``plan diff`` is deliberately ABSENT even though ``api.branches.get_diff``
exists and the MCP exposes it. The diff route answers
``{behind_base, entries, summary}`` — not a list — so it does not fit the one
document shape the seven verbs here share, and ``behind_base`` is the load-
bearing field: a branch whose base moved under it has a diff that no longer says
what merging it would do. Smuggling that into an ``items`` array, or minting a
second document shape for one verb, are both worse than not shipping it yet.
"""

from __future__ import annotations

import argparse
from dataclasses import replace

import httpx

from tripl_cli.api import branches as branches_api
from tripl_cli.api import event_types as event_types_api
from tripl_cli.api import search as search_api
from tripl_cli.api import variables as variables_api
from tripl_cli.commands import (
    add_json,
    add_project,
    add_timeout,
    bounded_int,
    bounded_text,
    group_help,
    nonneg_int,
    require_single_project,
)
from tripl_cli.commands._plan import MAIN, add_branch, begin, emit, resolve_branch
from tripl_cli.commands._resolve import resolve_one
from tripl_cli.config import Config
from tripl_cli.errors import EXIT_OK
from tripl_cli.model import PlanRead, as_dict, as_list, names_by_id, page_items, page_total
from tripl_cli.render import (
    branch_rows,
    event_type_rows,
    field_rows,
    render_plan_read,
    search_rows,
    variable_rows,
)
from tripl_cli.runner import run_async


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser(
        "plan",
        parents=[parent],
        help="read the tracking plan: event types, fields, properties, branches, search",
        description=(
            "Read the structure a project's events are described by. Needs a tk_r_ key. "
            "Every verb except `branches` takes --branch to read a plan branch instead of "
            "the live main plan. Editing the plan is not available here."
        ),
    )
    verbs = parser.add_subparsers(dest="plan_command", metavar="<verb>")
    _register_types(verbs, parent)
    _register_fields(verbs, parent)
    _register_variables(verbs, parent)
    _register_branches(verbs, parent)
    _register_search(verbs, parent)
    parser.set_defaults(handler=group_help(parser))


def _register_types(
    verbs: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    parser = verbs.add_parser(
        "types",
        parents=[parent],
        help="list the project's event types",
        description=(
            "List the event types events belong to, with a field count each. The route "
            "answers a bare array and pages nothing, though it does apply a defensive "
            "1000-row cap. `tripl plan fields <event-type>` reads one type's definitions."
        ),
    )
    add_project(parser, single=True)
    add_branch(parser)
    add_json(parser)
    add_timeout(parser)
    parser.set_defaults(handler=run_types)


def _register_fields(
    verbs: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    parser = verbs.add_parser(
        "fields",
        parents=[parent],
        help="list one event type's field definitions",
        description=(
            "Print the field definitions of one event type: type, whether it is required, "
            "its sensitivity and its enum options. This is what a value has to satisfy."
        ),
    )
    parser.add_argument(
        "event_type", metavar="<event-type>", help="event type name or id (exact match)"
    )
    add_project(parser, single=True)
    add_branch(parser)
    add_json(parser)
    add_timeout(parser)
    parser.set_defaults(handler=run_fields)


def _register_variables(
    verbs: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    parser = verbs.add_parser(
        "properties",
        # The former name, kept so scripts written against it keep working (F23).
        aliases=["variables"],
        parents=[parent],
        help="list the event properties (the documented ${name} placeholders)",
        description=(
            "List the project's properties with their type, how many events use them and "
            "how many open value drifts they carry. ONE page per invocation: the footer "
            "says when there are more and --offset walks them. `tripl plan variables` is "
            "the same command under its former name."
        ),
    )
    add_project(parser, single=True)
    add_branch(parser)
    parser.add_argument(
        "--offset",
        dest="offset",
        metavar="N",
        type=nonneg_int("--offset"),
        default=0,
        help="skip this many properties, to read the next page (default: 0)",
    )
    parser.add_argument(
        "--limit",
        dest="limit",
        metavar="N",
        type=bounded_int("--limit", 1, variables_api.LIMIT_MAX),
        default=variables_api.LIMIT_DEFAULT,
        help=(
            f"how many properties to ask for, 1..{variables_api.LIMIT_MAX} "
            f"(default: {variables_api.LIMIT_DEFAULT})"
        ),
    )
    add_json(parser)
    add_timeout(parser)
    parser.set_defaults(handler=run_variables)


def _register_branches(
    verbs: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    parser = verbs.add_parser(
        "branches",
        parents=[parent],
        help="list the project's plan branches",
        description=(
            "List the plan branches, with how far ahead of their base each one is and "
            "whether its base has moved under it. This is where the name you pass to "
            "--branch comes from. Merging, reverting and branch transitions are NOT "
            "available here - those stay in the tripl UI."
        ),
    )
    add_project(parser, single=True)
    add_json(parser)
    add_timeout(parser)
    parser.set_defaults(handler=run_branches)


def _register_search(
    verbs: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    parser = verbs.add_parser(
        "search",
        parents=[parent],
        help="search the plan for an entity by phrase or partial name",
        description=(
            "Search a project's plan and get back entity ids to read with `tripl events "
            "show` or `tripl plan fields`. NOT offset-paged: raise --limit to see more. "
            "The document reports whether the semantic index answered, because a substring "
            "fallback makes a low score mean something different."
        ),
    )
    parser.add_argument(
        "query",
        metavar="<query>",
        type=bounded_text("<query>", 1, search_api.QUERY_MAX_LENGTH),
        help="phrase or partial name to search for",
    )
    add_project(parser, single=True)
    add_branch(parser)
    parser.add_argument(
        "--type",
        dest="types",
        metavar="TYPE",
        action="append",
        choices=search_api.ENTITY_TYPES,
        help=(f"restrict to this entity kind: {'|'.join(search_api.ENTITY_TYPES)} (repeatable)"),
    )
    parser.add_argument(
        "--limit",
        dest="limit",
        metavar="N",
        type=bounded_int("--limit", 1, search_api.LIMIT_MAX),
        default=search_api.LIMIT_DEFAULT,
        help=(
            f"how many hits to ask for, 1..{search_api.LIMIT_MAX} "
            f"(default: {search_api.LIMIT_DEFAULT})"
        ),
    )
    add_json(parser)
    add_timeout(parser)
    parser.set_defaults(handler=run_search)


def run_types(args: argparse.Namespace, config: Config) -> int:
    slug = require_single_project(args)
    selector: str | None = args.branch
    context = begin(config)

    async def body(client: httpx.AsyncClient) -> PlanRead:
        reader = context.reader(client)
        branch = await resolve_branch(reader, slug, selector)
        listing = as_list(
            await reader.send(event_types_api.list_event_types(slug, branch=branch.id))
        )
        return context.read(
            reader,
            command="plan types",
            kind="event_type",
            project=slug,
            branch=branch,
            items=listing,
            # No total, no offset, no limit: the route answers a bare array and
            # pages nothing, so `truncated` is false by construction and the
            # three nulls are what say the rows ARE the whole answer.
        )

    read = run_async(config, body, timeout=float(args.timeout))
    emit(
        read,
        context,
        as_json=bool(args.as_json),
        human=render_plan_read(
            read,
            event_type_rows(read.items),
            empty="no event types",
        ),
    )
    return EXIT_OK


def run_fields(args: argparse.Namespace, config: Config) -> int:
    slug = require_single_project(args)
    selector: str | None = args.branch
    event_type: str = str(args.event_type)
    context = begin(config)

    async def body(client: httpx.AsyncClient) -> PlanRead:
        reader = context.reader(client)
        branch = await resolve_branch(reader, slug, selector)
        # Resolved on the SAME branch the fields are then read from: an event
        # type that exists only on a branch has no id to find on main, and one
        # renamed on a branch would otherwise resolve to the main name.
        listing = as_list(
            await reader.send(event_types_api.list_event_types(slug, branch=branch.id))
        )
        type_id, _ = resolve_one(names_by_id(listing), event_type, what="event type")
        fields = as_list(
            await reader.send(event_types_api.get_fields(slug, type_id, branch=branch.id))
        )
        return context.read(
            reader,
            command="plan fields",
            kind="field",
            project=slug,
            branch=branch,
            items=fields,
        )

    read = run_async(config, body, timeout=float(args.timeout))
    emit(
        read,
        context,
        as_json=bool(args.as_json),
        human=render_plan_read(read, field_rows(read.items), empty="no field definitions"),
    )
    return EXIT_OK


def run_variables(args: argparse.Namespace, config: Config) -> int:
    slug = require_single_project(args)
    selector: str | None = args.branch
    offset: int = int(args.offset)
    limit: int = int(args.limit)
    context = begin(config)

    async def body(client: httpx.AsyncClient) -> PlanRead:
        reader = context.reader(client)
        branch = await resolve_branch(reader, slug, selector)
        payload = as_dict(
            await reader.send(
                variables_api.list_variables(slug, branch=branch.id, offset=offset, limit=limit)
            )
        )
        return context.read(
            reader,
            command="plan properties",
            kind="property",
            project=slug,
            branch=branch,
            items=page_items(payload),
            total=page_total(payload),
            offset=offset,
            limit=limit,
        )

    read = run_async(config, body, timeout=float(args.timeout))
    emit(
        read,
        context,
        as_json=bool(args.as_json),
        human=render_plan_read(
            read,
            variable_rows(read.items),
            empty="no properties",
        ),
    )
    return EXIT_OK


def run_branches(args: argparse.Namespace, config: Config) -> int:
    slug = require_single_project(args)
    context = begin(config)

    async def body(client: httpx.AsyncClient) -> PlanRead:
        reader = context.reader(client)
        # The only caller that asks for the diff counts: without them 'ahead'
        # prints '-' on every row and 'behind base' never appears, which are the
        # two columns this table exists for.
        payload = as_dict(
            await reader.send(branches_api.list_branches(slug, include_diff_counts=True))
        )
        return context.read(
            reader,
            command="plan branches",
            kind="branch",
            project=slug,
            # No branch, and no `--branch` flag: the listing is a property of
            # the project rather than of a revision.
            branch=MAIN,
            items=page_items(payload),
            total=page_total(payload),
        )

    read = run_async(config, body, timeout=float(args.timeout))
    emit(
        read,
        context,
        as_json=bool(args.as_json),
        human=render_plan_read(read, branch_rows(read.items), empty="no branches"),
    )
    return EXIT_OK


def run_search(args: argparse.Namespace, config: Config) -> int:
    slug = require_single_project(args)
    selector: str | None = args.branch
    limit: int = int(args.limit)
    # Already length-checked and stripped at parse time by `bounded_text`, so a
    # blank or oversized phrase cost no request and no socket.
    query: str = str(args.query)
    context = begin(config)

    async def body(client: httpx.AsyncClient) -> PlanRead:
        reader = context.reader(client)
        branch = await resolve_branch(reader, slug, selector)
        payload = as_dict(
            await reader.send(
                search_api.search_plan(
                    slug,
                    query,
                    types=list(args.types) if args.types else None,
                    limit=limit,
                    branch=branch.id,
                )
            )
        )
        read = context.read(
            reader,
            command="plan search",
            kind="search_result",
            project=slug,
            branch=branch,
            items=page_items(payload),
            # No total: the route answers `total = len(items)` AFTER trimming to
            # the limit, so echoing it would state "20 of 20 shown" on a search
            # that dropped hits. Null says the count is not on offer.
            total=None,
            # No offset: the route has no such parameter, so a hit past --limit
            # is unreachable except by raising it. Saying `offset: null` is what
            # tells a consumer paging is not on offer here.
            limit=limit,
            meta={"semantic_used": search_api.semantic_used(payload)},
        )
        # The route's OWN truncation flag, which `PlanRead.truncated` prefers to
        # its page-fullness guess. That guess reads True whenever
        # the matches land exactly on --limit, so this command was printing "more
        # may have matched" on the same HTTP body that said nothing was dropped —
        # and answering the opposite of `tripl-mcp`'s `search_plan`, which reads
        # the same key from the same body. None against an instance that predates
        # the field, which is what keeps the guess as the fallback there.
        #
        # Attached here rather than through `ReadContext.read` so the shared
        # constructor keeps only the envelope numbers all seven verbs answer.
        return replace(read, route_truncated=search_api.reported_truncated(payload))

    read = run_async(config, body, timeout=float(args.timeout))
    emit(
        read,
        context,
        as_json=bool(args.as_json),
        human=render_plan_read(read, search_rows(read.items), empty="no matches"),
    )
    return EXIT_OK
