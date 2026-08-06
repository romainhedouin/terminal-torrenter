"""Tests for config-file loading, validation and CLI precedence."""

import os

import pytest

from terminal_torrenter.config import (
    Config,
    ConfigError,
    config_path,
    state_dir,
    write_default_config,
)
from terminal_torrenter.tui import parse_args


def write(tmp_path, body):
    path = tmp_path / "config.toml"
    path.write_text(body)
    return str(path)


def test_defaults_when_no_file(tmp_path):
    config = Config.load(str(tmp_path / "missing.toml"))
    assert config.download_dir == os.path.expanduser("~/Documents/downloads")
    assert config.sequential is True
    assert config.listen_port == 6881
    assert config.max_download_rate == 0


def test_file_overrides_defaults(tmp_path):
    config = Config.load(write(tmp_path, '''
        download_dir = "~/Movies"
        sequential = false
        max_download_rate = 2048
    '''))
    assert config.download_dir == os.path.expanduser("~/Movies")
    assert config.sequential is False
    assert config.max_download_rate == 2048
    assert config.listen_port == 6881  # untouched keys keep their default


def test_download_dir_is_expanded_and_absolute():
    assert Config(download_dir="~/x").download_dir == os.path.expanduser("~/x")
    assert os.path.isabs(Config(download_dir="relative/path").download_dir)


def test_cli_flags_beat_the_file(tmp_path):
    config = Config.load(write(tmp_path, 'download_dir = "~/FromFile"\nlisten_port = 7000\n'))
    args = parse_args(["-d", "/tmp/from-cli", "--max-upload-rate", "64"])
    merged = config.merged_with_args(args)
    assert merged.download_dir == "/tmp/from-cli"
    assert merged.max_upload_rate == 64
    assert merged.listen_port == 7000  # not passed on the CLI, file value survives


def test_unset_store_false_flag_does_not_override_file(tmp_path):
    """--no-sequential defaults to None, so omitting it must not force True."""
    config = Config.load(write(tmp_path, "sequential = false\n"))
    assert config.merged_with_args(parse_args([])).sequential is False
    assert config.merged_with_args(parse_args(["--no-sequential"])).sequential is False


def test_sequential_flag_overrides_file(tmp_path):
    config = Config.load(write(tmp_path, "sequential = true\n"))
    assert config.merged_with_args(parse_args(["--no-sequential"])).sequential is False


@pytest.mark.parametrize("body, message", [
    ("downlod_dir = 'x'", "unknown setting"),
    ("sequential = 'yes'", "must be true or false"),
    ("listen_port = 'x'", "must be a whole number"),
    ("download_dir = 42", "must be a string"),
    ("listen_port = 99999", "between 1 and 65535"),
    ("max_download_rate = -1", "0 or greater"),
    ("this is not toml", "invalid TOML"),
])
def test_bad_config_is_rejected(tmp_path, body, message):
    with pytest.raises(ConfigError, match=message):
        Config.load(write(tmp_path, body))


def test_write_default_config_round_trips(tmp_path):
    path = str(tmp_path / "nested" / "config.toml")
    assert write_default_config(path) == path
    # The generated file must parse, and reproduce the built-in defaults.
    assert Config.load(path) == Config()

    with pytest.raises(ConfigError, match="already exists"):
        write_default_config(path)
    write_default_config(path, force=True)  # explicit overwrite is allowed


def test_env_overrides_locations(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_TORRENTER_CONFIG", str(tmp_path / "c.toml"))
    monkeypatch.setenv("TERMINAL_TORRENTER_STATE_DIR", str(tmp_path / "s"))
    assert config_path() == str(tmp_path / "c.toml")
    assert state_dir() == str(tmp_path / "s")


def test_xdg_locations(tmp_path, monkeypatch):
    monkeypatch.delenv("TERMINAL_TORRENTER_CONFIG", raising=False)
    monkeypatch.delenv("TERMINAL_TORRENTER_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
    assert config_path() == str(tmp_path / "cfg" / "terminal-torrenter" / "config.toml")
    assert state_dir() == str(tmp_path / "st" / "terminal-torrenter")
