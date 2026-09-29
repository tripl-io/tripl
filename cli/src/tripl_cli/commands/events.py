"""``tripl events`` — read the catalog: list events, read one in full.

READ ONLY, and that is a decision rather than a first instalment. The routes to
create and update an event exist and ``tripl_cli.api.events`` builds them, but a
catalog write has to land on a plan branch — ``tripl-mcp`` makes ``branch_id``
REQUIRED on both of its write tools for the reason its descriptions give: an
edit that lands on main is an edit to the live plan nobody reviewed. Reproducing
that gate from a shell (resolve a branch, refuse main, prompt, then read the
server's canonical-name warnings back and tell the operator their proposed name
was ignored) is a command surface of its own, and shipping the easy half first
would leave the CLI with a write easier to get wrong than the agent's
(tripl-3ixs).

Both verbs take exactly one ``--project``: every route here is per project and
there is no instance-wide form, so a repeated ``--project`` would be a fan-out
this command does not do rather than a filter to widen.
"""

from __future__ import annotations

import argparse

import httpx

from tripl_cli.api import event_types as event_types_api
from tripl_cli.api import events as events_api
from tripl_cli.commands import (
    add_json,
    add_project,
    add_timeout,
    bounded_int,
    group_help,
    nonneg_int,
    require_single_project,
)
from tripl_cli.commands._plan import Branch, add_branch, begin, emit, resolve_branch
from tripl_cli.config import Config
from tripl_cli.diagnostics.collect import Reader
from tripl_cli.errors import EXIT_OK, TriplAPIError
from tripl_cli.model import JsonDict, JsonList, PlanRead, as_dict, as_list, page_items, page_total
from tripl_cli.render import event_rows, render_event_detail, render_plan_read
from tripl_cli.runner import run_async


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser(
        "events",
        parents=[parent],
        help="read a project's event catalog",
        description=(
            "Read the catalog of tracked events. Needs a tk_r_ key. Creating and updating "
            "events is NOT available here - a catalog edit has to land on a plan branch, and "
            "that gate stays in the tripl UI and the MCP server."
        ),
    )
    verbs = parser.add_subparsers(dest="events_command", metavar="<verb>")
    _register_list(verbs, parent)
    _register_show(verbs, parent)
    # Bare `tripl events` prints this group's help and exits 2, the same rule
    # every other group follows.
    parser.set_defaults(handler=group_help(parser))


