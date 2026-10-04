"""Outbound HTTPS to an address an organization typed (F20: SSO, audit webhook).

The rules every such request shares, in one place:

* on a hosted instance the host must be public (``reject_private_host``) and
  it is re-resolved right before EVERY request, so a name that pointed
  somewhere public when it was saved and at a private address now is refused.
  The connection then goes to the very address that was vetted (the TLS name
  and ``Host`` stay the hostname), so a short-TTL name cannot answer publicly
  for the check and privately for the connection (DNS rebinding);
* redirects are never followed: ``http.client`` does not follow them, and the
  self-hosted path uses a no-redirect opener, so a 3xx comes back as a
  response the caller refuses;
* a per-operation ``timeout`` AND a wall-clock ``deadline`` for the whole
  request: the name lookup, the connect, the TLS handshake, the status line,
  the headers and the body read all fit in it. A peer that drips one byte
  every few seconds keeps each socket operation under the timeout, so the
  deadline is enforced by a watchdog that shuts the socket down when it
  passes (:class:`_Watchdog`); the call then raises ``TimeoutError``;
* a cap on how much of the response is read; ``max_response_bytes=0`` reads
  none of it (the audit webhook only needs the status).

Self-hosted instances have no private-host rule (an operator may point at an
internal host on purpose) and keep their proxy settings (urllib).

Blocking: call through ``asyncio.to_thread`` from async code. Callers keep
their own seam over :func:`send` for tests (``idp_http._send``,
``audit_webhook_delivery._send``).
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import functools
import http.client
import ipaddress
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from tripl.alerting_validation import reject_private_host
from tripl.config import DEPLOYMENT_HOSTED, settings
from tripl.services.llm_service import _RefuseRedirects

#: Name lookups run here so a slow resolver cannot outlive the deadline (the
#: lookup itself cannot be interrupted; its thread is freed when it returns).
_RESOLVER = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="safe-http-dns")


class PrivateHostError(ValueError):
    """The host is, or resolves to, a private or internal address (hosted)."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


class Deadline:
    """A wall-clock budget: :meth:`remaining` raises ``TimeoutError`` once spent."""

    def __init__(self, seconds: float) -> None:
        self._at = time.monotonic() + seconds

    def remaining(self) -> float:
        left = self._at - time.monotonic()
        if left <= 0:
            raise TimeoutError("deadline exceeded")
        return left


