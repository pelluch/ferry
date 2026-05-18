from dataclasses import dataclass, field
from pathlib import Path

from ferry.domain.destination import Destination


@dataclass(frozen=True, slots=True, kw_only=True)
class RommConfig:
    url: str
    api_key: str = field(repr=False)
    allow_insecure_ssl: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncConfig:
    """Settings for `ferry sync`. Required for sync; ignored by other commands.

    The set of ROMs to sync is the *union* of every source — manual
    collections plus platforms (and, in a follow-up checkpoint, smart and
    virtual collections). At least one source must be non-empty.
    """

    # Manual user-created RomM collections, by name.
    collections: tuple[str, ...] = ()
    # RomM platform slugs (e.g., "gba", "snes"). Multi-valued is supported by
    # RomM's /api/roms?platform_ids=A&platform_ids=B endpoint, so a single
    # request fetches all platforms.
    platforms: tuple[str, ...] = ()
    primary_version_only: bool = False
    # Defaults to False so a first sync against a stale state can never silently
    # trash files. Users opt into mirror semantics explicitly when they're
    # confident the local state matches what they want RomM to authoritatively
    # govern.
    delete_on_remove: bool = False
    trash_retention_days: int = 14

    @property
    def has_any_source(self) -> bool:
        return bool(self.collections or self.platforms)


@dataclass(frozen=True, slots=True, kw_only=True)
class TransformsConfig:
    """Per-platform transform pipelines (DESIGN.md §5.5).

    Platforms not listed default to no pipeline (file flows through unchanged).
    """

    pipelines: dict[str, tuple[str, ...]]

    def for_platform(self, platform_slug: str) -> tuple[str, ...]:
        return self.pipelines.get(platform_slug, ())


def _empty_transforms() -> TransformsConfig:
    return TransformsConfig(pipelines={})


@dataclass(frozen=True, slots=True, kw_only=True)
class SavesConfig:
    """Settings for save sync (DESIGN.md §5.3, v2+v3).

    Presence of `[saves]` in config opts the user into save sync; the
    default `enabled = true` lets the section act as the on switch
    without requiring a redundant assignment. Set `enabled = false` to
    keep the section configured but pause the feature.

    `retroarch_install` / `dolphin_install` disambiguate when ferry
    detects multiple installations of the same emulator with active
    saves. Single-install cases leave them None and discovery picks
    automatically.
    """

    enabled: bool = True
    retroarch_install: str | None = None
    dolphin_install: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class BiosConfig:
    """Settings for BIOS / firmware sync (DESIGN.md §5.2, v5.5).

    Presence of `[bios]` opts the user into BIOS sync; `enabled = true`
    by default lets the bare section act as the on switch. Set
    `enabled = false` to keep the section configured but paused.

    BIOS sync *scope* follows `[sync]` — firmware is fetched for the same
    platforms ferry already syncs ROMs for. `files` optionally refines
    *which* firmware files sync within a platform: a platform slug absent
    from `files` syncs every firmware RomM has for it; a slug present
    syncs only the named files. An empty list syncs none for that slug.
    """

    enabled: bool = True
    # platform slug -> filename allowlist. See class docstring.
    files: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def allowlist_for(self, platform_slug: str) -> tuple[str, ...] | None:
        """Return the filename allowlist for *platform_slug*, or None for 'all'."""
        return self.files.get(platform_slug)


@dataclass(frozen=True, slots=True, kw_only=True)
class LaunchHooksConfig:
    """Settings for the `ferry install-launch-hooks` system (DESIGN.md §7 v8).

    Hooks wrap each ES-DE system's launch command with pre/post calls
    to `ferry sync --rom`, so save data syncs to RomM around every
    game session. The wrapper script writes to a single log file
    that's truncated at the start of each session — one game = one
    log file replacement.

    `log_enabled = false` makes the wrapper run silently (still syncs,
    just no logging). `log_path` overrides the default location of
    `$XDG_STATE_HOME/ferry/launch.log`.
    """

    log_enabled: bool = True
    log_path: Path | None = None


def _default_launch_hooks() -> LaunchHooksConfig:
    return LaunchHooksConfig()


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    romm: RommConfig
    destination: Destination | None = None
    sync: SyncConfig | None = None
    transforms: TransformsConfig = field(default_factory=_empty_transforms)
    saves: SavesConfig | None = None
    bios: BiosConfig | None = None
    launch_hooks: LaunchHooksConfig = field(default_factory=_default_launch_hooks)
