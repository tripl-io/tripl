"""PostgreSQL connection hardening: TLS, certificates, search_path.

None of this needs a server — every check here happens *before* ``psycopg.connect``
is reached, which is the point: a connection whose TLS is misconfigured must be
refused rather than opened in plaintext and discovered later.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from tripl.core.adapters.postgres import (
    _DEFAULT_LOCAL_SSLMODE,
    _DEFAULT_REMOTE_SSLMODE,
    PostgresAdapter,
    _materialize_tls_files,
    _resolve_sslmode,
    _tls_pem_material,
    _validated_search_path,
)

# Not a real key. It only has to carry a PEM header, which is all the adapter checks.
CA_PEM = "-----BEGIN CERTIFICATE-----\nZmFrZS1jYQ==\n-----END CERTIFICATE-----"
CERT_PEM = "-----BEGIN CERTIFICATE-----\nZmFrZS1jZXJ0\n-----END CERTIFICATE-----"
KEY_PEM = "-----BEGIN PRIVATE KEY-----\nZmFrZS1rZXk=\n-----END PRIVATE KEY-----"


# --- the constructor's parameter contract ------------------------------------
#
# These reach the adapter itself. They never open a socket: every one of them is
# refused before psycopg.connect is called, which is exactly the guarantee under
# test — a misconfigured connection must not be attempted, let alone opened in
# plaintext.


def test_an_unknown_connection_parameter_is_rejected_not_ignored() -> None:
    # tripl-64n8.7, verbatim: "Unknown or inapplicable connection parameters are
    # rejected, not ignored." A **kwargs that swallows `sslmod=verify-full` gives
    # you a plaintext connection and a configuration screen that says otherwise.
    with pytest.raises(ValueError, match="Unsupported PostgreSQL connection parameter"):
        PostgresAdapter(
            host="warehouse.example.com",
            port=5432,
            database="db",
            sslmod="verify-full",
        )


def test_the_constructor_refuses_a_bad_sslmode_before_connecting() -> None:
    with pytest.raises(ValueError, match="Unsupported sslmode"):
        PostgresAdapter(
            host="warehouse.example.com",
            port=5432,
            database="db",
            sslmode="verify_full",
        )


def test_the_constructor_refuses_an_injecting_search_path_before_connecting() -> None:
    with pytest.raises(ValueError, match="search_path"):
        PostgresAdapter(
            host="warehouse.example.com",
            port=5432,
            database="db",
            search_path="public; DROP TABLE users",
        )


# --- sslmode -----------------------------------------------------------------


def test_remote_hosts_default_to_require_not_prefer() -> None:
    # `prefer` silently accepts plaintext when the server offers no TLS, so it is
    # not a guarantee of anything. A remote connection defaults to a mode that fails
    # loudly instead.
    assert _resolve_sslmode("warehouse.example.com", None) == _DEFAULT_REMOTE_SSLMODE
    assert _DEFAULT_REMOTE_SSLMODE == "require"


def test_local_hosts_keep_the_permissive_default() -> None:
    # Dev/docker Postgres usually has no certificate, and the traffic never leaves
    # the machine.
    assert _resolve_sslmode("localhost", None) == _DEFAULT_LOCAL_SSLMODE
    assert _resolve_sslmode("127.0.0.1", None) == _DEFAULT_LOCAL_SSLMODE


@pytest.mark.parametrize(
    "mode",
    ["disable", "allow", "prefer", "require", "verify-ca", "verify-full"],
)
def test_every_allowlisted_mode_is_accepted(mode: str) -> None:
    assert _resolve_sslmode("warehouse.example.com", mode) == mode


@pytest.mark.parametrize("mode", ["verify_full", "VERIFY-FULL", "on", "true", "", "require "])
def test_an_unknown_sslmode_is_rejected_loudly(mode: str) -> None:
    # Passing an unknown mode through to libpq would make it fall back to its own
    # default; dropping it would silently downgrade the connection. Neither is
    # acceptable — the operator asked for something we do not understand.
    with pytest.raises(ValueError, match="Unsupported sslmode"):
        _resolve_sslmode("warehouse.example.com", mode)


# --- certificate material ----------------------------------------------------


def test_certificates_are_rejected_when_tls_is_disabled() -> None:
    with pytest.raises(ValueError, match="never negotiates TLS"):
        _tls_pem_material(
            sslmode="disable",
            sslrootcert=CA_PEM,
            sslcert=None,
            sslkey=None,
        )


@pytest.mark.parametrize("mode", ["verify-ca", "verify-full"])
def test_a_verifying_mode_without_a_ca_is_rejected(mode: str) -> None:
    with pytest.raises(ValueError, match="no sslrootcert"):
        _tls_pem_material(sslmode=mode, sslrootcert=None, sslcert=None, sslkey=None)


def test_half_a_client_certificate_pair_is_rejected() -> None:
    with pytest.raises(ValueError, match="both sslcert and sslkey"):
        _tls_pem_material(sslmode="require", sslrootcert=None, sslcert=CERT_PEM, sslkey=None)
    with pytest.raises(ValueError, match="both sslcert and sslkey"):
        _tls_pem_material(sslmode="require", sslrootcert=None, sslcert=None, sslkey=KEY_PEM)


def test_a_filesystem_path_is_not_mistaken_for_a_certificate() -> None:
    # The certificates arrive as PEM content out of an encrypted column. A path is
    # almost certainly a misconfiguration, and treating it as a certificate would
    # produce a connection failure far from its cause.
    with pytest.raises(ValueError, match="must be PEM content"):
        _tls_pem_material(
            sslmode="verify-full",
            sslrootcert="/etc/ssl/certs/ca.pem",
            sslcert=None,
            sslkey=None,
        )


def test_the_error_never_echoes_the_private_key() -> None:
    with pytest.raises(ValueError) as excinfo:
        _tls_pem_material(
            sslmode="require",
            sslrootcert=None,
            sslcert=CERT_PEM,
            sslkey="not-a-pem-secret-material",
        )
    assert "not-a-pem-secret-material" not in str(excinfo.value)


def test_pems_are_materialized_private_and_cleaned_up() -> None:
    material = _tls_pem_material(
        sslmode="verify-full",
        sslrootcert=CA_PEM,
        sslcert=CERT_PEM,
        sslkey=KEY_PEM,
    )
    tls = _materialize_tls_files(material)

    assert tls.directory is not None
    assert set(tls.paths) == {"sslrootcert", "sslcert", "sslkey"}
    # libpq REFUSES a key file that is group- or world-readable, so 0600 is not
    # merely hygiene — a looser mode makes the connection fail outright.
    assert stat.S_IMODE(os.stat(tls.directory).st_mode) == 0o700
    for name, path in tls.paths.items():
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600, name
        assert Path(path).read_text().startswith("-----BEGIN")

    directory = tls.directory
    import shutil

    shutil.rmtree(directory, ignore_errors=True)
    assert not Path(directory).exists()


def test_nothing_is_written_when_there_is_no_certificate_material() -> None:
    tls = _materialize_tls_files({})
    assert tls.directory is None
    assert tls.paths == {}


# --- search_path -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("public", "public"),
        ("analytics,public", "analytics,public"),
        (" analytics , public ", "analytics,public"),
        # A `$` is legal inside a Postgres identifier, and PostgresSettings accepts
        # it on the way in — the two layers must agree, or a config the API stored
        # would blow up at connect time instead.
        ("my$schema,public", "my$schema,public"),
    ],
)
def test_a_plain_identifier_list_is_accepted(raw: str, expected: str) -> None:
    assert _validated_search_path(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "public; DROP TABLE users",
        "public'",
        '"public"',
        "$user,public",
        "pg catalog",
        "",
        "public,",
        "1bad",
    ],
)
def test_search_path_cannot_smuggle_anything_past_the_identifier_check(raw: str) -> None:
    # search_path is an identifier list, not a literal: it cannot be bound as a
    # parameter, so it is interpolated — and therefore has to be validated.
    with pytest.raises(ValueError, match="search_path"):
        _validated_search_path(raw)
