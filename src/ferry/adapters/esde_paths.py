"""Discover ES-DE installations + their bundled / custom_systems XML paths.

Two profiles ship today:

- **retrodeck-flatpak** — ES-DE bundled inside the RetroDECK flatpak.
  Bundled `es_systems.xml` lives at
  `/var/lib/flatpak/app/net.retrodeck.retrodeck/.../helper_files/es_systems.xml`
  (RetroDECK ships its curated overlay there, separate from the
  upstream-ES-DE bundled files at `share/es-de/resources/systems/linux/`).
  User custom overrides go in
  `~/.var/app/net.retrodeck.retrodeck/config/ES-DE/custom_systems/es_systems.xml`.
- **native** — ES-DE installed natively (distro package, AppImage, etc.).
  Bundled at `/usr/share/es-de/resources/systems/linux/es_systems.xml`.
  User custom overrides go in `~/ES-DE/custom_systems/es_systems.xml`
  (post-3.0; older versions used `~/.emulationstation/custom_systems/`).

Discovery returns the install when EITHER the bundled file is readable
OR the user has already created their custom_systems file. The latter
is rare without ES-DE having been launched at least once, but we accept
it so users who hand-edit overrides before running ferry don't get a
"not detected" message.

`ferry install-launch-hooks` (a future feature) reads the bundled file
to know which systems exist and what their commands look like, then
writes wrapping entries into custom_systems within a managed block.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ferry.domain.install_selection import select_active

logger = logging.getLogger(__name__)

ESDESource = Literal["retrodeck-flatpak", "native"]


@dataclass(frozen=True, slots=True, kw_only=True)
class ESDEInstall:
    """An ES-DE install on disk, with paths the launch-hooks installer needs.

    `bundled_systems_xml` is the file we *read* (RetroDECK's curated
    overlay or upstream's stock systems list) — read-only; we never
    modify it. `custom_systems_xml` is what we *write* (the path may
    not exist yet; `install-launch-hooks` creates the parent dir +
    file if needed).

    `bundled_systems_xml` is None when only the custom_systems path
    exists (rare — user hand-edited an override before any ES-DE run);
    in that case `install-launch-hooks` errors with a friendly hint.
    """

    source: ESDESource
    bundled_systems_xml: Path | None
    custom_systems_xml: Path
    has_custom_systems_file: bool


# (source, bundled_relpath_or_abspath, custom_systems_xml_relpath). Order
# is preference for active-install selection — RetroDECK first because
# opting into RetroDECK is opinionated; native second.
_PROFILES: tuple[tuple[ESDESource, Path, str], ...] = (
    (
        "retrodeck-flatpak",
        # RetroDECK's curated overlay — ships with the flatpak. Path
        # contains a runtime-version commit hash (`stable/<hash>/...`),
        # so we glob to find whichever's installed. See
        # `_find_retrodeck_bundled` for the glob.
        Path("/var/lib/flatpak/app/net.retrodeck.retrodeck"),
        ".var/app/net.retrodeck.retrodeck/config/ES-DE/custom_systems/es_systems.xml",
    ),
    (
        "native",
        # Upstream-ES-DE stock bundled file. Linux-only path; macOS /
        # Windows users would need a different probe. ferry only
        # targets Linux today (DESIGN.md).
        Path("/usr/share/es-de/resources/systems/linux/es_systems.xml"),
        "ES-DE/custom_systems/es_systems.xml",
    ),
)


def discover_esde_installs(
    home: Path | None = None,
    *,
    profiles: tuple[tuple[ESDESource, Path, str], ...] | None = None,
) -> list[ESDEInstall]:
    """Return every ES-DE install whose bundled or custom paths exist.

    Order matches `_PROFILES` — RetroDECK first, then native. An install
    counts as "present" if either the bundled XML is readable (ES-DE
    installed but never launched yet, no custom file) or the custom
    file exists (custom was hand-created or ES-DE has run).

    `profiles` is injectable for tests — the default tuple contains
    absolute system paths that real installs land at, which makes
    isolated tests difficult on a host with ES-DE actually installed.
    """
    home = home or Path.home()
    installs: list[ESDEInstall] = []
    for source, bundled_probe, custom_rel in profiles or _PROFILES:
        bundled = _resolve_bundled(source, bundled_probe)
        custom_path = home / custom_rel
        if bundled is None and not custom_path.exists():
            continue
        installs.append(
            ESDEInstall(
                source=source,
                bundled_systems_xml=bundled,
                custom_systems_xml=custom_path,
                has_custom_systems_file=custom_path.is_file(),
            )
        )
    return installs


def _resolve_bundled(source: ESDESource, probe: Path) -> Path | None:
    """Resolve the bundled `es_systems.xml` path for a given source.

    For native installs, `probe` IS the file path. For RetroDECK,
    `probe` is the flatpak app dir; we glob to find the curated
    `helper_files/es_systems.xml` under whichever runtime hash is
    installed.
    """
    if source == "native":
        return probe if probe.is_file() else None
    if source == "retrodeck-flatpak":
        if not probe.is_dir():
            return None
        # The REAL bundled ES-DE systems list ships under
        # `components/es-de/share/es-de/resources/systems/linux/es_systems.xml`
        # (the same upstream-ES-DE path, just inside RetroDECK's flatpak
        # tree). The file at `config/retrodeck/helper_files/es_systems.xml`
        # is RetroDECK's empty-example template for custom systems, not the
        # actual bundled list — picking it would yield 0 systems wrapped.
        candidates = sorted(
            probe.glob(
                "*/stable/*/files/retrodeck/components/es-de/share/"
                "es-de/resources/systems/linux/es_systems.xml"
            )
        )
        return candidates[-1] if candidates else None
    return None


def select_active_install(installs: list[ESDEInstall]) -> ESDEInstall | None:
    """Pick the ES-DE install ferry should target, or None if ambiguous.

    Active-use signal: a present `custom_systems.xml` (the user has
    customised this profile). See
    `domain.install_selection.select_active` for the full decision
    table.
    """
    return select_active(installs, has_active=lambda i: i.has_custom_systems_file)
