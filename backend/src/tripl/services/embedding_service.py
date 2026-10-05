from __future__ import annotations

import hashlib
import http.client
import json
import logging
import math
import urllib.request
from typing import Any, cast
from urllib.parse import urlparse

from tripl.alerting_validation import reject_private_host
from tripl.config import settings
from tripl.services.app_settings_service import AiConfig
from tripl.services.llm_service import NO_REDIRECT_OPENER

logger = logging.getLogger(__name__)

#: Default only. The endpoint actually used is the config's
#: ``search_embedding_base_url`` — see :func:`embeddings_url`.
OPENAI_EMBEDDINGS_BASE_URL = "https://api.openai.com/v1"

#: What the save-time probe embeds (F20 PR10). Any short text would do.
_PROBE_TEXT = "tripl embedding dimension check"


def embedding_provenance(config: AiConfig) -> str:
    """Stable identity of the embedding space stored in a 128-char DB field.

    Per organization (F20 PR10): the endpoint is the CONFIG's, so two
    organizations on the same model behind different endpoints never compare
    vectors with each other. An organization that inherits the operator's
    endpoint gets exactly the operator's identity — the value every document
    stored before per-organization embeddings was stamped with — so nothing is
    re-embedded when organizations arrive (critique #21).
    """
    source = "\n".join(
        (
            config.search_embedding_base_url.rstrip("/"),
            config.search_embedding_provider,
            config.search_embedding_model,
            str(settings.search_embedding_dimensions),
        )
    )
    return "sha256:" + hashlib.sha256(source.encode("utf-8")).hexdigest()


def sanitize_embedding(values: list[float]) -> list[float]:
    """Accept only finite vectors that fit the fixed database column."""
    try:
        sanitized = [float(value) for value in values]
    except TypeError, ValueError:
        return []
    if len(sanitized) != settings.search_embedding_dimensions:
        return []
    if any(not math.isfinite(value) for value in sanitized):
        return []
    return sanitized


def embeddings_url(config: AiConfig | None = None) -> str:
    """The embeddings endpoint ``config`` posts to.

    ``None`` is the operator's endpoint (``SEARCH_EMBEDDING_BASE_URL``); an
    organization's config carries its own (F20 PR10).

    Read at CALL time rather than bound at import, so a test — and an operator
    reading the value back out of a running process — sees the configured
    endpoint rather than whatever the environment held when this module was
    first imported.

    Built from a BASE exactly as ``llm_service`` builds its chat endpoint
    (``ai_base_url.rstrip("/") + "/chat/completions"``), so one provider's two
    endpoints are configured alike rather than one taking a base and the other a
    full URL.

    This was a hardcoded api.openai.com constant while the docs told self-hosters
    to point ``SEARCH_EMBEDDING_*`` at their own endpoint to keep plan text
    inside their infrastructure. Following that instruction sent the text to
    OpenAI anyway, with the operator's own credential attached.
    """
    base = (
        settings.search_embedding_base_url if config is None else config.search_embedding_base_url
    )
    return base.rstrip("/") + "/embeddings"


def _host_allowed(cfg: AiConfig) -> bool:
    """The use-time half of the SSRF guard for an organization's endpoint.

    Only for a config that says so (``embedding_host_guard``: the endpoint came
    from an organization). Re-resolving right before the request catches a name
    that pointed somewhere public when it was saved and at a private address now.
    """
    if not cfg.embedding_host_guard:
        return True
    hostname = urlparse(cfg.search_embedding_base_url).hostname
    if not hostname:
        return False
    try:
        reject_private_host(hostname, field="Search embedding base URL")
    except ValueError:
        logger.warning("Embedding request refused: the organization's host is not public")
        return False
    return True


def _refuses_redirects(cfg: AiConfig) -> bool:
    """An organization's endpoint, or any endpoint where outbound requests must
    reach public hosts only (``Settings.public_hosts_only``).

    A 3xx from a public endpoint would otherwise carry the ``Authorization``
    header to wherever it points, past :func:`_host_allowed` (the same reasoning
    as ``llm_service._RefuseRedirects``).
    """
    return cfg.embedding_host_guard or settings.public_hosts_only


def can_embed(config: AiConfig) -> bool:
    """Whether ``config`` could embed anything at all (on, a supported provider, a key).

    No request is made: this is what :func:`embed_texts` checks before its HTTP call.
    """
    return (
        config.search_embeddings_enabled
        and config.search_embedding_provider == "openai"
        and bool(config.search_embedding_api_key)
    )


def embed_query(text: str, *, config: AiConfig, timeout: float = 30) -> list[float]:
    embeddings = embed_texts([text], config=config, timeout=timeout)
    return embeddings[0] if embeddings else []


