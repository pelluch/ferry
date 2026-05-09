"""Tests for dolphin_tool: discovery, header parsing, and cache."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ferry.adapters.dolphin.dolphin_tool import (
    DiscHeader,
    DiscHeaderCache,
    DolphinTool,
    _parse_header_json,
    default_cache_path,
    discover_dolphin_tool,
)

# ---------------------------------------------------------------------------
# _parse_header_json
# ---------------------------------------------------------------------------


def test_parse_header_real_dolphin_tool_output() -> None:
    """Verbatim from `dolphin-tool header -j -i Metroid.rvz`."""
    raw = (
        '{"block_size":131072,"compression_level":19,"compression_method":"Zstandard",'
        '"country":"USA","game_id":"GM8E01","internal_name":"Metroid Prime",'
        '"region":"NTSC-U","revision":2}'
    )
    header = _parse_header_json(raw)
    assert header == DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U")


def test_parse_header_pal_game() -> None:
    raw = '{"game_id":"GM8P01","region":"PAL","country":"EUR"}'
    header = _parse_header_json(raw)
    assert header == DiscHeader(game_code="GM8P", maker_code="01", region="PAL")


def test_parse_header_returns_none_for_invalid_json() -> None:
    assert _parse_header_json("not json") is None


def test_parse_header_returns_none_for_missing_game_id() -> None:
    assert _parse_header_json('{"region": "NTSC-U"}') is None


def test_parse_header_returns_none_for_missing_region() -> None:
    assert _parse_header_json('{"game_id": "GM8E01"}') is None


def test_parse_header_returns_none_for_wrong_length_game_id() -> None:
    """Dolphin/GameCube IDs are always 6 chars (4 gamecode + 2 maker)."""
    assert _parse_header_json('{"game_id": "GM8E", "region": "NTSC-U"}') is None
    assert _parse_header_json('{"game_id": "GM8E0102", "region": "NTSC-U"}') is None


def test_parse_header_returns_none_for_non_object() -> None:
    assert _parse_header_json('["not", "an", "object"]') is None


# ---------------------------------------------------------------------------
# DolphinTool.read_header — mocked subprocess
# ---------------------------------------------------------------------------


def _make_tool() -> DolphinTool:
    return DolphinTool(
        source="system-path",
        label="/usr/bin/dolphin-tool",
        argv_prefix=("/usr/bin/dolphin-tool",),
    )


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    cp = subprocess.CompletedProcess[str](
        args=["dolphin-tool"], returncode=returncode, stdout=stdout, stderr=stderr
    )
    return cp


def test_read_header_returns_parsed_data_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _make_tool()
    monkeypatch.setattr(
        subprocess,
        "run",
        MagicMock(return_value=_completed(stdout='{"game_id":"GM8E01","region":"NTSC-U"}')),
    )
    header = tool.read_header(Path("/x/Metroid.rvz"))
    assert header == DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U")


def test_read_header_returns_none_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _make_tool()
    monkeypatch.setattr(
        subprocess,
        "run",
        MagicMock(return_value=_completed(stderr="Unable to open disc image", returncode=1)),
    )
    assert tool.read_header(Path("/x/missing.rvz")) is None


def test_read_header_returns_none_on_unparseable_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _make_tool()
    monkeypatch.setattr(
        subprocess,
        "run",
        MagicMock(return_value=_completed(stdout="not json")),
    )
    assert tool.read_header(Path("/x/garbage.rvz")) is None


def test_read_header_passes_argv_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool's argv_prefix is prepended verbatim to the dolphin-tool args."""
    tool = DolphinTool(
        source="retrodeck-flatpak",
        label="flatpak",
        argv_prefix=("flatpak", "run", "--command=sh", "X", "-c", 'exec foo "$@"', "_"),
    )
    captured: dict = {}

    def fake_run(argv, **kw):  # type: ignore[no-untyped-def]
        captured["argv"] = argv
        return _completed(stdout='{"game_id":"GM8E01","region":"NTSC-U"}')

    monkeypatch.setattr(subprocess, "run", fake_run)
    tool.read_header(Path("/x/rom.rvz"))
    assert captured["argv"] == [
        "flatpak",
        "run",
        "--command=sh",
        "X",
        "-c",
        'exec foo "$@"',
        "_",
        "header",
        "-j",
        "-i",
        "/x/rom.rvz",
    ]


# ---------------------------------------------------------------------------
# discover_dolphin_tool
# ---------------------------------------------------------------------------


def _make_flatpak_app(root: Path, app_id: str) -> Path:
    app_dir = root / app_id
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir


def test_discover_returns_none_when_nothing_available(tmp_path: Path) -> None:
    result = discover_dolphin_tool(
        home=tmp_path,
        flatpak_dirs=(tmp_path / "user-flatpak", tmp_path / "system-flatpak"),
        path_env={"PATH": str(tmp_path / "empty-path")},
        flatpak_info_path=tmp_path / "no-flatpak-info",
    )
    assert result is None


