"""Small Snowflake building blocks shared by the adapter, the registry and the tests.

Kept apart from ``snowflake.py`` so that nothing here imports the driver:
``snowflake-connector-python`` is imported lazily (:func:`import_driver`), the
way the registry imports every adapter, so a process that never builds a
Snowflake adapter never loads it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from types import ModuleType
from typing import Any

from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.core.bucketing import to_utc

#: The domains Snowflake serves accounts from. ``host`` may name the account
#: identifier alone (``myorg-myaccount``, ``xy12345.eu-central-1.aws``) or the
#: full hostname under one of these.
SNOWFLAKE_HOST_SUFFIXES = (".snowflakecomputing.com", ".snowflakecomputing.cn")

#: An account identifier: an org-account name or a locator with its region and
#: cloud. Underscores are allowed (account names may hold them); the hostname
#: spells them as hyphens.
_ACCOUNT_RE = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,253}[a-z0-9])?(?:\.[a-z0-9][a-z0-9-]{0,62})*")

#: An unquoted Snowflake identifier. Such a name resolves case-insensitively, so
#: it is spelled bare; anything else is double-quoted and matches exactly.
_UNQUOTED_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

# Cap on how much generated SQL reaches a log line; statements go to DEBUG.
_SQL_LOG_MAX_CHARS = 300


def truncate_sql(sql: str) -> str:
    return sql[:_SQL_LOG_MAX_CHARS] + ("..." if len(sql) > _SQL_LOG_MAX_CHARS else "")


def quote_ident(name: str) -> str:
    """A double-quoted Snowflake identifier; a double quote inside is doubled.

    Quoted identifiers match exactly, case included, which is what the column
    names ``get_columns`` reads back need: an unquoted ``event_name`` in a base
    query comes back as ``EVENT_NAME``, and ``"EVENT_NAME"`` is that column.
    """
    return '"' + name.replace('"', '""') + '"'


def object_name(name: str) -> str:
    """A database or schema name as a user typed it into the connection form.

    A plain identifier stays bare, so ``analytics`` finds ``ANALYTICS`` the way
    the Snowflake UI and the driver's own ``database=`` resolve it; any other
    name is quoted and must match exactly.
    """
    return name if _UNQUOTED_IDENTIFIER_RE.match(name) else quote_ident(name)


class Params:
    """The positional parameters of one statement, bound server-side.

    The connection uses the driver's ``numeric`` paramstyle: each value is
    referenced as ``:N`` and sent separately, so no value from data or from an
    analyst is ever part of the SQL text, and a ``%`` in a base query needs no
    escaping. Numbered rather than ``?`` markers because the generated SQL does
    not reference values in the order they are bound.
    """

    def __init__(self) -> None:
        self.values: list[str] = []

    def bind(self, value: str) -> str:
        self.values.append(str(value))
        return f":{len(self.values)}"


@dataclass(frozen=True)
class SnowflakeHost:
    """Where an account is reached: the identifier the driver logs in with, and its host."""

    account: str
    hostname: str


def resolve_host(host: str) -> SnowflakeHost:
    """The account identifier and hostname for what the ``host`` field holds.

    Refuses anything that is not an account identifier or a Snowflake hostname:
    the name is handed to the driver as a hostname, and the domain test in
    :func:`check_account_host` must see the name the connection will use.
    """
    name = host.strip().lower().rstrip(".")
    for prefix in ("https://", "http://"):
        if name.startswith(prefix):
            name = name[len(prefix) :].rstrip("/")
    suffix = next((s for s in SNOWFLAKE_HOST_SUFFIXES if name.endswith(s)), None)
    account = name[: -len(suffix)] if suffix else name
    if not account or not _ACCOUNT_RE.fullmatch(account):
        raise WarehouseCapabilityError(
            "Snowflake: the host must be the account identifier (for example "
            "myorg-myaccount or xy12345.eu-central-1.aws) or its hostname "
            "(myorg-myaccount.snowflakecomputing.com), without a port or path."
        )
    hostname = account.replace("_", "-") + (suffix or SNOWFLAKE_HOST_SUFFIXES[0])
    return SnowflakeHost(account=account, hostname=hostname)


def load_private_key(pem: str, passphrase: str | None = None) -> Any:
    """The RSA private key of a key-pair user, from the PEM stored as the secret."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    text = _pem_lines(pem.strip())
    if "PRIVATE KEY" not in text:
        raise WarehouseCapabilityError(
            "Snowflake: key-pair sign-in needs the user's private key in PEM form "
            "(-----BEGIN PRIVATE KEY----- ...) in the secret field"
        )
    try:
        return load_pem_private_key(
            text.encode(), password=passphrase.encode() if passphrase else None
        )
    except (TypeError, ValueError) as exc:
        raise WarehouseCapabilityError(
            "Snowflake: the private key could not be read. Paste an unencrypted PKCS#8 "
            "PEM key (openssl pkcs8 -topk8 -nocrypt ...)."
        ) from exc


