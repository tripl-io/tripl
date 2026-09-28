from __future__ import annotations

import http.client
import json
import logging
import urllib.error
import urllib.request
from email.message import Message
from typing import IO, Any
from urllib.parse import urlparse

from tripl.alerting_validation import reject_private_host
from tripl.config import settings  # noqa: F401 - kept for test monkeypatching
from tripl.services.app_settings_service import AiConfig

logger = logging.getLogger(__name__)

_MAX_USER_PROMPT_CHARS = 24_000
# One attempt plus one per parameter _adjust_payload_for_error can drop.
_MAX_PARAM_RETRIES = 4


def is_enabled(config: AiConfig) -> bool:
    """Whether ``config`` can make a completion.

    ``config`` is required (F20 PR9): there is no env default any more, because
    env holds the OPERATOR's key and a caller that forgot its organization's
    config would otherwise quietly send that organization's text on it.
    """
    return config.ai_enabled and bool(config.ai_api_key)


def _host_allowed(cfg: AiConfig) -> bool:
    """The use-time half of the SSRF guard for an organization's endpoint.

    Only for a config that says so (an organization's ``ai_base_url`` on a
    hosted instance). Re-resolving right before the request catches a name that
    pointed somewhere public when it was saved and at a private address now.
    """
    if not cfg.host_guard:
        return True
    hostname = urlparse(cfg.ai_base_url).hostname
    if not hostname:
        return False
    try:
        reject_private_host(hostname, field="AI base URL")
    except ValueError:
        logger.warning("AI request refused: the organization's AI host is not public")
        return False
    return True


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect from a chat-completions endpoint.

    urllib's default handler turns a 301/302/303 answer to our POST into a GET
    to any host, carrying the ``Authorization`` header along — so a public
    ``ai_base_url`` answering ``302 -> 169.254.169.254`` would send tripl into
    the internal network past :func:`_host_allowed`, which only checks the
    configured host. A chat-completions POST has no legitimate reason to move,
    so the 3xx surfaces as an ``HTTPError`` (the provider-error branch below).
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


# Not installed with install_opener: other urllib callers keep their policy.
_NO_REDIRECT_OPENER = urllib.request.build_opener(_RefuseRedirects)


def _post_chat_completions(
    url: str, payload: dict[str, Any], api_key: str, timeout: float
) -> tuple[str | None, dict[str, Any] | None]:
    """POST the payload; return (response_body, None) or (None, provider_error).

    provider_error is the parsed ``error`` object from an HTTP 4xx/5xx response
    (empty dict when the body is missing or not JSON). Network-level failures
    return (None, None) after logging.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
            return response.read().decode("utf-8"), None
    except urllib.error.HTTPError as exc:
        try:
            error_body = exc.read().decode("utf-8", errors="replace")[:2000]
        except OSError, http.client.HTTPException:
            error_body = "<unreadable>"
        logger.warning(
            "AI completion request failed with HTTP %s: %s; model=%s url=%s body=%s",
            exc.code,
            exc.reason,
            payload.get("model"),
            url,
            error_body,
        )
        error: dict[str, Any] = {}
        try:
            parsed = json.loads(error_body)
            if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
                error = parsed["error"]
        except json.JSONDecodeError:
            pass
        return None, error
    except OSError, http.client.HTTPException, UnicodeError, TimeoutError:
        logger.exception("AI completion request failed")
        return None, None


def _adjust_payload_for_error(payload: dict[str, Any], error: dict[str, Any]) -> bool:
    """Mutate payload to work around an unsupported-parameter error.

    Newer OpenAI models (gpt-5.x, o-series) reject ``max_tokens`` in favor of
    ``max_completion_tokens`` and only allow the default ``temperature``; some
    OpenAI-compatible servers do not support ``response_format``.
    Returns True when the payload changed and the request is worth retrying.
    """
    if error.get("code") not in {"unsupported_parameter", "unsupported_value"}:
        return False
    param = error.get("param")
    if param == "max_tokens" and "max_tokens" in payload:
        payload["max_completion_tokens"] = payload.pop("max_tokens")
        return True
    if param == "temperature" and "temperature" in payload:
        del payload["temperature"]
        return True
    if param == "response_format" and "response_format" in payload:
        del payload["response_format"]
        return True
    return False


def complete(
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: int | None = None,
    temperature: float = 0.2,
    response_format: dict[str, Any] | None = None,
    config: AiConfig,
) -> str | None:
    cfg = config
    if not is_enabled(cfg) or not _host_allowed(cfg):
        return None
    api_key = cfg.ai_api_key
    if not api_key:
        logger.warning("AI features enabled but no API key is configured")
        return None

    truncated_user_prompt = user_prompt[:_MAX_USER_PROMPT_CHARS]
    payload: dict[str, Any] = {
        "model": cfg.ai_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": truncated_user_prompt},
        ],
        "max_tokens": max_tokens if max_tokens is not None else cfg.ai_max_output_tokens,
        "temperature": temperature,
    }
    if response_format is not None:
        payload["response_format"] = response_format

    url = cfg.ai_base_url.rstrip("/") + "/chat/completions"
    body: str | None = None
    for _ in range(_MAX_PARAM_RETRIES):
        body, error = _post_chat_completions(url, payload, api_key, cfg.ai_timeout_seconds)
        if body is not None:
            break
        if error is None or not _adjust_payload_for_error(payload, error):
            return None
        logger.info("Retrying AI completion with adjusted parameters: %s", error.get("param"))
    if body is None:
        return None

    try:
        parsed = json.loads(body)
        if not isinstance(parsed, dict):
            logger.warning("AI completion response is not an object")
            return None
        choices = parsed.get("choices")
        if not isinstance(choices, list) or not choices:
            logger.warning("AI completion response has no choices")
            return None
        if not isinstance(choices[0], dict):
            logger.warning("AI completion response choice is not an object")
            return None
        message = choices[0].get("message")
        if not isinstance(message, dict):
            logger.warning("AI completion response message is not an object")
            return None
        content = message.get("content")
        if not isinstance(content, str):
            logger.warning("AI completion response content is not a string")
            return None
        return content
    except json.JSONDecodeError:
        logger.exception("Failed to parse AI completion response")
        return None