def test_discover_prefers_in_sandbox_when_running_inside_retrodeck(tmp_path: Path) -> None:
    """When ferry runs INSIDE RetroDECK's sandbox (e.g., via an ES-DE launch
    wrapper), the in-sandbox strategy beats the flatpak-app-installed check.

    The sandbox-internal binary at `/app/retrodeck/...` is invokable directly
    (we're already inside the sandbox); we skip the `flatpak run` wrapper
    which would require `--talk-name=org.freedesktop.Flatpak` permission
    that RetroDECK's manifest doesn't grant.
    """
    flatpak_info = tmp_path / ".flatpak-info"
    flatpak_info.write_text("[Application]\nname=net.retrodeck.retrodeck\n")
    # Even with the host-side flatpak app dir present, in-sandbox wins.
    user_fp = tmp_path / "user-flatpak"
    user_fp.mkdir()
    _make_flatpak_app(user_fp, "net.retrodeck.retrodeck")

    result = discover_dolphin_tool(
        home=tmp_path,
        flatpak_dirs=(user_fp,),
        path_env={"PATH": ""},
        flatpak_info_path=flatpak_info,
    )
    assert result is not None
    assert result.source == "retrodeck-in-sandbox"
    # No `flatpak run` prefix — direct sh invocation against /app/...
    assert result.argv_prefix[0] == "sh"
    assert "flatpak" not in result.argv_prefix


def test_discover_skips_in_sandbox_for_other_flatpaks(tmp_path: Path) -> None:
    """`/.flatpak-info` for a different app shouldn't trigger the in-sandbox
    path — it's RetroDECK-specific."""
    flatpak_info = tmp_path / ".flatpak-info"
    flatpak_info.write_text("[Application]\nname=org.libretro.RetroArch\n")

    user_fp = tmp_path / "user-flatpak"
    user_fp.mkdir()
    _make_flatpak_app(user_fp, "net.retrodeck.retrodeck")

    result = discover_dolphin_tool(
        home=tmp_path,
        flatpak_dirs=(user_fp,),
        path_env={"PATH": ""},
        flatpak_info_path=flatpak_info,
    )
    # Falls through to the standard retrodeck-flatpak (host-side) strategy.
    assert result is not None
    assert result.source == "retrodeck-flatpak"


def test_discover_prefers_retrodeck(tmp_path: Path) -> None:
    user_fp = tmp_path / "user-flatpak"
    user_fp.mkdir()
    _make_flatpak_app(user_fp, "net.retrodeck.retrodeck")
    _make_flatpak_app(user_fp, "org.DolphinEmu.dolphin-emu")

    result = discover_dolphin_tool(
        home=tmp_path,
        flatpak_dirs=(user_fp,),
        path_env={"PATH": ""},
    )
    assert result is not None
    assert result.source == "retrodeck-flatpak"
    assert "net.retrodeck.retrodeck" in result.argv_prefix
    # Sanity: argv_prefix is a flatpak run --command=sh invocation
    assert result.argv_prefix[0] == "flatpak"
    assert "--command=sh" in result.argv_prefix


def test_discover_falls_back_to_emudeck(tmp_path: Path) -> None:
    fp = tmp_path / "user-flatpak"
    fp.mkdir()
    _make_flatpak_app(fp, "org.DolphinEmu.dolphin-emu")

    result = discover_dolphin_tool(
        home=tmp_path,
        flatpak_dirs=(fp,),
        path_env={"PATH": ""},
    )
    assert result is not None
    assert result.source == "emudeck-flatpak"
    # EmuDeck uses direct `--command=` to the binary; no shell shim needed.
    assert "--command=/app/bin/dolphin-tool" in result.argv_prefix


def test_discover_falls_back_to_system_path(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / "dolphin-tool"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)

    result = discover_dolphin_tool(
        home=tmp_path,
        flatpak_dirs=(tmp_path / "no-flatpak",),
        path_env={"PATH": str(bin_dir)},
    )
    assert result is not None
    assert result.source == "system-path"
    assert result.argv_prefix == (str(binary),)


def test_discover_accepts_dolphin_emu_tool_alternate_name(tmp_path: Path) -> None:
    """Some distros ship the binary as `dolphin-emu-tool`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / "dolphin-emu-tool"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)

    result = discover_dolphin_tool(
        home=tmp_path,
        flatpak_dirs=(tmp_path / "no-flatpak",),
        path_env={"PATH": str(bin_dir)},
    )
    assert result is not None
    assert result.source == "system-path"
    assert result.argv_prefix == (str(binary),)


def test_discover_checks_both_flatpak_dirs(tmp_path: Path) -> None:
    """System-wide flatpak install is detected even when user-wide isn't present."""
    user_fp = tmp_path / "user-flatpak"
    user_fp.mkdir()
    sys_fp = tmp_path / "system-flatpak"
    sys_fp.mkdir()
    _make_flatpak_app(sys_fp, "net.retrodeck.retrodeck")

    result = discover_dolphin_tool(
        home=tmp_path,
        flatpak_dirs=(user_fp, sys_fp),
        path_env={"PATH": ""},
    )
    assert result is not None
    assert result.source == "retrodeck-flatpak"


