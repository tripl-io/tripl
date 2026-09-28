"""A fake SAML 2.0 identity provider for the SAML tests. Not a test module.

It holds an RSA key and a self-signed certificate (``cryptography``), reads
tripl's HTTP-Redirect AuthnRequest (:meth:`FakeSamlIdp.read_request`) and
builds the ``samlp:Response`` tripl's ACS receives
(:meth:`FakeSamlIdp.response`), its Assertion signed with signxml's
``XMLSigner`` (exclusive c14n, the Signature right after the Issuer, as real
providers place it). Every field a check looks at can be overridden, and
:meth:`FakeSamlIdp.response_element` hands the unsigned-or-signed tree to
tests that tamper with it (signature wrapping).
"""

from __future__ import annotations

import base64
import copy
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from lxml import etree
from signxml import XMLSigner
from signxml.algorithms import DigestAlgorithm, SignatureMethod

from tripl.services.saml_xml import NS_DS, NS_MD, NS_SAML, NS_SAMLP, q, saml_instant

ENTITY_ID = "https://saml-idp.example.com/metadata"
SSO_URL = "https://saml-idp.example.com/sso"
EXC_C14N = "http://www.w3.org/2001/10/xml-exc-c14n#"
STATUS_SUCCESS = "urn:oasis:names:tc:SAML:2.0:status:Success"
NAMEID_EMAIL = "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"
NAMEID_PERSISTENT = "urn:oasis:names:tc:SAML:2.0:nameid-format:persistent"


def make_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def make_cert(
    key: rsa.RSAPrivateKey,
    *,
    common_name: str = "saml-idp.example.com",
    not_before: datetime | None = None,
    not_after: datetime | None = None,
) -> x509.Certificate:
    now = datetime.now(UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before or now - timedelta(days=1))
        .not_valid_after(not_after or now + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )


def cert_pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def cert_body(cert: x509.Certificate) -> str:
    """The base64 DER, as metadata carries it."""
    return base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode("ascii")


@dataclass(frozen=True)
class AuthnRequest:
    id: str
    destination: str
    acs_url: str
    sp_entity_id: str
    name_id_format: str | None
    relay_state: str


