"""The only module in ``watch`` that speaks HTTP, and it judges nothing.

Every read goes through ``diagnostics.collect.Reader.try_read_*``, so a 403, a
404 and a dead socket all arrive at the loop as DATA. ``Reader.read`` is never
called from here: in a polling loop the temptation TRAP B describes is worse than
in doctor, because a transient 500 read as "no new signals" makes watch quietly
stop reporting during the exact incident it was started for.

One ``gather_bounded`` per tick, so the whole fan-out is capped at
``MAX_CONCURRENT_REQUESTS`` sockets however many configs are followed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from tripl_cli.api import monitoring as monitoring_api
from tripl_cli.api import scans as scans_api
from tripl_cli.diagnostics.collect import Reader
from tripl_cli.model import Fetched, JsonDict, JsonList, page_items
from tripl_cli.runner import gather_bounded
from tripl_cli.watch.model import DELIVERY_STATUS_FAILED, JobTarget


@dataclass(frozen=True)
class TickReads:
    """What one tick asked for. A stream absent from a mapping was not read."""

    jobs: Mapping[JobTarget, Fetched[JsonList]] = field(default_factory=dict)
    scans: Mapping[str, Fetched[JsonList]] = field(default_factory=dict)
    signals: Mapping[str, Fetched[JsonList]] = field(default_factory=dict)
    deliveries: Mapping[str, Fetched[JsonDict]] = field(default_factory=dict)


async def read_tick(
    reader: Reader,
    *,
    job_targets: Sequence[JobTarget],
    scan_slugs: Sequence[str],
    signal_slugs: Sequence[str],
    delivery_slugs: Sequence[str],
    jobs_limit: int,
    deliveries_limit: int,
) -> TickReads:
    """Fire one tick's reads together and zip the answers back onto their targets.

    ``gather_bounded`` preserves input order, which is what makes the zips below
    correct rather than lucky.
    """
    job_reads = [
        reader.try_read_list(scans_api.list_jobs(slug, scan_id, limit=jobs_limit))
        for slug, scan_id in job_targets
    ]
    scan_reads = [reader.try_read_list(scans_api.list_configs(slug)) for slug in scan_slugs]
    # expanded=true is MANDATORY, not a preference: the collapsed list appends
    # SCOPE_EVENT only `if event_ids or expanded`, so an incident made purely of
    # event-scope anomalies would be dropped - precisely the incident watch would
    # be running for (TRAP A).
    signal_reads = [
        reader.try_read_list(monitoring_api.list_signals(slug, expanded=True))
        for slug in signal_slugs
    ]
    # No date floor. `date_from` filters AlertDelivery.created_at, so a delivery
    # created before the floor that FAILS during the run would be invisible;
    # asking for the newest failures instead has no coverage gap and one fewer flag.
    delivery_reads = [
        reader.try_read_dict(
            monitoring_api.list_alert_deliveries(
                slug, status=DELIVERY_STATUS_FAILED, limit=deliveries_limit
            )
        )
        for slug in delivery_slugs
    ]

    lists = await gather_bounded([*job_reads, *scan_reads, *signal_reads])
    dicts = await gather_bounded(delivery_reads)

    cut_jobs = len(job_reads)
    cut_scans = cut_jobs + len(scan_reads)
    return TickReads(
        jobs=dict(zip(job_targets, lists[:cut_jobs], strict=True)),
        scans=dict(zip(scan_slugs, lists[cut_jobs:cut_scans], strict=True)),
        signals=dict(zip(signal_slugs, lists[cut_scans:], strict=True)),
        deliveries=dict(zip(delivery_slugs, dicts, strict=True)),
    )


def deliveries_of(payload: JsonDict) -> JsonList:
    """``AlertDeliveryListResponse.items`` — the endpoint returns {items, total}.

    Kept as a name of its own because WHICH envelope this is, is the fact worth
    writing down here; the unwrapping itself is ``model.page_items``, which every
    reader of that envelope in either distribution now goes through. This was the
    last hand-written copy of it outside that definition.
    """
    return page_items(payload)


def describe(fetched: Fetched[Any]) -> str:
    return fetched.error or "the tripl API did not answer"
