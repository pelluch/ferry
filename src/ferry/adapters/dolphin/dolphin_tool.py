"""`dolphin-tool` discovery + invocation for disc-header reads.

ferry needs each GameCube ROM's `(game_code, maker_code, region)` triple
to map ROMs to their save files (`<MAKER>-<GAMECODE>-*.gci` under
`<region>/Card A/`). For uncompressed ISO/GCM that's a 6-byte read at
offset 0; for RVZ/WIA/CISO/WBFS it requires Dolphin's decompression
machinery. Rather than reimplement, ferry shells out to `dolphin-tool
header -j -i <rom>` and parses the JSON.

Three invocation strategies, probed in order:

1. **retrodeck-flatpak** — `/app/retrodeck/components/dolphin/bin/dolphin-tool`
   inside the RetroDECK flatpak. Requires a runtime-time LD_LIBRARY_PATH
   shim because the binary loads `libevdev.so.2` from a sibling
   `components/shared-libs/` tree the flatpak runtime doesn't surface
   automatically. We dynamically discover the libdir via `find` so the
   shim survives RetroDECK bumping its bundled GNOME platform version.
2. **emudeck-flatpak** — `/app/bin/dolphin-tool` inside
   `org.DolphinEmu.dolphin-emu`. No shim needed (standard runtime).
3. **system-path** — `dolphin-tool` (or `dolphin-emu-tool` on some
   distros) on `PATH`. Native standalone Dolphin install.

Disc-header lookups are persisted in a JSON cache keyed by
`(path, mtime_ns, size)` so a second `ferry sync` doesn't re-shell out
once per ROM. The cache is inherently safe: a ROM whose mtime or size
changes obsoletes its entry.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ferry.domain.user_dirs import cache_dir

logger = logging.getLogger(__name__)

DolphinToolSource = Literal[
    "retrodeck-in-sandbox", "retrodeck-flatpak", "emudeck-flatpak", "system-path"
]

_RETRODECK_APP_ID = "net.retrodeck.retrodeck"
_EMUDECK_APP_ID = "org.DolphinEmu.dolphin-emu"
_RETRODECK_TOOL_PATH = "/app/retrodeck/components/dolphin/bin/dolphin-tool"
_EMUDECK_TOOL_PATH = "/app/bin/dolphin-tool"
_SYSTEM_BINARY_NAMES = ("dolphin-tool", "dolphin-emu-tool")
_FLATPAK_INFO_PATH = Path("/.flatpak-info")

# Shell snippet for the RetroDECK flatpak invocation. RetroDECK ships
# extra runtime libraries (currently libevdev) under its own
# `components/shared-libs/<runtime>/` tree; we add every directory there
# to LD_LIBRARY_PATH so future gaps don't require code changes.
_RETRODECK_SHELL = (
    "LIBDIRS=$(find /app/retrodeck/components/shared-libs -type d 2>/dev/null "
    '| tr "\\n" ":"); '
    f'exec env LD_LIBRARY_PATH="${{LIBDIRS}}${{LD_LIBRARY_PATH:-}}" '
    f'{_RETRODECK_TOOL_PATH} "$@"'
)


@dataclass(frozen=True, slots=True, kw_only=True)
class DiscHeader:
    """The subset of `dolphin-tool header -j` output ferry consumes.

    `game_code` + `maker_code` together form Dolphin's 6-char `game_id`
    (e.g. `GM8E01` for Metroid Prime US Rev 2: gamecode `GM8E`, maker
    `01`). They're the prefix of every `.gci` filename Dolphin generates
    for that game.

    `region` is one of Dolphin's `Region` enum values: `NTSC-U`, `NTSC-J`,
    `PAL`, `NTSC-K`. The walker maps this to a folder name based on the
    install's `region_encoding` (3-letter `USA/JAP/EUR` for native /
    EmuDeck, 2-letter `US/JP/EU` for RetroDECK).
    """

    game_code: str
    maker_code: str
    region: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DolphinTool:
    """A discovered, invokable dolphin-tool binary.

    `argv_prefix` is the argv prefix prepended to every dolphin-tool
    invocation; for RetroDECK it includes the `flatpak run --command=sh
    -c <shim>` setup, for native it's just the binary path.
    """

    source: DolphinToolSource
    label: str
    argv_prefix: tuple[str, ...]

    def invoke(self, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
        """Run dolphin-tool with the given args and return the completed process.

        Captures both stdout and stderr as text. Raises `subprocess.TimeoutExpired`
        on timeout; otherwise returns even on non-zero exit (caller decides
        how to handle errors).
        """
        return subprocess.run(  # noqa: S603 — argv-form, no shell injection
            [*self.argv_prefix, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def read_header(self, rom_path: Path) -> DiscHeader | None:
        """Run `dolphin-tool header -j -i <rom>` and parse the JSON.

        Returns None on non-zero exit, parse failure, or missing fields.
        Logs a warning so failures aren't silent — callers see None and
        skip the ROM.
        """
        result = self.invoke("header", "-j", "-i", str(rom_path))
        if result.returncode != 0:
            logger.warning(
                "dolphin-tool failed for %s (exit %d): %s",
                rom_path,
                result.returncode,
                result.stderr.strip() or result.stdout.strip(),
            )
            return None
        return _parse_header_json(result.stdout)


def _parse_header_json(raw: str) -> DiscHeader | None:
    """Parse dolphin-tool's `header -j` JSON output into a DiscHeader."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("dolphin-tool header output not valid JSON: %s", exc)
        return None
    if not isinstance(data, dict):
        return None
    game_id = data.get("game_id")
    region = data.get("region")
    if not isinstance(game_id, str) or not isinstance(region, str):
        return None
    if len(game_id) != 6:
        # GameCube/Wii game IDs are always exactly 6 chars (4 gamecode + 2 maker).
        # Anything else means dolphin-tool returned something we don't understand.
        return None
    return DiscHeader(
        game_code=game_id[:4],
        maker_code=game_id[4:6],
        region=region,
    )


