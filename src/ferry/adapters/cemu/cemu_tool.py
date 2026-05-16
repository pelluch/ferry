"""`cemu` discovery + invocation for Wii U title-ID reads.

ferry needs each Wii U ROM's title ID to map ROMs to their Cemu save
folders (`00050000/<title_low>/`). Wii U disc images (`.wud`/`.wux`)
are encrypted; reading the title ID means decrypting the disc, which
needs the Wii U key set. Rather than reimplement that, ferry shells
out to Cemu's built-in extractor — `cemu --extract <rom> --path
meta/meta.xml` — which decrypts using the user's own `keys.txt` and
prints `meta/meta.xml` to stdout. ferry parses `<title_id>` out of it.
This is the Wii U analogue of `dolphin-tool header` for GameCube/Wii.

**Exit code is not a success signal.** Cemu's extractor:
  - prints the file and exits 0 on success;
  - prints `Unable to open "%s"` (an unformatted format string — a
    Cemu bug) and STILL exits 0 when the ROM can't be opened or the
    title key is missing from `keys.txt`;
  - segfaults (exit 139) when `keys.txt` is absent from the cwd.
So `extract_title_id` ignores the return code for the success case and
instead requires the stdout to parse as `meta.xml` with a 16-hex
`<title_id>`. A crash (or our cwd-guard's exit 91) is still treated as
a hard failure.

**cwd matters.** Cemu auto-detects its data directory by walking the
filesystem from its working directory; if the cwd has no `keys.txt`
file, that walk recurses unboundedly and stack-overflows (SIGSEGV,
exit 139) — that, not a clean error, is what "keys.txt missing" looks
like. So the cwd must be a directory holding a *resolvable* `keys.txt`
(a dangling symlink doesn't count — RetroDECK pre-creates one pointing
at its BIOS dir). The RetroDECK invocations `cd` into the Cemu data
dir (passed as the shell snippet's `$1`, never string-interpolated, so
no injection); the system-path invocation passes `cwd=` to subprocess.
`extract_title_id` pre-flights the `keys.txt` check so a missing one
is a clean failure rather than a crash.

Three invocation strategies, probed in order — mirrors
`dolphin_tool.discover_dolphin_tool`:

1. **retrodeck-in-sandbox** — ferry already runs inside RetroDECK's
   flatpak (ES-DE launch wrapper). Invoke `/app/retrodeck/.../cemu`
   directly with an LD_LIBRARY_PATH shim.
2. **retrodeck-flatpak** — `flatpak run net.retrodeck.retrodeck`,
   same shim.
3. **system-path** — `cemu` on PATH (native install).

Title-ID lookups are persisted in a JSON cache keyed by
`(path, mtime_ns, size)` so a second sync doesn't re-shell out — and
`cemu --extract` on a multi-GB `.wux` is far from free.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ferry.domain.user_dirs import cache_dir

logger = logging.getLogger(__name__)

CemuToolSource = Literal["retrodeck-in-sandbox", "retrodeck-flatpak", "system-path"]

_RETRODECK_APP_ID = "net.retrodeck.retrodeck"
_RETRODECK_CEMU_BIN = "/app/retrodeck/components/cemu/bin/Cemu_relwithdebinfo"
_RETRODECK_SHARED_LIBS = "/app/retrodeck/components/shared-libs"
_SYSTEM_BINARY_NAMES = ("cemu", "Cemu")
_FLATPAK_INFO_PATH = Path("/.flatpak-info")

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

# sh -c snippet for the RetroDECK invocations. `$1` is the Cemu data
# dir (where `keys.txt` lives) — we `cd` there (exit 91 if it's gone,
# distinct from Cemu's own 0/139 codes), shift it off, then build
# LD_LIBRARY_PATH from every directory under the shared-libs tree
# (covers libSPIRV / libwx* / libzip / libpugixml / libboost / libhidapi
# and survives RetroDECK bumping its bundled platform version). The
# remaining args are Cemu's.
_RETRODECK_SHELL = (
    'cd "$1" || exit 91; shift; '
    f'LIBDIRS=$(find {_RETRODECK_SHARED_LIBS} -type d 2>/dev/null | tr "\\n" ":"); '
    f'exec env LD_LIBRARY_PATH="${{LIBDIRS}}${{LD_LIBRARY_PATH:-}}" {_RETRODECK_CEMU_BIN} "$@"'
)

# Distinctive exit code our `cd` guard raises when the keys dir is
# missing — lets `extract_title_id` give an actionable message.
_CWD_GUARD_EXIT = 91


@dataclass(frozen=True, slots=True, kw_only=True)
class WiiUTitle:
    """A Wii U title ID extracted from a ROM's `meta/meta.xml`.

    `title_id` is the full 16-hex (8-byte) ID, normalized uppercase
    (e.g. `00050000101C9400`). Cemu's save tree splits it: the high
    half is the title type (`00050000` for standard games), the low
    half — lowercased — is the per-game save folder name
    (`00050000/101c9400/`).
    """

    title_id: str

    @property
    def title_id_high(self) -> str:
        """Title-type prefix — `00050000` for standard games."""
        return self.title_id[:8]

    @property
    def title_id_low(self) -> str:
        """Per-game save folder name, lowercased (e.g. `101c9400`)."""
        return self.title_id[8:].lower()


@dataclass(frozen=True, slots=True, kw_only=True)
class CemuTool:
    """A discovered, invokable `cemu` binary.

    `argv_prefix` is prepended to every invocation. For the RetroDECK
    sources it ends with the `sh -c <snippet> _` setup and the keys dir
    is appended as the snippet's `$1`; for system-path it's just the
    binary and the keys dir is passed to subprocess via `cwd=`.
    `cwd_via_snippet` records which.
    """

    source: CemuToolSource
    label: str
    argv_prefix: tuple[str, ...]
    cwd_via_snippet: bool

    def invoke(
        self, *args: str, keys_dir: Path, timeout: float = 120.0
    ) -> subprocess.CompletedProcess[str]:
        """Run `cemu` with the given args, resolving `keys.txt` via *keys_dir*.

        Captures stdout/stderr as text. Raises `subprocess.TimeoutExpired`
        on timeout; otherwise returns even on non-zero / crash exit
        (caller decides — see the class docstring on why the exit code
        can't be trusted).
        """
        if self.cwd_via_snippet:
            argv = [*self.argv_prefix, str(keys_dir), *args]
            cwd: str | None = None
        else:
            argv = [*self.argv_prefix, *args]
            cwd = str(keys_dir)
        return subprocess.run(  # noqa: S603 — argv-form, no shell injection
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=cwd,
        )

    def extract_title_id(self, rom_path: Path, *, keys_dir: Path) -> WiiUTitle | None:
        """Run `cemu --extract <rom> --path meta/meta.xml` and parse the title ID.

        Returns None on timeout, process crash, or output that doesn't
        parse as `meta.xml` with a valid `<title_id>`. Logs a warning
        so failures aren't silent — the caller sees None and falls back
        (manual override) or skips the ROM.
        """
        # Pre-flight: Cemu stack-overflows (SIGSEGV) if its working
        # directory has no `keys.txt` file. `.is_file()` follows
        # symlinks, so RetroDECK's dangling pre-created symlink (target
        # not yet populated under <retrodeck>/bios/cemu/) correctly
        # reads as absent. Catch it here for an actionable message
        # instead of letting Cemu crash.
        keys_txt = keys_dir / "keys.txt"
        if not keys_txt.is_file():
            logger.warning(
                "cemu --extract: no usable keys.txt at %s — Cemu cannot decrypt "
                "Wii U ROMs without it. On RetroDECK, place your Cemu keys at "
                "<retrodeck>/bios/cemu/keys.txt (RetroDECK symlinks it into the "
                "Cemu data dir).",
                keys_txt,
            )
            return None
        try:
            result = self.invoke("-e", str(rom_path), "-p", "meta/meta.xml", keys_dir=keys_dir)
        except subprocess.TimeoutExpired:
            logger.warning("cemu --extract timed out for %s", rom_path)
            return None
        if result.returncode == _CWD_GUARD_EXIT:
            logger.warning(
                "cemu --extract: keys dir %s does not exist (keys.txt unavailable)", keys_dir
            )
            return None
        if result.returncode != 0:
            logger.warning(
                "cemu --extract crashed for %s (exit %d) — keys.txt likely missing "
                "from %s, or the Cemu environment is broken: %s",
                rom_path,
                result.returncode,
                keys_dir,
                (result.stderr or result.stdout).strip() or "<no output>",
            )
            return None
        title = _parse_meta_xml(result.stdout)
        if title is None:
            # Exit 0 but no usable meta.xml — Cemu's silent-failure path
            # (`Unable to open "%s"`): ROM unreadable, or the title key
            # isn't in keys.txt, or the format isn't WUD/WUX.
            logger.warning(
                "cemu --extract produced no usable meta.xml for %s "
                "(missing title key in keys.txt, or unsupported ROM format)",
                rom_path,
            )
        return title


def lookup_wiiu_title(
    rom_path: Path,
    tool: CemuTool,
    cache: WiiUTitleCache | None,
    *,
    keys_dir: Path,
) -> WiiUTitle | None:
    """Cache-first title-ID lookup. Populates the cache on a fresh read.

    Callers resolving titles for many ROMs should pass a shared cache —
    `cemu --extract` is expensive enough that re-reading every sync
    would be a real cost.
    """
    if cache is not None:
        cached = cache.get(rom_path)
        if cached is not None:
            return cached
    title = tool.extract_title_id(rom_path, keys_dir=keys_dir)
    if title is not None and cache is not None:
        cache.put(rom_path, title)
    return title


def _parse_meta_xml(raw: str) -> WiiUTitle | None:
    """Parse a Wii U `meta.xml` document and pull out `<title_id>`.

    Returns None when the text isn't XML, has no `<title_id>` element,
    or that element isn't a 16-hex string.
    """
    raw = raw.strip()
    if not raw:
        return None
    try:
        root = ET.fromstring(raw)  # noqa: S314 — Cemu-produced, trusted local input
    except ET.ParseError:
        return None
    node = root.find("title_id")
    if node is None or node.text is None:
        return None
    tid = node.text.strip()
    if len(tid) != 16 or any(c not in _HEX_DIGITS for c in tid):
        return None
    return WiiUTitle(title_id=tid.upper())


def discover_cemu_tool(
    *,
    flatpak_dirs: tuple[Path, ...] | None = None,
    path_env: Mapping[str, str] | None = None,
    flatpak_info_path: Path | None = None,
    home: Path | None = None,
) -> CemuTool | None:
    """Find the first usable `cemu`, in priority order.

    Returns None when no strategy is available. Probing is cheap
    (filesystem checks + `which`) and never invokes Cemu. Mirrors
    `dolphin_tool.discover_dolphin_tool`; args are injectable for tests.
    """
    home = home or Path.home()
    flatpak_dirs = flatpak_dirs or (
        home / ".local/share/flatpak/app",
        Path("/var/lib/flatpak/app"),
    )
    flatpak_info_path = flatpak_info_path or _FLATPAK_INFO_PATH

    if _running_in_retrodeck_sandbox(flatpak_info_path):
        return CemuTool(
            source="retrodeck-in-sandbox",
            label=f"in-sandbox {_RETRODECK_CEMU_BIN}",
            argv_prefix=("sh", "-c", _RETRODECK_SHELL, "_"),
            cwd_via_snippet=True,
        )

    if _flatpak_app_installed(_RETRODECK_APP_ID, flatpak_dirs):
        return CemuTool(
            source="retrodeck-flatpak",
            label=f"flatpak {_RETRODECK_APP_ID}",
            argv_prefix=(
                "flatpak",
                "run",
                "--command=sh",
                _RETRODECK_APP_ID,
                "-c",
                _RETRODECK_SHELL,
                "_",  # placeholder $0; keys dir becomes $1, then Cemu args
            ),
            cwd_via_snippet=True,
        )

    env = path_env if path_env is not None else os.environ
    path_value = env.get("PATH", "")
    for name in _SYSTEM_BINARY_NAMES:
        binary = shutil.which(name, path=path_value)
        if binary is not None:
            return CemuTool(
                source="system-path",
                label=binary,
                argv_prefix=(binary,),
                cwd_via_snippet=False,
            )
    return None


def _flatpak_app_installed(app_id: str, flatpak_dirs: tuple[Path, ...]) -> bool:
    """True iff `app_id` is installed in any of the given flatpak roots."""
    return any((root / app_id).is_dir() for root in flatpak_dirs)


def _running_in_retrodeck_sandbox(flatpak_info_path: Path) -> bool:
    """True iff we're running inside the RetroDECK flatpak's sandbox.

    Flatpak sandboxes always have `/.flatpak-info` with `name=<app-id>`.
    Mirrors `dolphin_tool._running_in_retrodeck_sandbox`.
    """
    if not flatpak_info_path.is_file():
        return False
    try:
        text = flatpak_info_path.read_text()
    except OSError:
        return False
    return f"name={_RETRODECK_APP_ID}" in text


# ---------------------------------------------------------------------------
# Persistent title-ID cache
# ---------------------------------------------------------------------------


def default_cache_path(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the Wii U title-ID cache path under the user's cache dir."""
    return cache_dir(env) / "ferry" / "wiiu-titles.json"


class WiiUTitleCache:
    """JSON-backed `(path, mtime_ns, size)` → WiiUTitle cache.

    Cache hit only when both mtime and size match — either changing
    obsoletes the entry. Flat dict keyed by absolute path string;
    rewritten whole on each `put`. Missing/malformed file → treated as
    empty. Mirrors `dolphin_tool.DiscHeaderCache`.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._loaded = False
        self._entries: dict[str, dict[str, Any]] = {}

    def get(self, rom_path: Path) -> WiiUTitle | None:
        """Return cached title iff mtime + size match the file on disk."""
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
        title_id = entry.get("title_id")
        if not isinstance(title_id, str):
            return None
        return WiiUTitle(title_id=title_id)

    def put(self, rom_path: Path, title: WiiUTitle) -> None:
        """Persist a title keyed by the file's current mtime + size."""
        self._ensure_loaded()
        try:
            stat = rom_path.stat()
        except OSError as exc:
            logger.warning("could not stat %s for cache: %s", rom_path, exc)
            return
        self._entries[str(rom_path)] = {
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            "title_id": title.title_id,
        }
        self._write()

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("could not load Wii U title cache %s: %s", self._path, exc)
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
            logger.warning("could not write Wii U title cache %s: %s", self._path, exc)
