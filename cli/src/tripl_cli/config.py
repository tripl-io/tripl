"""Where the instance URL and API key come from, and how they are validated.

Precedence is PER FIELD, highest first:

1. command-line flag  (``--url`` / ``--base-url``, ``--api-key``)
2. environment        (``TRIPL_BASE_URL``, ``TRIPL_API_KEY``)
3. config file        (``base_url``, ``api_key``)

Per field, not per source: ``--url https://staging`` with the key still coming
from the config file has to work. "If any flag is given, ignore the file" is a
plausible-looking bug, so it has its own test.

Nothing here touches the network, and absent values are never an error at
resolution time — only a malformed file, a bad value, or an explicit ``--config``
that does not exist. That is what lets ``main`` resolve unconditionally, before
it knows whether the subcommand needs credentials at all.
"""

from __future__ import annotations

import os
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from tripl_cli.client import API_PREFIX
from tripl_cli.errors import TriplConfigError

# The same two names tripl-mcp reads (server.py, its README, both compose
# files). One name per value: TRIPL_URL is NOT accepted as an alias, because two
# supported spellings for one setting is this repo's recurring drift defect in
# miniature. It is only *detected*, to sharpen the error below.
ENV_BASE_URL = "TRIPL_BASE_URL"
ENV_API_KEY = "TRIPL_API_KEY"
ENV_WRONG_BASE_URL = "TRIPL_URL"

CONFIG_DIR_NAME = "tripl"
CONFIG_FILENAME = "config.toml"

FLAG_BASE_URL = "--url"
FLAG_API_KEY = "--api-key"


@dataclass(frozen=True)
class Config:
    """Resolved connection settings plus where each value came from.

    ``sources`` is provenance, and it is cheap now and expensive to retrofit:
    ``tripl doctor`` exists to answer "why is it talking to *that* instance",
    which was a week of the 2026-07-28..31 incident. It also makes
    today's failure messages specific instead of generic.
    """

    base_url: str | None
    api_key: str | None
    # The file that was CONSULTED — an explicit --config, or the default
    # location — whether or not it exists. Always set, and deliberately not
    # `Path | None`: the "nothing is configured" error has to name the file to
    # create, which is exactly the case where no file was there to read.
    # ``path_exists`` carries the "was it actually there" bit separately.
    path: Path
    path_exists: bool
    sources: Mapping[str, str]


def default_config_path() -> Path:
    """The config file location for this platform.

    ``~/.config`` on macOS rather than ``~/Library/Application Support``: Apple's
    location is right for app-managed state and wrong for a file an operator
    opens in ``$EDITOR``. git, gh, aws, kubectl and docker all put hand-edited
    config under ``~/.config`` on macOS, so that is where a user will look, and
    it lets one sentence of docs cover both platforms.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    # Honoured on every platform, but only when absolute — the XDG spec says a
    # relative value must be ignored.
    if xdg and Path(xdg).is_absolute():
        return Path(xdg) / CONFIG_DIR_NAME / CONFIG_FILENAME
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "").strip()
        if appdata:
            return Path(appdata) / CONFIG_DIR_NAME / CONFIG_FILENAME
    return Path.home() / ".config" / CONFIG_DIR_NAME / CONFIG_FILENAME


def normalize_base_url(raw: str, origin: str) -> str:
    """Canonicalise an instance URL, or explain precisely why it is not one.

    Applied to all three sources so a flag and a file value cannot disagree
    about normalisation. ``origin`` names where the value came from, so the
    message points at the thing to edit.
    """
    value = raw.strip().rstrip("/")
    # BEFORE urlsplit, which SILENTLY DELETES tab, CR and LF from what it parses
    # (WHATWG-alignment, CVE-2022-0391) and so reports a value with an embedded
    # newline as a perfectly good URL. This value becomes the `APP_BASE_URL=` line
    # of the 0600 .env that Docker Compose interpolates, where a newline is not a
    # cosmetic problem: it ends our line and starts an attacker's, turning one
    # pasted string into arbitrary extra settings for the whole stack. Nothing
    # legitimate needs a control character in an origin, so the whole class goes.
    control = next((ch for ch in value if ch < " " or ch == "\x7f"), None)
    if control is not None:
        raise TriplConfigError(
            f"{origin} contains a control character ({control!r}) and is not a valid tripl "
            f"URL: {raw!r}. An origin is one line - no newlines, tabs or control bytes."
        )
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        # Suggest a corrected value only for the common case — a bare host with
        # no scheme, `tripl.example.com` or `localhost:8000`. The test is "://"
        # rather than `parsed.scheme`, because urlsplit reads the "localhost" in
        # `localhost:8000` AS a scheme; and prefixing one onto something that
        # genuinely has a scheme gives "https://ftp://x", worse than silence.
        fix = f" — try https://{value.lstrip('/')}" if "://" not in value else ""
        raise TriplConfigError(
            f"{origin} is not a valid tripl URL: {raw!r}. It needs an http:// or "
            f"https:// scheme{fix}"
        )
    if value.endswith(API_PREFIX):
        # The client appends API_PREFIX itself. A pasted-from-the-browser
        # ".../api/v1" would otherwise produce /api/v1/api/v1 and a 404 that
        # reads like a broken instance rather than a typo.
        value = value[: -len(API_PREFIX)].rstrip("/")
    return value


def _warn_if_world_readable(path: Path, data: Mapping[str, Any]) -> None:
    """Warn, but do not refuse, when a key sits in a file others can read.

    A warning rather than a hard failure: an operator on a single-user box
    should not be blocked by it. A future ``tripl login`` creates the file 0600.
    Skipped on Windows, where ``st_mode`` carries no such meaning.
    """
    if sys.platform == "win32" or not data.get("api_key"):
        return
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:  # pragma: no cover - the file was just read successfully
        return
    if mode & 0o077:
        print(
            f"tripl: warning: {path} is readable by other users (mode {mode:03o}) "
            f"and holds an API key; chmod 600 {path}",
            file=sys.stderr,
        )


def _load_file(path: Path, *, explicit: bool) -> dict[str, Any] | None:
    """Parse the config file, or return None when there simply is not one.

    A missing DEFAULT file is not an error — the CLI is fully usable from flags
    and environment alone. A missing ``--config`` path is: an explicit request
    that cannot be honoured must never be silently ignored.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if explicit:
            raise TriplConfigError(f"config file not found: {path}") from None
        return None
    except IsADirectoryError:
        raise TriplConfigError(f"config path is a directory, not a file: {path}") from None
    except OSError as exc:
        raise TriplConfigError(f"could not read config file {path}: {exc}") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        # tomllib's message already carries the line/column; what it lacks is
        # which file it was reading.
        raise TriplConfigError(f"{path} is not valid TOML: {exc}") from exc
    _warn_if_world_readable(path, data)
    # Unknown keys and unknown tables are IGNORED, not rejected: an older CLI
    # must keep working against a file written by a newer one, and a future
    # [profiles.staging] table has to fit without a format change. The cost — a
    # typo doing nothing — is paid off by `tripl doctor`, which reports unknown
    # keys and the full provenance table.
    return data


