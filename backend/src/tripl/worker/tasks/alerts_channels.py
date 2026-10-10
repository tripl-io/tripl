from __future__ import annotations

import base64
import json
import smtplib
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from email.message import EmailMessage
from http.client import HTTPMessage
from typing import IO, Protocol, cast
from urllib.parse import quote, urlparse

from tripl import __version__
from tripl.alert_templates import (
    ALERT_MESSAGE_FORMAT_PLAIN,
    ALERT_MESSAGE_FORMAT_SLACK_MRKDWN,
    ALERT_MESSAGE_FORMAT_TELEGRAM_HTML,
    ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2,
)
from tripl.alerting_validation import (
    reject_private_host,
    validate_email_recipients,
    validate_sender_address,
    validate_slack_webhook_url,
)
from tripl.config import SMTP_SECURITY_IMPLICIT_TLS, SMTP_SECURITY_STARTTLS, settings
from tripl.crypto import decrypt_value
from tripl.models.alert_destination import AlertDestination, AlertDestinationType
from tripl.models.project import Project
from tripl.services import app_settings_service, safe_http
from tripl.worker.tasks.alerts_messages import _build_jira_adf_body

PostJson = Callable[..., dict[str, object] | None]
GetJson = Callable[..., dict[str, object] | None]
SendEmailMessage = Callable[..., None]


class SendSlackMessage(Protocol):
    def __call__(self, webhook_url: str, text: str, *, message_format: str) -> None: ...


def _safe_url_for_error(url: str) -> str:
    """Scheme and host only — the rest of a destination URL is often the secret.

    This used to mask the Telegram bot token with a Telegram-shaped regex, which
    left every other channel untouched: a failed Slack delivery put the full
    incoming-webhook URL into ``alert_deliveries.error_message``, which the API
    returns and the UI renders. That URL IS the credential — anyone who can read
    it can post to the channel. Generic webhooks and tracker URLs
    carry tokens in the path or query just as often.

    The status code and the response body already carry the diagnostic value;
    the host is enough to say which destination failed.
    """
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return "the destination URL"
    return f"{parsed.scheme}://{parsed.netloc}"


def _decrypt_secret(encrypted: str | None) -> str:
    return decrypt_value(encrypted or "")


def _reject_private_target(url: str, *, field: str) -> None:
    """Re-validate the destination host immediately before the outbound request.

    Config-time validation can be bypassed by DNS rebinding (a hostname that
    resolved to a public IP at save time later resolving to 169.254.169.254 /
    RFC1918), so we re-check the resolved host here to defend the send path.
    On its own that is check-then-connect: the connection resolves the name
    again. Where outbound hosts must be public, the request itself connects to
    the address it vetted (see the outbound rules below).
    """
    hostname = urlparse(url).hostname
    if hostname:
        reject_private_host(hostname, field=field)


# The outbound rules every request below shares, in one place
# (:func:`_request_json`):
#
# * HTTPS only, redirect hops included. Every URL these channels are configured
#   with is https already (the destination validators and the fixed Telegram,
#   Linear and PagerDuty endpoints), so refusing http costs nothing and keeps
#   TLS as the defence against DNS rebinding on every hop: a rebound private
#   address cannot present a certificate for the name. An https -> http hop
#   used to be followed, which turned a public destination's 302 into a
#   plain-HTTP request to whatever address the next lookup returned.
# * When outbound hosts must be public (``Settings.public_hosts_only``:
#   OUTBOUND_PUBLIC_HOSTS_ONLY, always on a hosted instance), the request goes
#   through ``safe_http.send_pinned``: the name is resolved once, every address
#   vetted, and the connection made to the vetted one, so what was checked is
#   what is reached. No redirect is followed there: a 3xx is a failure.
#   Otherwise urllib sends it (keeping an operator's proxy settings) and a hop
#   to a public https host is followed, its target checked first.
# * Bounded reads: an answer is read up to :data:`_MAX_RESPONSE_BYTES`, an
#   error body up to :data:`_MAX_ERROR_BODY_BYTES`, and the error message
#   carries at most :data:`_MAX_DETAIL_CHARS` of it. That message is stored on
#   the delivery, written to the audit log and shown to the editor who pressed
#   Test, so it must not be a way to read a receiver's whole answer back.
_TIMEOUT_SECONDS = 10
#: The whole request on the pinned path, name lookup and body read included.
_DEADLINE_SECONDS = 30
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_ERROR_BODY_BYTES = 64 * 1024
_MAX_DETAIL_CHARS = 500
_USER_AGENT = f"tripl/{__version__}"