def _register_list(
    verbs: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    parser = verbs.add_parser(
        "list",
        parents=[parent],
        help="list catalog events, with filters",
        description=(
            "List a project's events using the API's own filters. ONE page per invocation: "
            "the footer says when there are more and --offset walks them. The table shows "
            "what an operator scans for; --json carries every field and meta value."
        ),
    )
    add_project(parser, single=True)
    add_branch(parser)
    parser.add_argument(
        "--search",
        dest="search",
        metavar="TEXT",
        help="substring match over event name and description",
    )
    parser.add_argument(
        "--status",
        dest="status",
        metavar="STATUS",
        action="append",
        choices=events_api.STATUSES,
        help=(
            f"keep only this lifecycle status: {'|'.join(events_api.STATUSES)} "
            "(repeatable; the API keeps an event matching ANY of them)"
        ),
    )
    parser.add_argument("--tag", dest="tag", metavar="TAG", help="exact tag match")
    parser.add_argument(
        "--field-value",
        dest="field_value",
        metavar="TEXT",
        help="substring match on any field value, e.g. a screen name (case-insensitive)",
    )
    parser.add_argument(
        "--meta-value",
        dest="meta_value",
        metavar="TEXT",
        # "substring", not "exact": the route compares with ILIKE '%value%', so
        # `--meta-value TRIPL-4` also keeps TRIPL-412. This line claimed "exact
        # match" until tripl-nhj0 put the sibling --field-value next to it and
        # the two would have described one ILIKE two different ways.
        help="substring match on any meta value, e.g. a ticket key (case-insensitive)",
    )
    parser.add_argument(
        "--event-type",
        dest="event_type_id",
        metavar="ID",
        help="keep only events of this event type id (from `tripl plan types`)",
    )
    parser.add_argument(
        "--property",
        dest="property",
        metavar="NAME_OR_ID",
        help="keep only events whose property list carries this property (name or id)",
    )
    parser.add_argument(
        "--silent-since-days",
        dest="silent_since_days",
        metavar="N",
        type=bounded_int("--silent-since-days", 0, events_api.SILENT_SINCE_DAYS_MAX),
        help=(
            "keep only events the warehouse has not carried for this many days, "
            f"0..{events_api.SILENT_SINCE_DAYS_MAX}"
        ),
    )
    # Two flags rather than one taking a value: the axis is tri-state and its
    # third state is "leave the parameter off", which `--reviewed BOOL` could
    # only spell as a magic word. Mutually exclusive so giving both is exit 2
    # rather than a silent last-one-wins, and `default=None` on both is what
    # keeps `reviewed=` off the wire when neither was given.
    review = parser.add_mutually_exclusive_group()
    review.add_argument(
        "--reviewed",
        dest="reviewed",
        action="store_const",
        const=True,
        default=None,
        help="keep only events already marked reviewed",
    )
    review.add_argument(
        "--unreviewed",
        dest="reviewed",
        action="store_const",
        const=False,
        default=None,
        help=(
            "keep only events not yet marked reviewed (an axis of its own: an "
            "event can be marked reviewed and still sit at --status in_review)"
        ),
    )
    # Same tri-state shape as --reviewed/--unreviewed above, for the same
    # reason: the third state is "leave the parameter off".
    questions = parser.add_mutually_exclusive_group()
    questions.add_argument(
        "--open-questions",
        dest="has_open_questions",
        action="store_const",
        const=True,
        default=None,
        help="keep only events whose discussion has an unanswered thread",
    )
    questions.add_argument(
        "--no-open-questions",
        dest="has_open_questions",
        action="store_const",
        const=False,
        default=None,
        help="keep only events with nothing left open in their discussion",
    )
    parser.add_argument(
        "--offset",
        dest="offset",
        metavar="N",
        type=nonneg_int("--offset"),
        default=0,
        help="skip this many events, to read the next page (default: 0)",
    )
    parser.add_argument(
        "--limit",
        dest="limit",
        metavar="N",
        type=bounded_int("--limit", 1, events_api.LIMIT_MAX),
        default=events_api.LIMIT_DEFAULT,
        help=(
            f"how many events to ask for, 1..{events_api.LIMIT_MAX} "
            f"(default: {events_api.LIMIT_DEFAULT})"
        ),
    )
    # No `default=`, unlike --limit: omitting the flag leaves `order_by` off the
    # wire so the route applies its own default, which is what keeps this CLI
    # following the route rather than pinning today's answer. `choices=` costs
    # the request a Literal mismatch would otherwise spend on a 422.
    parser.add_argument(
        "--order-by",
        dest="order_by",
        metavar="ORDER",
        choices=events_api.ORDER_BY,
        help=(
            f"order the page by {'|'.join(events_api.ORDER_BY)} "
            f"(default: {events_api.ORDER_BY_DEFAULT}, the authored catalog order; "
            "volume ranks busiest-first by ingested volume over the last 24h; "
            "health ranks least healthy first, main plan only)"
        ),
    )
    add_json(parser)
    add_timeout(parser)
    parser.set_defaults(handler=run_list)


def _register_show(
    verbs: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    parser = verbs.add_parser(
        "show",
        parents=[parent],
        help="print one event in full, field values included",
        description=(
            "Print one event by id. Reads the event type's field definitions too, so the "
            "field values appear under the field NAMES they set rather than as definition "
            "ids - which is the part of this command worth having."
        ),
    )
    parser.add_argument(
        "event_id", metavar="<event-id>", help="the event to read (from `tripl events list`)"
    )
    add_project(parser, single=True)
    add_branch(parser)
    add_json(parser)
    add_timeout(parser)
    parser.set_defaults(handler=run_show)


def run_list(args: argparse.Namespace, config: Config) -> int:
    slug = require_single_project(args)
    offset: int = int(args.offset)
    limit: int = int(args.limit)
    selector: str | None = args.branch
    context = begin(config)

    async def body(client: httpx.AsyncClient) -> PlanRead:
        reader = context.reader(client)
        branch = await resolve_branch(reader, slug, selector)
        # `send`, not `try_read_*`: this command reaches no verdict, so a 403 is
        # an ordinary failure that exits 1 with the API's own sentence rather
        # than an empty table at exit 0 (TRAP 3).
        payload = as_dict(
            await reader.send(
                events_api.list_events(
                    slug,
                    search=args.search,
                    status=list(args.status) if args.status else None,
                    tag=args.tag,
                    field_value=args.field_value,
                    meta_value=args.meta_value,
                    event_type_id=args.event_type_id,
                    silent_since_days=args.silent_since_days,
                    reviewed=args.reviewed,
                    has_open_questions=args.has_open_questions,
                    property=args.property,
                    offset=offset,
                    limit=limit,
                    order_by=args.order_by,
                    branch=branch.id,
                )
            )
        )
        return context.read(
            reader,
            command="events list",
            kind="event",
            project=slug,
            branch=branch,
            items=page_items(payload),
            # The API's count BEFORE paging. It is the only number that can tell
            # "the page filled up" from "the catalog ended".
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
            event_rows(read.items),
            empty="no events match",
        ),
    )
    return EXIT_OK


def run_show(args: argparse.Namespace, config: Config) -> int:
    slug = require_single_project(args)
    event_id: str = str(args.event_id)
    selector: str | None = args.branch
    context = begin(config)

    async def body(client: httpx.AsyncClient) -> tuple[PlanRead, JsonDict, JsonList]:
        reader = context.reader(client)
        branch = await resolve_branch(reader, slug, selector)
        event = as_dict(await reader.send(events_api.get_event(slug, event_id, branch=branch.id)))
        fields = await _fields_of(reader, slug, event, branch)
        # The typed property list (F23), carried on the row so `--json` has it
        # too. A server that predates the route answers 404, read as no list.
        event = {**event, "properties": await _properties_of(reader, slug, event_id, branch)}
        return (
            context.read(
                reader,
                command="events show",
                kind="event",
                project=slug,
                branch=branch,
                # A single row, under the same `items` key as every listing.
                # One `jq` selector for the whole family beats a second shape
                # for the one verb that returns exactly one thing.
                items=[event],
            ),
            event,
            fields,
        )

    read, event, fields = run_async(config, body, timeout=float(args.timeout))
    emit(
        read,
        context,
        as_json=bool(args.as_json),
        human=render_event_detail(read, event, fields),
    )
    return EXIT_OK


async def _properties_of(reader: Reader, slug: str, event_id: str, branch: Branch) -> JsonList:
    try:
        return as_list(
            await reader.send(events_api.get_event_properties(slug, event_id, branch=branch.id))
        )
    except TriplAPIError as exc:
        if exc.status_code == 404:
            return []
        raise


async def _fields_of(reader: Reader, slug: str, event: JsonDict, branch: Branch) -> JsonList:
    """The event type's field definitions, so a value can be shown under its name.

    Raises like every other read here rather than falling back to definition
    ids: the same key just read the event, so a refusal on its event type is a
    real refusal, and a command that quietly degraded its own headline output
    would teach an operator to distrust the labels everywhere.

    The one no-request case is an event carrying no ``event_type_id`` at all —
    a payload that should not exist. It renders with ids and says nothing,
    because there is no request that could resolve them.
    """
    event_type_id = event.get("event_type_id")
    if not isinstance(event_type_id, str) or not event_type_id:
        return []
    return as_list(
        await reader.send(event_types_api.get_fields(slug, event_type_id, branch=branch.id))
    )
