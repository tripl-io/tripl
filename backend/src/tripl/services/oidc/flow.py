"""What every OpenID Connect sign-in flow shares: its error codes, PKCE, ``next``
and the authorization request URL.

A failed sign-in comes back to the app as ``/auth?sso_error=<code>``; the codes
here are the ones any provider's flow can end with, and the SPA maps them to
text. A flow raises :class:`SignInFlowError` with one of them.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
from collections.abc import Mapping
from urllib.parse import urlencode, urlsplit, urlunsplit

from tripl.auth_utils import hash_password
from tripl.services.oidc.idp_http import IdpError

# The codes the browser can come back with. Stable: the SPA maps them to text.
ERR_UNAVAILABLE = "sso_unavailable"
ERR_STATE = "invalid_state"
ERR_IDP = "idp_error"
ERR_DENIED = "idp_denied"
ERR_TOKEN = "invalid_token"
ERR_EMAIL_MISSING = "email_missing"
ERR_EMAIL_UNVERIFIED = "email_not_verified"
ERR_DOMAIN = "email_domain_not_allowed"
ERR_FAILED = "sso_failed"
ERR_RATE_LIMITED = "rate_limited"

_TOKEN_BYTES = 32
_MAX_NEXT = 2048


class SignInFlowError(Exception):
    """A sign-in that ends back on ``/auth`` with ``code`` (``sso_error``)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def flow_code(error: IdpError) -> str:
    """The browser-facing code of a provider error."""
    if error.code.startswith("id_token_"):
        return ERR_TOKEN
    if error.code == "email_missing":
        return ERR_EMAIL_MISSING
    return ERR_IDP


def safe_next(value: str | None) -> str:
    """``value`` when it is a same-origin path, else ``/``.

    A path starts with one ``/`` (never ``//`` or ``/\\``, which browsers read
    as another host) and carries no backslash or control character.
    """
    if not value or len(value) > _MAX_NEXT:
        return "/"
    if not value.startswith("/") or value.startswith("//"):
        return "/"
    if "\\" in value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return "/"
    return value


def pkce_challenge(verifier: str) -> str:
    """The S256 code challenge of a PKCE ``verifier``."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorization_url(endpoint: str, params: Mapping[str, str]) -> str:
    """The provider's ``authorization_endpoint`` with the request's ``params`` added.

    RFC 6749 3.1: a query the endpoint already has (Azure AD B2C's ``?p=<policy>``,
    say) is kept, and the parameters are appended to it.
    """
    parts = urlsplit(endpoint)
    query = "&".join(part for part in (parts.query, urlencode(dict(params))) if part)
    return urlunsplit(parts._replace(query=query))


async def unusable_password_hash() -> str:
    """A real scrypt hash of a secret nobody knows.

    The account signs in through its provider (a password reset can still give
    it a password). A real hash, not a marker: ``/auth/login`` then spends the
    same scrypt time on it as on any account or an unknown address, so the
    response time does not tell provider-only accounts apart.
    """
    return await asyncio.to_thread(hash_password, secrets.token_urlsafe(_TOKEN_BYTES))