# Everything this module sends beyond ``Accept`` and ``User-Agent`` is a
# credential: the Jira / Linear ``Authorization`` header, and the operator's
# configured webhook secret header — whose *name* is operator-chosen, so no
# denylist can enumerate it. Hence an allowlist: a header is forwarded across an
# origin change only if it is named here.
_CROSS_ORIGIN_SAFE_HEADERS = frozenset({"accept", "user-agent"})


def _origin(url: str) -> tuple[str | None, int]:
    """Host and port of an https URL, the only scheme this module sends to."""
    parsed = urlparse(url)
    return parsed.hostname, parsed.port or 443


class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Run the SSRF guard on every redirect hop, not just the configured URL.

    ``_reject_private_target`` only ever sees the URL an operator saved, while
    urllib's default opener follows a 3xx to any host with no second check. A
    public destination answering ``302 -> 169.254.169.254`` therefore reached
    the metadata endpoint anyway, and a non-2xx final hop put that response
    body into the delivery error the API returns.

    Raising the guard's own ValueError instead of returning ``None`` (which
    urllib turns into an opaque HTTP 302 error) keeps the reason in the message
    the operator reads.

    The hop is still followed when the target is a public https URL, so
    credentials must be dropped when the origin changes: urllib copies every
    header except the content ones onto the new request, which handed the
    operator's Jira basic auth and webhook secret to whatever public host
    answered the 302.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        parsed = urlparse(newurl)
        # urllib's own scheme check here also permits http://, ftp:// and
        # scheme-relative targets; see the outbound rules above.
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("Redirect target must be an https URL")
        reject_private_host(parsed.hostname, field="Redirect target")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None or _origin(req.full_url) == _origin(newurl):
            return redirected
        redirected.headers = {
            name: value
            for name, value in redirected.headers.items()
            if name.lower() in _CROSS_ORIGIN_SAFE_HEADERS
        }
        return redirected


# Deliberately not installed with install_opener: that would swap the redirect
# policy for every other urllib caller in the process. The redirect-chain limit
# stays urllib's default (HTTPRedirectHandler.max_redirections).
_REDIRECT_SAFE_OPENER = urllib.request.build_opener(_ValidatingRedirectHandler)


def _error_message(url: str, code: int, raw: bytes, *, detail: str | None = None) -> str:
    """``HTTP <code> from <scheme://host>: <detail>``, the detail bounded.

    The detail is the body, or Telegram's ``description`` when the body is its
    JSON error. Callers match on it (Telegram's "message is too long").
    """
    if detail is None:
        text = raw[:_MAX_ERROR_BODY_BYTES].decode("utf-8", errors="replace").strip()
        detail = text
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                description = parsed.get("description")
                if isinstance(description, str) and description.strip():
                    detail = description.strip()
    if len(detail) > _MAX_DETAIL_CHARS:
        detail = detail[: _MAX_DETAIL_CHARS - 3].rstrip() + "..."
    message = f"HTTP {code} from {_safe_url_for_error(url)}"
    return f"{message}: {detail}" if detail else message


def _open_with_urllib(request: urllib.request.Request) -> tuple[int, bytes]:
    """Self-hosted: urllib, following a public https redirect (see above)."""
    try:
        with _REDIRECT_SAFE_OPENER.open(request, timeout=_TIMEOUT_SECONDS) as response:
            return int(response.status), response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(_MAX_ERROR_BODY_BYTES)
        except Exception:  # noqa: BLE001
            raw = b""
        raise ValueError(_error_message(request.full_url, exc.code, raw)) from exc


