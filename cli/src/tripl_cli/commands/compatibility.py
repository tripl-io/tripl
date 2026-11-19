"""Compile a branch-authored compatibility mapping into reviewable warehouse SQL."""

from __future__ import annotations

import argparse
import json
from datetime import date
from typing import Any

import httpx

from tripl_cli.api import branches, events, meta_fields
from tripl_cli.commands import (
    add_json,
    add_project,
    add_timeout,
    group_help,
    require_single_project,
)
from tripl_cli.commands._plan import add_branch, begin
from tripl_cli.commands._resolve import resolve_one
from tripl_cli.config import Config
from tripl_cli.errors import TriplConfigError
from tripl_cli.model import as_dict, as_list, names_by_id, page_items
from tripl_cli.runner import run_async

FIELD = "compatibility_mapping"


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser], parent: argparse.ArgumentParser
) -> None:
    group = subparsers.add_parser(
        "compatibility", parents=[parent], help="compile a catalog mapping into ClickHouse SQL"
    )
    group.set_defaults(handler=group_help(group))
    verbs = group.add_subparsers(dest="compatibility_verb", metavar="<verb>")
    for name, help_text in [
        ("preview", "generate a bounded history check for a plan branch"),
        ("model", "generate the logical event model from the live main plan"),
    ]:
        parser = verbs.add_parser(name, parents=[parent], help=help_text)
        parser.add_argument("event_id", help="logical event ID in the selected branch")
        if name == "preview":
            add_branch(parser)
            parser.add_argument(
                "--start", type=date.fromisoformat, required=True, help="UTC start date (inclusive)"
            )
            parser.add_argument(
                "--end",
                type=date.fromisoformat,
                required=True,
                help="UTC end date (exclusive), up to 31 days from start",
            )
        add_project(parser, single=True)
        add_json(parser)
        add_timeout(parser)
        parser.set_defaults(handler=run)


def _mapping(event: dict[str, Any], definitions: list[dict[str, Any]]) -> object:
    fields = [f for f in definitions if f.get("name") == FIELD]
    if (
        len(fields) != 1
        or fields[0].get("field_type") != "string"
        or fields[0].get("allow_multiple")
        or not fields[0].get("id")
    ):
        raise TriplConfigError(f"define {FIELD} as a single string meta field on this branch")
    values = [
        v.get("value")
        for v in as_list(event.get("meta_values"))
        if v.get("meta_field_definition_id") == fields[0]["id"]
    ]
    if len(values) != 1 or not isinstance(values[0], str):
        raise TriplConfigError(f"set one {FIELD} JSON value on the selected event")
    try:
        return json.loads(values[0])
    except (ValueError, RecursionError):
        raise TriplConfigError(f"{FIELD} must be valid JSON") from None


def run(args: argparse.Namespace, config: Config) -> int:
    from tripl_cli.compat_sql import compile_model, compile_preview

    slug = require_single_project(args)
    context = begin(config)
    selector = getattr(args, "branch", None)

    async def read(client: httpx.AsyncClient) -> dict[str, Any]:
        reader = context.reader(client)
        listing = page_items(await reader.send(branches.list_branches(slug)))
        if selector is None:
            mains = [b for b in listing if b.get("kind") == "main" and b.get("id")]
            if len(mains) != 1:
                raise TriplConfigError("could not resolve the project's main branch")
            branch_id, branch_name = str(mains[0]["id"]), str(mains[0]["name"])
        else:
            branch_id, branch_name = resolve_one(names_by_id(listing), selector, what="branch")
        event = as_dict(await reader.send(events.get_event(slug, args.event_id, branch=branch_id)))
        if event.get("branch_id") != branch_id or event.get("id") != args.event_id:
            raise TriplConfigError("the event does not belong to the requested branch")
        defs = as_list(await reader.send(meta_fields.list_fields(slug, branch_id)))
        mapping = _mapping(event, defs)
        logical = event.get("name")
        if not isinstance(logical, str) or not logical:
            raise TriplConfigError("the selected event has no logical name")
        try:
            sql = (
                compile_model(mapping, logical)
                if args.compatibility_verb == "model"
                else compile_preview(mapping, logical, args.start, args.end)
            )
        except ValueError as exc:
            raise TriplConfigError(str(exc)) from None
        return {
            "project": slug,
            "branch": branch_name,
            "branch_id": branch_id,
            "event_id": args.event_id,
            "logical_event": logical,
            "sql": sql,
        }

    result = run_async(config, read, timeout=float(args.timeout))
    print(json.dumps(result) if args.as_json else result["sql"])
    return 0
