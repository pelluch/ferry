from dataclasses import dataclass
from pathlib import Path

from ferry.domain.destination import resolve_preset


@dataclass(frozen=True, slots=True, kw_only=True)
class DetectedCandidate:
    """A preset that the local filesystem suggests is in use.

    `signals` are the human-readable observations that flagged this preset.
    Detection is intentionally permissive — for RetroDECK / EmuDeck a single
    signal is enough; for ES-DE both a ROM dir and a tooling signal are
    required because `~/ROMs` alone is too weak a hint.
    """

    preset: str
    roms_base: Path
    bios_base: Path | None
    signals: list[str]


def detect_candidates(home: Path | None = None) -> list[DetectedCandidate]:
    """Probe the filesystem for known preset layouts. Returns all matches.

    This function never auto-selects — even with a single match, the caller
    (and ultimately the user) decides whether to use it. Multiple candidates
    are listed in stable order; zero is a valid result.
    """
    home = home or Path.home()
    out: list[DetectedCandidate] = []
    for probe in (
        _probe_retrodeck_flatpak,
        _probe_emudeck,
        _probe_esde_native,
        _probe_esde_flatpak,
    ):
        match = probe(home)
        if match is not None:
            out.append(match)
    return out


def _probe_retrodeck_flatpak(home: Path) -> DetectedCandidate | None:
    signals: list[str] = []
    if (home / "retrodeck/roms").is_dir():
        signals.append("ROM dir present (~/retrodeck/roms)")
    if (home / "retrodeck/bios").is_dir():
        signals.append("BIOS dir present (~/retrodeck/bios)")
    if (home / ".var/app/net.retrodeck.retrodeck").is_dir():
        signals.append("RetroDECK flatpak data dir present (~/.var/app/net.retrodeck.retrodeck)")
    if not signals:
        return None
    roms, bios = resolve_preset("retrodeck-flatpak", home)
    return DetectedCandidate(
        preset="retrodeck-flatpak", roms_base=roms, bios_base=bios, signals=signals
    )


def _probe_emudeck(home: Path) -> DetectedCandidate | None:
    signals: list[str] = []
    if (home / "Emulation/roms").is_dir():
        signals.append("ROM dir present (~/Emulation/roms)")
    if (home / "Emulation/bios").is_dir():
        signals.append("BIOS dir present (~/Emulation/bios)")
    if not signals:
        return None
    roms, bios = resolve_preset("emudeck", home)
    return DetectedCandidate(preset="emudeck", roms_base=roms, bios_base=bios, signals=signals)


def _probe_esde_native(home: Path) -> DetectedCandidate | None:
    """Both signals required — `~/ROMs` alone could be anything."""
    has_roms = (home / "ROMs").is_dir()
    has_userdata = (home / "ES-DE/settings/es_settings.xml").exists()
    if not (has_roms and has_userdata):
        return None
    roms, bios = resolve_preset("esde-native", home)
    return DetectedCandidate(
        preset="esde-native",
        roms_base=roms,
        bios_base=bios,
        signals=[
            "ROM dir present (~/ROMs)",
            "ES-DE userdata dir present (~/ES-DE/settings/es_settings.xml)",
        ],
    )


def _probe_esde_flatpak(home: Path) -> DetectedCandidate | None:
    has_roms = (home / "ROMs").is_dir()
    has_flatpak = (home / ".var/app/org.es_de.frontend").is_dir()
    if not (has_roms and has_flatpak):
        return None
    roms, bios = resolve_preset("esde-flatpak", home)
    return DetectedCandidate(
        preset="esde-flatpak",
        roms_base=roms,
        bios_base=bios,
        signals=[
            "ROM dir present (~/ROMs)",
            "ES-DE flatpak data dir present (~/.var/app/org.es_de.frontend)",
        ],
    )
