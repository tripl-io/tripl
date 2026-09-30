"""The "Test connection" / "Send test email" probes, for any settings scope.

Shared by the legacy ``/settings``, ``/orgs/{org}/settings`` and
``/platform/settings`` routes, which differ only in WHICH resolved config they
hand in. Both always answer (ok, message) rather than raising: a provider or a
relay refusing us is the answer the caller asked for, not a server fault.
"""

from __future__ import annotations

import asyncio
import logging

from tripl.schemas.app_settings import SettingsTestResponse
from tripl.services import _email_test_send, llm_service
from tripl.services.app_settings_service import AiConfig, EmailConfig

logger = logging.getLogger(__name__)


async def probe_ai(config: AiConfig, prompt: str) -> SettingsTestResponse:
    if not llm_service.is_enabled(config):
        return SettingsTestResponse(
            ok=False,
            message="AI is disabled or no API key is configured.",
        )
    try:
        raw = await asyncio.to_thread(
            llm_service.complete,
            "You are a connection test endpoint. Keep the response short.",
            prompt,
            max_tokens=20,
            temperature=0,
            config=config,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI settings test failed", exc_info=True)
        return SettingsTestResponse(ok=False, message=str(exc))
    message = (raw or "").strip()
    return SettingsTestResponse(ok=bool(message), message=message or "No response from provider.")


async def probe_email(config: EmailConfig, recipient: str) -> SettingsTestResponse:
    """Send one probe message with ``config`` and report what happened.

    The error text is passed through verbatim because a useful SMTP diagnostic
    is the server's own words ("535 authentication failed", a connection
    timeout); smtplib carries the relay's response in there, never the
    credential we sent.
    """
    try:
        await asyncio.to_thread(
            _email_test_send.send_test_email,
            email_config=config,
            recipient=recipient,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("SMTP settings test failed", exc_info=True)
        return SettingsTestResponse(ok=False, message=str(exc))
    return SettingsTestResponse(ok=True, message=f"Test message sent to {recipient}.")
