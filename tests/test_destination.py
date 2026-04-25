from pathlib import Path

import pytest

from ferry.domain.destination import PRESETS, Destination, resolve_preset


def test_all_documented_presets_are_present() -> None:
    expected = {"retrodeck-flatpak", "emudeck", "esde-flatpak", "esde-native"}
    assert set(PRESETS) == expected


@pytest.mark.parametrize(
    "preset,roms_rel,bios_rel",
    [
        ("retrodeck-flatpak", "retrodeck/roms", "retrodeck/bios"),
        ("emudeck", "Emulation/roms", "Emulation/bios"),
        # Bare ES-DE has no centralized BIOS root.
        ("esde-flatpak", "ROMs", None),
        ("esde-native", "ROMs", None),
    ],
)
def test_resolve_preset_anchors_at_home(preset: str, roms_rel: str, bios_rel: str | None) -> None:
    home = Path("/home/test")
    roms, bios = resolve_preset(preset, home)
    assert roms == home / roms_rel
    if bios_rel is None:
        assert bios is None
    else:
        assert bios == home / bios_rel


def test_esde_native_and_esde_flatpak_share_paths() -> None:
    """v4 launcher work will diverge them; v1 treats them identically."""
    home = Path("/home/test")
    assert resolve_preset("esde-native", home) == resolve_preset("esde-flatpak", home)


def test_esde_presets_have_no_centralized_bios() -> None:
    home = Path("/home/test")
    for preset in ("esde-native", "esde-flatpak"):
        _, bios = resolve_preset(preset, home)
        assert bios is None, f"{preset} should not centralize BIOS"


def test_destination_bios_base_defaults_to_none() -> None:
    d = Destination(roms_base=Path("/r"))
    assert d.bios_base is None


def test_destination_is_immutable() -> None:
    d = Destination(roms_base=Path("/r"), bios_base=Path("/b"))
    with pytest.raises((AttributeError, TypeError)):
        d.roms_base = Path("/elsewhere")  # type: ignore[misc]
