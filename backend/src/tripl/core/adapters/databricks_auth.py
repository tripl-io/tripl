"""Databricks credentials and the outbound rule for a workspace host.

Two ways to authenticate, both stored in the data source's encrypted
``password`` column like every other warehouse secret:

* ``pat`` — a personal access token (or a service principal's token). The
  driver sends it as a bearer token.
* ``oauth_m2m`` — a service principal's OAuth client ID (the source's
  ``username``) and secret (its ``password``). The client-credentials exchange
  is done here rather than through ``databricks-sdk`` so it adds no
  dependency, and so that it goes through :mod:`tripl.services.safe_http`
  like every other request tripl makes to a host a user typed: on an instance
  that only reaches public hosts the token endpoint is resolved, vetted and
  connected to at the vetted address, redirects are refused, and the whole
  exchange has a deadline.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from collections.abc import Callable
from urllib.parse import urlencode

from tripl.core.adapters.errors import WarehouseCapabilityError

#: Domains Databricks serves workspaces from (AWS, Azure, GCP, and the US
#: government and China clouds). On an instance that only reaches public
#: hosts, a workspace host must sit under one of these. See
#: :func:`check_workspace_host` for why the address check alone is not enough.
DATABRICKS_HOST_SUFFIXES = (
    ".cloud.databricks.com",
    ".gcp.databricks.com",
    ".azuredatabricks.net",
    ".cloud.databricks.us",
    ".databricks.azure.us",
    ".databricks.azure.cn",
)

#: The scope the token is asked for. ``all-apis`` is what the Databricks SQL
#: drivers request for a service principal; a narrower ``sql`` scope is not
#: accepted by every workspace's token endpoint.
_OAUTH_SCOPE = "all-apis"

#: Refresh this long before the token says it expires, so a statement that
#: starts just before expiry does not carry a token that lapses mid-flight.
_REFRESH_MARGIN_SECONDS = 60

#: An OAuth token response is a small JSON object; anything larger is refused.
_MAX_TOKEN_RESPONSE_BYTES = 64 * 1024

#: A DNS name: dot-separated labels of letters, digits and inner hyphens.
_HOSTNAME_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
)


def check_workspace_host(host: str) -> None:
    """Refuse a workspace host outside Databricks' domains, when hosts must be public.

    The adapter cannot pin the driver's connection to a vetted address: the
    connector builds its own urllib3 connection pools from the hostname, with
    no hook for the address to dial while keeping the TLS name. So the address
    is vetted right before the connection is opened (``registry.vetted_address``)
    and the driver then resolves the name again — a gap a short-TTL name could
    use to answer publicly for the check and privately for the connection.

    Requiring a Databricks-owned domain closes that gap the way BigQuery's key
    is limited to Google's token endpoint: the name's answers are controlled by
    Databricks, not by whoever configured the source. Without
    ``public_hosts_only`` an operator may point at any host (a private-link
    workspace, a proxy) on purpose.
    """
    from tripl.config import settings

    name = host.strip().lower().rstrip(".")
    # A bare DNS name, always: the host is spliced into the token URL and handed
    # to the driver as a hostname, and the suffix test below must see the name
    # the connection will use (``evil.com:80.cloud.databricks.com`` ends with a
    # Databricks suffix too).
    if not _HOSTNAME_RE.fullmatch(name):
        raise WarehouseCapabilityError(
            "Databricks: the host must be the workspace hostname alone, without a scheme, "
            "port or path (for example dbc-a1b2c3d4-e5f6.cloud.databricks.com)."
        )
    if not settings.public_hosts_only:
        return
    if not name.endswith(DATABRICKS_HOST_SUFFIXES):
        raise WarehouseCapabilityError(
            "Databricks: this instance only connects to Databricks workspaces on the public "
            "internet, and the host is not a Databricks workspace hostname "
            "(for example dbc-a1b2c3d4-e5f6.cloud.databricks.com)."
        )


class ServicePrincipalCredentials:
    """A ``credentials_provider`` for the Databricks SQL connector (OAuth M2M).

    The connector calls the provider once and then the returned header factory
    before each request; the token is fetched on first use and refreshed shortly
    before it expires. The connector's own ``CredentialsProvider`` base class is
    not subclassed: it only declares these two methods, and importing it would
    import the whole driver wherever this class is.
    """

    def __init__(self, host: str, client_id: str, client_secret: str, *, timeout: float) -> None:
        self._token_url = f"https://{host}/oidc/v1/token"
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    def auth_type(self) -> str:
        return "oauth-m2m"

    def __call__(self, *_args: object, **_kwargs: object) -> Callable[[], dict[str, str]]:
        return self._headers

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}

    def token(self) -> str:
        with self._lock:
            if self._token is None or time.monotonic() >= self._expires_at:
                self._token, lifetime = self._fetch()
                self._expires_at = time.monotonic() + max(0.0, lifetime - _REFRESH_MARGIN_SECONDS)
            return self._token

    def _fetch(self) -> tuple[str, float]:
        from tripl.services import safe_http

        basic = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()
        body = urlencode({"grant_type": "client_credentials", "scope": _OAUTH_SCOPE}).encode()
        try:
            response = safe_http.send(
                "POST",
                self._token_url,
                {
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                body,
                field="host",
                timeout=self._timeout,
                max_response_bytes=_MAX_TOKEN_RESPONSE_BYTES,
            )
        except safe_http.PrivateHostError:
            raise WarehouseCapabilityError(
                "this instance only connects to warehouses on the public internet, "
                "and the host resolves to a private or internal address."
            ) from None
        if response.status in (400, 401, 403):
            raise WarehouseCapabilityError(
                f"Databricks: the OAuth token request was refused (HTTP {response.status}). "
                "Check the service principal's client ID and secret, and that OAuth "
                "machine-to-machine access is enabled for it in this workspace."
            )
        if response.status != 200 or len(response.body) > _MAX_TOKEN_RESPONSE_BYTES:
            raise WarehouseCapabilityError(
                f"Databricks: the OAuth token endpoint answered HTTP {response.status} "
                "instead of a token."
            )
        try:
            payload = json.loads(response.body)
            token = str(payload["access_token"])
            lifetime = float(payload.get("expires_in") or 3600)
        except (ValueError, KeyError, TypeError) as exc:
            raise WarehouseCapabilityError(
                "Databricks: the OAuth token endpoint answered without an access token."
            ) from exc
        return token, lifetime
