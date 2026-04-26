import enum
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ferry.config.schema import Config, RommConfig, SyncConfig, TransformsConfig
from ferry.domain.destination import PRESETS, Destination, resolve_preset
from ferry.transforms import known_transforms

ENV_API_KEY = "FERRY_ROMM_API_KEY"
ENV_CONFIG_PATH = "FERRY_CONFIG"

_TOP_LEVEL_KEYS = frozenset({"romm", "destination", "sync", "transforms"})
_ROMM_KEYS = frozenset({"url", "api_key", "allow_insecure_ssl"})
_DESTINATION_KEYS = frozenset({"preset", "roms_base", "bios_base"})
_SYNC_KEYS = frozenset(
    {"collection", "primary_version_only", "delete_on_remove", "trash_retention_days"}
)
_TRANSFORMS_PLATFORM_KEYS = frozenset({"pipeline"})


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

    destination = _parse_destination(raw, path)
    sync = _parse_sync(raw, path)
    transforms = _parse_transforms(raw, path)

    config = Config(
        romm=RommConfig(
            url=url,
            api_key=api_key,
            allow_insecure_ssl=allow_insecure_ssl,
        ),
        destination=destination,
        sync=sync,
        transforms=transforms,
    )
    return LoadedConfig(config=config, config_path=path, api_key_source=api_key_source)


def _parse_sync(raw: dict, path: Path) -> SyncConfig | None:
    if "sync" not in raw:
        return None
    sync = raw["sync"]
    if not isinstance(sync, dict):
        raise ConfigInvalidError(f"[sync] must be a table in {path}")

    unknown = set(sync.keys()) - _SYNC_KEYS
    if unknown:
        raise ConfigInvalidError(f"unknown keys under [sync] in {path}: {sorted(unknown)}")

    collection = sync.get("collection")
    if not isinstance(collection, str) or not collection:
        raise ConfigInvalidError(f"[sync].collection must be a non-empty string in {path}")

    primary = sync.get("primary_version_only", False)
    if not isinstance(primary, bool):
        raise ConfigInvalidError(f"[sync].primary_version_only must be a boolean in {path}")

    delete_on_remove = sync.get("delete_on_remove", False)
    if not isinstance(delete_on_remove, bool):
        raise ConfigInvalidError(f"[sync].delete_on_remove must be a boolean in {path}")

    retention = sync.get("trash_retention_days", 14)
    if not isinstance(retention, int) or isinstance(retention, bool) or retention < 0:
        raise ConfigInvalidError(
            f"[sync].trash_retention_days must be a non-negative integer in {path}"
        )

    return SyncConfig(
        collection=collection,
        primary_version_only=primary,
        delete_on_remove=delete_on_remove,
        trash_retention_days=retention,
    )


def _parse_transforms(raw: dict, path: Path) -> TransformsConfig:
    if "transforms" not in raw:
        return TransformsConfig(pipelines={})

    section = raw["transforms"]
    if not isinstance(section, dict):
        raise ConfigInvalidError(f"[transforms] must be a table in {path}")

    valid_names = known_transforms()
    pipelines: dict[str, tuple[str, ...]] = {}
    for platform_slug, sub in section.items():
        if not isinstance(sub, dict):
            raise ConfigInvalidError(f"[transforms.{platform_slug}] must be a table in {path}")
        unknown = set(sub.keys()) - _TRANSFORMS_PLATFORM_KEYS
        if unknown:
            raise ConfigInvalidError(
                f"unknown keys under [transforms.{platform_slug}] in {path}: {sorted(unknown)}"
            )
        pipeline = sub.get("pipeline", [])
        if not isinstance(pipeline, list) or not all(isinstance(t, str) for t in pipeline):
            raise ConfigInvalidError(
                f"[transforms.{platform_slug}].pipeline must be a list of strings in {path}"
            )
        for t in pipeline:
            if t not in valid_names:
                known_str = ", ".join(sorted(valid_names))
                raise ConfigInvalidError(
                    f"unknown transform {t!r} in [transforms.{platform_slug}]"
                    f" in {path}; known: {known_str}"
                )
        pipelines[platform_slug] = tuple(pipeline)

    return TransformsConfig(pipelines=pipelines)


def _parse_destination(raw: dict, path: Path) -> Destination | None:
    if "destination" not in raw:
        return None

    dest = raw["destination"]
    if not isinstance(dest, dict):
        raise ConfigInvalidError(f"[destination] must be a table in {path}")

    unknown = set(dest.keys()) - _DESTINATION_KEYS
    if unknown:
        raise ConfigInvalidError(f"unknown keys under [destination] in {path}: {sorted(unknown)}")

    preset_name = dest.get("preset")
    roms_raw = dest.get("roms_base")
    bios_raw = dest.get("bios_base")

    if preset_name is not None:
        if not isinstance(preset_name, str):
            raise ConfigInvalidError(f"[destination].preset must be a string in {path}")
        if preset_name not in PRESETS:
            known = ", ".join(sorted(PRESETS))
            raise ConfigInvalidError(f"unknown preset {preset_name!r} in {path}; known: {known}")
        default_roms, default_bios = resolve_preset(preset_name, Path.home())
        roms_base = _require_path(roms_raw, default_roms, "[destination].roms_base", path)
        bios_base = _optional_path(bios_raw, default_bios, "[destination].bios_base", path)
        return Destination(roms_base=roms_base, bios_base=bios_base, preset=preset_name)

    if roms_raw is None:
        raise ConfigInvalidError(
            f"[destination] in {path} requires either `preset` or `roms_base` "
            f"(`bios_base` is optional)."
        )
    return Destination(
        roms_base=_require_path(roms_raw, None, "[destination].roms_base", path),
        bios_base=_optional_path(bios_raw, None, "[destination].bios_base", path),
        preset=None,
    )


def _require_path(raw: object, default: Path | None, label: str, path: Path) -> Path:
    if raw is None:
        if default is None:
            raise ConfigInvalidError(f"{label} is required in {path}")
        return default
    if not isinstance(raw, str) or not raw:
        raise ConfigInvalidError(f"{label} must be a non-empty string in {path}")
    return Path(raw).expanduser()


def _optional_path(raw: object, default: Path | None, label: str, path: Path) -> Path | None:
    if raw is None:
        return default
    if not isinstance(raw, str) or not raw:
        raise ConfigInvalidError(f"{label} must be a non-empty string in {path}")
    return Path(raw).expanduser()


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
