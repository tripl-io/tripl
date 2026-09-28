# ruff: noqa: F811  (the fixtures imported from test_org_sso are redefined as parameters)
"""Organization SAML 2.0 single sign-on (F20, GH #273).

* the SAML settings are an OWNER's, like the OIDC ones (an admin and a member
  get 403, a stranger 404); the SSO URL must be https, the certificates must
  parse and be unexpired; the other protocol's settings survive a switch;
  pasted IdP metadata is read (never fetched) with the hardened parser;
  tripl's SP metadata is served for an organization configured for SAML;
* sign-in against a fake IdP (``_fake_saml_idp``): the first sign-in creates
  the account (JIT), a linked NameID signs straight in, an existing account is
  linked only after the confirmed ticket, two certificates allow a rotation;
* refused, fail closed, with a generic code: an unsigned assertion, a
  signature by an unknown certificate, SHA-1, signature wrapping (a second
  assertion, first or tucked in Extensions), a DOCTYPE, the wrong audience /
  recipient / destination / issuer, an expired or not-yet-valid assertion,
  an ``InResponseTo`` that is not this browser's request, an unsolicited
  (IdP-initiated) response, a replayed assertion, a reused or unbound state,
  an address outside the verified domains; an encrypted assertion with its
  own code.

Fixtures and helpers come from ``test_org_sso``.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import ASGITransport, AsyncClient, Response
from lxml import etree
from signxml.algorithms import DigestAlgorithm, SignatureMethod
from sqlalchemy import func, select, update

from tripl.main import app
from tripl.models.org_sso import OrgSsoConfig, SsoLinkTicket, UserSsoIdentity
from tripl.models.user_session import UserSession
from tripl.services import saml_response, saml_xml
from tripl.tests._fake_idp import ISSUER
from tripl.tests._fake_saml_idp import (
    NAMEID_PERSISTENT,
    FakeSamlIdp,
    cert_pem,
    encode,
    make_cert,
    make_key,
)
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_org_sso import (  # noqa: F401 - fixtures
    ACME,
    API,
    DOMAIN,
    PASSWORD,
    SSO_URL,
    START_URL,
    People,
    _audit,
    _config,
    _user_id,
    _verified_domain,
    acme,
    encryption_key,
    people,
)

ACS_URL = f"{API}/auth/sso/{ACME}/saml/acs"
METADATA_URL = f"{API}/auth/sso/{ACME}/saml/metadata"
IMPORT_URL = f"{SSO_URL}/saml/metadata-import"
BASE = "https://test"
SAML_NS = "{urn:oasis:names:tc:SAML:2.0:assertion}"


def _browser() -> AsyncClient:
    # https: the SAML state cookie is Secure (it must be SameSite=None).
    return AsyncClient(transport=ASGITransport(app=app), base_url=BASE)


@pytest.fixture
def saml_idp() -> FakeSamlIdp:
    return FakeSamlIdp()


def _saml_config(idp: FakeSamlIdp, **extra: Any) -> dict[str, Any]:
    return {
        "protocol": "saml",
        "saml_idp_entity_id": idp.entity_id,
        "saml_idp_sso_url": idp.sso_url,
        "saml_idp_certs": idp.cert_pem,
        **extra,
    }


async def _enable_saml(
    people: People, monkeypatch: pytest.MonkeyPatch, idp: FakeSamlIdp, **extra: Any
) -> dict[str, Any]:
    await _verified_domain(people["root"], monkeypatch)
    saved = await people["root"].put(SSO_URL, json=_saml_config(idp, enabled=True, **extra))
    assert saved.status_code == 200, saved.text
    body: dict[str, Any] = saved.json()
    return body


async def _start(browser: AsyncClient, idp: FakeSamlIdp, next_path: str | None = None) -> Any:
    params = {} if next_path is None else {"next": next_path}
    started = await browser.get(START_URL, params=params)
    assert started.status_code == 302, started.text
    return idp.read_request(started.headers["location"])


async def _post(browser: AsyncClient, saml_b64: str, relay_state: str) -> Response:
    return await browser.post(ACS_URL, data={"SAMLResponse": saml_b64, "RelayState": relay_state})


async def _sign_in(
    browser: AsyncClient, idp: FakeSamlIdp, *, next_path: str | None = None, **kwargs: Any
) -> Response:
    request = await _start(browser, idp, next_path)
    return await _post(browser, idp.response(request, **kwargs), request.relay_state)


def _sso_error(resp: Response) -> str | None:
    assert resp.status_code == 303, resp.text
    query = parse_qs(urlparse(resp.headers["location"]).query)
    return query.get("sso_error", [None])[0]


# ── the settings ────────────────────────────────────────────────────────────


async def test_only_an_owner_configures_saml_and_it_is_validated(
    people: People, acme: uuid.UUID, saml_idp: FakeSamlIdp
) -> None:
    root = people["root"]
    fresh = await root.get(SSO_URL)
    assert fresh.json()["protocol"] == "oidc"
    assert fresh.json()["saml_sp_entity_id"].endswith(f"/api/v1/auth/sso/{ACME}/saml/metadata")
    assert fresh.json()["saml_acs_url"].endswith(f"/api/v1/auth/sso/{ACME}/saml/acs")
    assert fresh.json()["saml_metadata_url"] == fresh.json()["saml_sp_entity_id"]

    saved = await root.put(SSO_URL, json=_saml_config(saml_idp))
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["protocol"] == "saml"
    assert body["saml_idp_entity_id"] == saml_idp.entity_id
    assert body["saml_idp_sso_url"] == saml_idp.sso_url
    assert "BEGIN CERTIFICATE" in body["saml_idp_certs"]
    [cert] = body["saml_cert_info"]
    assert cert["expired"] is False
    assert len(cert["fingerprint_sha256"].split(":")) == 32
    assert body["saml_name_id_format"].endswith("emailAddress")
    assert body["saml_email_attribute"] is None
    assert body["issuer"] == ""

    for name in ("bob", "mia"):
        for method, url, payload in (
            ("GET", SSO_URL, None),
            ("PUT", SSO_URL, _saml_config(saml_idp)),
            ("POST", IMPORT_URL, {"xml": saml_idp.metadata()}),
        ):
            resp = await people[name].request(method, url, json=payload)
            assert resp.status_code == 403, f"{name} {method} {url}: {resp.text}"
    stranger = await people["stranger"].post(IMPORT_URL, json={"xml": saml_idp.metadata()})
    assert stranger.status_code == 404, stranger.text

    plain_http = await root.put(
        SSO_URL, json=_saml_config(saml_idp, saml_idp_sso_url="http://saml-idp.example.com/sso")
    )
    assert plain_http.status_code == 422, plain_http.text
    garbage = await root.put(SSO_URL, json=_saml_config(saml_idp, saml_idp_certs="not a cert"))
    assert garbage.status_code == 422, garbage.text
    key = make_key()
    old = make_cert(
        key,
        not_before=datetime.now(UTC) - timedelta(days=30),
        not_after=datetime.now(UTC) - timedelta(days=1),
    )
    expired = await root.put(SSO_URL, json=_saml_config(saml_idp, saml_idp_certs=cert_pem(old)))
    assert expired.status_code == 422, expired.text
    missing = await root.put(SSO_URL, json={"protocol": "saml"})
    assert missing.status_code == 422, missing.text
    transient = await root.put(
        SSO_URL,
        json=_saml_config(
            saml_idp, saml_name_id_format="urn:oasis:names:tc:SAML:2.0:nameid-format:transient"
        ),
    )
    assert transient.status_code == 422, transient.text

    rows = await _audit("org.sso.update")
    assert rows[0].payload["protocol"] == "saml"
    assert rows[0].payload["saml_idp_entity_id"] == saml_idp.entity_id


async def test_switching_protocols_keeps_the_other_settings(
    people: People, acme: uuid.UUID, saml_idp: FakeSamlIdp, encryption_key: None
) -> None:
    root = people["root"]
    oidc = await root.put(SSO_URL, json=_config())
    assert oidc.status_code == 200, oidc.text
    # The settings page sends every field; the unused protocol's as null.
    saml = await root.put(
        SSO_URL,
        json=_saml_config(
            saml_idp,
            issuer=None,
            client_id=None,
            scopes=None,
            saml_email_attribute=None,
            enabled=False,
            sso_required=False,
        ),
    )
    assert saml.status_code == 200, saml.text
    assert saml.json()["issuer"] == oidc.json()["issuer"]
    assert saml.json()["client_secret_configured"] is True

    back = await root.put(
        SSO_URL,
        json={
            "protocol": "oidc",
            "issuer": oidc.json()["issuer"],
            "client_id": oidc.json()["client_id"],
            "scopes": None,
            "saml_idp_entity_id": None,
            "saml_idp_sso_url": None,
            "saml_idp_certs": None,
        },
    )
    assert back.status_code == 200, back.text
    assert back.json()["protocol"] == "oidc"
    assert back.json()["saml_idp_entity_id"] == saml_idp.entity_id
    assert back.json()["scopes"] == "openid email profile"
    async with TestSessionLocal() as session:
        config = await session.scalar(
            select(OrgSsoConfig).where(OrgSsoConfig.organization_id == acme)
        )
        assert config is not None
        assert config.saml_idp_certs is not None


async def test_idp_metadata_is_read_from_a_paste(
    people: People, acme: uuid.UUID, saml_idp: FakeSamlIdp
) -> None:
    root = people["root"]
    second = make_cert(make_key(), common_name="next.saml-idp.example.com")
    parsed = await root.post(IMPORT_URL, json={"xml": saml_idp.metadata(extra_certs=[second])})
    assert parsed.status_code == 200, parsed.text
    body = parsed.json()
    assert body["saml_idp_entity_id"] == saml_idp.entity_id
    # The HTTP-Redirect endpoint, not the POST one listed first.
    assert body["saml_idp_sso_url"] == saml_idp.sso_url
    assert body["saml_idp_certs"].count("BEGIN CERTIFICATE") == 2
    assert len(body["saml_cert_info"]) == 2
    # Nothing saved.
    assert (await root.get(SSO_URL)).json()["configured"] is False

    evil = (
        '<?xml version="1.0"?><!DOCTYPE md [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        '<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" '
        'entityID="&x;"/>'
    )
    refused = await root.post(IMPORT_URL, json={"xml": evil})
    assert refused.status_code == 422, refused.text
    assert "passwd" not in refused.text
    not_metadata = await root.post(IMPORT_URL, json={"xml": "<html/>"})
    assert not_metadata.status_code == 422, not_metadata.text


async def test_sp_metadata_is_served_once_saml_is_configured(
    people: People, acme: uuid.UUID, saml_idp: FakeSamlIdp
) -> None:
    async with _browser() as anon:
        before = await anon.get(METADATA_URL)
        assert before.status_code == 404, before.text
        saved = await people["root"].put(SSO_URL, json=_saml_config(saml_idp))
        assert saved.status_code == 200, saved.text
        served = await anon.get(METADATA_URL)
    assert served.status_code == 200, served.text
    assert served.headers["content-type"].startswith("application/samlmetadata+xml")
    root = etree.fromstring(served.content)
    md = "{urn:oasis:names:tc:SAML:2.0:metadata}"
    assert root.get("entityID") == f"{BASE}{METADATA_URL}"
    sp = root.find(f"{md}SPSSODescriptor")
    assert sp is not None
    assert sp.get("WantAssertionsSigned") == "true"
    assert sp.get("AuthnRequestsSigned") == "false"
    acs = sp.find(f"{md}AssertionConsumerService")
    assert acs is not None
    assert acs.get("Location") == f"{BASE}{ACS_URL}"
    assert acs.get("Binding") == "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
    assert (sp.findtext(f"{md}NameIDFormat") or "").endswith("emailAddress")


async def test_the_saml_probe_checks_the_saved_settings(
    people: People, acme: uuid.UUID, saml_idp: FakeSamlIdp
) -> None:
    root = people["root"]
    await root.put(SSO_URL, json=_saml_config(saml_idp))
    ok = await root.post(f"{SSO_URL}/test")
    assert ok.status_code == 200, ok.text
    assert ok.json()["ok"] is True
    assert len(ok.json()["saml_cert_info"]) == 1


# ── signing in ──────────────────────────────────────────────────────────────


async def test_the_first_saml_sign_in_creates_a_verified_member(
    people: People, acme: uuid.UUID, saml_idp: FakeSamlIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enable_saml(people, monkeypatch, saml_idp)
    async with _browser() as browser:
        started = await browser.get(START_URL, params={"next": "/projects?tab=1"})
        assert started.status_code == 302, started.text
        cookie = started.headers["set-cookie"]
        assert "tripl_saml_state=" in cookie
        assert "samesite=none" in cookie.lower()
        assert "secure" in cookie.lower()
        assert "httponly" in cookie.lower()
        request = saml_idp.read_request(started.headers["location"])
        assert request.destination == saml_idp.sso_url
        assert request.acs_url == f"{BASE}{ACS_URL}"
        assert request.sp_entity_id == f"{BASE}{METADATA_URL}"
        assert request.id.startswith("_")

        done = await _post(browser, saml_idp.response(request), request.relay_state)
        assert done.status_code == 303, done.text
        assert done.headers["location"] == f"{BASE}/projects?tab=1"
        me = await browser.get(f"{API}/auth/me")
        assert me.status_code == 200, me.text
        assert me.json()["email"] == f"alice@{DOMAIN}"
        assert me.json()["name"] == "Alice Example"
        assert me.json()["email_verified"] is True
        assert {o["slug"]: o["role"] for o in me.json()["orgs"]} == {ACME: "member"}

    user_id = await _user_id(f"alice@{DOMAIN}")
    async with TestSessionLocal() as session:
        identity = await session.scalar(
            select(UserSsoIdentity).where(UserSsoIdentity.user_id == user_id)
        )
        assert identity is not None
        assert (identity.issuer, identity.subject, identity.organization_id) == (
            f"saml:{saml_idp.entity_id}",
            f"alice@{DOMAIN}",
            acme,
        )
        sso_session = await session.scalar(
            select(UserSession).where(UserSession.user_id == user_id)
        )
        assert sso_session is not None
        assert sso_session.auth_method == "sso"
        assert sso_session.sso_organization_id == acme
    provisioned = await _audit("user.sso_provision")
    assert [row.payload["protocol"] for row in provisioned] == ["saml"]


async def test_a_linked_name_id_signs_straight_in(
    people: People, acme: uuid.UUID, saml_idp: FakeSamlIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enable_saml(
        people,
        monkeypatch,
        saml_idp,
        saml_name_id_format=NAMEID_PERSISTENT,
        saml_email_attribute="email",
    )
    persistent = {"subject": "alice-0001", "name_id_format": NAMEID_PERSISTENT}
    async with _browser() as first:
        done = await _sign_in(first, saml_idp, **persistent)
        assert done.status_code == 303, done.text
        assert _sso_error(done) is None
    async with _browser() as second:
        # The address changed at the provider; the NameID is what counts.
        done = await _sign_in(second, saml_idp, email=f"alice.new@{DOMAIN}", **persistent)
        assert done.headers["location"] == f"{BASE}/"
        me = await second.get(f"{API}/auth/me")
        assert me.json()["email"] == f"alice@{DOMAIN}"
    assert len(await _audit("user.sso_login")) == 1


async def test_an_existing_account_gets_a_link_ticket(
    people: People, acme: uuid.UUID, saml_idp: FakeSamlIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enable_saml(people, monkeypatch, saml_idp)
    async with _browser() as browser:
        done = await _sign_in(browser, saml_idp, email=f"carol@{DOMAIN}")
        assert done.status_code == 303, done.text
        location = urlparse(done.headers["location"])
        assert location.path == "/sso/link"
        ticket = parse_qs(location.query)["ticket"][0]
        assert (await browser.get(f"{API}/auth/me")).status_code == 401
        unproven = await browser.post(f"{API}/auth/sso/link", json={"ticket": ticket})
        assert unproven.status_code == 401, unproven.text

        login = await browser.post(
            f"{API}/auth/login", json={"email": f"carol@{DOMAIN}", "password": PASSWORD}
        )
        assert login.status_code == 200, login.text
        confirmed = await browser.post(f"{API}/auth/sso/link", json={"ticket": ticket})
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["user"]["email"] == f"carol@{DOMAIN}"
    async with TestSessionLocal() as session:
        identity = await session.scalar(
            select(UserSsoIdentity).where(UserSsoIdentity.subject == f"carol@{DOMAIN}")
        )
        assert identity is not None
        assert identity.issuer == f"saml:{saml_idp.entity_id}"
    async with _browser() as again:
        direct = await _sign_in(again, saml_idp, email=f"carol@{DOMAIN}")
        assert direct.headers["location"] == f"{BASE}/"


async def test_a_rotated_certificate_signs_in(
    people: People, acme: uuid.UUID, saml_idp: FakeSamlIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    next_key = make_key()
    next_cert = make_cert(next_key, common_name="next.saml-idp.example.com")
    await _enable_saml(
        people,
        monkeypatch,
        saml_idp,
        saml_idp_certs=saml_idp.cert_pem + cert_pem(next_cert),
    )
    async with _browser() as browser:
        done = await _sign_in(browser, saml_idp, key=next_key, cert=next_cert)
        assert done.status_code == 303, done.text
        assert _sso_error(done) is None
        assert (await browser.get(f"{API}/auth/me")).status_code == 200


_REFUSALS: list[tuple[str, Callable[[FakeSamlIdp], dict[str, Any]], str]] = [
    ("unsigned", lambda idp: {"sign": False}, "saml_signature_invalid"),
    (
        "unknown certificate",
        lambda idp: (lambda key: {"key": key, "cert": make_cert(key)})(make_key()),
        "saml_signature_invalid",
    ),
    (
        "sha1",
        lambda idp: {
            "signature_method": SignatureMethod.RSA_SHA1,
            "digest": DigestAlgorithm.SHA1,
        },
        "saml_signature_invalid",
    ),
    ("audience", lambda idp: {"audience": "https://other.example.com"}, "saml_invalid"),
    (
        "recipient",
        lambda idp: {"recipient": "https://other.example.com/acs"},
        "saml_invalid",
    ),
    (
        "destination",
        lambda idp: {"destination": "https://other.example.com/acs"},
        "saml_invalid",
    ),
    ("issuer", lambda idp: {"issuer": "https://evil.example.com"}, "saml_invalid"),
    (
        "expired",
        lambda idp: {
            "not_before": datetime.now(UTC) - timedelta(minutes=30),
            "not_on_or_after": datetime.now(UTC) - timedelta(minutes=10),
        },
        "saml_invalid",
    ),
    (
        "not yet valid",
        lambda idp: {"not_before": datetime.now(UTC) + timedelta(minutes=10)},
        "saml_invalid",
    ),
    (
        "in response to another request",
        lambda idp: {"in_response_to": "_not-this-request"},
        "saml_invalid",
    ),
    (
        "subject confirmation for another request",
        lambda idp: {"scd_in_response_to": "_not-this-request"},
        "saml_invalid",
    ),
    (
        "unsolicited",
        lambda idp: {"omit": ("in_response_to",)},
        "saml_unsolicited",
    ),
    ("foreign domain", lambda idp: {"email": "alice@example.org"}, "email_domain_not_allowed"),
    (
        # No email attribute configured: only an emailAddress NameID is an email.
        "persistent NameID that looks like an address",
        lambda idp: {"name_id_format": NAMEID_PERSISTENT},
        "email_missing",
    ),
    (
        "sub-domain",
        lambda idp: {"email": f"alice@sub.{DOMAIN}"},
        "email_domain_not_allowed",
    ),
    (
        "idp refusal",
        lambda idp: {"status": "urn:oasis:names:tc:SAML:2.0:status:Requester"},
        "idp_denied",
    ),
]


@pytest.mark.parametrize(
    ("build", "code"),
    [pytest.param(build, code, id=name) for name, build, code in _REFUSALS],
)
async def test_a_bad_response_is_refused(
    people: People,
    acme: uuid.UUID,
    saml_idp: FakeSamlIdp,
    monkeypatch: pytest.MonkeyPatch,
    build: Callable[[FakeSamlIdp], dict[str, Any]],
    code: str,
) -> None:
    await _enable_saml(people, monkeypatch, saml_idp)
    async with _browser() as browser:
        refused = await _sign_in(browser, saml_idp, **build(saml_idp))
        assert _sso_error(refused) == code
        assert (await browser.get(f"{API}/auth/me")).status_code == 401
    assert await _user_id(f"alice@{DOMAIN}") is None


def _wrap_first(idp: FakeSamlIdp, request: Any) -> etree._Element:
    """A valid signed assertion moved into Extensions, the attacker's put first."""
    response = idp.response_element(request)
    signed = response.find(f"{SAML_NS}Assertion")
    assert signed is not None
    forged = idp.response_element(request, email=f"mia@{DOMAIN}", sign=False).find(
        f"{SAML_NS}Assertion"
    )
    assert forged is not None
    forged.set("ID", signed.get("ID") or "")
    response.remove(signed)
    extensions = etree.Element("{urn:oasis:names:tc:SAML:2.0:protocol}Extensions")
    extensions.append(signed)
    response.insert(1, extensions)
    response.append(forged)
    return response


