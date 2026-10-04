"""The generated credentials, and the ONE definition of how each is made.

Before this there were three published recipes for ``SECRET_KEY`` alone
(``openssl rand -hex 48`` in deployment.md, ``openssl rand -base64 48 | tr '+/'
'-_'`` in .env.example, ``secrets.token_urlsafe(32)`` in the backend's own error
message) and none of them was executable, so none of them could be tested. This
module is executable, so it can be — see ``tests/test_install_files.py``, which
asserts the contracts the BACKEND actually enforces rather than the shape this
file happens to produce.

Stdlib only: ``secrets`` and ``base64``. Not ``cryptography`` (a compiled
dependency this CLI must not acquire — tripl-mcp inherits everything here) and
not an ``openssl`` subprocess (not installed everywhere, and a password on an
argv is visible to ``ps(1)``).

NOTHING HERE IS EVER PRINTED. The commands report the KEY NAMES they generated;
the values exist only inside the 0600 file they are written to.
"""

from __future__ import annotations

import base64
import secrets as _secrets
from typing import Protocol

# The four values `tripl install` generates. Order is the order they appear in
# the generated .env, and `tests/test_contract.py` holds the docs to these names.
REQUIRED_SECRETS = ("ENCRYPTION_KEY", "SECRET_KEY", "POSTGRES_PASSWORD", "RABBITMQ_PASSWORD")

# The three the operator supplies or the CLI defaults. Not secrets, but just as
# required: compose.yaml interpolates APP_BASE_URL with `${APP_BASE_URL:?...}`,
# so a .env missing it fails the stack before a container starts.
REQUIRED_SETTINGS = ("APP_BASE_URL", "TRIPL_IMAGE", "TRIPL_VERSION")

# Fernet keys are exactly 32 raw bytes, url-safe base64 encoded (44 characters
# with the trailing '='). backend/src/tripl/config.py builds `Fernet(key)` at
# startup and Fernet.__init__ urlsafe_b64decodes it and rejects any other length.
FERNET_KEY_BYTES = 32
# 384 bits. The backend only requires SECRET_KEY to be non-empty; this is well
# past any brute-force concern and url-safe, so it never needs quoting in .env.
SECRET_KEY_BYTES = 48
# 24 bytes -> 48 hex characters.
PASSWORD_BYTES = 24


class Rng(Protocol):
    """The entropy source, injectable so the golden .env test is deterministic.

    Structurally satisfied by the stdlib ``secrets`` module itself, which is the
    default and the only thing that is ever used in production.
    """

    def token_bytes(self, nbytes: int) -> bytes: ...

    def token_hex(self, nbytes: int) -> str: ...

    def token_urlsafe(self, nbytes: int) -> str: ...


def default_rng() -> Rng:
    """The stdlib CSPRNG, resolved at call time so a test can replace it.

    The same seam ``commands.watch`` uses for its clock. Its only purpose is
    ``tests/test_install_cmd.py``, which runs a whole install with known values
    and then asserts that not one of them appears in stdout, in stderr or in the
    --json document.
    """
    return _secrets


def generate(rng: Rng = _secrets) -> dict[str, str]:
    """One fresh value per name in ``REQUIRED_SECRETS``.

    The two passwords are HEX and that is load-bearing, not stylistic:
    compose.yaml builds
    ``postgresql+asyncpg://tripl:${POSTGRES_PASSWORD}@postgres:5432/tripl`` and
    ``amqp://tripl:${RABBITMQ_PASSWORD}@rabbitmq:5672//`` by plain string
    interpolation, so a base64 password containing ``/``, ``+``, ``@`` or ``:``
    silently corrupts the URL into something that fails to connect with an error
    naming neither the password nor the encoding. Hex has no such character.

    Neither password can come out as ``tripl`` or ``guest``: the backend's
    ``assert_production_ready`` refuses those dev credentials outright, and 48
    hex characters cannot spell either.
    """
    return {
        "ENCRYPTION_KEY": base64.urlsafe_b64encode(rng.token_bytes(FERNET_KEY_BYTES)).decode(
            "ascii"
        ),
        "SECRET_KEY": rng.token_urlsafe(SECRET_KEY_BYTES),
        "POSTGRES_PASSWORD": rng.token_hex(PASSWORD_BYTES),
        "RABBITMQ_PASSWORD": rng.token_hex(PASSWORD_BYTES),
    }
