import enum
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ferry.config.schema import (
    BiosConfig,
    Config,
    LaunchHooksConfig,
    RommConfig,
    SavesConfig,
    SyncConfig,
    TransformsConfig,
)
from ferry.domain.destination import PRESETS, Destination, resolve_preset
from ferry.domain.user_dirs import config_dir
from ferry.transforms import known_transforms

ENV_API_KEY = "FERRY_ROMM_API_KEY"
ENV_CONFIG_PATH = "FERRY_CONFIG"

_TOP_LEVEL_KEYS = frozenset(
    {"romm", "destination", "sync", "transforms", "saves", "bios", "launch_hooks"}
)
_LAUNCH_HOOKS_KEYS = frozenset({"log_enabled", "log_path"})
_ROMM_KEYS = frozenset({"url", "api_key", "allow_insecure_ssl"})
_DESTINATION_KEYS = frozenset({"preset", "roms_base"})
_SYNC_KEYS = frozenset(
    {
        "collections",
        "platforms",
        "primary_version_only",
        "delete_on_remove",
        "trash_retention_days",
    }
)
_TRANSFORMS_PLATFORM_KEYS = frozenset({"pipeline"})
_SAVES_KEYS = frozenset({"enabled", "retroarch_install", "dolphin_install"})
_BIOS_KEYS = frozenset({"enabled", "files"})
_RETROARCH_INSTALL_VALUES = frozenset({"retrodeck-flatpak", "libretro-flatpak", "native"})
_DOLPHIN_INSTALL_VALUES = frozenset({"retrodeck-flatpak", "emudeck-flatpak", "native"})


class ApiKeySource(enum.StrEnum):
    TOML = "config.toml"
    ENV = f"{ENV_API_KEY} env var"


class ConfigError(Exception):
    """Base class for configuration errors surfaced to the user."""


class ConfigNotFoundError(ConfigError):
    """The configuration file does not exist."""


class ConfigInvalidError(ConfigError):
    """The configuration file is malformed or missing required values."""


@dataclass(frozen=True, slots=True, kw_only=True)
class LoadedConfig:
    config: Config
    config_path: Path
    api_key_source: ApiKeySource


def default_config_path(env: Mapping[str, str] | None = None) -> Path:
    return config_dir(env) / "ferry" / "config.toml"


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
    _check_unknown_keys(romm_raw, allowed=_ROMM_KEYS, label="romm", path=path)

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
    saves = _parse_saves(raw, path)
    bios = _parse_bios(raw, path)
    launch_hooks = _parse_launch_hooks(raw, path)

    config = Config(
        romm=RommConfig(
            url=url,
            api_key=api_key,
            allow_insecure_ssl=allow_insecure_ssl,
        ),
        destination=destination,
        sync=sync,
        transforms=transforms,
        saves=saves,
        bios=bios,
        launch_hooks=launch_hooks,
    )
    return LoadedConfig(config=config, config_path=path, api_key_source=api_key_source)


def _extract_section(
    raw: dict, name: str, *, allowed_keys: frozenset[str], path: Path
) -> dict | None:
    """Pull a top-level config section out, validate shape + unknown keys.

    Returns the section dict, or None if the section isn't present in the
    config. The "must be a table" / "unknown keys under X" boilerplate
    every section parser used to repeat lives here.
    """
    if name not in raw:
        return None
    section = raw[name]
    if not isinstance(section, dict):
        raise ConfigInvalidError(f"[{name}] must be a table in {path}")
    _check_unknown_keys(section, allowed=allowed_keys, label=name, path=path)
    return section


def _check_unknown_keys(section: dict, *, allowed: frozenset[str], label: str, path: Path) -> None:
    """Raise if *section* has keys outside *allowed*. Pure validation."""
    unknown = set(section.keys()) - allowed
    if unknown:
        raise ConfigInvalidError(f"unknown keys under [{label}] in {path}: {sorted(unknown)}")


def _parse_launch_hooks(raw: dict, path: Path) -> LaunchHooksConfig:
    """Parse the optional `[launch_hooks]` section.

    Section is fully optional — defaults give sensible behavior
    (logging on, default log path). Section presence with empty body
    behaves the same as the section being absent.
    """
    section = _extract_section(raw, "launch_hooks", allowed_keys=_LAUNCH_HOOKS_KEYS, path=path)
    if section is None:
        return LaunchHooksConfig()
    log_enabled = section.get("log_enabled", True)
    if not isinstance(log_enabled, bool):
        raise ConfigInvalidError(f"[launch_hooks].log_enabled must be a boolean in {path}")
    log_path_raw = section.get("log_path")
    log_path: Path | None = None
    if log_path_raw is not None:
        if not isinstance(log_path_raw, str) or not log_path_raw:
            raise ConfigInvalidError(
                f"[launch_hooks].log_path must be a non-empty string in {path}"
            )
        log_path = Path(log_path_raw).expanduser()
    return LaunchHooksConfig(log_enabled=log_enabled, log_path=log_path)