# ---------------------------------------------------------------------------
# DiscHeaderCache
# ---------------------------------------------------------------------------


def _make_rom(tmp_path: Path, name: str = "Metroid.rvz", content: bytes = b"x" * 1024) -> Path:
    rom = tmp_path / name
    rom.write_bytes(content)
    return rom


def test_cache_miss_when_file_not_in_cache(tmp_path: Path) -> None:
    cache = DiscHeaderCache(tmp_path / "cache.json")
    rom = _make_rom(tmp_path)
    assert cache.get(rom) is None


def test_cache_roundtrip(tmp_path: Path) -> None:
    cache = DiscHeaderCache(tmp_path / "cache.json")
    rom = _make_rom(tmp_path)
    header = DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U")
    cache.put(rom, header)
    # Re-create cache instance to exercise the on-disk read path.
    cache2 = DiscHeaderCache(tmp_path / "cache.json")
    assert cache2.get(rom) == header


def test_cache_invalidates_on_size_change(tmp_path: Path) -> None:
    cache = DiscHeaderCache(tmp_path / "cache.json")
    rom = _make_rom(tmp_path, content=b"x" * 100)
    cache.put(rom, DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U"))
    rom.write_bytes(b"y" * 200)  # change size
    assert cache.get(rom) is None


def test_cache_invalidates_on_mtime_change(tmp_path: Path) -> None:
    cache = DiscHeaderCache(tmp_path / "cache.json")
    rom = _make_rom(tmp_path, content=b"x" * 100)
    cache.put(rom, DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U"))
    # Bump mtime without changing size.
    import os

    later = rom.stat().st_mtime_ns + 1_000_000_000
    os.utime(rom, ns=(later, later))
    assert cache.get(rom) is None


def test_cache_handles_missing_file_gracefully(tmp_path: Path) -> None:
    """If the ROM was deleted between cache write and read, get() returns None."""
    cache = DiscHeaderCache(tmp_path / "cache.json")
    rom = _make_rom(tmp_path)
    cache.put(rom, DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U"))
    rom.unlink()
    assert cache.get(rom) is None


def test_cache_corrupt_file_treated_as_empty(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    cache_path.write_text("{not json")
    cache = DiscHeaderCache(cache_path)
    rom = _make_rom(tmp_path)
    assert cache.get(rom) is None
    # Subsequent put still works — recovers from the corruption.
    cache.put(rom, DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U"))
    cache2 = DiscHeaderCache(cache_path)
    assert cache2.get(rom) == DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U")


def test_cache_persists_multiple_entries(tmp_path: Path) -> None:
    cache = DiscHeaderCache(tmp_path / "cache.json")
    rom1 = _make_rom(tmp_path, name="A.rvz")
    rom2 = _make_rom(tmp_path, name="B.rvz")
    cache.put(rom1, DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U"))
    cache.put(rom2, DiscHeader(game_code="GZ2E", maker_code="01", region="NTSC-U"))

    cache2 = DiscHeaderCache(tmp_path / "cache.json")
    assert cache2.get(rom1) is not None
    assert cache2.get(rom2) is not None


def test_cache_json_format_is_indented_and_sorted(tmp_path: Path) -> None:
    """Pretty-printed JSON makes diffs human-readable; sorted keys keep stable diffs."""
    cache_path = tmp_path / "cache.json"
    cache = DiscHeaderCache(cache_path)
    rom = _make_rom(tmp_path)
    cache.put(rom, DiscHeader(game_code="GM8E", maker_code="01", region="NTSC-U"))
    raw = cache_path.read_text()
    # Indented + each entry has all expected keys
    parsed = json.loads(raw)
    entry = next(iter(parsed.values()))
    assert set(entry) == {"mtime_ns", "size", "game_code", "maker_code", "region"}
    assert "  " in raw  # indented


# ---------------------------------------------------------------------------
# default_cache_path
# ---------------------------------------------------------------------------


def test_default_cache_path_uses_xdg_when_set(tmp_path: Path) -> None:
    p = default_cache_path(env={"XDG_CACHE_HOME": str(tmp_path / "xdg")})
    assert p == tmp_path / "xdg" / "ferry" / "dolphin-headers.json"


def test_default_cache_path_falls_back_to_home_cache() -> None:
    p = default_cache_path(env={})
    assert p == Path.home() / ".cache" / "ferry" / "dolphin-headers.json"