def _wrap_inside(idp: FakeSamlIdp, request: Any) -> etree._Element:
    """The attacker's assertion carrying the signed one in its Advice."""
    response = idp.response_element(request)
    signed = response.find(f"{SAML_NS}Assertion")
    assert signed is not None
    forged = idp.response_element(request, email=f"mia@{DOMAIN}", sign=False).find(
        f"{SAML_NS}Assertion"
    )
    assert forged is not None
    response.remove(signed)
    advice = etree.SubElement(forged, f"{SAML_NS}Advice")
    advice.append(signed)
    response.append(forged)
    return response


def _tampered(idp: FakeSamlIdp, request: Any) -> etree._Element:
    """The signed assertion's NameID rewritten after signing."""
    response = idp.response_element(request)
    name_id = response.find(f".//{SAML_NS}NameID")
    assert name_id is not None
    name_id.text = f"mia@{DOMAIN}"
    return response


@pytest.mark.parametrize(
    ("forge", "code"),
    [
        (_wrap_first, "saml_invalid"),
        (_wrap_inside, "saml_invalid"),
        (_tampered, "saml_signature_invalid"),
    ],
)
async def test_signature_wrapping_and_tampering_are_refused(
    people: People,
    acme: uuid.UUID,
    saml_idp: FakeSamlIdp,
    monkeypatch: pytest.MonkeyPatch,
    forge: Callable[[FakeSamlIdp, Any], etree._Element],
    code: str,
) -> None:
    await _enable_saml(people, monkeypatch, saml_idp)
    async with _browser() as browser:
        request = await _start(browser, saml_idp)
        refused = await _post(browser, encode(forge(saml_idp, request)), request.relay_state)
        assert _sso_error(refused) == code
        assert (await browser.get(f"{API}/auth/me")).status_code == 401