def _open_pinned(request: urllib.request.Request) -> tuple[int, bytes]:
    """Public hosts only: one lookup, the vetted address, no redirect (see above)."""
    url = request.full_url
    answer = safe_http.send_pinned(
        request.get_method(),
        url,
        dict(request.header_items()),
        cast(bytes | None, request.data),
        field="Destination URL",
        timeout=_TIMEOUT_SECONDS,
        max_response_bytes=_MAX_RESPONSE_BYTES,
        deadline=_DEADLINE_SECONDS,
    )
    if 200 <= answer.status < 300:
        return answer.status, answer.body
    redirect = 300 <= answer.status < 400
    # Raised from an HTTPError like the urllib path's, so a caller reading the
    # cause chain (the Test dialog's HTTP status, the ticket retry policy) sees
    # the same thing whichever path sent it.
    cause = urllib.error.HTTPError(url, answer.status, "", HTTPMessage(), None)
    message = _error_message(
        url,
        answer.status,
        answer.body,
        detail="redirects are not followed on this instance" if redirect else None,
    )
    raise ValueError(message) from cause


def _request_json(
    method: str,
    url: str,
    body: dict[str, object] | None,
    headers: dict[str, str] | None,
) -> tuple[int, dict[str, object] | None]:
    """Send one request; the 2xx status and its JSON object answer, if it has one.

    Every non-2xx raises ``ValueError("HTTP <code> from <scheme://host>: ...")``
    from a ``urllib.error.HTTPError``. An answer over :data:`_MAX_RESPONSE_BYTES`
    counts as one with no JSON object.
    """
    if urlparse(url).scheme != "https":
        raise ValueError("Destination URL must be an https URL")
    request_headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    data = None
    if body is not None:
        request_headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    opener = _open_pinned if settings.public_hosts_only else _open_with_urllib
    status, raw = opener(request)
    if not raw or len(raw) > _MAX_RESPONSE_BYTES:
        return status, None
    try:
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return status, None
    return status, parsed if isinstance(parsed, dict) else None


def _post_json(
    url: str,
    body: dict[str, object],
    headers: dict[str, str] | None = None,
) -> dict[str, object] | None:
    """POST JSON and return a JSON object response when one is available."""
    return _request_json("POST", url, body, headers)[1]


def _post_json_with_status(
    url: str,
    body: dict[str, object],
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object] | None]:
    """:func:`_post_json`, plus the HTTP status of the 2xx answer.

    For the one channel whose contract names a specific success code: the
    PagerDuty Events API answers an accepted event with 202, and anything else
    — a 200 from a proxy that swallowed the request included — is not an
    event PagerDuty has queued. Every non-2xx still raises, exactly as above.
    """
    return _request_json("POST", url, body, headers)


def _get_json(
    url: str,
    headers: dict[str, str] | None = None,
) -> dict[str, object] | None:
    """GET a URL and return a JSON object response when one is available.

    The read-side twin of :func:`_post_json`. Used to poll a tracker for issue
    status."""
    return _request_json("GET", url, None, headers)[1]


def _send_slack_message(
    post_json: PostJson,
    webhook_url: str,
    text: str,
    *,
    message_format: str,
) -> None:
    mrkdwn = message_format == ALERT_MESSAGE_FORMAT_SLACK_MRKDWN
    post_json(webhook_url, {"text": text, "mrkdwn": mrkdwn})


def _send_telegram_message(
    post_json: PostJson,
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    message_format: str,
) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    body: dict[str, object] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if message_format == ALERT_MESSAGE_FORMAT_TELEGRAM_HTML:
        body["parse_mode"] = "HTML"
    elif message_format == ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2:
        body["parse_mode"] = "MarkdownV2"
    post_json(url, body)


def _send_webhook_message(
    post_json: PostJson,
    target_url: str,
    payload: dict[str, object],
    *,
    header_name: str | None = None,
    header_value: str | None = None,
) -> None:
    headers: dict[str, str] | None = None
    if header_name and header_value is not None:
        headers = {header_name: header_value}
    post_json(target_url, payload, headers)


def _parse_email_recipients(value: str | None) -> list[str]:
    if not value:
        return []
    return [r.strip() for r in value.split(",") if r.strip()]