def probe_embedding_dimensions(config: AiConfig, *, timeout: float = 15) -> str | None:
    """Embed one short text with ``config``; ``None`` when the vector fits the index.

    The save-time check of an organization's embedding settings (F20 PR10): the
    column is vector(:attr:`~tripl.config.Settings.search_embedding_dimensions`)
    for everyone, so a model that answers with another width would have every
    document marked ``failed`` with nothing on any screen to say why. Blocking
    (HTTP): async callers run it in a thread. Returns the reason otherwise.
    """
    if config.search_embedding_provider != "openai":
        return (
            f"Unsupported search embedding provider {config.search_embedding_provider!r}; "
            "only 'openai' (any OpenAI-compatible endpoint) is supported."
        )
    if not config.search_embedding_api_key:
        return "Search embeddings need an API key for this endpoint."
    vectors = embed_texts([_PROBE_TEXT], config=config, timeout=timeout)
    if not vectors or not vectors[0]:
        return "The embedding endpoint returned no vector: check the base URL, model and API key."
    width = len(vectors[0])
    expected = settings.search_embedding_dimensions
    if width != expected:
        return (
            f"The embedding model returned {width}-dimension vectors; "
            f"tripl stores {expected}-dimension vectors."
        )
    if not sanitize_embedding(vectors[0]):
        return "The embedding model returned a vector tripl cannot store."
    return None


def embed_texts(texts: list[str], *, config: AiConfig, timeout: float = 30) -> list[list[float]]:
    cfg = config
    if not cfg.search_embeddings_enabled:
        return []
    if cfg.search_embedding_provider != "openai":
        logger.warning(
            "Unsupported search embedding provider: %s",
            cfg.search_embedding_provider,
        )
        return []
    api_key = cfg.search_embedding_api_key
    if not api_key:
        logger.warning("Search embeddings enabled but no API key is configured")
        return []

    payload: dict[str, object] = {
        "model": cfg.search_embedding_model,
        "input": [text[:16_000] for text in texts],
    }
    # The column is vector(1536); Settings rejects any other width at startup.
    if settings.search_embedding_dimensions > 0:
        payload["dimensions"] = settings.search_embedding_dimensions

    if not _host_allowed(cfg):
        return []

    url = embeddings_url(cfg)
    parsed_url = urlparse(url)
    if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
        # A malformed endpoint (a stored blank base URL gives "/embeddings")
        # would raise ValueError out of ``Request``; no caller expects that.
        logger.warning("Search embedding endpoint is not an http(s) URL")
        return []
    # ``OSError`` is the transport family in one name: ``URLError``, its
    # ``HTTPError`` subclass, and ``TimeoutError`` all derive from it.
    # ``http.client.HTTPException`` is the other half — a connection dropped
    # mid-body raises ``IncompleteRead`` out of ``read()``, after the 200 has
    # already been accepted, so nothing downstream would catch it. ``ValueError``
    # is what ``Request`` raises for a URL it cannot use.
    try:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        if _refuses_redirects(cfg):
            with NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
                body = response.read()
        else:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                body = response.read()
    except OSError, http.client.HTTPException, ValueError:
        logger.exception("Search embedding request failed")
        return []

    # A 200 is not a promise of a parseable body. The endpoint is
    # operator-configurable, so a gateway in front of a self-hosted provider can
    # answer 200 with an HTML error page (JSONDecodeError, a ValueError), bytes
    # that are not UTF-8 at all (UnicodeDecodeError, also a ValueError — which is
    # why the undecoded body is handed to ``json.loads`` inside this guard rather
    # than decoded above it), a JSON array instead of an object (AttributeError
    # on ``.get``), or a vector carrying a null or a string (TypeError/ValueError
    # in ``float``). All of them degrade to [] like the transport failures above,
    # because every caller — the request path's semantic leg and the worker task
    # alike — is written against "no embeddings" and not against an exception.
    #
    # Vectors land at the position the PROVIDER named, not at the position they
    # happened to arrive in: an OpenAI-compatible ``data`` array carries an
    # ``index`` per item and does not promise order. Skipping an unusable entry
    # while appending to a list attributed every later vector to the wrong text —
    # document N's embedding written onto document N-1, silently.
    try:
        parsed = cast(dict[str, Any], json.loads(body))
        data = parsed.get("data")
        if not isinstance(data, list):
            return []
        by_index: dict[int, list[float]] = {}
        for position, item in enumerate(data):
            if not isinstance(item, dict):
                continue
            raw_embedding = item.get("embedding")
            if not isinstance(raw_embedding, list):
                continue
            index = item.get("index")
            # ``bool`` is an ``int`` subclass, so a JSON ``true`` would key slot
            # 1. A provider that omits ``index`` entirely falls back to array
            # position, which is safe now only because a skipped entry leaves its
            # OWN slot empty rather than pulling the rest of the array forward.
            if not isinstance(index, int) or isinstance(index, bool):
                index = position
            by_index[index] = [float(value) for value in raw_embedding]
    except AttributeError, TypeError, ValueError:
        logger.exception("Search embedding response could not be parsed")
        return []

    # Nothing usable anywhere is a REQUEST-level failure, not one per document:
    # the worker retries an empty list but marks a gapped one failed, and "the
    # gateway answered with no vectors" is not evidence about any one document.
    if not by_index:
        return []
    return [by_index.get(position, []) for position in range(len(texts))]
