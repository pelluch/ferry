"""Parse RetroArch's `retroarch.cfg` for save-related runtime settings.

`retroarch.cfg` is a flat key=value file (with quoted values) holding the
emulator's runtime settings. ferry only needs three of them:

- `savefile_directory` — the absolute path RetroArch writes SRMs to. May be
  overridden by RetroDECK to point outside RetroArch's own config tree (a
  surprise the first time you encounter it).
- `sort_savefiles_enable` — when true, RetroArch nests SRMs inside per-core
  subdirectories (e.g., `saves/snes9x/Mario.srm`).
- `sort_savefiles_by_content_enable` — when true, RetroArch nests SRMs
  inside subdirectories named after the ROM's containing content directory
  (e.g., `saves/snes/Mario.srm` when the ROM was loaded from `roms/snes/`).

Both sort flags can be on simultaneously (content/core/file). They can also
both be off (flat saves dir). The walker uses these to interpret subdirs.

Lifted in spirit from decky-romm-sync's
`py_modules/adapters/retroarch_config.py` (GPLv3) per DESIGN.md §6.
ferry's variant takes a Path (caller decides which cfg to parse) and
returns a richer dataclass with the resolved savefile_directory included —
the thing decky's plugin hard-codes per profile, ferry honors as the
authoritative source.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Keys we extract. Anything else in retroarch.cfg is ignored.
_SAVEFILE_DIRECTORY_KEY = "savefile_directory"
_SORT_BY_CORE_KEY = "sort_savefiles_enable"
_SORT_BY_CONTENT_KEY = "sort_savefiles_by_content_enable"


@dataclass(frozen=True, slots=True, kw_only=True)
class RetroArchSaveSettings:
    """Save-related settings parsed out of a single `retroarch.cfg`.

    `savefile_directory` is left as `None` when the cfg doesn't override it
    (or sets it to empty) — the caller falls back to RetroArch's own default
    (`<config_dir>/saves/`) in that case.
    """

    cfg_path: Path
    savefile_directory: Path | None
    sort_savefiles_enable: bool
    sort_savefiles_by_content_enable: bool


def parse_retroarch_cfg(
    cfg_path: Path, *, home: Path | None = None
) -> RetroArchSaveSettings | None:
    """Parse the three save-related keys from a `retroarch.cfg`.

    Returns `None` when the file doesn't exist or can't be read; callers
    treat that as "this RetroArch install isn't present."

    `home` is used to expand `~/...` in `savefile_directory`. Defaults to
    `Path.home()`. Tests inject a fake home to keep the parser pure.
    """
    if not cfg_path.is_file():
        return None
    try:
        text = cfg_path.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("could not read %s: %s", cfg_path, exc)
        return None

    home = home or Path.home()
    savefile_dir: Path | None = None
    sort_by_core = False
    sort_by_content = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw_value = line.partition("=")
        key = key.strip()
        value = raw_value.strip().strip('"').strip("'")
        if key == _SAVEFILE_DIRECTORY_KEY:
            if value and value != "default":
                savefile_dir = _expand_path(value, home)
        elif key == _SORT_BY_CORE_KEY:
            sort_by_core = value.lower() == "true"
        elif key == _SORT_BY_CONTENT_KEY:
            sort_by_content = value.lower() == "true"

    return RetroArchSaveSettings(
        cfg_path=cfg_path,
        savefile_directory=savefile_dir,
        sort_savefiles_enable=sort_by_core,
        sort_savefiles_by_content_enable=sort_by_content,
    )


def _expand_path(raw: str, home: Path) -> Path:
    """Expand `~/...` or `~user/...` against the given home, or return absolute Path."""
    if raw.startswith("~/"):
        return home / raw[2:]
    if raw == "~":
        return home
    return Path(raw)
