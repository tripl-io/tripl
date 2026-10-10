"""Warehouse scan tools: list configs, trigger runs, poll job status."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from tripl_cli.api import scans, send

from tripl_mcp.runtime import client_for
from tripl_mcp.tools._common import READ_ONLY, WRITE, summarize_collection


async def list_scans(
    slug: str,
    ctx: Context,
) -> Any:
    client = client_for(ctx)
    data = await send(client, scans.list_configs(slug))
    if not isinstance(data, list):
        return data
    return [scans.scan_config_summary(item) if isinstance(item, dict) else item for item in data]


async def get_scan(
    slug: str,
    scan_id: str,
    ctx: Context,
) -> Any:
    client = client_for(ctx)
    return await send(client, scans.get_config(slug, scan_id))


async def trigger_scan(
    slug: str,
    scan_id: str,
    ctx: Context,
) -> Any:
    client = client_for(ctx)
    return await send(client, scans.run(slug, scan_id))


async def get_scan_status(
    slug: str,
    scan_id: str,
    ctx: Context,
    job_id: str | None = None,
) -> Any:
    client = client_for(ctx)
    if job_id is not None:
        return await send(client, scans.get_job(slug, scan_id, job_id))
    # No explicit limit: an agent polling status wants the newest few and the
    # server's own default (50) is the right budget. doctor asks the same builder
    # for the maximum instead, because it is measuring how long a streak has run.
    jobs = await send(client, scans.list_jobs(slug, scan_id))
    return summarize_collection(jobs)


def register(mcp: MCPServer) -> None:
    mcp.tool(
        name="list_scans",
        annotations=READ_ONLY,
        description=(
            "List the project's warehouse scan configurations (id, name, schedule, "
            "governed event types) plus a 'dispatchable' flag: whether the scheduler "
            "would ever select the config. Trimmed - base_query and the tuning knobs "
            "are omitted; use get_scan for one config in full. Requires a tk_r_ or "
            "tk_w_ key."
        ),
    )(list_scans)
    mcp.tool(
        name="get_scan",
        annotations=READ_ONLY,
        description=(
            "One scan configuration in full, including base_query and every tuning "
            "knob that list_scans trims. Use it when list_scans has narrowed the "
            "field to one config and you need the detail. Requires a tk_r_ or tk_w_ "
            "key."
        ),
    )(get_scan)
    mcp.tool(
        name="trigger_scan",
        annotations=WRITE,
        description=(
            "WRITE: trigger a warehouse scan run now (needs a tk_w_ key). Kicks off "
            "an async job — it does NOT edit the plan directly, but its results can "
            "update observed traffic and drift state. Poll progress with "
            "get_scan_status using the returned job id."
        ),
    )(trigger_scan)
    mcp.tool(
        name="get_scan_status",
        annotations=READ_ONLY,
        description=(
            "Check scan job status. With job_id: that job's detail. Without: recent "
            "jobs for the scan (count + latest sample). Requires a tk_r_ or tk_w_ "
            "key."
        ),
    )(get_scan_status)
