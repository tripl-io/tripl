"""Every request tripl makes to an organization's identity provider (F20 SSO).

Three of them: the discovery document, the JWKS and the token endpoint. All go
through :func:`request`, which holds the outbound rules in one place:

* ``https`` only, whatever the deployment mode;
* on a hosted instance the host must be public (``reject_private_host``), and
  it is re-resolved right before EVERY request, so a name that pointed
  somewhere public when the issuer was saved and at a private address now is
  refused (the pattern of ``llm_service`` / ``embedding_service``). The
  connection then goes to the very address that was vetted (the TLS name and
  ``Host`` stay the hostname), so a short-TTL name cannot answer publicly for
  the check and privately for the connection (DNS rebinding);
* redirects are refused (the shared no-redirect opener), so a public IdP
  answering ``302 -> 169.254.169.254`` cannot lead tripl anywhere;
* a 10 second timeout and a response size cap.

Blocking: call through ``asyncio.to_thread``. The network itself is behind
:func:`_send`, the one seam tests replace with a fake identity provider.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse

from tripl.alerting_validation import reject_private_host
from tripl.config import DEPLOYMENT_HOSTED, settings
from tripl.services.llm_service import NO_REDIRECT_OPENER

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10.0
#: Discovery documents and key sets are a few KB; a token response too.
MAX_RESPONSE_BYTES = 512 * 1024
_DISCOVERY_PATH = "/.well-known/openid-configuration"


class IdpError(Exception):
    """The identity provider could not be reached or answered something unusable.

    ``code`` is a short stable identifier safe to show (never the IdP's text).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


@dataclass(frozen=True)
class Discovery:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    token_endpoint_auth_methods: tuple[str, ...]
    id_token_signing_algs: tuple[str, ...]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to ``pinned_ip``, with SNI, certificate check and ``Host`` for ``host``."""

    def __init__(
        self, host: str, port: int, *, pinned_ip: str, timeout: float, context: ssl.SSLContext
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip
        self._tls_context = context

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        try:
            self.sock = self._tls_context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def _public_address(hostname: str, port: int, *, field: str) -> str:
    """One address of ``hostname``, after EVERY address it resolves to is vetted.

    The connection goes to this address, so what was checked is what is
    reached. Raises :class:`IdpError` for a private one; ``OSError`` when the
    name does not resolve.
    """
    literal = hostname.strip("[]")
    try:
        ipaddress.ip_address(literal)
    except ValueError:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        addresses = [str(info[4][0]) for info in infos]
    else:
        addresses = [literal]
    if not addresses:
        raise OSError(f"{hostname} did not resolve")
    for address in addresses:
        try:
            reject_private_host(address, field=field)
        except ValueError as exc:
            raise IdpError("idp_private_host", str(exc)) from None
    return addresses[0]


def _send_pinned(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> HttpResponse:
    """A hosted instance's request: resolved once, vetted, connected to that address."""
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    port = parsed.port or 443
    address = _public_address(hostname, port, field="Identity provider")
    connection = _PinnedHTTPSConnection(
        hostname,
        port,
        pinned_ip=address,
        timeout=TIMEOUT_SECONDS,
        context=ssl.create_default_context(),
    )
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    try:
        connection.request(method, target, body=body, headers=headers)
        response = connection.getresponse()
        # http.client never follows a redirect; ``request`` refuses a 3xx.
        return HttpResponse(status=int(response.status), body=response.read(MAX_RESPONSE_BYTES + 1))
    except http.client.HTTPException as exc:
        raise OSError(type(exc).__name__) from None
    finally:
        connection.close()


def _send(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
    """The network. Replaced by tests; everything else in this module is policy.

    Hosted: :func:`_send_pinned`. Self-hosted (no private-host rule, and an
    operator's proxy settings apply): the shared no-redirect opener.
    """
    if settings.deployment_mode == DEPLOYMENT_HOSTED:
        return _send_pinned(method, url, headers, body)
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with NO_REDIRECT_OPENER.open(req, timeout=TIMEOUT_SECONDS) as response:
            return HttpResponse(
                status=int(response.status), body=response.read(MAX_RESPONSE_BYTES + 1)
            )
    except urllib.error.HTTPError as exc:
        data = exc.read(MAX_RESPONSE_BYTES + 1) if exc.fp is not None else b""
        return HttpResponse(status=int(exc.code), body=data)


def check_url(url: str, *, field: str) -> str:
    """The URL rules of every IdP request; the hostname. Raises :class:`IdpError`.

    Blocking (DNS) on a hosted instance.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise IdpError("idp_insecure_url", f"{field} must be an https URL")
    if settings.deployment_mode == DEPLOYMENT_HOSTED:
        try:
            reject_private_host(parsed.hostname, field=field)
        except ValueError as exc:
            raise IdpError("idp_private_host", str(exc)) from None
    return parsed.hostname


def request(
    method: str,
    url: str,
    *,
    field: str,
    headers: dict[str, str] | None = None,
    form: dict[str, str] | None = None,
) -> Any:
    """One IdP request under the rules above; the decoded JSON body. Blocking."""
    check_url(url, field=field)
    sent_headers = {"Accept": "application/json", **(headers or {})}
    body: bytes | None = None
    if form is not None:
        body = urlencode(form).encode("utf-8")
        sent_headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        response = _send(method, url, sent_headers, body)
    except (OSError, ValueError) as exc:  # URLError is an OSError
        logger.warning("SSO: %s request failed: %s", field, type(exc).__name__)
        raise IdpError("idp_unreachable", f"{field} could not be reached") from None
    if 300 <= response.status < 400:
        raise IdpError("idp_redirect", f"{field} answered with a redirect")
    if len(response.body) > MAX_RESPONSE_BYTES:
        raise IdpError("idp_response_too_large", f"{field} answered too much data")
    if response.status != 200:
        logger.info("SSO: %s answered HTTP %s", field, response.status)
        raise IdpError("idp_http_error", f"{field} answered HTTP {response.status}")
    try:
        return json.loads(response.body.decode("utf-8"))
    except UnicodeDecodeError, ValueError:
        raise IdpError("idp_bad_response", f"{field} did not answer JSON") from None


def _str_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def discovery_url(issuer: str) -> str:
    return issuer.rstrip("/") + _DISCOVERY_PATH


def fetch_discovery(issuer: str) -> Discovery:
    """The issuer's discovery document, checked. Blocking.

    Its ``issuer`` must equal the configured one exactly (OIDC Discovery 4.3),
    and every endpoint used later must be an https URL; the endpoints are only
    ever taken from here, never configured by hand.
    """
    document = request("GET", discovery_url(issuer), field="Discovery document")
    if not isinstance(document, dict):
        raise IdpError("idp_bad_discovery", "The discovery document is not a JSON object")
    if document.get("issuer") != issuer:
        raise IdpError(
            "idp_issuer_mismatch",
            "The discovery document names a different issuer than the one configured",
        )
    endpoints: dict[str, str] = {}
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        value = document.get(key)
        if not isinstance(value, str) or urlparse(value).scheme != "https":
            raise IdpError("idp_bad_discovery", f"The discovery document has no https {key}")
        endpoints[key] = value
    return Discovery(
        issuer=issuer,
        authorization_endpoint=endpoints["authorization_endpoint"],
        token_endpoint=endpoints["token_endpoint"],
        jwks_uri=endpoints["jwks_uri"],
        token_endpoint_auth_methods=_str_list(
            document.get("token_endpoint_auth_methods_supported")
        ),
        id_token_signing_algs=_str_list(document.get("id_token_signing_alg_values_supported")),
    )


def fetch_jwks(discovery: Discovery) -> list[dict[str, Any]]:
    """The provider's signing keys. Blocking."""
    document = request("GET", discovery.jwks_uri, field="JWKS")
    keys = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(keys, list):
        raise IdpError("idp_bad_jwks", "The JWKS has no keys")
    return [key for key in keys if isinstance(key, dict)]


def token_auth_method(discovery: Discovery) -> str:
    """``client_secret_basic`` or ``client_secret_post``, as the provider supports.

    An absent list means ``client_secret_basic`` (OIDC Discovery 3).
    """
    supported = discovery.token_endpoint_auth_methods or ("client_secret_basic",)
    if "client_secret_basic" in supported:
        return "client_secret_basic"
    if "client_secret_post" in supported:
        return "client_secret_post"
    raise IdpError(
        "idp_unsupported_client_auth",
        "The provider supports neither client_secret_basic nor client_secret_post",
    )
