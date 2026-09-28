"""Email section of the runtime settings: the SMTP relay config and its guards.

Private half of :mod:`tripl.services.app_settings_service`, which re-exports
every name here; import from the facade.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from tripl.alerting_validation import reject_private_host
from tripl.config import settings
from tripl.services._app_settings_core import build_service_values

if TYPE_CHECKING:
    from tripl.services._app_settings_sources import ResolvedSettings

# Records keep the facade's logger name, so log filters and captures that
# match ``tripl.services.app_settings_service`` still see them.
logger = logging.getLogger("tripl.services.app_settings_service")


@dataclass(frozen=True)
class EmailConfig:
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    # One of config.SMTP_SECURITY_MODES. A string rather than a bool because
    # the three transports are not orderable: implicit TLS is not "more" than
    # STARTTLS, it is a different conversation from the first byte.
    smtp_security: str
    smtp_from_address: str
    #: Whether a per-destination From: override may replace ``smtp_from_address``
    #: (critique #16). True for the operator scope — which, on a self-hosted
    #: instance, includes the default organization — and for an organization
    #: that supplies its OWN relay. False when an organization's mail goes out
    #: through the operator's relay: the operator's server must not send under
    #: a sender the organization picked, so the configured sender is used.
    from_override_allowed: bool = True


def env_email_config() -> EmailConfig:
    return build_email_config({})


def build_email_config(overrides: dict[str, Any]) -> EmailConfig:
    return _email_config_from(build_service_values(overrides))


def _email_config_from(
    values: Mapping[str, Any], *, from_override_allowed: bool = True
) -> EmailConfig:
    return EmailConfig(
        smtp_host=str(values["smtp_host"]),
        smtp_port=int(values["smtp_port"]),
        smtp_username=str(values["smtp_username"]),
        smtp_password=str(values["smtp_password"]),
        smtp_security=str(values["smtp_security"]),
        smtp_from_address=str(values["smtp_from_address"]),
        from_override_allowed=from_override_allowed,
    )


def email_sender_for(override: str | None, email_config: EmailConfig) -> str:
    """The From: address an alert email goes out under (critique #16).

    A destination's ``email_from_address`` is honoured only when the relay
    belongs to the scope that set it (``from_override_allowed``); otherwise the
    relay's configured sender is used and the override is silently ignored.
    """
    if override and email_config.from_override_allowed:
        return override
    return email_config.smtp_from_address


def disabled_email_config() -> EmailConfig:
    """What an organization gets when its settings cannot be read: no relay."""
    return EmailConfig(
        smtp_host="",
        smtp_port=int(settings.smtp_port),
        smtp_username="",
        smtp_password="",
        smtp_security=settings.resolved_smtp_security(),
        smtp_from_address="",
        from_override_allowed=False,
    )


def email_can_send(email_config: EmailConfig) -> bool:
    """Whether this instance can actually deliver mail.

    Both the host and a From: address, not just the host: the password-reset
    sender returns without sending when there is no From: address, so a relay
    with no sender would mint a token, drop the mail, and still have the UI
    promise a link was on its way.
    """
    return bool(email_config.smtp_host and email_config.smtp_from_address)


def _guard_smtp_host(resolved: ResolvedSettings, config: EmailConfig) -> EmailConfig:
    """Refuse an organization's private SMTP host (use time, every organization scope).

    Blocking (DNS): async callers run it in a thread. A refused host leaves the
    organization without a relay rather than failing the caller: mail is off,
    which every sender already handles.
    """
    if "smtp_host" not in resolved.guarded_hosts or not config.smtp_host:
        return config
    try:
        reject_private_host(config.smtp_host, field="SMTP host")
    except ValueError:
        logger.warning(
            "Organization %s SMTP host refused by the private-address guard",
            resolved.org_scope,
        )
        return replace(config, smtp_host="", smtp_password="")
    return config


def _normalized_host(host: str) -> str:
    return host.strip().rstrip(".").lower()


def relay_is_scope_owned(resolved: ResolvedSettings) -> bool:
    """Whether the resolved relay belongs to the resolved scope (critique #16).

    The operator view always owns its relay. An organization scope owns it only
    when it supplied its own SMTP host; an inherited relay is the operator's.
    An organization that entered the operator's own host (compared
    case-insensitively, ignoring a trailing dot) still sends through the
    operator's relay, which may trust the app server by source IP, so that
    relay is not the organization's either.
    """
    if resolved.org_scope is None:
        return True
    if "smtp_host" not in resolved.guarded_hosts:
        return False
    org_host = _normalized_host(str(resolved.values.get("smtp_host") or ""))
    if not org_host:
        return False
    return org_host != _normalized_host(resolved.operator_smtp_host)


def _email_config_of(resolved: ResolvedSettings) -> EmailConfig:
    return _email_config_from(resolved.values, from_override_allowed=relay_is_scope_owned(resolved))


def email_config_for(resolved: ResolvedSettings) -> EmailConfig:
    """The email config of a resolution, with the hosted SMTP-host guard applied."""
    return _guard_smtp_host(resolved, _email_config_of(resolved))