def discover_dolphin_tool(
    home: Path | None = None,
    *,
    flatpak_dirs: tuple[Path, ...] | None = None,
    path_env: Mapping[str, str] | None = None,
    flatpak_info_path: Path | None = None,
) -> DolphinTool | None:
    """Find the first usable dolphin-tool, in priority order.

    Returns None when none of the strategies are available. Probing is
    cheap (filesystem checks + `which`) but never invokes the tool —
    callers do that explicitly via `read_header`.

    Args are injectable for tests:
      - `flatpak_dirs`: roots to scan for installed flatpak apps.
        Defaults to standard system + user flatpak install locations.
      - `path_env`: PATH lookup environment. Defaults to `os.environ`.
      - `flatpak_info_path`: where to look for `/.flatpak-info` (used
        to detect "we're running inside RetroDECK's sandbox").
    """
    home = home or Path.home()
    flatpak_dirs = flatpak_dirs or (
        home / ".local/share/flatpak/app",
        Path("/var/lib/flatpak/app"),
    )
    flatpak_info_path = flatpak_info_path or _FLATPAK_INFO_PATH

    # Are we already running INSIDE the RetroDECK flatpak's sandbox? If
    # so, prefer direct in-sandbox invocation: `/app/retrodeck/...` is
    # accessible without `flatpak run` (we're already there), and we
    # don't need talk-name=org.freedesktop.Flatpak (which RetroDECK's
    # manifest lacks, so `flatpak-spawn --host` wouldn't work anyway).
    # This is the only path that works for ferry running inside an
    # ES-DE launch wrapper: RetroDECK ES-DE → sandboxed shell → ferry.
    if _running_in_retrodeck_sandbox(flatpak_info_path):
        return DolphinTool(
            source="retrodeck-in-sandbox",
            label=f"in-sandbox {_RETRODECK_TOOL_PATH}",
            argv_prefix=(
                "sh",
                "-c",
                _RETRODECK_SHELL,
                "_",  # placeholder for $0
            ),
        )

    if _flatpak_app_installed(_RETRODECK_APP_ID, flatpak_dirs):
        return DolphinTool(
            source="retrodeck-flatpak",
            label=f"flatpak {_RETRODECK_APP_ID}",
            argv_prefix=(
                "flatpak",
                "run",
                "--command=sh",
                _RETRODECK_APP_ID,
                "-c",
                _RETRODECK_SHELL,
                "_",  # placeholder for $0; subsequent args become $1, $2, ...
            ),
        )

    if _flatpak_app_installed(_EMUDECK_APP_ID, flatpak_dirs):
        return DolphinTool(
            source="emudeck-flatpak",
            label=f"flatpak {_EMUDECK_APP_ID}",
            argv_prefix=(
                "flatpak",
                "run",
                f"--command={_EMUDECK_TOOL_PATH}",
                _EMUDECK_APP_ID,
            ),
        )

    env = path_env if path_env is not None else os.environ
    path_value = env.get("PATH", "")
    for name in _SYSTEM_BINARY_NAMES:
        binary = shutil.which(name, path=path_value)
        if binary is not None:
            return DolphinTool(
                source="system-path",
                label=binary,
                argv_prefix=(binary,),
            )
    return None