async def test_a_doctype_is_refused_before_anything_is_read(
    people: People, acme: uuid.UUID, saml_idp: FakeSamlIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enable_saml(people, monkeypatch, saml_idp)
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaaaaaaaa">'
        b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        b'<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol">&b;</samlp:Response>'
    )
    async with _browser() as browser:
        request = await _start(browser, saml_idp)
        refused = await _post(browser, base64.b64encode(bomb).decode(), request.relay_state)
        assert _sso_error(refused) == "saml_invalid"


async def test_an_encrypted_assertion_is_refused_with_its_code(
    people: People, acme: uuid.UUID, saml_idp: FakeSamlIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enable_saml(people, monkeypatch, saml_idp)
    async with _browser() as browser:
        request = await _start(browser, saml_idp)
        response = saml_idp.response_element(request)
        assertion = response.find(f"{SAML_NS}Assertion")
        assert assertion is not None
        response.remove(assertion)
        etree.SubElement(response, f"{SAML_NS}EncryptedAssertion")
        refused = await _post(browser, encode(response), request.relay_state)
        assert _sso_error(refused) == "encrypted_assertion_unsupported"


async def test_a_replayed_assertion_is_refused(
    people: People, acme: uuid.UUID, saml_idp: FakeSamlIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enable_saml(people, monkeypatch, saml_idp)
    async with _browser() as first:
        done = await _sign_in(first, saml_idp, assertion_id="_replayed")
        assert _sso_error(done) is None
    async with _browser() as second:
        replay = await _sign_in(second, saml_idp, assertion_id="_replayed")
        assert _sso_error(replay) == "saml_replay"
        assert (await second.get(f"{API}/auth/me")).status_code == 401


async def test_the_state_is_single_use_and_bound_to_the_browser(
    people: People, acme: uuid.UUID, saml_idp: FakeSamlIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enable_saml(people, monkeypatch, saml_idp)
    async with _browser() as browser:
        request = await _start(browser, saml_idp)
        posted = saml_idp.response(request)
        # Carried to another browser: no state cookie there.
        async with _browser() as elsewhere:
            stolen = await _post(elsewhere, posted, request.relay_state)
            assert _sso_error(stolen) == "invalid_state"
        done = await _post(browser, posted, request.relay_state)
        assert _sso_error(done) is None
    async with _browser() as again:
        # The same state again, its cookie sent explicitly: bound, but used.
        reused = await again.post(
            ACS_URL,
            data={"SAMLResponse": posted, "RelayState": request.relay_state},
            headers={"Cookie": f"tripl_saml_state={request.relay_state}"},
        )
        assert _sso_error(reused) == "invalid_state"


async def test_an_idp_initiated_post_is_refused(
    people: People, acme: uuid.UUID, saml_idp: FakeSamlIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enable_saml(people, monkeypatch, saml_idp)
    async with _browser() as browser:
        request = await _start(browser, saml_idp)
        unsolicited = saml_idp.response(request, omit=("in_response_to",))
        refused = await browser.post(ACS_URL, data={"SAMLResponse": unsolicited})
        assert _sso_error(refused) == "saml_unsolicited"


async def test_the_acs_of_an_oidc_organization_is_unavailable(
    people: People, acme: uuid.UUID, saml_idp: FakeSamlIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enable_saml(people, monkeypatch, saml_idp)
    async with _browser() as browser:
        request = await _start(browser, saml_idp)
        switched = await people["root"].put(
            SSO_URL,
            json={
                "issuer": "https://idp.example.com",
                "client_id": "tripl-client",
                "client_secret": "s3cret-value",
                "enabled": True,
            },
        )
        assert switched.status_code == 200, switched.text
        refused = await _post(browser, saml_idp.response(request), request.relay_state)
        assert _sso_error(refused) == "sso_unavailable"


# ── the verifier itself ─────────────────────────────────────────────────────


def test_a_comment_in_the_name_id_does_not_truncate_it(saml_idp: FakeSamlIdp) -> None:
    """Only the canonical form signxml verified is read: the text around a
    comment is one value, not the part before it."""
    sp, acs = f"{BASE}{METADATA_URL}", f"{BASE}{ACS_URL}"
    request_xml = saml_xml.authn_request(
        request_id="_req1",
        issue_instant=datetime.now(UTC),
        destination=saml_idp.sso_url,
        acs_url=acs,
        sp_entity_id=sp,
        name_id_format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
    )
    request = saml_idp.read_request(saml_xml.authn_request_url(saml_idp.sso_url, request_xml, "s"))
    response = saml_idp.response_element(
        request, email=f"alice@{DOMAIN}.evil.example.com", attributes={}
    )
    name_id = response.find(f".//{SAML_NS}NameID")
    assert name_id is not None
    name_id.text = f"alice@{DOMAIN}"
    comment = etree.Comment("x")
    comment.tail = ".evil.example.com"
    name_id.append(comment)
    identity = saml_response.verify_response(
        encode(response),
        idp_entity_id=saml_idp.entity_id,
        certs=[saml_idp.cert],
        sp_entity_id=sp,
        acs_url=acs,
        request_id="_req1",
        email_attribute=None,
    )
    assert identity.email == f"alice@{DOMAIN}.evil.example.com"
    assert identity.subject == identity.email


# ── repairs: the trust anchor, stale values, limits ────────────────────────


async def _identity_count(org_id: uuid.UUID) -> int:
    async with TestSessionLocal() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(UserSsoIdentity)
            .where(UserSsoIdentity.organization_id == org_id)
        )
    return int(count or 0)


async def test_replacing_every_certificate_unlinks_the_members(
    people: People, acme: uuid.UUID, saml_idp: FakeSamlIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An owner who swaps in a key of their own cannot sign in as a linked user."""
    await _enable_saml(people, monkeypatch, saml_idp)
    async with _browser() as browser:
        done = await _sign_in(browser, saml_idp)
        assert _sso_error(done) is None
    assert await _identity_count(acme) == 1
    root = people["root"]

    # A rotation that keeps a saved certificate keeps the links.
    next_key = make_key()
    next_cert = make_cert(next_key, common_name="next.saml-idp.example.com")
    rotated = await root.put(
        SSO_URL,
        json=_saml_config(
            saml_idp, saml_idp_certs=saml_idp.cert_pem + cert_pem(next_cert), enabled=True
        ),
    )
    assert rotated.status_code == 200, rotated.text
    assert await _identity_count(acme) == 1

    # None of the saved certificates kept: every link of that provider goes.
    owner_key = make_key()
    owner_cert = make_cert(owner_key, common_name="owner.example.com")
    swapped = await root.put(
        SSO_URL, json=_saml_config(saml_idp, saml_idp_certs=cert_pem(owner_cert), enabled=True)
    )
    assert swapped.status_code == 200, swapped.text
    assert await _identity_count(acme) == 0
    rows = await _audit("org.sso.update")
    # Rows are unordered: exactly one save (the swap) unlinked anyone.
    assert sorted(row.payload["unlinked_identities"] for row in rows) == [0] * (len(rows) - 1) + [1]

    async with _browser() as browser:
        forged = await _sign_in(browser, saml_idp, key=owner_key, cert=owner_cert)
        assert forged.status_code == 303, forged.text
        # A link ticket (the user must confirm from their own session), not a session.
        assert urlparse(forged.headers["location"]).path == "/sso/link"
        assert (await browser.get(f"{API}/auth/me")).status_code == 401


async def test_saml_named_like_the_oidc_issuer_matches_no_oidc_link(
    people: People,
    acme: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    encryption_key: None,
) -> None:
    root = people["root"]
    assert (await root.put(SSO_URL, json=_config())).status_code == 200
    carol = await _user_id(f"carol@{DOMAIN}")
    async with TestSessionLocal() as session:
        session.add(
            UserSsoIdentity(
                user_id=carol, organization_id=acme, issuer=ISSUER, subject=f"carol@{DOMAIN}"
            )
        )
        session.add(
            SsoLinkTicket(
                ticket_hash="a" * 64,
                user_id=carol,
                organization_id=acme,
                issuer=ISSUER,
                subject=f"carol@{DOMAIN}",
                next_path="/",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        await session.commit()

    impostor = FakeSamlIdp(entity_id=ISSUER)
    await _enable_saml(people, monkeypatch, impostor)
    assert await _identity_count(acme) == 0
    async with TestSessionLocal() as session:
        tickets = await session.scalar(
            select(func.count())
            .select_from(SsoLinkTicket)
            .where(SsoLinkTicket.organization_id == acme)
        )
        assert tickets == 0
    async with _browser() as browser:
        done = await _sign_in(browser, impostor, email=f"carol@{DOMAIN}")
        assert urlparse(done.headers["location"]).path == "/sso/link"
        assert (await browser.get(f"{API}/auth/me")).status_code == 401


async def test_an_expired_saved_certificate_blocks_no_unrelated_save(
    people: People,
    acme: uuid.UUID,
    saml_idp: FakeSamlIdp,
    monkeypatch: pytest.MonkeyPatch,
    encryption_key: None,
) -> None:
    body = await _enable_saml(people, monkeypatch, saml_idp)
    expired_pem = cert_pem(
        make_cert(
            make_key(),
            not_before=datetime.now(UTC) - timedelta(days=30),
            not_after=datetime.now(UTC) - timedelta(days=1),
        )
    )
    async with TestSessionLocal() as session:
        await session.execute(
            update(OrgSsoConfig)
            .where(OrgSsoConfig.organization_id == acme)
            .values(saml_idp_certs=expired_pem)
        )
        await session.commit()
    root = people["root"]

    # The settings page echoes the stored certificate back: turning SSO off works.
    off = await root.put(
        SSO_URL,
        json=_saml_config(saml_idp, saml_idp_certs=expired_pem, enabled=False),
    )
    assert off.status_code == 200, off.text
    assert off.json()["enabled"] is False

    # An OIDC save carrying the stale SAML certificate back works too.
    oidc = await root.put(
        SSO_URL,
        json=_config(
            saml_idp_entity_id=body["saml_idp_entity_id"],
            saml_idp_sso_url=body["saml_idp_sso_url"],
            saml_idp_certs=expired_pem,
        ),
    )
    assert oidc.status_code == 200, oidc.text
    assert oidc.json()["protocol"] == "oidc"

    # Switching SAML back on with it is checked, and refused.
    back = await root.put(SSO_URL, json=_saml_config(saml_idp, saml_idp_certs=expired_pem))
    assert back.status_code == 422, back.text


async def test_an_entity_id_longer_than_the_issuer_columns_is_refused(
    people: People, acme: uuid.UUID
) -> None:
    long_id = "https://saml-idp.example.com/" + "a" * 480
    assert len(long_id) > 507
    root = people["root"]
    idp = FakeSamlIdp(entity_id=long_id)
    saved = await root.put(SSO_URL, json=_saml_config(idp))
    assert saved.status_code == 422, saved.text
    imported = await root.post(IMPORT_URL, json={"xml": idp.metadata()})
    assert imported.status_code == 422, imported.text
    fits = FakeSamlIdp(entity_id="https://saml-idp.example.com/" + "a" * (507 - 29))
    assert len(fits.entity_id) == 507
    assert (await root.put(SSO_URL, json=_saml_config(fits))).status_code == 200


def test_out_of_range_instants_are_refused_or_clamped_not_raised(
    saml_idp: FakeSamlIdp,
) -> None:
    with pytest.raises(saml_response.SamlError) as refused:
        saml_response._parse_instant("0001-01-01T00:00:00+01:00")
    assert refused.value.code == "saml_invalid"

    sp, acs = f"{BASE}{METADATA_URL}", f"{BASE}{ACS_URL}"
    request_xml = saml_xml.authn_request(
        request_id="_req9",
        issue_instant=datetime.now(UTC),
        destination=saml_idp.sso_url,
        acs_url=acs,
        sp_entity_id=sp,
        name_id_format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
    )
    request = saml_idp.read_request(saml_xml.authn_request_url(saml_idp.sso_url, request_xml, "s"))
    far = saml_idp.response(
        request,
        email=f"alice@{DOMAIN}",
        not_on_or_after=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
    )
    identity = saml_response.verify_response(
        far,
        idp_entity_id=saml_idp.entity_id,
        certs=[saml_idp.cert],
        sp_entity_id=sp,
        acs_url=acs,
        request_id="_req9",
        email_attribute=None,
    )
    assert identity.replay_until <= datetime.now(UTC) + saml_response.MAX_REPLAY_WINDOW
