"""tripl: the analytics tracking plan service (the ``tripl-server`` distribution)."""

from importlib.metadata import PackageNotFoundError, version

# The DISTRIBUTION name, not this import package's: ``tripl`` on PyPI is the
# operator CLI in cli/ (see backend/pyproject.toml).
DISTRIBUTION_NAME = "tripl-server"

#: The one version this server reports: in its OpenAPI document, in the usage
#: ping and on Settings -> Platform -> System. Read from the installed package
#: metadata: backend/pyproject.toml's ``version``.
try:
    __version__ = version(DISTRIBUTION_NAME)
except PackageNotFoundError:  # pragma: no cover - a source tree with no install
    # Honest rather than plausible: a hardcoded number here would drift from
    # pyproject's ``version`` and ship silently, as "0.1.0" in the API
    # document did.
    __version__ = "0.0.0+unknown"
