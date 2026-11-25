"""Configuration resolution: precedence, discovery, normalisation, validation."""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from tripl_cli.config import (
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_WRONG_BASE_URL,
    default_config_path,
    normalize_base_url,
    require_api_key,
    require_base_url,
    resolve,
)
from tripl_cli.errors import EXIT_USAGE, TriplConfigError

WriteConfig = Callable[[str], Path]


class TestPrecedence:
    def test_base_url_flag_beats_env_beats_file(
        self, monkeypatch: pytest.MonkeyPatch, write_config: WriteConfig
    ) -> None:
        path = write_config('base_url = "https://from-file"')
        monkeypatch.setenv(ENV_BASE_URL, "https://from-env")

        assert resolve(base_url="https://from-flag", config_path=path).base_url == (
            "https://from-flag"
        )
        assert resolve(config_path=path).base_url == "https://from-env"

        monkeypatch.delenv(ENV_BASE_URL)
        assert resolve(config_path=path).base_url == "https://from-file"

    def test_api_key_flag_beats_env_beats_file(
        self, monkeypatch: pytest.MonkeyPatch, write_config: WriteConfig
    ) -> None:
        path = write_config('api_key = "tk_r_file"')
        monkeypatch.setenv(ENV_API_KEY, "tk_r_env")

        assert resolve(api_key="tk_r_flag", config_path=path).api_key == "tk_r_flag"
        assert resolve(config_path=path).api_key == "tk_r_env"

        monkeypatch.delenv(ENV_API_KEY)
        assert resolve(config_path=path).api_key == "tk_r_file"

    def test_merge_is_per_field_not_per_source(self, write_config: WriteConfig) -> None:
        """--url must not disable the file's api_key.

        Whole-source precedence ("any flag given, ignore the file") is a
        plausible-looking bug, so it gets its own test.
        """
        path = write_config('base_url = "https://from-file"\napi_key = "tk_r_file"\n')

        config = resolve(base_url="https://staging.example.com", config_path=path)

        assert config.base_url == "https://staging.example.com"
        assert config.api_key == "tk_r_file"

    def test_empty_env_is_treated_as_absent(
        self, monkeypatch: pytest.MonkeyPatch, write_config: WriteConfig
    ) -> None:
        """docker compose materialises undefined variables as empty strings."""
        path = write_config('base_url = "https://from-file"')
        monkeypatch.setenv(ENV_BASE_URL, "")

        assert resolve(config_path=path).base_url == "https://from-file"

    def test_provenance_names_each_layer(
        self, monkeypatch: pytest.MonkeyPatch, write_config: WriteConfig
    ) -> None:
        path = write_config('base_url = "https://from-file"')

        assert resolve(config_path=path).sources["base_url"] == str(path)

        monkeypatch.setenv(ENV_BASE_URL, "https://from-env")
        assert resolve(config_path=path).sources["base_url"] == f"${ENV_BASE_URL}"

        assert resolve(base_url="https://x", config_path=path).sources["base_url"] == "--url"