class _Watchdog:
    """Shuts a connection's socket down when the deadline passes.

    It watches a DUPLICATE of the TCP socket's descriptor, taken right after
    the connect: ``shutdown`` acts on the connection, not the descriptor, so
    it also ends a TLS handshake or a read blocked on the wrapped socket, and
    the duplicate is never reused for something else while it is watched.
    """

    def __init__(self, deadline: Deadline) -> None:
        self._deadline = deadline
        self._lock = threading.Lock()
        self._watched: socket.socket | None = None
        self._timer: threading.Timer | None = None
        self.fired = False

    def create_connection(
        self, address: tuple[str, int], timeout: Any = None, source_address: Any = None
    ) -> socket.socket:
        per_op = self._deadline.remaining()
        if isinstance(timeout, int | float):
            per_op = min(per_op, float(timeout))
        sock = socket.create_connection(address, per_op, source_address)
        self.watch(sock)
        return sock

    def watch(self, sock: socket.socket) -> None:
        try:
            left = self._deadline.remaining()
        except TimeoutError:
            sock.close()
            raise
        with self._lock:
            self._release_locked()
            self._watched = sock.dup()
            self._timer = threading.Timer(left, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            if self._watched is None:
                return
            self.fired = True
            with contextlib.suppress(OSError):
                self._watched.shutdown(socket.SHUT_RDWR)

    def raise_if_fired(self) -> None:
        """``TimeoutError`` when the deadline cut the connection.

        A cut can end a header block or a body early without an error (the
        peer seems to have closed), so a response parsed after it is never
        trusted.
        """
        if self.fired:
            raise TimeoutError("request exceeded the deadline")

    def _release_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._watched is not None:
            self._watched.close()
            self._watched = None

    def disarm(self) -> None:
        with self._lock:
            self._release_locked()


class _WatchedHTTPSConnection(http.client.HTTPSConnection):
    """``HTTPSConnection`` whose TCP socket is watched by ``watchdog``."""

    def __init__(self, *args: Any, watchdog: _Watchdog, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = watchdog.create_connection


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to ``pinned_ip``, with SNI, certificate check and ``Host`` for ``host``."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        pinned_ip: str,
        timeout: float,
        context: ssl.SSLContext,
        watchdog: _Watchdog | None = None,
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip
        self._tls_context = context
        self._watchdog = watchdog

    def connect(self) -> None:
        if self._watchdog is not None:
            sock = self._watchdog.create_connection((self._pinned_ip, self.port), self.timeout)
        else:
            sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        try:
            self.sock = self._tls_context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def _resolve(hostname: str, port: int, deadline: Deadline | None) -> list[Any]:
    if deadline is None:
        return socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    future = _RESOLVER.submit(socket.getaddrinfo, hostname, port, type=socket.SOCK_STREAM)
    try:
        return future.result(timeout=deadline.remaining())
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise TimeoutError("name lookup exceeded the deadline") from None


def public_address(
    hostname: str, port: int, *, field: str, deadline: Deadline | None = None
) -> str:
    """One address of ``hostname``, after EVERY address it resolves to is vetted.

    The connection goes to this address, so what was checked is what is
    reached. Raises :class:`PrivateHostError` for a private one; ``OSError``
    when the name does not resolve; ``TimeoutError`` past ``deadline``.
    """
    literal = hostname.strip("[]")
    try:
        ipaddress.ip_address(literal)
    except ValueError:
        infos = _resolve(hostname, port, deadline)
        addresses = [str(info[4][0]) for info in infos]
    else:
        addresses = [literal]
    if not addresses:
        raise OSError(f"{hostname} did not resolve")
    for address in addresses:
        try:
            reject_private_host(address, field=field)
        except ValueError as exc:
            raise PrivateHostError(str(exc)) from None
    return addresses[0]


def _read(response: Any, max_response_bytes: int) -> bytes:
    """Up to ``max_response_bytes + 1`` bytes (none for 0), so a caller can tell "too much"."""
    if max_response_bytes <= 0:
        return b""
    return bytes(response.read(max_response_bytes + 1))


def send_pinned(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    *,
    field: str,
    timeout: float,
    max_response_bytes: int,
    deadline: float | None = None,
) -> HttpResponse:
    """A hosted instance's request: resolved once, vetted, connected to that address.

    At most ``max_response_bytes + 1`` bytes of the body are read, so the
    caller can tell "too much" from "exactly the cap". ``deadline`` (seconds,
    default ``timeout``) bounds the whole request, the name lookup included.
    """
    budget = Deadline(timeout if deadline is None else deadline)
    watchdog = _Watchdog(budget)
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    port = parsed.port or 443
    address = public_address(hostname, port, field=field, deadline=budget)
    connection = PinnedHTTPSConnection(
        hostname,
        port,
        pinned_ip=address,
        timeout=timeout,
        context=ssl.create_default_context(),
        watchdog=watchdog,
    )
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    try:
        connection.request(method, target, body=body, headers=headers)
        response = connection.getresponse()
        # http.client never follows a redirect; the caller refuses a 3xx.
        answer = HttpResponse(status=int(response.status), body=_read(response, max_response_bytes))
        watchdog.raise_if_fired()
        return answer
    except (OSError, http.client.HTTPException) as exc:
        if watchdog.fired:
            raise TimeoutError("request exceeded the deadline") from None
        if isinstance(exc, OSError):
            raise
        raise OSError(type(exc).__name__) from None
    finally:
        watchdog.disarm()
        connection.close()


class _WatchedHTTPSHandler(urllib.request.HTTPSHandler):
    """urllib's HTTPS handler, opening :class:`_WatchedHTTPSConnection` (proxies too)."""

    def __init__(self, watchdog: _Watchdog) -> None:
        super().__init__()
        self._watchdog = watchdog

    def https_open(self, req: urllib.request.Request) -> Any:
        connection_class = functools.partial(_WatchedHTTPSConnection, watchdog=self._watchdog)
        return self.do_open(connection_class, req, context=self._context)  # type: ignore[attr-defined]


def send(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    *,
    field: str,
    timeout: float,
    max_response_bytes: int,
    deadline: float | None = None,
) -> HttpResponse:
    """Hosted: :func:`send_pinned`. Self-hosted: a no-redirect urllib opener.

    A non-2xx answer (a 3xx included) is returned, not raised. Network
    failures raise ``OSError``; a request past ``deadline`` (seconds, default
    ``timeout``) ``TimeoutError``; a private host (hosted)
    :class:`PrivateHostError`.
    """
    if settings.deployment_mode == DEPLOYMENT_HOSTED:
        return send_pinned(
            method,
            url,
            headers,
            body,
            field=field,
            timeout=timeout,
            max_response_bytes=max_response_bytes,
            deadline=deadline,
        )
    budget = Deadline(timeout if deadline is None else deadline)
    watchdog = _Watchdog(budget)
    # Per request: the handler carries this request's watchdog.
    opener = urllib.request.build_opener(_RefuseRedirects, _WatchedHTTPSHandler(watchdog))
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with opener.open(req, timeout=timeout) as response:
            answer = HttpResponse(
                status=int(response.status), body=_read(response, max_response_bytes)
            )
        watchdog.raise_if_fired()
        return answer
    except urllib.error.HTTPError as exc:
        try:
            data = _read(exc, max_response_bytes) if exc.fp is not None else b""
        except OSError, http.client.HTTPException:
            data = b""
        watchdog.raise_if_fired()
        return HttpResponse(status=int(exc.code), body=data)
    except (OSError, http.client.HTTPException) as exc:
        if watchdog.fired:
            raise TimeoutError("request exceeded the deadline") from None
        if isinstance(exc, OSError):
            raise
        raise OSError(type(exc).__name__) from None
    finally:
        watchdog.disarm()


def check_https_url(url: str, *, field: str, deadline: Deadline | None = None) -> str:
    """``https`` with a host (public, on a hosted instance); the hostname.

    Raises ``ValueError`` for a URL that is not https, and
    :class:`PrivateHostError` for a private host when hosted. Blocking (DNS)
    on a hosted instance; with a ``deadline`` the lookup is abandoned
    (``TimeoutError``) once it is spent.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"{field} must be an https URL")
    if settings.deployment_mode == DEPLOYMENT_HOSTED:
        try:
            if deadline is None:
                reject_private_host(parsed.hostname, field=field)
            else:
                future = _RESOLVER.submit(reject_private_host, parsed.hostname, field=field)
                try:
                    future.result(timeout=deadline.remaining())
                except concurrent.futures.TimeoutError:
                    future.cancel()
                    raise TimeoutError("name lookup exceeded the deadline") from None
        except ValueError as exc:
            raise PrivateHostError(str(exc)) from None
    return parsed.hostname


__all__ = [
    "Deadline",
    "HttpResponse",
    "PinnedHTTPSConnection",
    "PrivateHostError",
    "check_https_url",
    "public_address",
    "send",
    "send_pinned",
]
