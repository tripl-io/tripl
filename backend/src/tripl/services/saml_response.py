"""Verifying a SAML 2.0 ``Response`` posted to tripl's ACS (F20, GH #273).

:func:`verify_response` fails closed, with a short code (:class:`SamlError`),
in this order:

1. size cap, base64, the hardened parser of :mod:`saml_xml` (no DOCTYPE, no
   entities, no network);
2. the root is a ``samlp:Response``; an ``EncryptedAssertion`` (or an
   ``EncryptedID``) is refused with its own code — tripl has no SP key to
   decrypt it; a status other than Success is the IdP's refusal;
3. exactly ONE ``saml:Assertion`` in the whole document, a direct child of the
   Response;
4. its enveloped XML signature, verified with signxml against each configured
   certificate in turn (RSA/ECDSA over SHA-256 or stronger; SHA-1 refused), and
   covering that assertion itself (the reference is its ``ID``). From here on
   only the element signxml returns (``signed_xml``, rebuilt from the
   canonical form it verified) is read, never the posted tree: an assertion
   moved or duplicated elsewhere in the document (signature wrapping) is
   never what tripl looks at;
5. the Issuer is the configured IdP; the Response's Destination (when given)
   is the ACS; ``InResponseTo`` (Response when given, and the bearer
   SubjectConfirmationData always) is the AuthnRequest this browser started —
   unsolicited (IdP-initiated) responses are refused; the bearer confirmation's
   Recipient is the ACS and it has not expired; the Conditions' window (120 s
   of clock skew) holds and every AudienceRestriction names tripl's entity id;
   an AuthnStatement is present;
6. the email: the configured attribute, else the NameID when its Format is
   emailAddress (any other format: ``email_missing``). The subject is the
   NameID (transient NameIDs are refused: they cannot identify anyone twice).

The codes: ``saml_signature_invalid`` (step 4), ``saml_unsolicited`` (no
``InResponseTo``), ``encrypted_assertion_unsupported``, ``idp_denied`` (a
non-Success status), ``email_missing``, and ``saml_invalid`` for everything
else; the reason is for the log only. Replay (the assertion ID seen before,
``saml_replay``) is the caller's check, against the database.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from lxml import etree
from signxml.algorithms import DigestAlgorithm, SignatureMethod
from signxml.exceptions import SignXMLException
from signxml.verifier import SignatureConfiguration, XMLVerifier

from tripl.services.saml_xml import (
    MAX_XML_BYTES,
    NS_SAML,
    NS_SAMLP,
    SamlXmlError,
    parse_xml,
    parser,
    q,
)

CLOCK_SKEW = timedelta(seconds=120)
#: Kept for replay refusal at least this long, whatever the assertion says.
MIN_REPLAY_WINDOW = timedelta(minutes=10)
MAX_REPLAY_WINDOW = timedelta(days=1)

STATUS_SUCCESS = "urn:oasis:names:tc:SAML:2.0:status:Success"
CM_BEARER = "urn:oasis:names:tc:SAML:2.0:cm:bearer"
NAMEID_TRANSIENT = "urn:oasis:names:tc:SAML:2.0:nameid-format:transient"
NAMEID_EMAIL = "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"
_MIN_INSTANT = datetime.min.replace(tzinfo=UTC)
_MAX_INSTANT = datetime.max.replace(tzinfo=UTC)

# The codes, stable: the SPA maps them to text.
ERR_INVALID = "saml_invalid"
ERR_SIGNATURE = "saml_signature_invalid"
ERR_UNSOLICITED = "saml_unsolicited"
ERR_ENCRYPTED = "encrypted_assertion_unsupported"
ERR_DENIED = "idp_denied"
ERR_EMAIL_MISSING = "email_missing"

ALLOWED_SIGNATURE_METHODS = frozenset(
    {
        SignatureMethod.RSA_SHA256,
        SignatureMethod.RSA_SHA384,
        SignatureMethod.RSA_SHA512,
        SignatureMethod.ECDSA_SHA256,
        SignatureMethod.ECDSA_SHA384,
        SignatureMethod.ECDSA_SHA512,
    }
)
ALLOWED_DIGESTS = frozenset(
    {DigestAlgorithm.SHA256, DigestAlgorithm.SHA384, DigestAlgorithm.SHA512}
)
_SIGNATURE_CONFIG = SignatureConfiguration(
    location="./",
    expect_references=1,
    signature_methods=ALLOWED_SIGNATURE_METHODS,
    digest_algorithms=ALLOWED_DIGESTS,
)

_NAME_ATTRIBUTES = (
    "displayName",
    "http://schemas.microsoft.com/identity/claims/displayname",
    "name",
    "urn:oid:2.16.840.1.113730.3.1.241",
)
_GIVEN_ATTRIBUTES = (
    "givenName",
    "firstName",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname",
    "urn:oid:2.5.4.42",
)
_SURNAME_ATTRIBUTES = (
    "sn",
    "surname",
    "lastName",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/surname",
    "urn:oid:2.5.4.4",
)
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,253}$")
_FRACTION = re.compile(r"\.(\d+)")


class SamlError(Exception):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


def _invalid(reason: str) -> SamlError:
    return SamlError(ERR_INVALID, reason)


@dataclass(frozen=True)
class SamlIdentity:
    issuer: str
    subject: str
    email: str
    name: str | None
    assertion_id: str
    #: Until when the assertion ID must be remembered (replay refusal).
    replay_until: datetime


def _parse_instant(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # xs:dateTime may carry more fractional digits than Python reads.
    text = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise _invalid(f"bad dateTime {value!r}") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    try:
        return moment.astimezone(UTC)
    except OverflowError:
        # An offset pushes it past year 1 or 9999: no real instant.
        raise _invalid(f"dateTime out of range {value!r}") from None


def _shift(moment: datetime, delta: timedelta) -> datetime:
    """``moment + delta``, clamped to the representable range (no OverflowError)."""
    try:
        return moment + delta
    except OverflowError:
        return _MAX_INSTANT if delta > timedelta(0) else _MIN_INSTANT


def _text(element: etree._Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def _decode(saml_response: str) -> bytes:
    if len(saml_response) > (MAX_XML_BYTES * 4) // 3 + 8:
        raise _invalid("response too large")
    try:
        data = base64.b64decode(re.sub(r"\s+", "", saml_response), validate=True)
    except ValueError:
        raise _invalid("not base64") from None
    if len(data) > MAX_XML_BYTES:
        raise _invalid("response too large")
    return data


def _verify_signature(assertion: etree._Element, certs: list[x509.Certificate]) -> etree._Element:
    """The signed assertion as signxml rebuilt it; any configured cert may have signed."""
    assertion_id = assertion.get("ID")
    if not assertion_id:
        raise _invalid("assertion without ID")
    last: str = "no certificate configured"
    for cert in certs:
        try:
            result = XMLVerifier().verify(
                assertion,
                x509_cert=cert,
                parser=parser(),
                id_attribute="ID",
                expect_config=_SIGNATURE_CONFIG,
            )
        except (
            SignXMLException,
            UnsupportedAlgorithm,
            etree.LxmlError,
            ValueError,
            TypeError,
            KeyError,
        ) as exc:
            # Fail closed on anything the verifier raises; try the next certificate.
            last = type(exc).__name__ + ": " + str(exc)[:200]
            continue
        signed = getattr(result, "signed_xml", None)
        if signed is None or isinstance(signed, (str, bytes)):
            raise SamlError(ERR_SIGNATURE, "signature does not cover an element")
        if signed.tag != q(NS_SAML, "Assertion") or signed.get("ID") != assertion_id:
            raise SamlError(ERR_SIGNATURE, "signature does not cover the assertion")
        signed_element: etree._Element = signed
        return signed_element
    raise SamlError(ERR_SIGNATURE, f"assertion signature not valid: {last}")


def _single_assertion(root: etree._Element) -> etree._Element:
    assertions = list(root.iter(q(NS_SAML, "Assertion")))
    if len(assertions) != 1:
        raise _invalid(f"{len(assertions)} assertions")
    assertion = assertions[0]
    if assertion.getparent() is not root:
        raise _invalid("assertion not a child of the response")
    return assertion


def _check_subject(
    assertion: etree._Element, *, acs_url: str, request_id: str, now: datetime
) -> tuple[str, str | None, datetime]:
    """``(NameID, its Format, bearer NotOnOrAfter)``."""
    subject = assertion.find(q(NS_SAML, "Subject"))
    if subject is None:
        raise _invalid("no Subject")
    if subject.find(q(NS_SAML, "EncryptedID")) is not None:
        raise SamlError(ERR_ENCRYPTED, "encrypted NameID")
    name_id_element = subject.find(q(NS_SAML, "NameID"))
    name_id = _text(name_id_element)
    if not name_id or len(name_id) > 255:
        raise _invalid("no usable NameID")
    assert name_id_element is not None
    name_id_format = name_id_element.get("Format")
    if name_id_format == NAMEID_TRANSIENT:
        raise _invalid("transient NameID")
    for confirmation in subject.findall(q(NS_SAML, "SubjectConfirmation")):
        if confirmation.get("Method") != CM_BEARER:
            continue
        data = confirmation.find(q(NS_SAML, "SubjectConfirmationData"))
        if data is None:
            continue
        not_on_or_after = _parse_instant(data.get("NotOnOrAfter"))
        not_before = _parse_instant(data.get("NotBefore"))
        if (
            data.get("Recipient") == acs_url
            and data.get("InResponseTo") == request_id
            and not_on_or_after is not None
            and now < _shift(not_on_or_after, CLOCK_SKEW)
            and (not_before is None or _shift(not_before, -CLOCK_SKEW) <= now)
        ):
            return name_id, name_id_format, not_on_or_after
    raise _invalid("no valid bearer SubjectConfirmation")


def _check_conditions(
    assertion: etree._Element, *, sp_entity_id: str, now: datetime
) -> datetime | None:
    conditions = assertion.find(q(NS_SAML, "Conditions"))
    if conditions is None:
        raise _invalid("no Conditions")
    not_before = _parse_instant(conditions.get("NotBefore"))
    not_on_or_after = _parse_instant(conditions.get("NotOnOrAfter"))
    if not_before is not None and now < _shift(not_before, -CLOCK_SKEW):
        raise _invalid("assertion not yet valid")
    if not_on_or_after is not None and now >= _shift(not_on_or_after, CLOCK_SKEW):
        raise _invalid("assertion expired")
    restrictions = conditions.findall(q(NS_SAML, "AudienceRestriction"))
    if not restrictions:
        raise _invalid("no AudienceRestriction")
    for restriction in restrictions:
        audiences = {_text(a) for a in restriction.findall(q(NS_SAML, "Audience"))}
        if sp_entity_id not in audiences:
            raise _invalid("audience mismatch")
    return not_on_or_after


def _attributes(assertion: etree._Element) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for statement in assertion.findall(q(NS_SAML, "AttributeStatement")):
        for attribute in statement.findall(q(NS_SAML, "Attribute")):
            name = attribute.get("Name") or ""
            for value in attribute.findall(q(NS_SAML, "AttributeValue")):
                text = _text(value)
                if text:
                    values.setdefault(name, []).append(text)
    return values


def _first(attributes: dict[str, list[str]], names: tuple[str, ...]) -> str | None:
    for name in names:
        if attributes.get(name):
            return attributes[name][0]
    return None


def _display_name(attributes: dict[str, list[str]]) -> str | None:
    name = _first(attributes, _NAME_ATTRIBUTES)
    if not name:
        parts = [_first(attributes, _GIVEN_ATTRIBUTES), _first(attributes, _SURNAME_ATTRIBUTES)]
        name = " ".join(part for part in parts if part) or None
    if name is None:
        return None
    cleaned = " ".join(name.split())[:255]
    return cleaned or None


def _email(
    attributes: dict[str, list[str]],
    name_id: str,
    name_id_format: str | None,
    *,
    email_attribute: str | None,
) -> str:
    """The configured attribute, else the NameID — only in the emailAddress format.

    A persistent or unspecified NameID that merely looks like an address is no
    statement by the provider that it is the user's email.
    """
    if email_attribute is not None:
        candidate = _first(attributes, (email_attribute,))
    elif name_id_format == NAMEID_EMAIL:
        candidate = name_id
    else:
        raise SamlError(ERR_EMAIL_MISSING, f"NameID format {name_id_format!r} is not an email")
    if not candidate:
        raise SamlError(ERR_EMAIL_MISSING, "no email")
    email = candidate.strip().lower()
    if len(email) > 320 or not _EMAIL.match(email):
        raise SamlError(ERR_EMAIL_MISSING, "email not an address")
    return email


def verify_response(
    saml_response: str,
    *,
    idp_entity_id: str,
    certs: list[x509.Certificate],
    sp_entity_id: str,
    acs_url: str,
    request_id: str,
    email_attribute: str | None,
    now: datetime | None = None,
) -> SamlIdentity:
    """The identity a posted ``SAMLResponse`` proves; :class:`SamlError` otherwise."""
    moment = now or datetime.now(UTC)
    try:
        root = parse_xml(_decode(saml_response))
    except SamlXmlError as exc:
        raise _invalid(str(exc)) from None
    if root.tag != q(NS_SAMLP, "Response") or root.get("Version") != "2.0":
        raise _invalid("not a SAML 2.0 Response")
    if next(root.iter(q(NS_SAML, "EncryptedAssertion")), None) is not None:
        raise SamlError(ERR_ENCRYPTED, "encrypted assertion")
    status_code = root.find(f"{q(NS_SAMLP, 'Status')}/{q(NS_SAMLP, 'StatusCode')}")
    if status_code is None or status_code.get("Value") != STATUS_SUCCESS:
        value = "missing" if status_code is None else str(status_code.get("Value"))
        raise SamlError(ERR_DENIED, f"status {value}")
    destination = root.get("Destination")
    if destination is not None and destination != acs_url:
        raise _invalid("destination mismatch")
    in_response_to = root.get("InResponseTo")
    if in_response_to is None:
        # An unsolicited (IdP-initiated) response: not supported.
        raise SamlError(ERR_UNSOLICITED, "no InResponseTo")
    if in_response_to != request_id:
        raise _invalid("InResponseTo mismatch")
    response_issuer = root.find(q(NS_SAML, "Issuer"))
    if response_issuer is not None and _text(response_issuer) != idp_entity_id:
        raise _invalid("response issuer mismatch")

    signed = _verify_signature(_single_assertion(root), certs)

    if signed.get("Version") != "2.0":
        raise _invalid("assertion version")
    if _text(signed.find(q(NS_SAML, "Issuer"))) != idp_entity_id:
        raise _invalid("assertion issuer mismatch")
    name_id, name_id_format, bearer_until = _check_subject(
        signed, acs_url=acs_url, request_id=request_id, now=moment
    )
    conditions_until = _check_conditions(signed, sp_entity_id=sp_entity_id, now=moment)
    authn = signed.findall(q(NS_SAML, "AuthnStatement"))
    if not authn:
        raise _invalid("no AuthnStatement")
    for statement in authn:
        session_until = _parse_instant(statement.get("SessionNotOnOrAfter"))
        if session_until is not None and moment >= _shift(session_until, CLOCK_SKEW):
            raise _invalid("session expired")
    attributes = _attributes(signed)
    email = _email(attributes, name_id, name_id_format, email_attribute=email_attribute)
    expiry = _shift(max(bearer_until, conditions_until or bearer_until), CLOCK_SKEW)
    replay_until = min(max(expiry, moment + MIN_REPLAY_WINDOW), moment + MAX_REPLAY_WINDOW)
    assertion_id = signed.get("ID") or ""
    if len(assertion_id) > 255:
        raise _invalid("assertion ID too long")
    return SamlIdentity(
        issuer=idp_entity_id,
        subject=name_id,
        email=email,
        name=_display_name(attributes),
        assertion_id=assertion_id,
        replay_until=replay_until,
    )