# Every key the config file may define. Used to type-check the whole file up
# front rather than only the fields that happen to win the precedence contest.
_FILE_KEYS = ("base_url", "api_key")


def _string_value(data: Mapping[str, Any], key: str, path: Path) -> str | None:
    """Read one top-level string from the config file, refusing coercion."""
    if key not in data:
        return None
    value = data[key]
    if not isinstance(value, str):
        # Never str()-coerce: `base_url = 8000` is a mistake, and turning it into
        # "8000" would surface as a confusing URL error three layers later.
        raise TriplConfigError(
            f"{path}: '{key}' must be a string, got {type(value).__name__} ({value!r})"
        )
    return value.strip() or None


def _pick(
    key: str,
    *,
    flag: str | None,
    flag_label: str,
    env_name: str,
    file_data: Mapping[str, Any],
    path: Path,
    sources: dict[str, str],
) -> str | None:
    """Resolve one field through the three layers, recording its provenance.

    Empty strings count as ABSENT at every layer, mirroring tripl-mcp's
    ``os.environ.get(...).strip()``. This matters concretely: docker compose
    materialises undefined variables as empty strings, so ``TRIPL_BASE_URL=""``
    must fall through to the config file rather than shadow it with nothing.
    """
    flag_value = (flag or "").strip()
    if flag_value:
        sources[key] = flag_label
        return flag_value
    env_value = os.environ.get(env_name, "").strip()
    if env_value:
        sources[key] = f"${env_name}"
        return env_value
    file_value = _string_value(file_data, key, path)
    if file_value:
        sources[key] = str(path)
        return file_value
    return None


def resolve(
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    config_path: Path | None = None,
) -> Config:
    """Merge flags, environment and config file into one immutable Config."""
    path = config_path if config_path is not None else default_config_path()
    file_data = _load_file(path, explicit=config_path is not None)
    sources: dict[str, str] = {}
    data: Mapping[str, Any] = file_data if file_data is not None else {}

    # Validate the file's types eagerly, before the layered pick below decides
    # which value wins. _pick short-circuits — it only reaches _string_value when
    # both the flag and the env var are empty — so a file holding
    # `base_url = 8000` stayed silent for as long as TRIPL_BASE_URL happened to
    # be exported, then failed later from a file the user had not touched. A
    # malformed config is malformed whether or not something overrides it.
    for key in _FILE_KEYS:
        _string_value(data, key, path)

    raw_url = _pick(
        "base_url",
        flag=base_url,
        flag_label=FLAG_BASE_URL,
        env_name=ENV_BASE_URL,
        file_data=data,
        path=path,
        sources=sources,
    )
    resolved_url = normalize_base_url(raw_url, sources["base_url"]) if raw_url is not None else None
    resolved_key = _pick(
        "api_key",
        flag=api_key,
        flag_label=FLAG_API_KEY,
        env_name=ENV_API_KEY,
        file_data=data,
        path=path,
        sources=sources,
    )
    return Config(
        base_url=resolved_url,
        api_key=resolved_key,
        path=path,
        path_exists=file_data is not None,
        sources=sources,
    )


def _wrong_env_var_hint() -> str:
    """Name the near-miss env var when it, and only it, is set."""
    if not os.environ.get(ENV_WRONG_BASE_URL, "").strip():
        return ""
    return (
        f" {ENV_WRONG_BASE_URL} is set but is not the variable this tool reads — "
        f"the name is {ENV_BASE_URL} (tripl-mcp uses the same one)."
    )


def require_base_url(config: Config) -> str:
    """The instance URL, or an error naming every way to supply one."""
    if config.base_url:
        return config.base_url
    raise TriplConfigError(
        f"no tripl instance URL configured.{_wrong_env_var_hint()} Pass "
        f"{FLAG_BASE_URL}, set {ENV_BASE_URL}, or add base_url to {config.path}"
    )


def require_api_key(config: Config) -> str:
    """The API key, or an error naming every way to supply one."""
    if config.api_key:
        return config.api_key
    raise TriplConfigError(
        f"no tripl API key configured. Pass {FLAG_API_KEY}, set {ENV_API_KEY}, or "
        f"add api_key to {config.path}. Create keys in the tripl app under "
        f"Settings -> API keys (tk_r_ for read-only, tk_w_ for write)."
    )
