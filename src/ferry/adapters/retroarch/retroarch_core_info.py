"""Read RetroArch's `<core>_libretro.info` files for canonical core naming.

RetroArch stores per-core metadata in `*.info` files alongside (or in a
sibling `info/` dir to) the `*.so` core libraries. Each .info file has a
`corename` field — the human-friendly name RetroArch uses as the per-core
save subdirectory when `sort_savefiles_enable=true`. ferry needs the
forward map (core_so_prefix → corename, e.g., `snes9x` → `Snes9x`) for
download path resolution AND the reverse (corename → core_so_prefix) for
walking saves and producing the matching emulator label.

Without this mapping the casing drifts: decky-romm-sync uploads with the
lowercase prefix (`retroarch-snes9x`), but RetroArch creates the save
subdir as `Snes9x/`. ferry would then write downloads to `snes9x/` (the
lowercase label as-is) while RA writes new saves to `Snes9x/`, leaving
two parallel directories. Reading the .info files makes both directions
authoritative.

The `.info` format is straightforward `key = "value"` pairs (sometimes
unquoted, with `# comments` and blank lines mixed in). We parse only what
we need (`corename`); other fields are ignored.

Lifted in spirit from decky-romm-sync's `adapters/retroarch_core_info.py`
(GPLv3) per DESIGN.md §6. Adapted for ferry's three install flavors via
`RetroArchInstall.core_info_candidates`.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ferry.adapters.retroarch.retroarch_paths import RetroArchInstall

logger = logging.getLogger(__name__)

# Core .so files (and matching .info) end in `_libretro` by convention.
# `snes9x_libretro.info` → core_so_prefix is `snes9x`.
_CORE_SO_SUFFIX = "_libretro"
_INFO_EXT = ".info"


def parse_core_info(text: str) -> dict[str, str]:
    """Parse the `key = "value"` pairs from a `*.info` file body.

    Tolerant of blank lines, `# comments`, unquoted values, and extra
    whitespace. Values are stripped of one layer of `"` or `'` quoting.
    Returns a flat dict; nested/repeated keys aren't a thing in this
    format.
    """
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            result[key] = value
    return result


class CoreInfoIndex:
    """Forward + reverse maps between `core_so_prefix` and `corename`.

    Built lazily on first access by walking the install's `core_info_candidates`
    in order, parsing every `*_libretro.info` in the first existing dir.
    Cached for the lifetime of the instance — .info files only change when
    the user reinstalls/updates RetroArch, which doesn't happen mid-sync.

    Identity fallback: if a core isn't in the index (e.g., user has a custom
    core not present in any candidate dir), `forward(prefix)` returns `prefix`
    unchanged and `reverse(corename)` returns `corename` unchanged. The casing
    bug recurs for unknown cores but the system still functions.
    """

    def __init__(self, install: RetroArchInstall) -> None:
        self._install = install
        self._loaded = False
        self._forward: dict[str, str] = {}
        self._reverse: dict[str, str] = {}

    def forward(self, core_so_prefix: str) -> str:
        """`snes9x` → `Snes9x`, falling back to the input on miss."""
        self._ensure_loaded()
        return self._forward.get(core_so_prefix, core_so_prefix)

    def reverse(self, corename: str) -> str:
        """`Snes9x` → `snes9x`, falling back to the input on miss."""
        self._ensure_loaded()
        return self._reverse.get(corename, corename)

    def has_data(self) -> bool:
        """True iff at least one .info file was successfully parsed."""
        self._ensure_loaded()
        return bool(self._forward)

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        info_dir = self._find_info_dir()
        if info_dir is None:
            logger.debug(
                "no readable core_info_candidates for %s install",
                self._install.source,
            )
            return
        self._scan(info_dir)

    def _find_info_dir(self) -> Path | None:
        for candidate in self._install.core_info_candidates:
            if not candidate.is_dir():
                continue
            try:
                # Confirm there's at least one .info file before committing.
                for entry in candidate.iterdir():
                    if entry.is_file() and entry.suffix.lower() == _INFO_EXT:
                        return candidate
            except OSError as exc:
                logger.warning("could not list %s: %s", candidate, exc)
                continue
        return None

    def _scan(self, info_dir: Path) -> None:
        try:
            entries = sorted(info_dir.iterdir())
        except OSError as exc:
            logger.warning("could not iterate %s: %s", info_dir, exc)
            return
        for entry in entries:
            if not entry.is_file() or entry.suffix.lower() != _INFO_EXT:
                continue
            stem = entry.stem
            if not stem.endswith(_CORE_SO_SUFFIX):
                # Non-libretro file (or naming convention we don't recognize) — skip.
                continue
            prefix = stem[: -len(_CORE_SO_SUFFIX)]
            try:
                parsed = parse_core_info(entry.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError) as exc:
                logger.warning("could not read %s: %s", entry, exc)
                continue
            corename = parsed.get("corename")
            if not corename:
                continue
            self._forward[prefix] = corename
            # If two cores share a corename (rare but possible — e.g., bsnes
            # variants), last write wins. The forward direction stays correct
            # because each .so prefix is unique.
            self._reverse[corename] = prefix