_PEM_RE = re.compile(r"(-----BEGIN [A-Z ]+-----)\s*(.*?)\s*(-----END [A-Z ]+-----)", re.S)


def _pem_lines(text: str) -> str:
    """A PEM block with its line breaks restored.

    A key pasted into a one-line secret field arrives with the breaks turned
    into spaces (or gone); the base64 body is re-wrapped at 64 characters so
    the parser accepts it again.
    """
    match = _PEM_RE.search(text)
    if match is None:
        return text
    begin, body, end = match.groups()
    compact = "".join(body.split())
    wrapped = "\n".join(compact[i : i + 64] for i in range(0, len(compact), 64))
    return f"{begin}\n{wrapped}\n{end}\n"


def import_driver() -> ModuleType:
    """``snowflake.connector``, imported on first use."""
    import snowflake.connector

    return snowflake.connector


def field_type_name(type_code: int) -> str:
    """The driver's name for a result column's type code (``FIXED``, ``VARIANT`` ...)."""
    from snowflake.connector.constants import FIELD_ID_TO_NAME

    return FIELD_ID_TO_NAME[int(type_code)] or "UNKNOWN"


# --------------------------------------------------------------------------- #
# result cells
# --------------------------------------------------------------------------- #


def as_utc_bucket(value: object) -> object:
    """One ``_bucket`` cell as an aware UTC ``datetime``.

    Every bucket expression returns a TIMESTAMP_NTZ holding the UTC wall clock,
    which the driver hands back naive; it is stamped as UTC. ``datetime`` is
    tested before ``date`` because it is a subclass of it.
    """
    if isinstance(value, datetime):
        return to_utc(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return value


def count_cell(value: object) -> int:
    """A COUNT cell as an int; an aggregate over no rows is 0, never NULL."""
    if value is None:
        return 0
    if isinstance(value, int | float | str | Decimal):
        return int(value)
    msg = f"Snowflake: expected a count, got {type(value).__name__}"
    raise ValueError(msg)


def decode_json_list(value: object) -> object:
    """A grouped key list (``TO_JSON(ARRAY_SORT(...))``) back as the list it stands for."""
    if value is None or isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if not isinstance(value, str):
        msg = f"Snowflake: expected a JSON array string, got {type(value).__name__}"
        raise ValueError(msg)
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        msg = f"Snowflake: could not decode a grouped key list {value!r}: {exc}"
        raise ValueError(msg) from exc
    if decoded is not None and not isinstance(decoded, list):
        msg = f"Snowflake: a grouped key list decoded to {type(decoded).__name__}, not a list"
        raise ValueError(msg)
    return decoded


def decode_array_value(value: object) -> object:
    """An ARRAY regular column as a list; the driver returns ARRAY values as JSON text."""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value
        return decoded if isinstance(decoded, list) else value
    if isinstance(value, tuple):
        return list(value)
    return value


# --------------------------------------------------------------------------- #
# column types
# --------------------------------------------------------------------------- #

#: The driver's result type names, spelled the way Snowflake's SQL does. TEXT is
#: reported as STRING (a Snowflake synonym of VARCHAR) so the shared string-type
#: test, which ``varchar`` cannot join without catching PostgreSQL columns,
#: knows the column can be parsed as JSON.
_TYPE_NAMES = {"TEXT": "STRING", "REAL": "FLOAT", "TIMESTAMP": "TIMESTAMP_NTZ"}


def type_name_of(driver_name: str, precision: int | None, scale: int | None) -> str:
    """A column's type from the driver's metadata (``FIXED`` -> ``NUMBER(p,s)``)."""
    name = driver_name.upper()
    if name == "FIXED":
        if precision is None:
            return "NUMBER"
        return f"NUMBER({precision},{scale or 0})"
    return _TYPE_NAMES.get(name, name)


def is_ntz_type(type_name: str) -> bool:
    """A zone-less wall clock (``TIMESTAMP_NTZ``; ``DATETIME`` is its synonym)."""
    name = type_name.strip().upper()
    return name.startswith(("TIMESTAMP_NTZ", "DATETIME")) or name == "TIMESTAMP"


def is_zoned_type(type_name: str) -> bool:
    """An instant with a zone: ``TIMESTAMP_LTZ`` or ``TIMESTAMP_TZ``."""
    return type_name.strip().upper().startswith(("TIMESTAMP_LTZ", "TIMESTAMP_TZ"))


def is_array_type(type_name: str) -> bool:
    return type_name.strip().upper().startswith("ARRAY")