class SmtpModule(Protocol):
    """The two smtplib entry points, injected together.

    Taking the MODULE rather than one class is what keeps the transport choice
    in a single place. Handing each of the six call sites a class would copy the
    "which client does this mode need?" decision six times over, and the mode is
    already threaded past all of them anyway.
    """

    # Read-only properties rather than plain attributes on purpose: a mutable
    # protocol member is invariant, so ``smtplib`` itself would not satisfy
    # ``SMTP: type`` — ``type[smtplib.SMTP]`` is narrower, and invariance
    # rejects exactly that. Declared this way the match is covariant, which is
    # what "I only ever read these two names off you" actually means.
    @property
    def SMTP(self) -> type: ...  # noqa: N802 — mirrors smtplib's own name

    @property
    def SMTP_SSL(self) -> type: ...  # noqa: N802 — mirrors smtplib's own name


# Long enough for a busy relay to answer, short enough that a caller waiting on
# the send does not look hung. Worth a name: when the client speaks the wrong
# protocol the connection does not fail, it stalls until exactly this deadline,
# and as a bare literal that read like a network problem.
SMTP_TIMEOUT_SECONDS = 10


def _send_email_message(
    *,
    smtp_module: SmtpModule,
    smtp_host: str,
    smtp_port: int,
    smtp_username: str,
    smtp_password: str,
    smtp_security: str,
    from_address: str,
    recipients: list[str],
    subject: str,
    body: str,
) -> None:
    msg = EmailMessage()
    msg["From"] = from_address
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)
    # Implicit TLS wraps the socket before a single byte is exchanged, so it
    # needs SMTP_SSL: the server's greeting arrives already encrypted and the
    # plaintext 220 that smtplib.SMTP blocks waiting for is never spoken.
    # STARTTLS is the opposite order — connect in the clear, read the greeting,
    # then upgrade — so asking for it here would be too late to help.
    implicit_tls = smtp_security == SMTP_SECURITY_IMPLICIT_TLS
    smtp_cls = smtp_module.SMTP_SSL if implicit_tls else smtp_module.SMTP
    with smtp_cls(smtp_host, smtp_port, timeout=SMTP_TIMEOUT_SECONDS) as conn:
        if smtp_security == SMTP_SECURITY_STARTTLS:
            conn.starttls()
        if smtp_username:
            conn.login(smtp_username, smtp_password)
        refused = conn.send_message(msg)
    # smtplib raises ONLY when every recipient is refused (SMTPRecipientsRefused);
    # a partial refusal is returned quietly as {address: (code, reason)}. That
    # return used to be discarded, so an alert that reached two of five people was
    # stored and displayed as "sent".
    #
    # Failing the whole delivery is deliberate: a manual retry may duplicate the
    # mail for the recipients who did get it, but the alternative — the operator
    # believing an alert was delivered when it silently was not — is worse for the
    # one job alerting has. The refused addresses go in the message so the Audit
    # view names them rather than just saying "failed".
    if refused:
        detail = (
            f"SMTP refused {len(refused)} of {len(recipients)} recipients: "
            f"{', '.join(sorted(refused))}"
        )
        raise ValueError(detail)


def send_with_config(
    email_config: app_settings_service.EmailConfig,
    *,
    recipients: list[str],
    subject: str,
    body: str,
) -> None:
    """Send one message through ``email_config``'s relay, from its configured sender.

    For mail that is not an alert delivery: account mail and the SMTP test send.
    No destination From: override applies to those, so the sender is always
    the relay's own ``smtp_from_address``. It calls :func:`_send_email_message`
    by its name in this module, so a test that replaces that name here also
    captures these sends.
    """
    _send_email_message(
        smtp_module=smtplib,
        smtp_host=email_config.smtp_host,
        smtp_port=email_config.smtp_port,
        smtp_username=email_config.smtp_username,
        smtp_password=email_config.smtp_password,
        smtp_security=email_config.smtp_security,
        from_address=email_config.smtp_from_address,
        recipients=recipients,
        subject=subject,
        body=body,
    )


# The title half of the email subject the weekly plan digest has always
# carried, and the default below so that path keeps sending exactly the subject
# it did. It is a parameter at all because this helper now serves two beats:
# ``check_deprecated_sunset_events`` reuses it on its own DAILY schedule
# (celery_app.py's ``check-deprecated-sunset-events``), and with the subject
# hardcoded here that alert reached the operator's inbox titled "Weekly tripl
# digest" — wrong about the cadence, wrong about the contents, and threaded by
# the mail client into the weekly digest's conversation.
#
# Only the title is the caller's: the ``[project]`` prefix is applied below, so
# the two beats cannot drift into different subject SHAPES. Slack has no
# subject, so that arm never reads this.
DIGEST_SUBJECT_TITLE = "Weekly tripl digest"


