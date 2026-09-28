"""SAML 2.0 XML for the service provider side (F20, GH #273).

Everything that reads XML goes through :func:`parse_xml`: a size cap before
parsing, a parser that never resolves entities, loads a DTD or touches the
network, and a refusal of any document carrying a DOCTYPE at all.

* certificates: :func:`load_certs` (PEM blocks or bare base64, as metadata
  carries them) and :func:`cert_info` (SHA-256 fingerprint, validity);
* :func:`parse_idp_metadata` — an IdP's pasted metadata: entity id, the
  HTTP-Redirect SSO location, the signing certificates. Never fetched: tripl
  takes no metadata URL;
* :func:`sp_metadata` — tripl's own metadata (unsigned; tripl has no SP key,
  its AuthnRequests are unsigned);
* :func:`authn_request_url` — the HTTP-Redirect binding of an AuthnRequest.
"""

from __future__ import annotations

import base64
import re
import secrets
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode, urlparse

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import Encoding
from lxml import etree

from tripl.models.org_sso import SAML_ENTITY_ID_MAX_LENGTH

NS_SAML = "urn:oasis:names:tc:SAML:2.0:assertion"
NS_SAMLP = "urn:oasis:names:tc:SAML:2.0:protocol"
NS_MD = "urn:oasis:names:tc:SAML:2.0:metadata"
NS_DS = "http://www.w3.org/2000/09/xmldsig#"
BINDING_REDIRECT = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
BINDING_POST = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"

#: No SAML document tripl reads is bigger (a response, a pasted metadata file).
MAX_XML_BYTES = 256 * 1024
MAX_CERTS = 5
_PEM_BLOCK = re.compile(
    rb"-----BEGIN CERTIFICATE-----\s*(.+?)\s*-----END CERTIFICATE-----", re.DOTALL
)
_DOCTYPE = re.compile(rb"<!\s*(doctype|entity)", re.IGNORECASE)


class SamlXmlError(ValueError):
    """A document tripl will not read; ``str()`` is safe to show the owner."""


def _parser() -> etree.XMLParser:
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        remove_pis=True,
    )


def parser() -> etree.XMLParser:
    """A fresh hardened parser (lxml parsers are not shared across threads)."""
    return _parser()


def parse_xml(data: bytes) -> etree._Element:
    """The root element of ``data``; :class:`SamlXmlError` for anything suspect."""
    if len(data) > MAX_XML_BYTES:
        raise SamlXmlError("The document is too large")
    if _DOCTYPE.search(data):
        raise SamlXmlError("A document with a DOCTYPE is not accepted")
    try:
        root = etree.fromstring(data, parser=_parser())
    except etree.XMLSyntaxError:
        raise SamlXmlError("The document is not well-formed XML") from None
    tree = root.getroottree()
    # The byte check misses a DOCTYPE in another encoding (UTF-16); this does not.
    if tree.docinfo.doctype or tree.docinfo.internalDTD is not None:
        raise SamlXmlError("A document with a DOCTYPE is not accepted")
    return root


