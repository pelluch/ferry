"""Tests for esde_paths' install discovery + active-install selection."""

from __future__ import annotations

from pathlib import Path

from ferry.adapters.esde_paths import (
    ESDEInstall,
    discover_esde_installs,
    select_active_install,
)

# ---------------------------------------------------------------------------
# discover_esde_installs — discovery shape
# ---------------------------------------------------------------------------


def _profiles_under(tmp_path: Path) -> tuple:
    """Test profile fixture: scope all probes to tmp_path so the real
    /usr/share/es-de or /var/lib/flatpak don't leak into tests."""
    return (
        (
            "retrodeck-flatpak",
            tmp_path / "fake-flatpak/net.retrodeck.retrodeck",
            ".var/app/net.retrodeck.retrodeck/config/ES-DE/custom_systems/es_systems.xml",
        ),
        (
            "native",
            tmp_path / "fake-usr/share/es-de/resources/systems/linux/es_systems.xml",
            "ES-DE/custom_systems/es_systems.xml",
        ),
    )


def test_no_installs_returns_empty(tmp_path: Path) -> None:
    """Empty tmp_path with no bundled or custom files → no installs."""
    assert discover_esde_installs(tmp_path, profiles=_profiles_under(tmp_path)) == []


def test_native_with_only_custom_file(tmp_path: Path) -> None:
    """User hand-created a custom_systems file before any ES-DE run.
    bundled_systems_xml is None but the install is still surfaced."""
    custom = tmp_path / "ES-DE" / "custom_systems" / "es_systems.xml"
    custom.parent.mkdir(parents=True)
    custom.write_text('<?xml version="1.0"?><systemList></systemList>')

    result = discover_esde_installs(tmp_path, profiles=_profiles_under(tmp_path))
    assert len(result) == 1
    install = result[0]
    assert install.source == "native"
    assert install.bundled_systems_xml is None
    assert install.custom_systems_xml == custom
    assert install.has_custom_systems_file is True


def test_native_bundled_present_no_custom(tmp_path: Path) -> None:
    """Bundled file exists but user hasn't created a custom file yet."""
    bundled = tmp_path / "fake-usr/share/es-de/resources/systems/linux/es_systems.xml"
    bundled.parent.mkdir(parents=True)
    bundled.write_text('<?xml version="1.0"?><systemList></systemList>')

    result = discover_esde_installs(tmp_path, profiles=_profiles_under(tmp_path))
    assert len(result) == 1
    install = result[0]
    assert install.source == "native"
    assert install.bundled_systems_xml == bundled
    assert install.has_custom_systems_file is False


def test_retrodeck_bundled_resolved_via_glob(tmp_path: Path) -> None:
    """RetroDECK ships its bundled file under `<arch>/stable/<hash>/files/...`.
    We glob to find whichever runtime hash is installed. Highest-sorted
    candidate wins (last entry after sort)."""
    fp_root = tmp_path / "fake-flatpak/net.retrodeck.retrodeck"
    bundled_rel = (
        "files/retrodeck/components/es-de/share/es-de/resources/systems/linux/es_systems.xml"
    )
    older_bundled = fp_root / "x86_64/stable/aaa1111" / bundled_rel
    newer_bundled = fp_root / "x86_64/stable/zzz9999" / bundled_rel
    for f in (older_bundled, newer_bundled):
        f.parent.mkdir(parents=True)
        f.write_text("<systemList/>")

    result = discover_esde_installs(tmp_path, profiles=_profiles_under(tmp_path))
    assert len(result) == 1
    assert result[0].source == "retrodeck-flatpak"
    assert result[0].bundled_systems_xml == newer_bundled  # last after sort


def test_both_profiles_returned_in_priority_order(tmp_path: Path) -> None:
    bundled = tmp_path / "fake-usr/share/es-de/resources/systems/linux/es_systems.xml"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("<systemList/>")
    rd_custom = (
        tmp_path / ".var/app/net.retrodeck.retrodeck/config/ES-DE/custom_systems/es_systems.xml"
    )
    rd_custom.parent.mkdir(parents=True)
    rd_custom.write_text("<systemList/>")

    result = discover_esde_installs(tmp_path, profiles=_profiles_under(tmp_path))
    assert [i.source for i in result] == ["retrodeck-flatpak", "native"]


# ---------------------------------------------------------------------------
# select_active_install
# ---------------------------------------------------------------------------


def _install(source, has_custom: bool) -> ESDEInstall:
    return ESDEInstall(
        source=source,
        bundled_systems_xml=Path(f"/x/{source}/bundled/es_systems.xml"),
        custom_systems_xml=Path(f"/x/{source}/custom/es_systems.xml"),
        has_custom_systems_file=has_custom,
    )


def test_select_returns_none_for_empty() -> None:
    assert select_active_install([]) is None


def test_select_returns_only_install() -> None:
    only = _install("native", has_custom=False)
    assert select_active_install([only]) is only


def test_select_picks_install_with_custom_file_when_others_dont() -> None:
    rd = _install("retrodeck-flatpak", has_custom=False)
    native = _install("native", has_custom=True)
    assert select_active_install([rd, native]) is native


def test_select_returns_none_when_two_installs_have_custom_files() -> None:
    rd = _install("retrodeck-flatpak", has_custom=True)
    native = _install("native", has_custom=True)
    assert select_active_install([rd, native]) is None


def test_select_falls_back_to_first_priority_when_no_custom_files() -> None:
    rd = _install("retrodeck-flatpak", has_custom=False)
    native = _install("native", has_custom=False)
    assert select_active_install([rd, native]) is rd
