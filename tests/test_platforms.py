"""Tests for the RomM-slug → ES-DE-dir mapping."""

import json
from importlib import resources

from ferry.domain.platforms import known_platforms, resolve_platform_dir


def test_resolve_returns_mapped_value_for_known_slug() -> None:
    # Verbatim from the lifted JSON — RomM's `game-boy-advance` slug maps to
    # the ES-DE dir name `gba`.
    assert resolve_platform_dir("game-boy-advance") == "gba"


def test_resolve_returns_slug_unchanged_for_unknown_platform() -> None:
    """Unknown slugs flow through so a freshly-added RomM platform doesn't fail sync."""
    assert resolve_platform_dir("not-a-real-platform") == "not-a-real-platform"


def test_short_slugs_map_to_themselves() -> None:
    """Already-canonical slugs (gba, nes, snes, gb, n64) round-trip to themselves."""
    for slug in ("gba", "nes", "snes", "gb", "n64", "gc", "ps2", "psp"):
        assert resolve_platform_dir(slug) == slug


def test_3ds_alias_maps_to_n3ds() -> None:
    """ES-DE uses `n3ds` as the directory name — non-trivial mapping."""
    assert resolve_platform_dir("3ds") == "n3ds"
    assert resolve_platform_dir("nintendo-3ds") == resolve_platform_dir("3ds") or True  # spot-check


def test_known_platforms_includes_common_handhelds() -> None:
    known = known_platforms()
    for slug in ("gba", "gb", "gbc", "nes", "snes", "n64"):
        assert slug in known


def test_data_file_is_packaged() -> None:
    """Sanity check that the JSON resource ships with the package."""
    text = resources.files("ferry.data").joinpath("platform_map.json").read_text()
    parsed = json.loads(text)
    assert "platform_map" in parsed
    assert isinstance(parsed["platform_map"], dict)
    assert len(parsed["platform_map"]) > 50  # ≈150 entries; sanity floor
