"""Discover standalone-Dolphin installations.

Each install has three pieces of layout that aren't always co-located:

- **saves_root** — the parent of `<region>/Card A/`. Where ferry walks
  for `.gci` files.
- **config_path** — `Dolphin.ini`. Read for memcard-mode detection
  (SlotA / SlotB). On native + EmuDeck this lives at
  `<saves_root>/../Config/Dolphin.ini`; on RetroDECK it's in a
  completely different tree (the flatpak's data dir, while saves are
  redirected to `~/retrodeck/saves/gc/dolphin/`).
- **region_encoding** — RetroDECK's bundled standalone Dolphin uses
  2-letter region folder names (`US`, `JP`, `EU`); native + EmuDeck use
  Dolphin's documented 3-letter convention (`USA`, `JAP`, `EUR`). Either
  way, `dolphin-tool header` returns 3-letter `Country` strings, so the
  walker / save-resolver layer needs to know which to slice down to.

Probed sources:

- **retrodeck-flatpak** — saves at `~/retrodeck/saves/gc/dolphin/`,
  config at `~/.var/app/net.retrodeck.retrodeck/config/dolphin-emu/Dolphin.ini`,
  2-letter regions. RetroDECK launches its bundled standalone Dolphin
  with `MemcardA`/`MemcardB` paths overridden to point at this flat tree.
- **emudeck-flatpak** — `~/.var/app/org.DolphinEmu.dolphin-emu/data/dolphin-emu/`,
  3-letter. Standard XDG_DATA_HOME mapping inside the flatpak sandbox.
- **native** — `~/.local/share/dolphin-emu/`, 3-letter. Default for
  distro-installed standalone Dolphin on Linux.

The v3 deferred libretro-dolphin tree (`~/retrodeck/saves/gc/User/`) is
NOT in scope and isn't probed: same Dolphin engine, but the user-dir
layout is libretro's, and syncing both standalone + libretro from the
same machine would cause cross-build save churn.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ferry.adapters.dolphin.gamecube_config import (
    GameCubeMemcardSettings,
    MemcardMode,
    parse_gamecube_memcard_settings,
)
from ferry.domain.install_selection import select_active

logger = logging.getLogger(__name__)

DolphinSource = Literal["retrodeck-flatpak", "emudeck-flatpak", "native"]
RegionEncoding = Literal["2-letter", "3-letter"]


@dataclass(frozen=True, slots=True, kw_only=True)
class _DolphinProfile:
    """Static layout description for one DolphinSource flavor.

    Paths are relative to `home`; expanded at discovery time. `config_path`
    is independent of `saves_root` because RetroDECK separates them.

    `wii_saves_root_rel` is the per-title NAND root
    (`title/<TID_HIGH>/<TID_LOW>/data/`). None for profiles whose Wii
    layout we haven't pinned down yet — the Wii save backend skips
    those installs entirely. Today only retrodeck is filled in;
    emudeck-flatpak and native are added once verified on those layouts.
    """

    source: DolphinSource
    saves_root_rel: str
    config_path_rel: str
    region_encoding: RegionEncoding
    wii_saves_root_rel: str | None = None


# Order is preference for active-install selection — RetroDECK first since
# opting into RetroDECK is an opinionated choice that suggests active GC
# use; EmuDeck before native because flatpak installs are typically more
# recent than long-tail native ones.
_PROFILES: tuple[_DolphinProfile, ...] = (
    _DolphinProfile(
        source="retrodeck-flatpak",
        saves_root_rel="retrodeck/saves/gc/dolphin",
        config_path_rel=".var/app/net.retrodeck.retrodeck/config/dolphin-emu/Dolphin.ini",
        region_encoding="2-letter",
        wii_saves_root_rel="retrodeck/saves/wii/dolphin/title",
    ),
    _DolphinProfile(
        source="emudeck-flatpak",
        saves_root_rel=".var/app/org.DolphinEmu.dolphin-emu/data/dolphin-emu/GC",
        config_path_rel=".var/app/org.DolphinEmu.dolphin-emu/data/dolphin-emu/Config/Dolphin.ini",
        region_encoding="3-letter",
    ),
    _DolphinProfile(
        source="native",
        saves_root_rel=".local/share/dolphin-emu/GC",
        config_path_rel=".local/share/dolphin-emu/Config/Dolphin.ini",
        region_encoding="3-letter",
    ),
)


@dataclass(frozen=True, slots=True, kw_only=True)
class DolphinInstall:
    """A standalone-Dolphin install present on disk.

    `settings` is None when `Dolphin.ini` doesn't exist yet — Dolphin
    writes the INI on first launch, so a freshly-created profile is
    "discoverable but not configured." We treat this as "modern Dolphin
    defaults will apply" (GCI Folder), since that's what Dolphin will
    actually use on first run.

    `has_saves` is True iff any `.gci` file exists anywhere under
    `saves_root`. Used for active-install disambiguation when multiple
    Dolphin installs are detected. (Wii NAND presence is intentionally
    not folded into this signal in v3.6 — the install-selection layer
    is revisited when Wii sync wires into the CLI.)

    `wii_saves_root` is the per-title NAND root, or None when this
    install's profile doesn't have a verified Wii layout. Wii-only
    callers must check for None before walking.
    """

    source: DolphinSource
    saves_root: Path
    config_path: Path
    region_encoding: RegionEncoding
    settings: GameCubeMemcardSettings | None
    has_saves: bool
    wii_saves_root: Path | None = None

    @property
    def slot_a_mode(self) -> MemcardMode:
        if self.settings is None:
            return "gci_folder"  # modern Dolphin default
        return self.settings.slot_a_mode

    @property
    def slot_b_mode(self) -> MemcardMode:
        if self.settings is None:
            return "none"  # modern Dolphin default
        return self.settings.slot_b_mode


def discover_dolphin_installs(home: Path | None = None) -> list[DolphinInstall]:
    """Return every Dolphin install whose saves_root or config_path exists.

    A profile is "present" if EITHER `saves_root` is a directory OR
    `config_path` is a readable file. Both being absent → the install
    isn't on this machine. Either being present → we surface what's
    there; the caller can warn (e.g. config exists but saves dir hasn't
    been created yet, meaning Dolphin hasn't been launched).
    """
    home = home or Path.home()
    installs: list[DolphinInstall] = []
    for profile in _PROFILES:
        saves_root = home / profile.saves_root_rel
        config_path = home / profile.config_path_rel
        if not saves_root.is_dir() and not config_path.is_file():
            continue
        wii_saves_root = (
            home / profile.wii_saves_root_rel if profile.wii_saves_root_rel is not None else None
        )
        installs.append(
            DolphinInstall(
                source=profile.source,
                saves_root=saves_root,
                config_path=config_path,
                region_encoding=profile.region_encoding,
                settings=parse_gamecube_memcard_settings(config_path),
                has_saves=_has_gci_files(saves_root),
                wii_saves_root=wii_saves_root,
            )
        )
    return installs


def select_active_install(installs: list[DolphinInstall]) -> DolphinInstall | None:
    """Pick the Dolphin install ferry should sync, or None if ambiguous.

    Active-use signal: any `.gci` file under `saves_root`. See
    `domain.install_selection.select_active` for the full decision
    table.
    """
    return select_active(installs, has_active=lambda i: i.has_saves)


def _has_gci_files(saves_root: Path) -> bool:
    """True iff any `.gci` file exists anywhere under `saves_root`.

    Searches recursively because saves live two-or-three levels down
    (`<region>/Card A/<file>.gci`). Returns early on the first match.
    """
    if not saves_root.is_dir():
        return False
    try:
        for entry in saves_root.rglob("*.gci"):
            if entry.is_file():
                return True
    except OSError:
        return False
    return False