def _send_digest_to_destination(
    *,
    destination: AlertDestination,
    message: str,
    project: Project,
    email_config: app_settings_service.EmailConfig,
    send_slack_message: SendSlackMessage,
    send_email_message: SendEmailMessage,
    subject_title: str = DIGEST_SUBJECT_TITLE,
) -> None:
    if destination.type == AlertDestinationType.slack.value:
        webhook_url = _decrypt_secret(destination.webhook_url_encrypted)
        validate_slack_webhook_url(webhook_url)
        _reject_private_target(webhook_url, field="Slack webhook URL")
        send_slack_message(
            webhook_url,
            message,
            message_format=ALERT_MESSAGE_FORMAT_PLAIN,
        )
        return
    if destination.type == AlertDestinationType.email.value:
        recipients = _parse_email_recipients(destination.email_recipients)
        validate_email_recipients(destination.email_recipients)
        # The override only on the organization's own relay (critique #16).
        from_address = app_settings_service.email_sender_for(
            destination.email_from_address, email_config
        )
        if not from_address:
            raise ValueError(f"Email destination {destination.name!r} requires a From address")
        # The same helper the per-delivery path resolves with
        # (``alerts._resolve_email_context``) and the same one both test sends
        # check with, so all four agree on which senders are usable: a display
        # name in the global Default From is legal — it reaches ``msg["From"]``
        # below intact — and a value with no @-sign is still refused. The strict
        # ``validate_email_address`` used to stand here and refused the display
        # name, which cost more here than anywhere else: both callers swallow
        # the failure into ``logger.warning`` (alerts_digest.py), so the weekly
        # plan digest and the sunset alert simply stopped arriving and said so
        # only in the worker log.
        #
        # Assigned rather than called for its raise, so this site and
        # ``_resolve_email_context`` read identically; the helper returns the
        # string unchanged, so the assignment itself is a no-op.
        from_address = validate_sender_address(from_address)
        send_email_message(
            smtp_host=email_config.smtp_host,
            smtp_port=email_config.smtp_port,
            smtp_username=email_config.smtp_username,
            smtp_password=email_config.smtp_password,
            smtp_security=email_config.smtp_security,
            from_address=from_address,
            recipients=recipients,
            subject=f"[{project.name}] {subject_title}",
            body=message,
        )


def _send_jira_issue(
    post_json: PostJson,
    *,
    base_url: str,
    auth_email: str,
    api_token: str,
    project_key: str,
    issue_type: str,
    summary: str,
    body_text: str,
    labels: Sequence[str] | None = None,
) -> tuple[str | None, str | None]:
    """Create a Jira issue and return ``(issue_id, issue_key)`` from the response.

    ``labels`` is how a caller makes its own create findable again. Jira's create
    is not idempotent and offers no idempotency key, so the only way to answer
    "did I already create this?" after a crash is to have written something
    searchable onto the issue itself. Omitted by default, so the
    alerting path sends exactly the payload it always has.
    """
    credentials = base64.b64encode(f"{auth_email}:{api_token}".encode()).decode()
    url = f"{base_url}/rest/api/3/issue"
    fields: dict[str, object] = {
        "project": {"key": project_key},
        "issuetype": {"name": issue_type},
        "summary": summary,
        "description": _build_jira_adf_body(body_text),
    }
    if labels:
        fields["labels"] = list(labels)
    payload: dict[str, object] = {"fields": fields}
    response = post_json(
        url,
        payload,
        headers={"Authorization": f"Basic {credentials}", "Accept": "application/json"},
    )
    issue_id = response.get("id") if isinstance(response, dict) else None
    issue_key = response.get("key") if isinstance(response, dict) else None
    return (
        issue_id if isinstance(issue_id, str) else None,
        issue_key if isinstance(issue_key, str) else None,
    )


