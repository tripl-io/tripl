"""The code exchange and the id_token checks of an OIDC sign-in (F20 SSO).

Blocking (network): call through ``asyncio.to_thread``. Every failure is an
:class:`~tripl.services.sso_http.IdpError` whose ``code`` is what the browser is
told; the provider's own text is never echoed.
"""

from __future__ import annotations

import base64
import hmac
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import jwt

from tripl.services import sso_http
from tripl.services.sso_http import Discovery, IdpError

logger = logging.getLogger(__name__)

#: The only signature algorithms accepted. ``none`` and the HMAC family never.
ALLOWED_ALGORITHMS = frozenset({"RS256", "ES256"})
_KEY_TYPE = {"RS256": "RSA", "ES256": "EC"}
LEEWAY_SECONDS = 60


@dataclass(frozen=True)
class IdTokenClaims:
    issuer: str
    subject: str
    email: str
    email_verified: bool
    name: str | None


def exchange_code(
    discovery: Discovery,
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> str:
    """Trade the authorization code for tokens; the raw id_token. Blocking."""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    headers: dict[str, str] = {}
    if sso_http.token_auth_method(discovery) == "client_secret_basic":
        # RFC 6749 2.3.1: both halves form-urlencoded before base64.
        pair = f"{quote(client_id, safe='')}:{quote(client_secret, safe='')}"
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode("ascii")
    else:
        form["client_id"] = client_id
        form["client_secret"] = client_secret
    body = sso_http.request(
        "POST", discovery.token_endpoint, field="Token endpoint", headers=headers, form=form
    )
    id_token = body.get("id_token") if isinstance(body, dict) else None
    if not isinstance(id_token, str) or not id_token:
        raise IdpError("idp_no_id_token", "The token endpoint returned no id_token")
    return id_token


def _signing_key(header: dict[str, Any], keys: list[dict[str, Any]]) -> tuple[Any, str]:
    """The public key and algorithm the token claims to be signed with, checked."""
    alg = header.get("alg")
    if alg not in ALLOWED_ALGORITHMS:
        raise IdpError("id_token_bad_alg", "The id_token is not signed with RS256 or ES256")
    kid = header.get("kid")
    candidates = [
        key
        for key in keys
        if key.get("kty") == _KEY_TYPE[alg]
        and key.get("use", "sig") == "sig"
        and (kid is None or key.get("kid") == kid)
    ]
    if kid is None and len(candidates) != 1:
        raise IdpError("id_token_unknown_key", "The id_token names no key id")
    if not candidates:
        raise IdpError("id_token_unknown_key", "The id_token's key is not in the JWKS")
    jwk = candidates[0]
    # The JWK's own ``alg``, when it states one, must be the header's.
    if jwk.get("alg") not in (None, alg):
        raise IdpError("id_token_bad_alg", "The id_token's algorithm does not match its key")
    try:
        return jwt.PyJWK(jwk, algorithm=alg).key, alg
    except jwt.PyJWTError:
        raise IdpError("id_token_unknown_key", "The id_token's key is unusable") from None


def verify_id_token(
    id_token: str,
    *,
    keys: list[dict[str, Any]],
    issuer: str,
    client_id: str,
    nonce: str,
) -> IdTokenClaims:
    """Signature, ``iss``, ``aud``, ``exp``/``iat`` (60 s leeway) and ``nonce``."""
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError:
        raise IdpError("id_token_invalid", "The id_token is malformed") from None
    key, alg = _signing_key(header, keys)
    try:
        claims: dict[str, Any] = jwt.decode(
            id_token,
            key=key,
            algorithms=[alg],
            audience=client_id,
            issuer=issuer,
            leeway=LEEWAY_SECONDS,
            options={"require": ["iss", "sub", "aud", "exp", "iat"]},
        )
    except jwt.ExpiredSignatureError:
        raise IdpError("id_token_expired", "The id_token has expired") from None
    except jwt.PyJWTError as exc:
        logger.info("SSO: id_token refused: %s", type(exc).__name__)
        raise IdpError("id_token_invalid", "The id_token did not verify") from None
    audience = claims.get("aud")
    # OIDC Core 3.1.3.7: several audiences need an ``azp``, and an ``azp`` that
    # is present must be this client, whatever ``aud`` holds.
    if "azp" in claims and claims.get("azp") != client_id:
        raise IdpError("id_token_invalid", "The id_token was issued to another party")
    if isinstance(audience, list) and len(audience) > 1 and "azp" not in claims:
        raise IdpError("id_token_invalid", "The id_token was issued to another party")
    token_nonce = claims.get("nonce")
    # Bytes: ``compare_digest`` raises TypeError on a non-ASCII ``str``.
    if not isinstance(token_nonce, str) or not hmac.compare_digest(
        token_nonce.encode("utf-8"), nonce.encode("utf-8")
    ):
        raise IdpError("id_token_bad_nonce", "The id_token's nonce does not match")
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject or len(subject) > 255:
        raise IdpError("id_token_invalid", "The id_token has no usable subject")
    email = claims.get("email")
    if not isinstance(email, str) or not email.strip():
        raise IdpError("email_missing", "The identity provider sent no email address")
    name = claims.get("name")
    return IdTokenClaims(
        issuer=issuer,
        subject=subject,
        email=email.strip().lower(),
        # Only a literal JSON ``true`` counts; ``"true"`` does not.
        email_verified=claims.get("email_verified") is True,
        name=(name.strip()[:255] or None) if isinstance(name, str) else None,
    )