@dataclass
class FakeSamlIdp:
    entity_id: str = ENTITY_ID
    sso_url: str = SSO_URL
    counter: int = field(default=0)

    def __post_init__(self) -> None:
        self.key = make_key()
        self.cert = make_cert(self.key)

    @property
    def cert_pem(self) -> str:
        return cert_pem(self.cert)

    def metadata(self, *, extra_certs: list[x509.Certificate] | None = None) -> str:
        certs = [self.cert, *(extra_certs or [])]
        keys = "".join(
            f'<md:KeyDescriptor use="signing"><ds:KeyInfo><ds:X509Data>'
            f"<ds:X509Certificate>{cert_body(cert)}</ds:X509Certificate>"
            f"</ds:X509Data></ds:KeyInfo></md:KeyDescriptor>"
            for cert in certs
        )
        return (
            f'<md:EntityDescriptor xmlns:md="{NS_MD}" xmlns:ds="{NS_DS}" '
            f'entityID="{self.entity_id}">'
            f'<md:IDPSSODescriptor protocolSupportEnumeration="{NS_SAMLP}">{keys}'
            f'<md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
            f'Location="{self.sso_url}/post"/>'
            f'<md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" '
            f'Location="{self.sso_url}"/>'
            f"</md:IDPSSODescriptor></md:EntityDescriptor>"
        )

    # ── what tripl sends ─────────────────────────────────────────────────

    def read_request(self, location: str) -> AuthnRequest:
        """tripl's redirect ``location``, decoded (raw DEFLATE, base64)."""
        assert location.startswith(f"{self.sso_url}?"), location
        query = {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}
        xml = zlib.decompress(base64.b64decode(query["SAMLRequest"]), -15)
        root = etree.fromstring(xml)
        assert root.tag == q(NS_SAMLP, "AuthnRequest")
        assert root.get("ProtocolBinding") == "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
        policy = root.find(q(NS_SAMLP, "NameIDPolicy"))
        return AuthnRequest(
            id=root.get("ID") or "",
            destination=root.get("Destination") or "",
            acs_url=root.get("AssertionConsumerServiceURL") or "",
            sp_entity_id=(root.findtext(q(NS_SAML, "Issuer")) or "").strip(),
            name_id_format=None if policy is None else policy.get("Format"),
            relay_state=query["RelayState"],
        )

    # ── what the provider answers ────────────────────────────────────────

    def response_element(
        self,
        request: AuthnRequest,
        *,
        email: str = "alice@acme.example.com",
        subject: str | None = None,
        name_id_format: str = NAMEID_EMAIL,
        attributes: dict[str, str] | None = None,
        issuer: str | None = None,
        audience: str | None = None,
        recipient: str | None = None,
        destination: str | None = None,
        in_response_to: str | None = None,
        scd_in_response_to: str | None = None,
        not_before: datetime | None = None,
        not_on_or_after: datetime | None = None,
        status: str = STATUS_SUCCESS,
        assertion_id: str | None = None,
        sign: bool = True,
        key: Any = None,
        cert: x509.Certificate | None = None,
        signature_method: SignatureMethod = SignatureMethod.RSA_SHA256,
        digest: DigestAlgorithm = DigestAlgorithm.SHA256,
        omit: tuple[str, ...] = (),
    ) -> etree._Element:
        """The ``samlp:Response`` for ``request`` (overrides as named). ``omit``:
        ``in_response_to`` / ``destination`` drop those Response attributes."""
        self.counter += 1
        now = datetime.now(UTC)
        nsmap = {"samlp": NS_SAMLP, "saml": NS_SAML}
        response = etree.Element(q(NS_SAMLP, "Response"), nsmap=nsmap)
        response.set("ID", f"_resp{self.counter:04d}")
        response.set("Version", "2.0")
        response.set("IssueInstant", saml_instant(now))
        if "destination" not in omit:
            response.set("Destination", destination or request.acs_url)
        if "in_response_to" not in omit:
            response.set("InResponseTo", in_response_to or request.id)
        etree.SubElement(response, q(NS_SAML, "Issuer")).text = issuer or self.entity_id
        status_element = etree.SubElement(response, q(NS_SAMLP, "Status"))
        etree.SubElement(status_element, q(NS_SAMLP, "StatusCode")).set("Value", status)
        if status == STATUS_SUCCESS:
            assertion = self._assertion(
                request,
                now=now,
                email=email,
                subject=subject,
                name_id_format=name_id_format,
                attributes=attributes or {},
                issuer=issuer or self.entity_id,
                audience=audience or request.sp_entity_id,
                recipient=recipient or request.acs_url,
                in_response_to=scd_in_response_to or in_response_to or request.id,
                not_before=not_before or now - timedelta(seconds=5),
                not_on_or_after=not_on_or_after or now + timedelta(minutes=5),
                assertion_id=assertion_id or f"_assert{self.counter:04d}",
            )
            if sign:
                assertion = self.sign(
                    assertion,
                    key=key or self.key,
                    cert=cert or self.cert,
                    signature_method=signature_method,
                    digest=digest,
                )
            response.append(assertion)
        return response

    def response(self, request: AuthnRequest, **kwargs: Any) -> str:
        """:meth:`response_element`, base64 as the HTTP-POST binding carries it."""
        return encode(self.response_element(request, **kwargs))

    def _assertion(
        self,
        request: AuthnRequest,
        *,
        now: datetime,
        email: str,
        subject: str | None,
        name_id_format: str,
        attributes: dict[str, str],
        issuer: str,
        audience: str,
        recipient: str,
        in_response_to: str,
        not_before: datetime,
        not_on_or_after: datetime,
        assertion_id: str,
    ) -> etree._Element:
        del request
        a = etree.Element(q(NS_SAML, "Assertion"), nsmap={"saml": NS_SAML})
        a.set("ID", assertion_id)
        a.set("Version", "2.0")
        a.set("IssueInstant", saml_instant(now))
        etree.SubElement(a, q(NS_SAML, "Issuer")).text = issuer
        subject_element = etree.SubElement(a, q(NS_SAML, "Subject"))
        name_id = etree.SubElement(subject_element, q(NS_SAML, "NameID"))
        name_id.set("Format", name_id_format)
        name_id.text = subject or email
        confirmation = etree.SubElement(subject_element, q(NS_SAML, "SubjectConfirmation"))
        confirmation.set("Method", "urn:oasis:names:tc:SAML:2.0:cm:bearer")
        data = etree.SubElement(confirmation, q(NS_SAML, "SubjectConfirmationData"))
        data.set("InResponseTo", in_response_to)
        data.set("Recipient", recipient)
        data.set("NotOnOrAfter", saml_instant(not_on_or_after))
        conditions = etree.SubElement(a, q(NS_SAML, "Conditions"))
        conditions.set("NotBefore", saml_instant(not_before))
        conditions.set("NotOnOrAfter", saml_instant(not_on_or_after))
        restriction = etree.SubElement(conditions, q(NS_SAML, "AudienceRestriction"))
        etree.SubElement(restriction, q(NS_SAML, "Audience")).text = audience
        authn = etree.SubElement(a, q(NS_SAML, "AuthnStatement"))
        authn.set("AuthnInstant", saml_instant(now))
        authn.set("SessionIndex", assertion_id)
        context = etree.SubElement(authn, q(NS_SAML, "AuthnContext"))
        etree.SubElement(
            context, q(NS_SAML, "AuthnContextClassRef")
        ).text = "urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport"
        all_attributes = {"email": email, "displayName": "Alice Example", **attributes}
        statement = etree.SubElement(a, q(NS_SAML, "AttributeStatement"))
        for attr_name, value in all_attributes.items():
            attribute = etree.SubElement(statement, q(NS_SAML, "Attribute"))
            attribute.set("Name", attr_name)
            etree.SubElement(attribute, q(NS_SAML, "AttributeValue")).text = value
        return a

    @staticmethod
    def sign(
        assertion: etree._Element,
        *,
        key: Any,
        cert: x509.Certificate,
        signature_method: SignatureMethod = SignatureMethod.RSA_SHA256,
        digest: DigestAlgorithm = DigestAlgorithm.SHA256,
    ) -> etree._Element:
        """``assertion`` with an enveloped signature right after its Issuer.

        SHA-1 is refused by signxml's constructor; the algorithms are set after
        it so a test can produce the legacy signature tripl must refuse.
        """
        element = copy.deepcopy(assertion)
        placeholder = etree.Element(q(NS_DS, "Signature"), nsmap={"ds": NS_DS})
        placeholder.set("Id", "placeholder")
        element.insert(1, placeholder)
        signer = XMLSigner(c14n_algorithm=EXC_C14N)
        signer.sign_alg = signature_method
        signer.digest_alg = digest
        signed: etree._Element = signer.sign(
            element,
            key=key,
            cert=[cert],
            reference_uri=element.get("ID"),
        )
        return signed


def encode(response: etree._Element) -> str:
    return base64.b64encode(etree.tostring(response)).decode("ascii")
