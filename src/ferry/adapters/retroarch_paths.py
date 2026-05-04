"""Discover the on-disk location of RetroArch's saves directory.

RetroArch stores SRM/state files under its config tree, but the config tree
itself lives in different places depending on how RetroArch was installed.
The three installations we recognize, in detection priority order:

1. **RetroDECK** — bundles its own RetroArch inside the
   `net.retrodeck.retrodeck` flatpak. The user has explicitly opted into
   RetroDECK's curated install, so this wins ahead of any libretro flatpak
   they might also have.
2. **libretro flatpak** (`org.libretro.RetroArch`) — what EmuDeck installs
   and what most "I just want RetroArch" Linux users on Steam Deck end up
   with.
3. **Native** (`~/.config/retroarch/`) — distro-package or AppImage installs
   that put their config under XDG defaults.

Multi-installation environments are real (a user might keep RetroDECK for
RetroDECK-only games and a separate native RetroArch for handheld play),
but ferry's save-sync model treats RetroArch as a single backend. The user
picks one — explicitly via config in a later checkpoint, or implicitly via
the priority order here. Shipping a "two RetroArchs at once" mode without
clear semantics around which one owns a save would be a footgun.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

RetroArchSource = Literal["retrodeck-flatpak", "libretro-flatpak", "native"]


@dataclass(frozen=True, slots=True)
class RetroArchInstall:
    """A RetroArch installation found on disk.

    `saves_dir` is the absolute path to RetroArch's saves tree
    (`<config_root>/saves/`). Saves themselves live one or more levels deeper
    keyed by core (e.g., `saves/snes9x/Super Mario World.srm`); ferry's
    SaveBackend handles that walking.
    """

    saves_dir: Path
    source: RetroArchSource


# Detection table — labels must match RetroArchSource literal values.
# Each entry is (source, config-root-relative-to-home).
_FLAVORS: tuple[tuple[RetroArchSource, str], ...] = (
    ("retrodeck-flatpak", ".var/app/net.retrodeck.retrodeck/config/retroarch"),
    ("libretro-flatpak", ".var/app/org.libretro.RetroArch/config/retroarch"),
    ("native", ".config/retroarch"),
)


def discover_retroarch_saves(home: Path | None = None) -> RetroArchInstall | None:
    """Probe known RetroArch install locations and return the first match.

    A match requires the saves directory itself to exist as a directory —
    not just the config root. RetroArch only creates `saves/` once a save
    has been written, so a fresh install with no plays yet won't be
    detected. That's the right call: ferry has nothing to sync until the
    user has actually saved something, and a missing dir is the most
    reliable "no saves yet" signal across all three install flavors.
    """
    home = home or Path.home()
    for source, config_root in _FLAVORS:
        saves_dir = home / config_root / "saves"
        if saves_dir.is_dir():
            return RetroArchInstall(saves_dir=saves_dir, source=source)
    return None
