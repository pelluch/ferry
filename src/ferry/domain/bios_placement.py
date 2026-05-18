"""BIOS / firmware placement — where a firmware file lands under `bios_base`.

v5.5 scope is the central pile: every firmware file goes flat into
`Destination.bios_base`, with a *deliberately small* per-platform subfolder
override for the handful of RetroDECK cases that genuinely need a
subdirectory. This is NOT the v9+ multi-destination registry (PCSX2-standalone
paths, regional-variant arbitration, files routed outside `bios/`) — see
DESIGN.md §5.2.

The map is keyed by RomM platform slug (the same slug `[sync].platforms`
uses), and the subfolder is relative to `bios_base`.

## What the map covers (verified against RetroDECK component manifests)

- `dc` (Dreamcast) → `bios/dc` — RetroArch's Flycast core reads its BIOS
  from the `dc` subfolder; the manifest declares `paths: $bios_path/dc`.
- `wiiu` (Wii U) → `bios/cemu` — Cemu's `keys.txt` lives at
  `bios/cemu/keys.txt` (RetroDECK symlinks it into the Cemu data dir; see
  ferry v5). Not manifest-declared — it comes from Cemu's prepare script.

## Known gaps — firmware ferry currently mis-places (tracked, not handled)

These route *outside* `bios/` or to niche subfolders; v5.5 places them flat,
which is wrong-or-suboptimal but acceptable (all are optional/niche). Proper
routing is the deferred v9+ work:

- `ngc` (GameCube) IPL `gc-*.bin` → RetroDECK puts these in the *saves*
  tree (`saves/gc/dolphin/{EU,US,JP}`), per region. Optional BIOS.
- Triforce `segaboot.gcm` → `bios/Triforce` (arcade GameCube variant).
- `pico`/PICO-8 → `bios/pico-8`.
- Arcade / Neo Geo romsets → RetroDECK routes these under the *roms* tree
  (`roms/arcade`, `roms/neogeo`, `roms/fbneo`); `neogeo.zip` also accepts
  flat `bios/`, so that one is fine.

When extending the map, verify the pairing against a real RetroDECK install
(`components/<name>/component_manifest.json`, the `.bios[].paths` field).
"""

from __future__ import annotations

# RomM platform slug -> subfolder under `bios_base`. Absent = flat.
BIOS_SUBFOLDERS: dict[str, str] = {
    "dc": "dc",
    "wiiu": "cemu",
}


def placement_for(platform_slug: str, file_name: str) -> str:
    """Return the path *file_name* should land at, relative to `bios_base`.

    Flat (`file_name`) unless the platform has a `BIOS_SUBFOLDERS` entry,
    in which case `<subfolder>/<file_name>`. Always a POSIX-style relative
    path — state stores it verbatim for portability.
    """
    subfolder = BIOS_SUBFOLDERS.get(platform_slug)
    return f"{subfolder}/{file_name}" if subfolder else file_name