def _find_jira_issue_by_label(
    get_json: GetJson,
    *,
    base_url: str,
    auth_email: str,
    api_token: str,
    label: str,
) -> tuple[str | None, str | None]:
    """The issue carrying ``label``, as ``(issue_id, issue_key)``, or ``(None, None)``.

    This is the read half of the idempotency ``_send_jira_issue``'s ``labels``
    makes possible: having stamped a create with a label derived from what it was
    for, a later run can ask whether that create already happened rather than
    repeating it.

    TWO LIMITS, both deliberate and both failing SAFE — a miss here means the
    caller creates, which is exactly what it did before this existed:

    - Jira indexes asynchronously, so an issue created moments ago may not be
      searchable yet. A redelivery inside that window still duplicates.
    - This asks the JQL search endpoint. If an instance answers it differently
      than expected, the parse below yields ``None`` and nothing breaks.
    """
    credentials = base64.b64encode(f"{auth_email}:{api_token}".encode()).decode()
    # Quoted so a label is never parsed as JQL syntax; labels cannot contain
    # spaces or quotes, and the caller derives this one from a uuid.
    jql = quote(f'labels = "{label}"', safe="")
    url = f"{base_url}/rest/api/3/search/jql?jql={jql}&fields=id,key&maxResults=1"
    response = get_json(
        url,
        headers={"Authorization": f"Basic {credentials}", "Accept": "application/json"},
    )
    issues = response.get("issues") if isinstance(response, dict) else None
    first = issues[0] if isinstance(issues, list) and issues else None
    if not isinstance(first, dict):
        return (None, None)
    issue_id = first.get("id")
    issue_key = first.get("key")
    return (
        issue_id if isinstance(issue_id, str) else None,
        issue_key if isinstance(issue_key, str) else None,
    )


def _get_jira_issue_status(
    get_json: GetJson,
    *,
    base_url: str,
    auth_email: str,
    api_token: str,
    issue_key: str,
) -> str | None:
    """Return a Jira issue's status **category** key, lowercased.

    Jira groups statuses into three categories — ``new`` (to-do),
    ``indeterminate`` (in-progress) and ``done`` — via
    ``fields.status.statusCategory.key``. Polling on the category (not the
    workflow-specific status name) means any "done"-category status closes the
    ticket regardless of a project's custom workflow. Returns ``None`` when the
    field is missing or malformed."""
    credentials = base64.b64encode(f"{auth_email}:{api_token}".encode()).decode()
    url = f"{base_url}/rest/api/3/issue/{issue_key}?fields=status"
    response = get_json(
        url,
        headers={"Authorization": f"Basic {credentials}", "Accept": "application/json"},
    )
    if not isinstance(response, dict):
        return None
    fields = response.get("fields")
    if not isinstance(fields, dict):
        return None
    status = fields.get("status")
    if not isinstance(status, dict):
        return None
    category = status.get("statusCategory")
    if not isinstance(category, dict):
        return None
    key = category.get("key")
    if not isinstance(key, str) or not key.strip():
        return None
    return key.strip().lower()


def _send_linear_issue(
    post_json: PostJson,
    *,
    api_key: str,
    team_id: str,
    title: str,
    body_text: str,
    state_id: str | None = None,
    label_ids: list[str] | None = None,
) -> tuple[str | None, str | None]:
    """Create a Linear issue and return ``(issue_id, identifier)`` from the response."""
    input_payload: dict[str, object] = {
        "teamId": team_id,
        "title": title,
        "description": body_text,
    }
    if state_id:
        input_payload["stateId"] = state_id
    if label_ids:
        input_payload["labelIds"] = label_ids
    mutation = (
        "mutation IssueCreate($input: IssueCreateInput!) {"
        " issueCreate(input: $input) { success issue { id identifier } }"
        " }"
    )
    response = post_json(
        "https://api.linear.app/graphql",
        {"query": mutation, "variables": {"input": input_payload}},
        headers={"Authorization": api_key, "Accept": "application/json"},
    )
    issue: dict[str, object] = {}
    if isinstance(response, dict):
        data = response.get("data")
        if isinstance(data, dict):
            issue_create = data.get("issueCreate")
            if isinstance(issue_create, dict) and isinstance(issue_create.get("issue"), dict):
                issue = issue_create["issue"]
    issue_id = issue.get("id")
    identifier = issue.get("identifier")
    return (
        issue_id if isinstance(issue_id, str) else None,
        identifier if isinstance(identifier, str) else None,
    )
