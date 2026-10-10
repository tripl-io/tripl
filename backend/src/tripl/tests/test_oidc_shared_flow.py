"""The OIDC pieces every sign-in flow shares (``tripl.services.oidc``).

The instance-wide sign-in and an organization's SSO (Enterprise) build the
authorization request and finish the callback through the same two functions,
so a fix to either reaches both. The provider is never reached here.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from tripl.services import instance_login
from tripl.services.oidc import id_tokens, idp_http
from tripl.services.oidc.flow import authorization_url
from tripl.services.oidc.id_tokens import IdTokenClaims

ISSUER = "https://idp.example.com"


def _discovery(authorization_endpoint: str) -> idp_http.Discovery:
    return idp_http.Discovery(
        issuer=ISSUER,
        authorization_endpoint=authorization_endpoint,
        token_endpoint=f"{ISSUER}/token",
        jwks_uri=f"{ISSUER}/keys",
        token_endpoint_auth_methods=("client_secret_basic",),
        id_token_signing_algs=("RS256",),
    )


def test_the_parameters_start_the_query_of_a_plain_endpoint() -> None:
    assert (
        authorization_url(f"{ISSUER}/authorize", {"state": "s 1", "scope": "openid email"})
        == f"{ISSUER}/authorize?state=s+1&scope=openid+email"
    )


def test_a_query_the_endpoint_already_has_is_kept() -> None:
    # RFC 6749 3.1, as Azure AD B2C's policy endpoints need.
    url = authorization_url(f"{ISSUER}/authorize?p=b2c_1_signin", {"state": "s1"})

    assert url == f"{ISSUER}/authorize?p=b2c_1_signin&state=s1"


def test_an_endpoint_ending_in_a_question_mark_gets_no_second_one() -> None:
    assert authorization_url(f"{ISSUER}/authorize?", {"state": "s1"}) == (
        f"{ISSUER}/authorize?state=s1"
    )


async def test_the_instance_sign_in_keeps_the_endpoints_own_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        idp_http, "fetch_discovery", lambda _issuer: _discovery(f"{ISSUER}/authorize?p=policy")
    )
    provider = instance_login.Provider(
        key="oidc",
        issuer=ISSUER,
        client_id="tripl",
        client_secret="s3cret",
        scopes="openid email profile",
        callback_path="/api/v1/auth/oidc/callback",
        audit_action="auth.oidc_login",
    )

    started = await instance_login.start(provider, next_path=None, app_base_url="http://test")

    parts = urlsplit(started.authorization_url)
    query = parse_qs(parts.query)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{ISSUER}/authorize"
    assert query["p"] == ["policy"]
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["tripl"]
    assert query["code_challenge_method"] == ["S256"]


def test_authenticate_runs_discovery_exchange_jwks_and_checks_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    discovery = _discovery(f"{ISSUER}/authorize")
    claims = IdTokenClaims(
        issuer=ISSUER, subject="u-1", email="ann@corp.example", email_verified=True, name="Ann"
    )

    def fetch_discovery(issuer: str) -> idp_http.Discovery:
        calls.append(("discovery", {"issuer": issuer}))
        return discovery

    def exchange_code(found: idp_http.Discovery, **kwargs: Any) -> str:
        assert found is discovery
        calls.append(("exchange", kwargs))
        return "raw-id-token"

    def fetch_jwks(found: idp_http.Discovery) -> list[dict[str, Any]]:
        assert found is discovery
        calls.append(("jwks", {}))
        return [{"kid": "k1"}]

    def verify_id_token(token: str, **kwargs: Any) -> IdTokenClaims:
        calls.append(("verify", {"token": token, **kwargs}))
        return claims

    monkeypatch.setattr(idp_http, "fetch_discovery", fetch_discovery)
    monkeypatch.setattr(idp_http, "fetch_jwks", fetch_jwks)
    monkeypatch.setattr(id_tokens, "exchange_code", exchange_code)
    monkeypatch.setattr(id_tokens, "verify_id_token", verify_id_token)

    result = id_tokens.authenticate(
        issuer=ISSUER,
        client_id="tripl",
        client_secret="s3cret",
        code="c-1",
        redirect_uri="http://test/cb",
        code_verifier="v-1",
        nonce="n-1",
    )

    assert result is claims
    assert calls == [
        ("discovery", {"issuer": ISSUER}),
        (
            "exchange",
            {
                "client_id": "tripl",
                "client_secret": "s3cret",
                "code": "c-1",
                "redirect_uri": "http://test/cb",
                "code_verifier": "v-1",
            },
        ),
        ("jwks", {}),
        (
            "verify",
            {
                "token": "raw-id-token",
                "keys": [{"kid": "k1"}],
                "issuer": ISSUER,
                "client_id": "tripl",
                "nonce": "n-1",
            },
        ),
    ]
