import enum
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ferry.config.schema import Config, RommConfig

ENV_API_KEY = "FERRY_ROMM_API_KEY"
ENV_CONFIG_PATH = "FERRY_CONFIG"

_TOP_LEVEL_KEYS = frozenset({"romm"})
_ROMM_KEYS = frozenset({"url", "api_key", "allow_insecure_ssl"})


class ApiKeySource(enum.StrEnum):
    TOML = "config.toml"
    ENV = f"{ENV_API_KEY} env var"


class ConfigError(Exception):
    """Base class for configuration errors surfaced to the user."""


class ConfigNotFoundError(ConfigError):
    """The configuration file does not exist."""


class ConfigInvalidError(ConfigError):
    """The configuration file is malformed or missing required values."""


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    config: Config
    config_path: Path
    api_key_source: ApiKeySource


def default_config_path(env: Mapping[str, str] | None = None) -> Path:
    env = env if env is not None else os.environ
    base = env.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "ferry" / "config.toml"


def load_config(
    path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> LoadedConfig:
    env = env if env is not None else os.environ
    if path is None:
        env_path = env.get(ENV_CONFIG_PATH)
        path = Path(env_path) if env_path else default_config_path(env)

    if not path.exists():
        raise ConfigNotFoundError(
            f"config file not found: {path}\n"
            f"create it with the [romm] section, or set {ENV_CONFIG_PATH} "
            f"to point at an existing file."
        )

    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigInvalidError(f"invalid TOML in {path}: {e}") from e

    if not isinstance(raw, dict):
        raise ConfigInvalidError(f"config root must be a table: {path}")

    unknown_top = set(raw.keys()) - _TOP_LEVEL_KEYS
    if unknown_top:
        raise ConfigInvalidError(f"unknown top-level keys in {path}: {sorted(unknown_top)}")

    romm_raw = raw.get("romm")
    if not isinstance(romm_raw, dict):
        raise ConfigInvalidError(f"[romm] section is required in {path}")

    unknown_romm = set(romm_raw.keys()) - _ROMM_KEYS
    if unknown_romm:
        raise ConfigInvalidError(f"unknown keys under [romm] in {path}: {sorted(unknown_romm)}")

    url = _require_str(romm_raw, "url", path)
    url = _validate_url(url, path)

    env_api_key = env.get(ENV_API_KEY)
    toml_api_key = romm_raw.get("api_key")
    if env_api_key:
        api_key = env_api_key
        api_key_source = ApiKeySource.ENV
    elif isinstance(toml_api_key, str) and toml_api_key:
        api_key = toml_api_key
        api_key_source = ApiKeySource.TOML
    else:
        raise ConfigInvalidError(
            f"missing RomM API key: set [romm].api_key in {path} or export {ENV_API_KEY}."
        )

    allow_insecure_ssl = romm_raw.get("allow_insecure_ssl", False)
    if not isinstance(allow_insecure_ssl, bool):
        raise ConfigInvalidError(f"[romm].allow_insecure_ssl must be a boolean in {path}")

    config = Config(
        romm=RommConfig(
            url=url,
            api_key=api_key,
            allow_insecure_ssl=allow_insecure_ssl,
        )
    )
    return LoadedConfig(config=config, config_path=path, api_key_source=api_key_source)


def _require_str(table: dict, key: str, path: Path) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigInvalidError(f"[romm].{key} must be a non-empty string in {path}")
    return value


def _validate_url(url: str, path: Path) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ConfigInvalidError(f"[romm].url must start with http:// or https:// in {path}")
    if not parsed.netloc:
        raise ConfigInvalidError(f"[romm].url is missing a host in {path}")
    return url.rstrip("/")
