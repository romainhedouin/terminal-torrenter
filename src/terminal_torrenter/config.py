"""User configuration, loaded from a TOML file.

Settings come from three places, later ones winning:

1. the defaults in :class:`Config`
2. the config file (``~/.config/terminal-torrenter/config.toml`` by default)
3. command-line flags

Every path supports ``~``. Environment variables
``TERMINAL_TORRENTER_CONFIG`` and ``TERMINAL_TORRENTER_STATE_DIR`` override the
config-file and state-directory locations respectively, which is mostly useful
for tests and for running several isolated instances.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, fields

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on 3.10 only
    import tomli as tomllib  # type: ignore[no-redef]

APP_NAME = "terminal-torrenter"

DEFAULT_DOWNLOAD_DIR = "~/Documents/downloads"


class ConfigError(ValueError):
    """The config file exists but could not be used."""


def _xdg_dir(env_var: str, fallback: str) -> str:
    base = os.environ.get(env_var) or os.path.expanduser(fallback)
    return os.path.join(base, APP_NAME)


def config_dir() -> str:
    return _xdg_dir("XDG_CONFIG_HOME", "~/.config")


def config_path() -> str:
    """Where the config file lives (it need not exist)."""
    override = os.environ.get("TERMINAL_TORRENTER_CONFIG")
    if override:
        return os.path.expanduser(override)
    return os.path.join(config_dir(), "config.toml")


def state_dir() -> str:
    """Where resume data and per-torrent sidecars live."""
    override = os.environ.get("TERMINAL_TORRENTER_STATE_DIR")
    if override:
        return os.path.expanduser(override)
    return _xdg_dir("XDG_STATE_HOME", "~/.local/state")


@dataclass
class Config:
    """Runtime settings. Field names match the keys in config.toml."""

    download_dir: str = DEFAULT_DOWNLOAD_DIR
    sequential: bool = True
    listen_port: int = 6881
    max_download_rate: int = 0  # KiB/s, 0 = unlimited
    max_upload_rate: int = 0

    def __post_init__(self) -> None:
        self.download_dir = os.path.abspath(os.path.expanduser(self.download_dir))

    @classmethod
    def load(cls, path: str | None = None) -> Config:
        """Read the config file, falling back to defaults when absent."""
        path = path or config_path()
        if not os.path.isfile(path):
            return cls()
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"{path}: {exc}") from exc
        return cls.from_dict(data, origin=path)

    @classmethod
    def from_dict(cls, data: dict, origin: str = "config") -> Config:
        known = {f.name: f for f in fields(cls)}
        unknown = sorted(set(data) - set(known))
        if unknown:
            raise ConfigError(
                f"{origin}: unknown setting(s): {', '.join(unknown)}. "
                f"Valid keys: {', '.join(sorted(known))}"
            )
        values = {}
        for name, value in data.items():
            expected = known[name].type
            # Annotations are strings under `from __future__ import annotations`.
            if expected == "bool" and not isinstance(value, bool):
                raise ConfigError(f"{origin}: '{name}' must be true or false")
            if expected == "int" and not isinstance(value, int):
                raise ConfigError(f"{origin}: '{name}' must be a whole number")
            if expected == "str" and not isinstance(value, str):
                raise ConfigError(f"{origin}: '{name}' must be a string")
            values[name] = value
        config = cls(**values)
        if config.listen_port < 1 or config.listen_port > 65535:
            raise ConfigError(f"{origin}: 'listen_port' must be between 1 and 65535")
        for rate in ("max_download_rate", "max_upload_rate"):
            if getattr(config, rate) < 0:
                raise ConfigError(f"{origin}: '{rate}' must be 0 or greater")
        return config

    def merged_with_args(self, args) -> Config:  # noqa: ANN001 - argparse.Namespace
        """Overlay command-line flags that were actually provided."""
        overrides = {
            name: getattr(args, name)
            for name in (f.name for f in fields(self))
            if getattr(args, name, None) is not None
        }
        return Config(**{**asdict(self), **overrides})


DEFAULT_CONFIG_TEMPLATE = f"""\
# {APP_NAME} configuration
# Every setting is optional; the values below are the defaults.

# Where finished and in-progress files are written.
download_dir = "{DEFAULT_DOWNLOAD_DIR}"

# Download pieces in order (nice for previewing media while it downloads).
sequential = true

# Local port used for peer connections.
listen_port = 6881

# Speed limits in KiB/s. 0 means unlimited.
max_download_rate = 0
max_upload_rate = 0
"""


def write_default_config(path: str | None = None, force: bool = False) -> str:
    """Write a commented config file and return where it landed."""
    path = path or config_path()
    if os.path.exists(path) and not force:
        raise ConfigError(f"{path} already exists (use --force to overwrite)")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(DEFAULT_CONFIG_TEMPLATE)
    return path