def _parse_saves(raw: dict, path: Path) -> SavesConfig | None:
    section = _extract_section(raw, "saves", allowed_keys=_SAVES_KEYS, path=path)
    if section is None:
        return None
    enabled = section.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigInvalidError(f"[saves].enabled must be a boolean in {path}")

    retroarch_install = section.get("retroarch_install")
    if retroarch_install is not None:
        if not isinstance(retroarch_install, str):
            raise ConfigInvalidError(f"[saves].retroarch_install must be a string in {path}")
        if retroarch_install not in _RETROARCH_INSTALL_VALUES:
            allowed = ", ".join(sorted(_RETROARCH_INSTALL_VALUES))
            raise ConfigInvalidError(
                f"[saves].retroarch_install in {path} must be one of: {allowed}"
            )

    dolphin_install = section.get("dolphin_install")
    if dolphin_install is not None:
        if not isinstance(dolphin_install, str):
            raise ConfigInvalidError(f"[saves].dolphin_install must be a string in {path}")
        if dolphin_install not in _DOLPHIN_INSTALL_VALUES:
            allowed = ", ".join(sorted(_DOLPHIN_INSTALL_VALUES))
            raise ConfigInvalidError(f"[saves].dolphin_install in {path} must be one of: {allowed}")

    return SavesConfig(
        enabled=enabled,
        retroarch_install=retroarch_install,
        dolphin_install=dolphin_install,
    )


def _parse_bios(raw: dict, path: Path) -> BiosConfig | None:
    """Parse the optional `[bios]` section.

    `[bios.files]` is a sub-table whose keys are arbitrary platform
    slugs (like `[transforms]`), each mapping to a filename allowlist.
    """
    section = _extract_section(raw, "bios", allowed_keys=_BIOS_KEYS, path=path)
    if section is None:
        return None
    enabled = section.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigInvalidError(f"[bios].enabled must be a boolean in {path}")

    files: dict[str, tuple[str, ...]] = {}
    files_raw = section.get("files", {})
    if not isinstance(files_raw, dict):
        raise ConfigInvalidError(f"[bios.files] must be a table in {path}")
    for platform_slug, names in files_raw.items():
        if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
            raise ConfigInvalidError(
                f"[bios.files].{platform_slug} must be a list of non-empty strings in {path}"
            )
        # Preserve order, dedup defensively.
        seen: set[str] = set()
        unique: list[str] = []
        for n in names:
            if n not in seen:
                seen.add(n)
                unique.append(n)
        files[platform_slug] = tuple(unique)

    return BiosConfig(enabled=enabled, files=files)


def _parse_sync(raw: dict, path: Path) -> SyncConfig | None:
    sync = _extract_section(raw, "sync", allowed_keys=_SYNC_KEYS, path=path)
    if sync is None:
        return None
    collections = _parse_string_list(sync, "collections", path)
    platforms = _parse_string_list(sync, "platforms", path)
    if not collections and not platforms:
        raise ConfigInvalidError(
            f"[sync] in {path} requires at least one of `collections = [...]` or "
            f"`platforms = [...]` to be non-empty."
        )

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
        collections=collections,
        platforms=platforms,
        primary_version_only=primary,
        delete_on_remove=delete_on_remove,
        trash_retention_days=retention,
    )


def _parse_string_list(table: dict, key: str, path: Path) -> tuple[str, ...]:
    raw = table.get(key, [])
    if not isinstance(raw, list) or not all(isinstance(v, str) and v for v in raw):
        raise ConfigInvalidError(f"[sync].{key} must be a list of non-empty strings in {path}")
    # Preserve config order; dedup defensively (user might list "gba" twice).
    seen: set[str] = set()
    unique: list[str] = []
    for v in raw:
        if v not in seen:
            seen.add(v)
            unique.append(v)
    return tuple(unique)


def _parse_transforms(raw: dict, path: Path) -> TransformsConfig:
    # `[transforms]` is special — its keys are arbitrary platform slugs,
    # so we don't pass `allowed_keys` here. Each per-slug sub-table IS
    # validated against `_TRANSFORMS_PLATFORM_KEYS`.
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
        _check_unknown_keys(
            sub,
            allowed=_TRANSFORMS_PLATFORM_KEYS,
            label=f"transforms.{platform_slug}",
            path=path,
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
    dest = _extract_section(raw, "destination", allowed_keys=_DESTINATION_KEYS, path=path)
    if dest is None:
        return None
    preset_name = dest.get("preset")
    roms_raw = dest.get("roms_base")

    # `bios_base` is NOT a user-settable key — it's derived from the preset
    # only. A standalone override could silently diverge from where the
    # frontend's emulators actually read BIOS (the wrong-folder footgun
    # v5.5 ck3.5 removed). Preset → its `bios/`; explicit-paths config →
    # None (no central pile; BIOS sync skips, same as bare ES-DE).
    if preset_name is not None:
        if not isinstance(preset_name, str):
            raise ConfigInvalidError(f"[destination].preset must be a string in {path}")
        if preset_name not in PRESETS:
            known = ", ".join(sorted(PRESETS))
            raise ConfigInvalidError(f"unknown preset {preset_name!r} in {path}; known: {known}")
        default_roms, default_bios = resolve_preset(preset_name, Path.home())
        roms_base = _require_path(roms_raw, default_roms, "[destination].roms_base", path)
        return Destination(roms_base=roms_base, bios_base=default_bios, preset=preset_name)

    if roms_raw is None:
        raise ConfigInvalidError(
            f"[destination] in {path} requires either `preset` or `roms_base`."
        )
    return Destination(
        roms_base=_require_path(roms_raw, None, "[destination].roms_base", path),
        bios_base=None,
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
