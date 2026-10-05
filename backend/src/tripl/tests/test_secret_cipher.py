"""The cipher of every stored secret (``tripl.crypto``) and the hook that replaces it.

Without an extension the cipher is Community's: Fernet under ``ENCRYPTION_KEY``,
passthrough without a key. An extension's ``secret_cipher`` takes over
``encrypt_value`` / ``decrypt_value`` everywhere, and its ``check`` refuses
startup when it fails.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from cryptography.fernet import Fernet, InvalidToken

from tripl import crypto
from tripl.config import settings
from tripl.crypto import SecretCipher
from tripl.extensions import Extension, override_extensions

KEY = Fernet.generate_key()


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "encryption_key", KEY.decode())
    crypto.reset_cipher_cache()
    yield
    crypto.reset_cipher_cache()


class _Reversing:
    """A toy cipher: ``rev:`` and the text backwards."""

    def __init__(self, *, broken: bool = False) -> None:
        self.broken = broken

    def encrypt(self, plaintext: str) -> str:
        return "rev:" + plaintext[::-1]

    def decrypt(self, token: str) -> str:
        if not token.startswith("rev:"):
            raise InvalidToken
        return token[4:][::-1]

    def check(self) -> None:
        if self.broken:
            raise ConnectionError("key service unreachable")


class _Supplies(Extension):
    def __init__(self, cipher: SecretCipher | None, name: str = "supplies") -> None:
        self.cipher = cipher
        self.name = name
        self.asked = 0

    def secret_cipher(self) -> SecretCipher | None:
        self.asked += 1
        return self.cipher


# -- the default, unchanged -----------------------------------------------------


def test_default_is_fernet_under_the_encryption_key() -> None:
    with override_extensions([]):
        token = crypto.encrypt_value("hunter2")
        assert token != "hunter2"
        assert Fernet(KEY).decrypt(token.encode()) == b"hunter2"
        assert crypto.decrypt_value(token) == "hunter2"
        assert crypto.active_cipher() is crypto.DEFAULT_CIPHER


def test_default_passes_through_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "encryption_key", "")
    crypto.reset_cipher_cache()
    with override_extensions([]):
        assert crypto.encrypt_value("plain") == "plain"
        assert crypto.decrypt_value("plain") == "plain"


def test_empty_in_empty_out() -> None:
    with override_extensions([_Supplies(_Reversing())]):
        assert crypto.encrypt_value("") == ""
        assert crypto.decrypt_value("") == ""


def test_default_refuses_a_foreign_token() -> None:
    foreign = Fernet(Fernet.generate_key()).encrypt(b"x").decode()
    with override_extensions([]), pytest.raises(InvalidToken):
        crypto.decrypt_value(foreign)


def test_the_base_extension_supplies_no_cipher() -> None:
    assert Extension().secret_cipher() is None


def test_an_extension_without_a_cipher_keeps_the_default() -> None:
    with override_extensions([_Supplies(None)]):
        assert crypto.active_cipher() is crypto.DEFAULT_CIPHER
        assert Fernet(KEY).decrypt(crypto.encrypt_value("v").encode()) == b"v"


# -- the hook -------------------------------------------------------------------


def test_an_extension_cipher_takes_over() -> None:
    with override_extensions([_Supplies(_Reversing())]):
        token = crypto.encrypt_value("abc")
        assert token == "rev:cba"
        assert crypto.decrypt_value(token) == "abc"
        with pytest.raises(InvalidToken):
            crypto.decrypt_value("not-ours")


def test_the_first_extension_with_a_cipher_wins() -> None:
    first, second = _Reversing(), _Reversing()
    with override_extensions([_Supplies(None, "none"), _Supplies(first), _Supplies(second, "b")]):
        assert crypto.active_cipher() is first


def test_the_cipher_is_chosen_once_per_set_of_extensions() -> None:
    supplier = _Supplies(_Reversing())
    with override_extensions([supplier]):
        crypto.encrypt_value("a")
        crypto.decrypt_value("rev:a")
        crypto.active_cipher()
        assert supplier.asked == 1
    # Another set of extensions chooses again, and the default comes back.
    with override_extensions([]):
        assert crypto.active_cipher() is crypto.DEFAULT_CIPHER


def test_reset_makes_it_choose_again() -> None:
    supplier = _Supplies(_Reversing())
    with override_extensions([supplier]):
        crypto.active_cipher()
        crypto.reset_cipher_cache()
        crypto.active_cipher()
    assert supplier.asked == 2


# -- the startup check ----------------------------------------------------------


def test_check_passes_for_the_default() -> None:
    with override_extensions([]):
        crypto.check_cipher()


def test_check_refuses_a_failing_cipher() -> None:
    with (
        override_extensions([_Supplies(_Reversing(broken=True))]),
        pytest.raises(RuntimeError, match="startup check failed.*key service unreachable"),
    ):
        crypto.check_cipher()


def test_check_refuses_a_cipher_that_cannot_be_built() -> None:
    class _Misconfigured(Extension):
        def secret_cipher(self) -> SecretCipher | None:
            raise ValueError("KMS_KEY_ID is not set")

    with (
        override_extensions([_Misconfigured()]),
        pytest.raises(RuntimeError, match="KMS_KEY_ID is not set"),
    ):
        crypto.check_cipher()


def test_the_worker_refuses_to_start_on_a_failing_cipher() -> None:
    """Celery swallows an ``Exception`` from a signal handler, so it is a SystemExit."""
    from tripl.worker.celery_app import _check_secret_cipher

    with override_extensions([_Supplies(_Reversing(broken=True))]), pytest.raises(SystemExit):
        _check_secret_cipher()
    with override_extensions([]):
        _check_secret_cipher()


async def test_the_api_refuses_to_start_on_a_failing_cipher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tripl.main import app, lifespan

    monkeypatch.setattr(settings, "debug", True)
    with (
        override_extensions([_Supplies(_Reversing(broken=True))]),
        pytest.raises(RuntimeError, match="startup check failed"),
    ):
        async with lifespan(app):
            pass
    with override_extensions([]):
        async with lifespan(app):
            pass