class TestNothingConfigured:
    def test_resolves_cleanly_with_no_sources_at_all(self) -> None:
        config = resolve()

        assert config.base_url is None
        assert config.api_key is None
        assert config.path_exists is False
        assert config.sources == {}

    def test_require_base_url_names_all_three_ways_to_fix_it(self) -> None:
        config = resolve()

        with pytest.raises(TriplConfigError) as excinfo:
            require_base_url(config)

        message = str(excinfo.value)
        assert "--url" in message
        assert ENV_BASE_URL in message
        assert str(config.path) in message
        # Unusable configuration is a usage error, not a runtime failure.
        assert excinfo.value.exit_code == EXIT_USAGE

    def test_require_api_key_names_all_three_ways_to_fix_it(self) -> None:
        config = resolve()

        with pytest.raises(TriplConfigError) as excinfo:
            require_api_key(config)

        message = str(excinfo.value)
        assert "--api-key" in message
        assert ENV_API_KEY in message
        assert str(config.path) in message

    def test_wrong_env_var_name_is_called_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TRIPL_URL is never read, only detected — one name per value."""
        monkeypatch.setenv(ENV_WRONG_BASE_URL, "https://tripl.example.com")

        config = resolve()
        assert config.base_url is None

        with pytest.raises(TriplConfigError) as excinfo:
            require_base_url(config)

        message = str(excinfo.value)
        assert ENV_WRONG_BASE_URL in message
        assert "is not the variable this tool reads" in message


class TestNormalizeBaseUrl:
    def test_strips_trailing_slash(self) -> None:
        assert (
            normalize_base_url("https://tripl.example.com/", "--url") == "https://tripl.example.com"
        )

    def test_strips_a_pasted_api_prefix(self) -> None:
        """Otherwise every request doubles the prefix into /api/v1/api/v1."""
        assert normalize_base_url("https://tripl.example.com/api/v1", "--url") == (
            "https://tripl.example.com"
        )
        assert normalize_base_url("https://tripl.example.com/api/v1/", "--url") == (
            "https://tripl.example.com"
        )

    def test_scheme_less_host_is_rejected_with_the_fix_shown(self) -> None:
        with pytest.raises(TriplConfigError) as excinfo:
            normalize_base_url("tripl.example.com", f"${ENV_BASE_URL}")

        message = str(excinfo.value)
        assert ENV_BASE_URL in message  # names the thing to edit
        assert "https://tripl.example.com" in message  # shows the corrected value

    def test_host_port_is_rejected_with_the_fix_shown(self) -> None:
        """urlsplit reads the "localhost" in `localhost:8000` as a SCHEME.

        So the "did you mean" branch keys off "://" being absent, not off
        urlsplit's scheme — otherwise the most likely local-instance typo of all
        gets the least help.
        """
        with pytest.raises(TriplConfigError) as excinfo:
            normalize_base_url("localhost:8000", "--url")

        assert "try https://localhost:8000" in str(excinfo.value)

    def test_a_real_but_wrong_scheme_gets_no_nonsense_suggestion(self) -> None:
        with pytest.raises(TriplConfigError) as excinfo:
            normalize_base_url("ftp://tripl.example.com", "--url")

        assert "https://ftp://" not in str(excinfo.value)

    @pytest.mark.parametrize("raw", ["https://[::1", "https://[not-an-ip]:8000"])
    def test_a_host_urlsplit_cannot_parse_is_a_config_error(self, raw: str) -> None:
        """urlsplit raises ValueError here, which is not a TriplError: a traceback and exit 1."""
        with pytest.raises(TriplConfigError) as excinfo:
            normalize_base_url(raw, "$TRIPL_BASE_URL")

        message = str(excinfo.value)
        assert "$TRIPL_BASE_URL" in message
        assert "not a valid tripl URL" in message
        assert excinfo.value.exit_code == EXIT_USAGE

    def test_an_unparseable_base_url_exits_two_without_a_traceback(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from tripl_cli.cli import main

        monkeypatch.setenv("TRIPL_BASE_URL", "https://[::1")
        monkeypatch.setenv("TRIPL_API_KEY", "tk_r_test-key")

        code = main(["whoami"])

        captured = capsys.readouterr()
        assert code == EXIT_USAGE
        assert "Traceback" not in captured.err
        assert "not a valid tripl URL" in captured.err

    @pytest.mark.parametrize(
        "raw",
        [
            "https://tripl.example.com\nEVIL=1",
            "https://tripl.example.com\r\nEVIL=1",
            "https://tripl.example\tcom",
            "https://tripl.example.com\x7f",
        ],
    )
    def test_a_control_character_is_rejected(self, raw: str) -> None:
        """urlsplit DELETES tab, CR and LF before parsing, so it says these are fine.

        They are not. This value becomes the `APP_BASE_URL=` line of the 0600
        `.env` Docker Compose interpolates, where a newline does not corrupt the
        file - it ADDS settings to it. One pasted `--app-url` would otherwise
        write arbitrary environment into every container in the stack.
        """
        with pytest.raises(TriplConfigError) as excinfo:
            normalize_base_url(raw, "--app-url")

        message = str(excinfo.value)
        assert "control character" in message
        assert "--app-url" in message
        # The refusal must not itself smuggle the raw bytes into a terminal.
        assert "\n" not in message
        assert "\t" not in message

    def test_urlsplit_really_does_accept_those(self) -> None:
        """Guards the test above: a check on a value nothing accepts proves nothing.

        urlsplit DELETES the newline and splices the halves together, so the
        payload parses as a perfectly ordinary https netloc. Nothing downstream
        of it would have objected.
        """
        from urllib.parse import urlsplit

        parsed = urlsplit("https://tripl.example.com\nEVIL=1")
        assert parsed.scheme == "https"
        assert parsed.netloc == "tripl.example.comEVIL=1"
        assert "\n" not in parsed.netloc

    def test_applies_to_every_source(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_BASE_URL, "https://tripl.example.com/api/v1/")
        assert resolve().base_url == "https://tripl.example.com"

        assert resolve(base_url="https://tripl.example.com/").base_url == (
            "https://tripl.example.com"
        )


class TestDefaultConfigPath:
    def test_xdg_config_home_wins(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        assert default_config_path() == tmp_path / "tripl" / "config.toml"

    def test_relative_xdg_is_ignored_per_the_spec(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", "relative/dir")

        assert default_config_path() == Path.home() / ".config" / "tripl" / "config.toml"

    def test_falls_back_to_dot_config(self) -> None:
        assert default_config_path() == Path.home() / ".config" / "tripl" / "config.toml"

    def test_windows_uses_appdata(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("APPDATA", str(tmp_path))

        assert default_config_path() == tmp_path / "tripl" / "config.toml"

    def test_windows_without_appdata_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")

        assert default_config_path() == Path.home() / ".config" / "tripl" / "config.toml"


class TestConfigFile:
    def test_missing_default_file_is_not_an_error(self) -> None:
        config = resolve()

        assert config.path_exists is False
        assert config.path == default_config_path()

    def test_missing_explicit_config_is_an_error(self, tmp_path: Path) -> None:
        """An explicit request that cannot be honoured is never silently ignored."""
        missing = tmp_path / "nope.toml"

        with pytest.raises(TriplConfigError, match="config file not found"):
            resolve(config_path=missing)

    def test_default_file_is_read_when_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        target = tmp_path / "tripl" / "config.toml"
        target.parent.mkdir(parents=True)
        target.write_text('base_url = "https://discovered.example.com"\n', encoding="utf-8")

        config = resolve()

        assert config.base_url == "https://discovered.example.com"
        assert config.path_exists is True

    def test_malformed_toml_names_the_file(self, write_config: WriteConfig) -> None:
        path = write_config("base_url = = broken\n")

        with pytest.raises(TriplConfigError, match="is not valid TOML") as excinfo:
            resolve(config_path=path)

        assert str(path) in str(excinfo.value)

    def test_wrong_scalar_type_names_key_and_type(self, write_config: WriteConfig) -> None:
        path = write_config("base_url = 8000\n")

        with pytest.raises(TriplConfigError, match="'base_url' must be a string") as excinfo:
            resolve(config_path=path)

        assert "int" in str(excinfo.value)

    def test_wrong_type_is_caught_even_when_something_overrides_it(
        self, write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A malformed file is malformed whether or not its value would be used.

        The layered pick short-circuits, so validation used to run only when the
        file actually won. With TRIPL_BASE_URL exported, `base_url = 8000` stayed
        silent — and surfaced days later, from a file the user had not touched,
        the moment the variable went away.
        """
        path = write_config("base_url = 8000\n")
        monkeypatch.setenv("TRIPL_BASE_URL", "https://tripl.example.com")

        with pytest.raises(TriplConfigError, match="'base_url' must be a string"):
            resolve(config_path=path)

    def test_unknown_keys_and_tables_are_ignored(self, write_config: WriteConfig) -> None:
        """Forward compatibility: an older CLI must read a newer file."""
        path = write_config(
            'base_url = "https://tripl.example.com"\n'
            'future_key = "whatever"\n'
            "\n[profiles.staging]\n"
            'base_url = "https://staging.example.com"\n'
        )

        config = resolve(config_path=path)

        assert config.base_url == "https://tripl.example.com"


@pytest.mark.skipif(sys.platform == "win32", reason="st_mode carries no such meaning on Windows")
class TestPermissionsWarning:
    def test_world_readable_key_warns_but_still_resolves(
        self, write_config: WriteConfig, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = write_config('api_key = "tk_r_secret"\n')
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)

        config = resolve(config_path=path)

        assert config.api_key == "tk_r_secret"
        err = capsys.readouterr().err
        assert "readable by other users" in err
        assert f"chmod 600 {path}" in err

    def test_private_file_is_silent(
        self, write_config: WriteConfig, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = write_config('api_key = "tk_r_secret"\n')
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)

        resolve(config_path=path)

        assert capsys.readouterr().err == ""

    def test_no_key_means_no_warning(
        self, write_config: WriteConfig, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = write_config('base_url = "https://tripl.example.com"\n')
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)

        resolve(config_path=path)

        assert capsys.readouterr().err == ""
