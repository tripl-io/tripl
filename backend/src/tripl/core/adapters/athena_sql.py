"""Amazon Athena connection building blocks that import no driver.

``pyathena`` (and through it ``boto3``) is imported lazily
(:func:`import_driver`), the way the registry imports every adapter, so a
process that never builds an Athena adapter never loads it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import ModuleType

from tripl.core.adapters.errors import WarehouseCapabilityError

#: An AWS region code: ``us-east-1``, ``eu-central-2``, ``us-gov-west-1``,
#: ``cn-north-1``.
_REGION_RE = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-\d{1,2}$")

#: ``athena.<region>.amazonaws.com`` (``.com.cn`` in the China regions), the
#: only names the connection is allowed to reach.
_ATHENA_HOST_RE = re.compile(r"^athena\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?$")


@dataclass(frozen=True)
class AthenaEndpoint:
    """Where the Athena API is reached: the region, and its hostname."""

    region: str
    hostname: str


def resolve_endpoint(host: str) -> AthenaEndpoint:
    """The region and API hostname for what the ``host`` field holds.

    Accepts a region code (``eu-west-1``) or the regional endpoint
    (``athena.eu-west-1.amazonaws.com``), nothing else: boto3 is only ever
    pointed at the region's own AWS endpoint, so no host field can steer the
    signed requests (and the access key's signature) anywhere else.
    """
    name = host.strip().lower().rstrip(".")
    for prefix in ("https://", "http://"):
        if name.startswith(prefix):
            name = name[len(prefix) :].rstrip("/")
    match = _ATHENA_HOST_RE.match(name)
    region = match.group(1) if match else name
    if not _REGION_RE.match(region):
        raise WarehouseCapabilityError(
            "Athena: the host must be the AWS region (for example eu-west-1) or its "
            "Athena endpoint (athena.eu-west-1.amazonaws.com), without a port or path."
        )
    suffix = ".amazonaws.com.cn" if region.startswith("cn-") else ".amazonaws.com"
    return AthenaEndpoint(region=region, hostname=f"athena.{region}{suffix}")


def import_driver() -> ModuleType:
    """``pyathena``, imported on first use."""
    import pyathena

    return pyathena
