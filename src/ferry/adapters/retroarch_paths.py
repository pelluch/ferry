"""Discover RetroArch installations by parsing their `retroarch.cfg` files.

Replaces the convention-based discovery from the previous checkpoint. The
old approach assumed `<config_root>/saves/` was always the savefile
directory — true for native RetroArch and the libretro flatpak, but wrong
for RetroDECK (which sets `savefile_directory = "~/retrodeck/saves"` in
its bundled RetroArch's cfg, pointing OUTSIDE the flatpak's own tree).

The new model probes three known cfg locations, parses each, and returns
all that exist. A separate selector picks the active one for save sync —
single install: that one; multiple installs: prefer the one with files
in its configured savefile_directory (the user actively plays through it),
otherwise None and the caller surfaces the ambiguity.

The "no saves anywhere" case is handled by treating any install as
acceptable (priority order) since there's nothing to sync regardless.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ferry.adapters.retroarch_config import parse_retroarch_cfg

logger = logging.getLogger(__name__)

RetroArchSource = Literal["retrodeck-flatpak", "libretro-flatpak", "native"]


@dataclass(frozen=True, slots=True, kw_only=True)
class RetroArchInstall:
    """A RetroArch install present on disk, with its parsed save settings.

    `savefile_directory` is the *resolved absolute* path RetroArch writes
    SRMs to — either the cfg's explicit override or the convention default
    of `<config_root>/saves/`. Always a Path; callers don't need to plumb
    fallback logic.

    `has_saves` is True iff any file currently lives under
    `savefile_directory`. Used by the selector to disambiguate between
    multiple installs.
    """

    source: RetroArchSource
    cfg_path: Path
    config_root: Path
    savefile_directory: Path
    sort_savefiles_enable: bool
    sort_savefiles_by_content_enable: bool
    has_saves: bool


# (source, config_root_relative_to_home). Order is preference — RetroDECK
# first since opting into RetroDECK is an opinionated choice that suggests
# active use; libretro flatpak before native because flatpak installs are
# typically more recent than long-tail native ones.
_FLAVORS: tuple[tuple[RetroArchSource, str], ...] = (
    ("retrodeck-flatpak", ".var/app/net.retrodeck.retrodeck/config/retroarch"),
    ("libretro-flatpak", ".var/app/org.libretro.RetroArch/config/retroarch"),
    ("native", ".config/retroarch"),
)


def discover_retroarch_installs(home: Path | None = None) -> list[RetroArchInstall]:
    """Return every RetroArch install whose `retroarch.cfg` parses successfully.

    Order matches `_FLAVORS` — RetroDECK first, then libretro flatpak, then
    native — so callers can use position as a tiebreaker.
    """
    home = home or Path.home()
    installs: list[RetroArchInstall] = []
    for source, config_root_rel in _FLAVORS:
        config_root = home / config_root_rel
        cfg_path = config_root / "retroarch.cfg"
        settings = parse_retroarch_cfg(cfg_path, home=home)
        if settings is None:
            continue
        savefile_dir = settings.savefile_directory or (config_root / "saves")
        installs.append(
            RetroArchInstall(
                source=source,
                cfg_path=cfg_path,
                config_root=config_root,
                savefile_directory=savefile_dir,
                sort_savefiles_enable=settings.sort_savefiles_enable,
                sort_savefiles_by_content_enable=settings.sort_savefiles_by_content_enable,
                has_saves=_dir_has_save_files(savefile_dir),
            )
        )
    return installs


def select_active_install(installs: list[RetroArchInstall]) -> RetroArchInstall | None:
    """Pick the RetroArch install ferry should sync from, or None if ambiguous.

    Decision table:
      - 0 installs → None.
      - 1 install → that one.
      - 2+ installs:
        - Exactly one has `has_saves=True` → that one (active use signal).
        - 0 have saves → first by priority order (nothing to sync; pick any).
        - 2+ have saves → None (ambiguous; caller should ask the user).

    Returning None on ambiguity is the safe default — uploading saves from
    the wrong install would polish off the wrong copy at conflict time.
    """
    if not installs:
        return None
    if len(installs) == 1:
        return installs[0]
    with_saves = [i for i in installs if i.has_saves]
    if len(with_saves) == 1:
        return with_saves[0]
    if not with_saves:
        return installs[0]  # priority-order fallback; nothing at risk
    return None  # ambiguous


# Save-file extensions we count as evidence of active RetroArch use.
# Other files (`.directory` from KDE, stale `.log`s from standalone emulators
# accidentally dumping into the saves dir, etc.) don't count — we want a
# strong signal that someone actually saves through this install.
_SAVE_EXTENSIONS = frozenset({".srm", ".sav", ".rtc"})


def _dir_has_save_files(path: Path) -> bool:
    """True iff `path` contains any file with a recognized RetroArch save extension."""
    if not path.is_dir():
        return False
    try:
        for entry in path.rglob("*"):
            if entry.is_file() and entry.suffix.lower() in _SAVE_EXTENSIONS:
                return True
    except OSError:
        return False
    return False
