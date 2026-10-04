from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable
from email.utils import parseaddr
from urllib.parse import urlparse

from email_validator import EmailNotValidError, validate_email

_TELEGRAM_BOT_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]+$")
_TELEGRAM_CHAT_ID_RE = re.compile(r"^(?:-?\d+|@[A-Za-z0-9_]+)$")
_ALLOWED_SLACK_HOSTS = {"hooks.slack.com", "hooks.slack-gov.com"}
# RFC 7230 header field-name token characters.
_HTTP_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
_RECIPIENT_LIMIT = 50
# Jira project keys / issue type names: ASCII alnum + underscore, kept conservative.
# Atlassian's actual rules are more permissive but harder to encode safely in a URL.
_JIRA_PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,31}$")
_LINEAR_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_LINEAR_LABEL_LIMIT = 20


def normalize_required_text(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def normalize_optional_secret(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _has_disallowed_characters(value: str) -> bool:
    return any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)


# --- generic shapes ---------------------------------------------------------


def _require_clean(value: str | None, *, field: str) -> str:
    """Require a non-empty value with no whitespace / control chars."""
    if value is None:
        raise ValueError(f"{field} is required")
    normalized = normalize_required_text(value, field_name=field)
    if _has_disallowed_characters(normalized):
        raise ValueError(f"{field} must not contain whitespace or control characters")
    return normalized


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for addresses an SSRF attacker would use to reach internal services:
    loopback (127.0.0.1/::1), RFC1918 private ranges, link-local (incl. the
    169.254.169.254 cloud-metadata endpoint), reserved, multicast, unspecified."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def reject_private_host(hostname: str, *, field: str) -> None:
    """SSRF guard: reject a destination host that is — or resolves to — a
    private/internal address. Defends both validation time (config save) and
    send time (DNS-rebinding re-check) against blind SSRF/exfiltration.

    Fails closed: a literal internal IP, *any* resolved address being internal,
    or a DNS lookup that errors out all raise ValueError.
    """
    # Literal IP (IPv4/IPv6, possibly bracketed) — no DNS needed.
    try:
        literal = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        if _is_blocked_ip(literal):
            raise ValueError(f"{field} must not point to a private or internal address")
        return

    # Hostname: reject if ANY resolved address is internal. Fail closed on errors.
    try:
        addrinfo = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ValueError(f"{field} host could not be resolved") from exc
    for entry in addrinfo:
        sockaddr = entry[4]
        try:
            resolved = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if _is_blocked_ip(resolved):
            raise ValueError(f"{field} must not point to a private or internal address")


def _validate_https_url(
    value: str | None,
    *,
    field: str,
    allowed_hosts: set[str] | None = None,
    strip_trailing_slash: bool = False,
    block_private_hosts: bool = False,
) -> str:
    """https-only URL validator shared by Slack/Webhook/Jira."""
    normalized = _require_clean(value, field=field)
    parsed = urlparse(normalized)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{field} must be a valid https URL")
    if allowed_hosts is not None and parsed.hostname not in allowed_hosts:
        hosts = ", ".join(sorted(allowed_hosts))
        raise ValueError(f"{field} must point to one of: {hosts}")
    # SSRF guard for free-form destinations (webhook/Jira). Allowlisted channels
    # (Slack) are already constrained to known public hosts.
    if block_private_hosts:
        if not parsed.hostname:
            raise ValueError(f"{field} must be a valid https URL")
        reject_private_host(parsed.hostname, field=field)
    if strip_trailing_slash:
        normalized = normalized.rstrip("/")
    return normalized


def _validate_id_regex(
    value: str | None,
    *,
    field: str,
    regex: re.Pattern[str],
    error_msg: str,
    optional: bool = False,
) -> str | None:
    """Regex-matched identifier shared by Telegram chat ids and Linear ids."""
    if value is None:
        if optional:
            return None
        raise ValueError(f"{field} is required")
    normalized = value.strip()
    if not normalized:
        if optional:
            return None
        raise ValueError(f"{field} is required")
    if not regex.fullmatch(normalized):
        raise ValueError(error_msg)
    return normalized


def _validate_single_line(
    value: str | None,
    *,
    field: str,
    max_len: int,
    required: bool = False,
) -> str | None:
    """Trimmed text that must not contain CR/LF — for header-like surfaces
    (email subject, jira issue type) where newlines enable header injection."""
    if value is None:
        if required:
            raise ValueError(f"{field} is required")
        return None
    normalized = value.strip()
    if not normalized:
        if required:
            raise ValueError(f"{field} is required")
        return None
    if "\n" in normalized or "\r" in normalized:
        raise ValueError(f"{field} must not contain newlines")
    if len(normalized) > max_len:
        raise ValueError(f"{field} must be <= {max_len} characters")
    return normalized


def _parse_csv_unique(
    value: str | None,
    *,
    item_validator: Callable[[str], str],
    limit: int,
    limit_msg: str,
) -> list[str]:
    """Split a CSV string, validate each entry, drop blanks, dedupe (first wins)."""
    seen: dict[str, None] = {}
    for raw in (value or "").split(","):
        candidate = raw.strip()
        if not candidate:
            continue
        seen.setdefault(item_validator(candidate), None)
    if len(seen) > limit:
        raise ValueError(limit_msg)
    return list(seen)


# --- channel validators -----------------------------------------------------


def validate_slack_webhook_url(value: str | None) -> str:
    return _validate_https_url(value, field="Slack webhook_url", allowed_hosts=_ALLOWED_SLACK_HOSTS)


def validate_telegram_bot_token(value: str | None) -> str:
    normalized = _require_clean(value, field="Telegram bot_token")
    if not _TELEGRAM_BOT_TOKEN_RE.fullmatch(normalized):
        raise ValueError("Telegram bot_token must match <digits>:<token>")
    return normalized


def validate_telegram_chat_id(value: str | None) -> str:
    result = _validate_id_regex(
        value,
        field="Telegram chat_id",
        regex=_TELEGRAM_CHAT_ID_RE,
        error_msg="Telegram chat_id must be a numeric chat id or @channel",
    )
    assert result is not None  # required path
    return result


def validate_webhook_target_url(value: str | None) -> str:
    return _validate_https_url(value, field="Webhook target_url", block_private_hosts=True)


def validate_webhook_header_name(value: str | None) -> str | None:
    normalized = normalize_optional_secret(value)
    if normalized is None:
        return None
    if not _HTTP_HEADER_NAME_RE.fullmatch(normalized):
        raise ValueError("Webhook header name must be a valid HTTP header token")
    return normalized


def validate_webhook_header_value(value: str | None) -> str | None:
    if value is None:
        return None
    # Header values may contain spaces (e.g. "Bearer xyz") but never CR/LF or
    # other control characters — those enable header injection.
    if any((ord(char) < 32 and char != "\t") or ord(char) == 127 for char in value):
        raise ValueError("Webhook header value must not contain control characters")
    normalized = value.strip()
    return normalized or None


def validate_email_address(value: str | None) -> str:
    """RFC-compliant email validation via the ``email-validator`` package
    (the same backend pydantic's ``EmailStr`` uses). DNS deliverability is
    skipped — that's the SMTP server's job at send time."""
    if value is None:
        raise ValueError("Email address is required")
    normalized = value.strip()
    if not normalized:
        raise ValueError("Email address is required")
    try:
        return validate_email(normalized, check_deliverability=False).normalized
    except EmailNotValidError as exc:
        raise ValueError(f"Invalid email address: {exc}") from exc


def validate_sender_address(value: str) -> str:
    """Check a From: address the way the SEND PATH will use it, and return it intact.

    Not ``validate_email_address``: that one refuses a display name, while
    ``EmailMessage`` accepts ``Tripl Alerts <no-reply@example.com>`` without
    complaint — and every caller below hands the configured string to that
    header untouched, so the only thing worth checking is the address part.

    It is the SINGLE answer to that question, and it has to be. While the strict
    helper guarded some of these callers and this one guarded the rest, the two
    disagreed — in both directions, a year apart. First the destination Test
    reported failure for a destination that delivers on every fire.
    Then, once the two diagnostics moved here and the send paths did not, the
    inverse: Settings → Send test email and the destination Test both passed a
    display-name Default From that afterwards failed every real alert, the
    combined digest, the weekly plan digest and the sunset alert.
    Either way round the operator is misled about a configuration they cannot
    otherwise inspect, which is worse than not checking at all.

    The callers, so that claim stays checkable: ``EmailSettingsUpdate`` on the
    way in (schemas/app_settings.py), ``alerts._resolve_email_context`` for the
    immediate alert and the combined digest,
    ``alerts_channels._send_digest_to_destination`` for the weekly plan digest
    and the sunset alert, and the two test sends (``_email_test_send``,
    ``_alerting_test_send``). Password-reset mail (``api/v1/auth.py``) reaches
    the same end state by a shorter road: it validates nothing and passes the
    configured string through.

    The per-destination From: OVERRIDE is on that list too:
    ``schemas.alerting`` saves it through this same helper, so a display name a
    destination's own send path would deliver can also be stored on it. It was
    the last place where saving was stricter than sending.

    So the display name is parsed off and only the address is validated, and the
    ORIGINAL string comes back — normalising it away would silently drop the
    name the operator configured, and none of the callers reads the normal form
    back for anything.

    This is not a header-injection guard and must not be described as one.
    ``EmailMessage.__setitem__`` already raises on a linefeed or carriage
    return, so a newline cannot reach the wire through any of these paths. What
    this catches is a value with no @-sign, which serialises happily and comes
    back from the relay as an error naming nothing.
    """
    _, address = parseaddr(value)
    if not address:
        raise ValueError(f"Not a usable From: address: {value!r}")
    validate_email_address(address)
    return value


def validate_email_recipients(value: str | None) -> str:
    """Parse a comma-separated recipient list and return a normalized CSV.

    Empty / whitespace-only entries are skipped. Duplicates are collapsed but
    order is preserved (first occurrence wins). Raises if no valid recipient
    remains so an empty destination can't slip through.
    """
    if value is None:
        raise ValueError("Email recipients list is required")
    addresses = _parse_csv_unique(
        value,
        item_validator=validate_email_address,
        limit=_RECIPIENT_LIMIT,
        limit_msg=f"Email recipients list cannot exceed {_RECIPIENT_LIMIT} entries",
    )
    if not addresses:
        raise ValueError("Email recipients list must contain at least one address")
    return ", ".join(addresses)


def validate_email_subject_template(value: str | None) -> str | None:
    """Optional subject template. CR/LF would enable header injection; reject."""
    return _validate_single_line(value, field="Email subject template", max_len=500)


def validate_jira_base_url(value: str | None) -> str:
    return _validate_https_url(
        value, field="Jira base_url", strip_trailing_slash=True, block_private_hosts=True
    )


def validate_jira_auth_email(value: str | None) -> str:
    if value is None:
        raise ValueError("Jira auth_email is required")
    return validate_email_address(value)


def validate_jira_api_token(value: str | None) -> str:
    return _require_clean(value, field="Jira api_token")


def validate_jira_project_key(value: str | None) -> str:
    normalized = _require_clean(value, field="Jira project_key").upper()
    if not _JIRA_PROJECT_KEY_RE.fullmatch(normalized):
        raise ValueError("Jira project_key must be uppercase ASCII (e.g. ENG, PLAT_OPS)")
    return normalized


def validate_jira_issue_type(value: str | None) -> str:
    """Issue-type name as configured in Jira (e.g. 'Task', 'Bug'). Atlassian
    treats these as case-sensitive display names — pass through after trimming
    and rejecting CR/LF."""
    result = _validate_single_line(value, field="Jira issue_type", max_len=64, required=True)
    assert result is not None  # required path
    return result


def validate_linear_api_key(value: str | None) -> str:
    return _require_clean(value, field="Linear api_key")


def validate_linear_team_id(value: str | None) -> str:
    """Linear identifiers are UUIDs or short alnum strings — keep the regex
    conservative so a typo doesn't reach the GraphQL endpoint."""
    result = _validate_id_regex(
        value,
        field="Linear team_id",
        regex=_LINEAR_ID_RE,
        error_msg="Linear team_id must be an alnum / dash / underscore id",
    )
    assert result is not None  # required path
    return result


def validate_linear_state_id(value: str | None) -> str | None:
    return _validate_id_regex(
        value,
        field="Linear state_id",
        regex=_LINEAR_ID_RE,
        error_msg="Linear state_id must be an alnum / dash / underscore id",
        optional=True,
    )


def _check_linear_id(value: str) -> str:
    if not _LINEAR_ID_RE.fullmatch(value):
        raise ValueError("Linear label_ids must be alnum / dash / underscore ids")
    return value


def validate_linear_label_ids(value: str | None) -> str | None:
    """Comma-separated label ids. Empty / whitespace-only input is normalized
    to None; each entry must match the Linear id format."""
    if value is None:
        return None
    ids = _parse_csv_unique(
        value,
        item_validator=_check_linear_id,
        limit=_LINEAR_LABEL_LIMIT,
        limit_msg=f"Linear label_ids must contain at most {_LINEAR_LABEL_LIMIT} entries",
    )
    if not ids:
        return None
    return ",".join(ids)