def q(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


# ── certificates ────────────────────────────────────────────────────────────


def _der_blocks(text: str) -> list[bytes]:
    raw = text.strip().encode("ascii", errors="replace")
    blocks = _PEM_BLOCK.findall(raw)
    if not blocks and raw:
        blocks = [raw]
    ders: list[bytes] = []
    for block in blocks:
        compact = re.sub(rb"\s+", b"", block)
        try:
            ders.append(base64.b64decode(compact, validate=True))
        except ValueError:
            raise SamlXmlError("A certificate is not valid PEM or base64") from None
    return ders


def load_certs(text: str) -> list[x509.Certificate]:
    """Every certificate in ``text`` (PEM blocks, or one bare base64 body)."""
    certs: list[x509.Certificate] = []
    for der in _der_blocks(text):
        try:
            certs.append(x509.load_der_x509_certificate(der))
        except ValueError:
            raise SamlXmlError("A certificate could not be read") from None
    if not certs:
        raise SamlXmlError("At least one certificate is required")
    if len(certs) > MAX_CERTS:
        raise SamlXmlError(f"At most {MAX_CERTS} certificates")
    return certs


def to_pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(Encoding.PEM).decode("ascii")


def certs_pem(certs: list[x509.Certificate]) -> str:
    return "".join(to_pem(cert) for cert in certs)


@dataclass(frozen=True)
class CertInfo:
    fingerprint_sha256: str
    subject: str
    not_before: datetime
    not_after: datetime
    expired: bool


def cert_info(cert: x509.Certificate, *, now: datetime | None = None) -> CertInfo:
    moment = now or datetime.now(UTC)
    digest = cert.fingerprint(hashes.SHA256()).hex().upper()
    return CertInfo(
        fingerprint_sha256=":".join(digest[i : i + 2] for i in range(0, len(digest), 2)),
        subject=cert.subject.rfc4514_string(),
        not_before=cert.not_valid_before_utc,
        not_after=cert.not_valid_after_utc,
        expired=cert.not_valid_after_utc <= moment,
    )


# ── IdP metadata ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IdpMetadata:
    entity_id: str
    sso_url: str
    certs: list[x509.Certificate]


def parse_idp_metadata(data: bytes) -> IdpMetadata:
    """The entity id, HTTP-Redirect SSO location and signing certificates of an IdP.

    The root is an ``EntityDescriptor`` or an ``EntitiesDescriptor`` holding
    exactly one IdP. Nothing in the document is fetched.
    """
    root = parse_xml(data)
    if root.tag == q(NS_MD, "EntitiesDescriptor"):
        entities = [
            entity
            for entity in root.iter(q(NS_MD, "EntityDescriptor"))
            if entity.find(q(NS_MD, "IDPSSODescriptor")) is not None
        ]
        if len(entities) != 1:
            raise SamlXmlError("The metadata must describe exactly one identity provider")
        entity = entities[0]
    elif root.tag == q(NS_MD, "EntityDescriptor"):
        entity = root
    else:
        raise SamlXmlError("This is not SAML metadata (no EntityDescriptor)")
    entity_id = (entity.get("entityID") or "").strip()
    if not entity_id:
        raise SamlXmlError("The metadata names no entityID")
    if len(entity_id) > SAML_ENTITY_ID_MAX_LENGTH:
        raise SamlXmlError(f"The entityID is longer than {SAML_ENTITY_ID_MAX_LENGTH} characters")
    idp = entity.find(q(NS_MD, "IDPSSODescriptor"))
    if idp is None:
        raise SamlXmlError("The metadata describes no identity provider (IDPSSODescriptor)")
    sso_url = ""
    for service in idp.findall(q(NS_MD, "SingleSignOnService")):
        if service.get("Binding") == BINDING_REDIRECT:
            sso_url = (service.get("Location") or "").strip()
            break
    if not sso_url:
        raise SamlXmlError("The identity provider offers no HTTP-Redirect sign-in endpoint")
    bodies: list[str] = []
    for descriptor in idp.findall(q(NS_MD, "KeyDescriptor")):
        if descriptor.get("use") not in (None, "signing"):
            continue
        for element in descriptor.iter(q(NS_DS, "X509Certificate")):
            body = "".join(element.itertext()).strip()
            if body and body not in bodies:
                bodies.append(body)
    if not bodies:
        raise SamlXmlError("The metadata carries no signing certificate")
    certs: list[x509.Certificate] = []
    for body in bodies:
        certs.extend(load_certs(body))
    if len(certs) > MAX_CERTS:
        raise SamlXmlError(f"At most {MAX_CERTS} certificates")
    return IdpMetadata(entity_id=entity_id, sso_url=sso_url, certs=certs)


def check_https_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.fragment


# ── what tripl sends ────────────────────────────────────────────────────────


def sp_metadata(*, entity_id: str, acs_url: str, name_id_format: str) -> bytes:
    """tripl's SP metadata: signed assertions wanted, one HTTP-POST ACS, unsigned."""
    md = etree.Element(q(NS_MD, "EntityDescriptor"), nsmap={"md": NS_MD})
    md.set("entityID", entity_id)
    sp = etree.SubElement(md, q(NS_MD, "SPSSODescriptor"))
    sp.set("AuthnRequestsSigned", "false")
    sp.set("WantAssertionsSigned", "true")
    sp.set("protocolSupportEnumeration", NS_SAMLP)
    fmt = etree.SubElement(sp, q(NS_MD, "NameIDFormat"))
    fmt.text = name_id_format
    acs = etree.SubElement(sp, q(NS_MD, "AssertionConsumerService"))
    acs.set("Binding", BINDING_POST)
    acs.set("Location", acs_url)
    acs.set("index", "0")
    acs.set("isDefault", "true")
    result: bytes = etree.tostring(md, xml_declaration=True, encoding="UTF-8")
    return result


def new_request_id() -> str:
    """An xs:ID (must not start with a digit)."""
    return "_" + secrets.token_hex(20)


def saml_instant(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def authn_request(
    *,
    request_id: str,
    issue_instant: datetime,
    destination: str,
    acs_url: str,
    sp_entity_id: str,
    name_id_format: str,
) -> bytes:
    nsmap = {"samlp": NS_SAMLP, "saml": NS_SAML}
    request = etree.Element(q(NS_SAMLP, "AuthnRequest"), nsmap=nsmap)
    request.set("ID", request_id)
    request.set("Version", "2.0")
    request.set("IssueInstant", saml_instant(issue_instant))
    request.set("Destination", destination)
    request.set("AssertionConsumerServiceURL", acs_url)
    request.set("ProtocolBinding", BINDING_POST)
    issuer = etree.SubElement(request, q(NS_SAML, "Issuer"))
    issuer.text = sp_entity_id
    policy = etree.SubElement(request, q(NS_SAMLP, "NameIDPolicy"))
    policy.set("Format", name_id_format)
    policy.set("AllowCreate", "true")
    result: bytes = etree.tostring(request, encoding="UTF-8")
    return result


def authn_request_url(sso_url: str, request_xml: bytes, relay_state: str) -> str:
    """The HTTP-Redirect binding: raw DEFLATE, base64, query parameters."""
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    deflated = compressor.compress(request_xml) + compressor.flush()
    query = urlencode(
        {"SAMLRequest": base64.b64encode(deflated).decode("ascii"), "RelayState": relay_state}
    )
    separator = "&" if "?" in sso_url else "?"
    return f"{sso_url}{separator}{query}"
