"""A fake OIDC identity provider for the SSO tests. Not a test module.

It stands in for the network at ``idp_http._send`` (monkeypatched), so every
policy of ``idp_http`` — https only, the private-host check, the redirect and
size refusals — still runs in front of it. It serves:

* ``/.well-known/openid-configuration`` (overridable per test);
* ``/jwks`` with one RSA key (``kid`` ``test-key``);
* ``/token``: checks the grant, the PKCE verifier against the challenge the
  code was issued for, the redirect URI and the client's Basic credentials,
  then answers an id_token signed with that key (or with whatever
  :attr:`FakeIdp.token_factory` builds).

The "user at the provider" is :meth:`FakeIdp.authorize`: it takes the query of
tripl's authorization redirect and returns the ``code`` the provider would send
back, remembering the claims to put in the id_token.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from tripl.services.oidc.idp_http import HttpResponse

ISSUER = "https://idp.example.com"
CLIENT_ID = "tripl-client"
CLIENT_SECRET = "s3cret-value"
KID = "test-key"


def _b64url_sha256(value: str) -> str:
    digest = hashlib.sha256(value.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass
class _Grant:
    nonce: str
    code_challenge: str
    redirect_uri: str
    claims: dict[str, Any]


@dataclass
class FakeIdp:
    issuer: str = ISSUER
    client_id: str = CLIENT_ID
    client_secret: str = CLIENT_SECRET
    #: Extra or replaced discovery fields.
    discovery_overrides: dict[str, Any] = field(default_factory=dict)
    #: Build the id_token from ``(claims, private_key)``; default: RS256 with ``kid``.
    token_factory: Callable[[dict[str, Any], Any], str] | None = None
    requests: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._grants: dict[str, _Grant] = {}
        self._counter = 0

    # ── what the provider publishes ──────────────────────────────────────

    def discovery(self) -> dict[str, Any]:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "jwks_uri": f"{self.issuer}/jwks",
            "token_endpoint_auth_methods_supported": ["client_secret_basic"],
            "id_token_signing_alg_values_supported": ["RS256"],
            **self.discovery_overrides,
        }

    def jwks(self) -> dict[str, Any]:
        public = jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        return {"keys": [{**public, "kid": KID, "use": "sig", "alg": "RS256"}]}

    # ── the user at the provider ─────────────────────────────────────────

    def authorize(self, location: str, **claims: Any) -> tuple[str, str]:
        """``(code, state)`` for tripl's authorization redirect ``location``.

        ``claims`` go into the id_token (over the defaults: this issuer, the
        client id as audience, the request's nonce, fresh ``iat``/``exp``).
        """
        query = {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}
        assert location.startswith(f"{self.issuer}/authorize?"), location
        assert query["response_type"] == "code"
        assert query["client_id"] == self.client_id
        assert query["code_challenge_method"] == "S256"
        assert "openid" in query["scope"].split()
        self._counter += 1
        code = f"code-{self._counter}"
        now = int(time.time())
        self._grants[code] = _Grant(
            nonce=query["nonce"],
            code_challenge=query["code_challenge"],
            redirect_uri=query["redirect_uri"],
            claims={
                "iss": self.issuer,
                "aud": self.client_id,
                "sub": "subject-1",
                "email": "alice@acme.example.com",
                "email_verified": True,
                "name": "Alice",
                "nonce": query["nonce"],
                "iat": now,
                "exp": now + 300,
                **claims,
            },
        )
        return code, query["state"]

    def sign(self, claims: dict[str, Any]) -> str:
        if self.token_factory is not None:
            return self.token_factory(claims, self.key)
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": KID})

    # ── the network ──────────────────────────────────────────────────────

    def send(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> HttpResponse:
        self.requests.append((method, url))
        path = url.removeprefix(self.issuer)
        if method == "GET" and path == "/.well-known/openid-configuration":
            return self._json(self.discovery())
        if method == "GET" and path == "/jwks":
            return self._json(self.jwks())
        if method == "POST" and path == "/token":
            return self._token(headers, body or b"")
        return HttpResponse(status=404, body=b"{}")

    def _token(self, headers: dict[str, str], body: bytes) -> HttpResponse:
        form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
        expected = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        if headers.get("Authorization") != f"Basic {expected}":
            return HttpResponse(status=401, body=b'{"error": "invalid_client"}')
        grant = self._grants.pop(form.get("code", ""), None)
        if (
            grant is None
            or form.get("grant_type") != "authorization_code"
            or form.get("redirect_uri") != grant.redirect_uri
            or _b64url_sha256(form.get("code_verifier", "")) != grant.code_challenge
        ):
            return HttpResponse(status=400, body=b'{"error": "invalid_grant"}')
        return self._json(
            {"access_token": "at", "token_type": "Bearer", "id_token": self.sign(grant.claims)}
        )

    @staticmethod
    def _json(document: dict[str, Any]) -> HttpResponse:
        return HttpResponse(status=200, body=json.dumps(document).encode())
