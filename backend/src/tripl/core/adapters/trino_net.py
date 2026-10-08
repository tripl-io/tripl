"""Hold a Trino client to the address the outbound check vetted.

The ``trino`` client opens its own ``requests`` session from a hostname, and the
coordinator hands back a ``nextUri`` for every page of a result, so a name that
answers publicly for :func:`~tripl.core.adapters.registry.vetted_address` could
answer privately for the connection (a short-TTL rebinding) and for any later
page. :class:`PinnedSession` closes both: every request is sent to the vetted
address with the configured name kept for TLS (SNI and certificate check) and
for the ``Host`` header, a request to any other host or port is refused, and no
redirect is followed. It is used only when outbound hosts must be public
(``Settings.public_hosts_only``), the only case the address is vetted at all.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter


def _netloc(address: str, port: int) -> str:
    host = f"[{address}]" if ":" in address else address
    return f"{host}:{port}"


class _PinnedAdapter(HTTPAdapter):
    def __init__(self, hostname: str, address: str, port: int, *, tls: bool) -> None:
        self._hostname = hostname.lower()
        self._address = address
        self._port = port
        self._tls = tls
        super().__init__(max_retries=0)

    def init_poolmanager(
        self, connections: int, maxsize: int, block: bool = False, **pool_kwargs: Any
    ) -> None:
        if self._tls:
            # Dial the address, but present and verify the configured name.
            pool_kwargs["server_hostname"] = self._hostname
            pool_kwargs["assert_hostname"] = self._hostname
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)

    def send(self, request: requests.PreparedRequest, *args: Any, **kwargs: Any) -> Any:
        parts = urlsplit(request.url or "")
        default_port = 443 if parts.scheme == "https" else 80
        if (parts.hostname or "").lower() != self._hostname or (
            parts.port or default_port
        ) != self._port:
            msg = (
                f"refusing a request to {parts.hostname}:{parts.port or default_port}: "
                f"this connection only reaches {self._hostname}:{self._port}"
            )
            raise requests.ConnectionError(msg)
        request.url = urlunsplit(parts._replace(netloc=_netloc(self._address, self._port)))
        request.headers["Host"] = f"{self._hostname}:{self._port}"
        return super().send(request, *args, **kwargs)


class PinnedSession(requests.Session):
    """A session that only ever reaches ``hostname:port``, at ``address``."""

    def __init__(self, hostname: str, address: str, port: int, *, tls: bool) -> None:
        super().__init__()
        self.max_redirects = 0
        self.trust_env = False  # no proxy may take the connection somewhere else
        adapter = _PinnedAdapter(hostname, address, port, tls=tls)
        self.mount("https://", adapter)
        self.mount("http://", adapter)
