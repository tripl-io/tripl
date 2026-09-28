"""``tripl whoami`` — which account the configured key acts as, and where.

One read, ``GET /auth/me``, which already answers everything asked here: the
account, the organization the key is bound to (``org``, F20 PR6) and the key's
scope (``api_key_scope``). A project-bound key is refused that route by design
(``deps._enforce_project_scope``), and that 403 IS the answer "this key reaches
one project" — the same oracle ``tripl doctor``'s auth check reads.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime

import httpx

from tripl_cli.api import auth as auth_api
from tripl_cli.commands import add_json, add_timeout
from tripl_cli.config import Config, require_base_url
from tripl_cli.diagnostics.collect import Reader, instance_of
from tripl_cli.errors import EXIT_OK, TriplAPIError
from tripl_cli.model import (
    SCOPE_INSTANCE,
    SCOPE_PROJECT,
    JsonDict,
    Run,
    as_dict,
    whoami_of,
)
from tripl_cli.render import render_header, render_whoami
from tripl_cli.report import whoami_document
from tripl_cli.runner import run_async


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser(
        "whoami",
        parents=[parent],
        help="show the account, organization and scope of the configured API key",
        description=(
            "Read-only: prints the account the API key acts as, the organization it is "
            "bound to and its role there, and the key's scope (read or write; whole "
            "organization or one project). Exits 1 when the key is rejected."
        ),
    )
    add_json(parser)
    add_timeout(parser)
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace, config: Config) -> int:
    as_json: bool = bool(args.as_json)
    base_url = require_base_url(config)
    human = sys.stderr if as_json else sys.stdout
    started = time.monotonic()
    generated_at = datetime.now(UTC)

    async def body(client: httpx.AsyncClient) -> tuple[Reader, JsonDict | None, str]:
        reader = Reader(client, base_url)
        try:
            me = as_dict(await reader.send(auth_api.get_me()))
        except TriplAPIError as exc:
            if exc.status_code != 403:
                raise
            return reader, None, SCOPE_PROJECT
        return reader, me, SCOPE_INSTANCE

    reader, me, reach = run_async(config, body, timeout=float(args.timeout))
    whoami = whoami_of(
        Run(
            instance=instance_of(config, base_url, reach),
            generated_at=generated_at,
            duration_ms=int((time.monotonic() - started) * 1000),
            requests=reader.requests,
        ),
        me,
        reach=reach,
        api_key=config.api_key,
    )

    print(render_header("whoami", base_url, config.sources.get("base_url", "unknown")), file=human)
    print(file=human)
    print(render_whoami(whoami), file=human)
    if as_json:
        json.dump(whoami_document(whoami), sys.stdout)
        sys.stdout.write("\n")
    return EXIT_OK
