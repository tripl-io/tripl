"""Connection settings for Trino (and Starburst) and Amazon Athena.

They share one SQL layer and one naming rule for catalogs, schemas and
workgroups, so they share a module. Re-exported by ``tripl.schemas.data_source``.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, field_validator

from tripl.schemas.connection_settings_base import (
    MAX_SCHEMA_ALLOWLIST,
    _ConnectionSettingsBase,
    object_name,
    object_name_list,
)

# Trino: the coordinator's scheme. HTTPS is the default and the only scheme a
# password is ever sent over; plain HTTP is for an unauthenticated local or
# in-cluster coordinator.
TrinoHttpScheme = Literal["https", "http"]

# A Trino/Athena catalog, schema (Glue database) or workgroup name. Letters,
# digits, ``_`` and ``-`` (Glue database names and Athena workgroups carry
# hyphens); rendered quoted or as data, never spliced into SQL bare.
_TRINO_OBJECT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,127}$")
# An S3 location for Athena query results: ``s3://bucket/prefix/``.
_S3_URI_RE = re.compile(r"^s3://[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9](/[^\s'\"\\]{0,900})?$")


def _trino_object(value: str | None, *, label: str) -> str | None:
    return object_name(value, pattern=_TRINO_OBJECT_RE, label=label, kind="name")


def _trino_schemas(value: list[str] | None) -> list[str] | None:
    return object_name_list(
        value,
        pattern=_TRINO_OBJECT_RE,
        label="schema_allowlist",
        kind="name",
        limit=MAX_SCHEMA_ALLOWLIST,
    )


class TrinoSettings(_ConnectionSettingsBase):
    """Trino / Starburst coordinator: its scheme, and what to browse.

    ``host`` and ``port`` reach the coordinator, ``database_name`` is the
    catalog and ``username`` the user; these are the settings that have no
    column of their own.
    """

    # Unset reads as "https".
    http_scheme: TrinoHttpScheme | None = None
    # The default schema unqualified names resolve in. Unset = none (every name
    # is schema-qualified, and the browser lists every schema of the catalog).
    schema_name: str | None = Field(default=None, max_length=128)
    # Further schemas of the same catalog the schema browser lists.
    schema_allowlist: list[str] | None = None

    @field_validator("schema_name")
    @classmethod
    def _check_schema_name(cls, value: str | None) -> str | None:
        return _trino_object(value, label="schema_name")

    @field_validator("schema_allowlist")
    @classmethod
    def _check_schemas(cls, value: list[str] | None) -> list[str] | None:
        return _trino_schemas(value)


class AthenaSettings(_ConnectionSettingsBase):
    """Amazon Athena: the workgroup statements run in, where results go, what to browse.

    ``host`` is the AWS region, ``database_name`` the default Glue database,
    ``username`` the access key ID and the secret its secret access key; these
    are the settings that have no column of their own.
    """

    # Unset runs in the account's ``primary`` workgroup.
    work_group: str | None = Field(default=None, max_length=128)
    # Where Athena writes query results. Unset relies on the workgroup's own
    # result location.
    s3_output_location: str | None = Field(default=None, max_length=1024)
    # The data catalog. Unset = AwsDataCatalog (the Glue catalog).
    catalog_name: str | None = Field(default=None, max_length=128)
    # Further Glue databases the schema browser lists.
    schema_allowlist: list[str] | None = None

    @field_validator("work_group")
    @classmethod
    def _check_work_group(cls, value: str | None) -> str | None:
        return _trino_object(value, label="work_group")

    @field_validator("catalog_name")
    @classmethod
    def _check_catalog(cls, value: str | None) -> str | None:
        return _trino_object(value, label="catalog_name")

    @field_validator("s3_output_location")
    @classmethod
    def _check_output(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            return None
        if not _S3_URI_RE.match(trimmed):
            raise ValueError(
                "s3_output_location must be an S3 location, e.g. s3://my-bucket/athena-results/"
            )
        return trimmed

    @field_validator("schema_allowlist")
    @classmethod
    def _check_schemas(cls, value: list[str] | None) -> list[str] | None:
        return _trino_schemas(value)
