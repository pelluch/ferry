"""Discover Cemu (Wii U emulator) installations.

Each install has two pieces of layout ferry needs:

- **wiiu_saves_root** — the parent of `00050000/<title_low>/`. Where
  ferry walks for Wii U save folders. Cemu's on-disk layout is
  `mlc01/usr/save/00050000/<title_low>/` internally; RetroDECK
  redirects this to a flat `saves/wiiu/cemu/` tree.
- **data_dir** — the Cemu data directory that holds `keys.txt`. The
  `cemu --extract` invocation (see `cemu_tool`) must run with this as
  its cwd or it segfaults — Cemu resolves `keys.txt` relative to the
  working directory.

Probed sources:

- **retrodeck-flatpak** — saves at `~/retrodeck/saves/wiiu/cemu/`,
  data dir at `~/.var/app/net.retrodeck.retrodeck/data/Cemu`. RetroDECK
  bundles Cemu as a flatpak component and redirects its save tree.
- **native** — placeholder. Native standalone Cemu on Linux keeps its
  data under `~/.local/share/Cemu`; not probed yet (v5 targets
  RetroDECK first — see DESIGN.md §7 v5). The literal is reserved so
  the native profile can be filled in without a schema change.

EmuDeck installs Cemu as a separate flatpak; that layout isn't pinned
down yet and isn't probed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ferry.domain.install_selection import select_active

logger = logging.getLogger(__name__)

CemuSource = Literal["retrodeck-flatpak", "native"]

# Wii U standard-game title-type prefix. Cemu nests per-game saves under
# `<wiiu_saves_root>/00050000/<title_low>/`. System titles (`00050010`)
# and the device-level `system/` tree are intentionally out of scope —
# see DESIGN.md §7 v5.
TITLE_TYPE_GAME = "00050000"


@dataclass(frozen=True, slots=True, kw_only=True)
class _CemuProfile:
    """Static layout description for one CemuSource flavor.

    Paths are relative to `home`; expanded at discovery time.
    """

    source: CemuSource
    wiiu_saves_root_rel: str
    data_dir_rel: str


# Order is preference for active-install selection. Only RetroDECK is
# filled in for v5; native is a reserved placeholder.
_PROFILES: tuple[_CemuProfile, ...] = (
    _CemuProfile(
        source="retrodeck-flatpak",
        wiiu_saves_root_rel="retrodeck/saves/wiiu/cemu",
        data_dir_rel=".var/app/net.retrodeck.retrodeck/data/Cemu",
    ),
)


@dataclass(frozen=True, slots=True, kw_only=True)
class CemuInstall:
    """A Cemu install present on disk.

    `has_saves` is True iff any per-game save directory exists under
    `wiiu_saves_root/00050000/`. Used for active-install
    disambiguation when multiple installs are detected.

    `data_dir` is where `keys.txt` lives; `cemu_tool` runs the
    extractor with this as cwd. It may not exist on disk yet (Cemu
    creates it on first launch) — callers that need `keys.txt` should
    check separately.
    """

    source: CemuSource
    wiiu_saves_root: Path
    data_dir: Path
    has_saves: bool

    @property
    def games_root(self) -> Path:
        """`wiiu_saves_root/00050000` — parent of per-game save folders."""
        return self.wiiu_saves_root / TITLE_TYPE_GAME


def discover_cemu_installs(home: Path | None = None) -> list[CemuInstall]:
    """Return every Cemu install whose saves root or data dir exists.

    A profile is "present" if EITHER `wiiu_saves_root` is a directory
    OR `data_dir` is a directory — mirrors `discover_dolphin_installs`'s
    either-signal rule so a configured-but-unused install still surfaces.
    """
    home = home or Path.home()
    installs: list[CemuInstall] = []
    for profile in _PROFILES:
        wiiu_saves_root = home / profile.wiiu_saves_root_rel
        data_dir = home / profile.data_dir_rel
        if not wiiu_saves_root.is_dir() and not data_dir.is_dir():
            continue
        installs.append(
            CemuInstall(
                source=profile.source,
                wiiu_saves_root=wiiu_saves_root,
                data_dir=data_dir,
                has_saves=_has_wiiu_saves(wiiu_saves_root),
            )
        )
    return installs


def select_active_install(installs: list[CemuInstall]) -> CemuInstall | None:
    """Pick the Cemu install ferry should sync, or None if ambiguous.

    Active-use signal: any per-game save folder under
    `00050000/`. See `domain.install_selection.select_active`.
    """
    return select_active(installs, has_active=lambda i: i.has_saves)


def _has_wiiu_saves(wiiu_saves_root: Path) -> bool:
    """True iff any per-game save directory exists under `00050000/`.

    Cemu creates `00050000/<title_low>/` only when a game has written
    a save, so a single subdirectory is sufficient evidence of use.
    """
    games_root = wiiu_saves_root / TITLE_TYPE_GAME
    if not games_root.is_dir():
        return False
    try:
        for entry in games_root.iterdir():
            if entry.is_dir():
                return True
    except OSError:
        return False
    return False
