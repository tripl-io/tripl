"""Symmetric encryption helpers for at-rest secrets.

Centralizes the ``encrypt_value`` / ``decrypt_value`` pair used by every
service that stores third-party credentials (data sources, alert destinations,
tracker tokens, settings overrides; :mod:`tripl.services.stored_secrets` lists
every stored one).

The work is done by the active :class:`SecretCipher`: the first installed
extension whose :meth:`~tripl.extensions.Extension.secret_cipher` returns one,
else :data:`DEFAULT_CIPHER`, Fernet keyed by ``ENCRYPTION_KEY``.

Default behavior:
- With a configured ``ENCRYPTION_KEY``: values round-trip through Fernet.
- With an empty key **and** ``DEBUG=true``: the value is stored as-is so
  local dev/test runs work without provisioning a key.
- With an empty key in production: ``Settings.assert_production_ready()``
  refuses startup, so this fall-through path can only execute in dev/test.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Protocol

from cryptography.fernet import Fernet, InvalidToken

from tripl.config import settings

if TYPE_CHECKING:
    from tripl.extensions import Extension


class SecretCipher(Protocol):
    """How stored secrets are encrypted: text in, text out.

    ``encrypt`` and ``decrypt`` are never called with an empty string (the
    helpers below answer ``""`` for ``""`` themselves). ``decrypt`` raises
    :class:`~cryptography.fernet.InvalidToken`, or a subclass, for a value it
    cannot decrypt with any key it accepts: callers treat that as "this stored
    secret is unreadable". Anything else it raises (a key service that cannot be
    reached, say) propagates as an error.
    """

    def encrypt(self, plaintext: str) -> str: ...

    def decrypt(self, token: str) -> str: ...

    def check(self) -> None:
        """Prove the cipher usable, or raise. Run once when the API and the worker start."""
        ...


@lru_cache(maxsize=1)
def _fernet() -> Fernet | None:
    if not settings.encryption_key:
        return None
    return Fernet(settings.encryption_key.encode())


class FernetCipher:
    """Community's cipher: Fernet under ``ENCRYPTION_KEY``, passthrough without one."""

    def encrypt(self, plaintext: str) -> str:
        f = _fernet()
        if f is None:
            return plaintext
        return f.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        f = _fernet()
        if f is None:
            return token
        return f.decrypt(token.encode()).decode()

    def check(self) -> None:
        """Nothing to reach: ``assert_production_ready`` already validates the key."""


#: The cipher in force when no extension supplies one.
DEFAULT_CIPHER = FernetCipher()

# The cipher chosen for one list of loaded extensions. Keyed on the list object
# itself, so ``override_extensions`` (which swaps the list) makes it choose again.
_chosen: tuple[list[Extension], SecretCipher] | None = None


def active_cipher() -> SecretCipher:
    """The first extension's cipher, else :data:`DEFAULT_CIPHER`. Chosen once."""
    global _chosen
    from tripl.extensions import extensions

    loaded = extensions()
    if _chosen is not None and _chosen[0] is loaded:
        return _chosen[1]
    cipher: SecretCipher = DEFAULT_CIPHER
    for extension in loaded:
        supplied = extension.secret_cipher()
        if supplied is not None:
            cipher = supplied
            break
    _chosen = (loaded, cipher)
    return cipher


def reset_cipher_cache() -> None:
    """Forget the chosen cipher and the Fernet key (tests, after changing settings)."""
    global _chosen
    _chosen = None
    _fernet.cache_clear()


def check_cipher() -> None:
    """Refuse to start when the active cipher cannot work (an unreachable key service).

    Raises ``RuntimeError`` naming the failure. The message carries the
    exception's own text, so a cipher must never put key material in one.
    """
    try:
        # Inside: building an extension's cipher can fail too (its configuration).
        active_cipher().check()
    except Exception as exc:
        raise RuntimeError(
            f"Secret encryption startup check failed: {type(exc).__name__}: {exc}"
        ) from exc


def encrypt_value(value: str) -> str:
    """Encrypt ``value``; empty in, empty out."""
    if not value:
        return ""
    return active_cipher().encrypt(value)


def decrypt_value(value: str) -> str:
    """Reverse of :func:`encrypt_value`; empty in, empty out.

    The default cipher returns the input unchanged when no key is configured.
    Raises ``InvalidToken`` if the ciphertext is corrupt or was encrypted with a
    key the active cipher does not accept — the caller decides how to surface
    that failure (typically as a connection error to the operator).
    """
    if not value:
        return ""
    return active_cipher().decrypt(value)


__all__ = [
    "DEFAULT_CIPHER",
    "FernetCipher",
    "InvalidToken",
    "SecretCipher",
    "active_cipher",
    "check_cipher",
    "decrypt_value",
    "encrypt_value",
    "reset_cipher_cache",
]