def _flatpak_app_installed(app_id: str, flatpak_dirs: tuple[Path, ...]) -> bool:
    """True iff `app_id` is installed in any of the given flatpak install roots."""
    return any((root / app_id).is_dir() for root in flatpak_dirs)


def _running_in_retrodeck_sandbox(flatpak_info_path: Path) -> bool:
    """True iff we're running inside the RetroDECK flatpak's sandbox.

    Flatpak sandboxes always have `/.flatpak-info` containing INI-style
    metadata including `name=<app-id>`. Outside a sandbox the file
    doesn't exist. Used to detect ES-DE-via-launch-wrapper invocations
    where ferry runs inside RetroDECK and can call dolphin-tool
    directly via `/app/...` paths instead of going through `flatpak run`
    (which can't escape back to the sandbox we're already in).
    """
    if not flatpak_info_path.is_file():
        return False
    try:
        text = flatpak_info_path.read_text()
    except OSError:
        return False
    # Robust against [Application] section vs flat layout — we just need
    # to see this app's name appear as a key=value somewhere.
    return f"name={_RETRODECK_APP_ID}" in text


# ---------------------------------------------------------------------------
# Persistent header cache
# ---------------------------------------------------------------------------


def default_cache_path(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the dolphin-headers cache path under the user's cache dir."""
    return cache_dir(env) / "ferry" / "dolphin-headers.json"


class DiscHeaderCache:
    """JSON-backed (path, mtime_ns, size) → DiscHeader cache.

    Cache hit only when both mtime and size match — either changing
    obsoletes the entry. Format is a flat dict keyed by absolute path
    string; small enough that we re-write the whole file on each `put`,
    keeping persistence simple.

    Missing or malformed cache file is treated as empty; ferry will
    re-shell out and rebuild the cache from scratch.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._loaded = False
        self._entries: dict[str, dict[str, Any]] = {}

    def get(self, rom_path: Path) -> DiscHeader | None:
        """Return cached header iff mtime + size match the file on disk."""
        self._ensure_loaded()
        entry = self._entries.get(str(rom_path))
        if entry is None:
            return None
        try:
            stat = rom_path.stat()
        except OSError:
            return None
        if entry.get("mtime_ns") != stat.st_mtime_ns or entry.get("size") != stat.st_size:
            return None
        try:
            return DiscHeader(
                game_code=entry["game_code"],
                maker_code=entry["maker_code"],
                region=entry["region"],
            )
        except KeyError:
            return None

    def put(self, rom_path: Path, header: DiscHeader) -> None:
        """Persist a header keyed by the file's current mtime + size."""
        self._ensure_loaded()
        try:
            stat = rom_path.stat()
        except OSError as exc:
            logger.warning("could not stat %s for cache: %s", rom_path, exc)
            return
        self._entries[str(rom_path)] = {
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            "game_code": header.game_code,
            "maker_code": header.maker_code,
            "region": header.region,
        }
        self._write()

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._path.is_file():
            return
        try:
            raw = self._path.read_text()
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("could not load dolphin header cache %s: %s", self._path, exc)
            return
        if isinstance(data, dict):
            self._entries = {k: v for k, v in data.items() if isinstance(v, dict)}

    def _write(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._entries, indent=2, sort_keys=True))
            tmp.replace(self._path)
        except OSError as exc:
            logger.warning("could not write dolphin header cache %s: %s", self._path, exc)
