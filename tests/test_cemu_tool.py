"""Tests for ferry.adapters.cemu.cemu_tool.

The load-bearing property is `extract_title_id`'s content-based success
check: Cemu's extractor exits 0 even on failure (`Unable to open "%s"`)
and segfaults when keys.txt is missing, so the exit code can't be
trusted — only a parsed `<title_id>` counts.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from ferry.adapters.cemu.cemu_tool import (
    CemuTool,
    WiiUTitle,
    WiiUTitleCache,
    _parse_meta_xml,
    discover_cemu_tool,
    lookup_wiiu_title,
)

# A minimal but realistic Wii U meta.xml (BotW USA), trimmed.
_META_XML = """<?xml version="1.0" encoding="utf-8"?>
<menu type="complex" access="777">
  <version type="unsignedInt" length="4">33</version>
  <product_code type="string" length="32">WUP-P-ALZE</product_code>
  <title_id type="hexBinary" length="8">00050000101C9400</title_id>
  <group_id type="hexBinary" length="4">00001C94</group_id>
  <longname_en type="string" length="512">The Legend of Zelda
Breath of the Wild</longname_en>
</menu>
"""

# Cemu's silent-failure output: an unformatted format string, exit 0.
_UNABLE_TO_OPEN = 'Unable to open "%s"\n'


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRun:
    """Stand-in for subprocess.run; records calls, returns a crafted result."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        raises: BaseException | None = None,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.raises = raises
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, kwargs))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


def _retrodeck_tool() -> CemuTool:
    """A CemuTool shaped like the in-sandbox RetroDECK discovery result."""
    return CemuTool(
        source="retrodeck-in-sandbox",
        label="test",
        argv_prefix=("sh", "-c", "<snippet>", "_"),
        cwd_via_snippet=True,
    )


def _system_tool() -> CemuTool:
    return CemuTool(
        source="system-path",
        label="/usr/bin/cemu",
        argv_prefix=("/usr/bin/cemu",),
        cwd_via_snippet=False,
    )


# ---------------------------------------------------------------------------
# WiiUTitle
# ---------------------------------------------------------------------------


def test_wiiu_title_splits_high_and_low() -> None:
    title = WiiUTitle(title_id="00050000101C9400")
    assert title.title_id_high == "00050000"
    # Low half is lowercased — that's the on-disk save folder name.
    assert title.title_id_low == "101c9400"


# ---------------------------------------------------------------------------
# _parse_meta_xml
# ---------------------------------------------------------------------------


def test_parse_meta_xml_extracts_title_id() -> None:
    title = _parse_meta_xml(_META_XML)
    assert title is not None
    assert title.title_id == "00050000101C9400"


def test_parse_meta_xml_normalizes_to_uppercase() -> None:
    xml = '<menu><title_id type="hexBinary" length="8">00050000101c9400</title_id></menu>'
    title = _parse_meta_xml(xml)
    assert title is not None
    assert title.title_id == "00050000101C9400"


def test_parse_meta_xml_rejects_non_xml() -> None:
    assert _parse_meta_xml(_UNABLE_TO_OPEN) is None


def test_parse_meta_xml_rejects_empty() -> None:
    assert _parse_meta_xml("") is None
    assert _parse_meta_xml("   \n  ") is None


def test_parse_meta_xml_rejects_xml_without_title_id() -> None:
    xml = '<menu><product_code type="string">WUP-P-ALZE</product_code></menu>'
    assert _parse_meta_xml(xml) is None


def test_parse_meta_xml_rejects_wrong_length_title_id() -> None:
    xml = "<menu><title_id>0005000010</title_id></menu>"
    assert _parse_meta_xml(xml) is None


def test_parse_meta_xml_rejects_non_hex_title_id() -> None:
    xml = "<menu><title_id>0005000010XZ9400</title_id></menu>"
    assert _parse_meta_xml(xml) is None


# ---------------------------------------------------------------------------
# CemuTool.invoke — argv construction
# ---------------------------------------------------------------------------


def test_invoke_snippet_source_appends_keys_dir_as_positional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """For shell-snippet sources, the keys dir is the snippet's $1 — never
    string-interpolated, so no shell injection — and cwd is left unset."""
    fake = _FakeRun(stdout=_META_XML)
    monkeypatch.setattr(subprocess, "run", fake)
    tool = _retrodeck_tool()
    keys_dir = tmp_path / "Cemu"

    tool.invoke("-e", "rom.wux", keys_dir=keys_dir)

    argv, kwargs = fake.calls[0]
    assert argv == ["sh", "-c", "<snippet>", "_", str(keys_dir), "-e", "rom.wux"]
    assert kwargs["cwd"] is None


def test_invoke_system_source_passes_keys_dir_as_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """For system-path, the keys dir goes to subprocess via cwd=."""
    fake = _FakeRun(stdout=_META_XML)
    monkeypatch.setattr(subprocess, "run", fake)
    tool = _system_tool()
    keys_dir = tmp_path / "Cemu"

    tool.invoke("-e", "rom.wux", keys_dir=keys_dir)

    argv, kwargs = fake.calls[0]
    assert argv == ["/usr/bin/cemu", "-e", "rom.wux"]
    assert kwargs["cwd"] == str(keys_dir)


# ---------------------------------------------------------------------------
# CemuTool.extract_title_id — the content-based success check
# ---------------------------------------------------------------------------


def _keys_dir(tmp_path: Path) -> Path:
    """A directory holding a keys.txt — `extract_title_id` pre-flights for one."""
    (tmp_path / "keys.txt").write_text("# fake cemu keys\n")
    return tmp_path


def test_extract_title_id_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRun(returncode=0, stdout=_META_XML)
    monkeypatch.setattr(subprocess, "run", fake)

    title = _retrodeck_tool().extract_title_id(tmp_path / "rom.wux", keys_dir=_keys_dir(tmp_path))
    assert title is not None
    assert title.title_id == "00050000101C9400"
    # The invocation is `cemu -e <rom> -p meta/meta.xml`.
    argv, _ = fake.calls[0]
    assert argv[-4:] == ["-e", str(tmp_path / "rom.wux"), "-p", "meta/meta.xml"]


def test_extract_title_id_missing_keys_txt_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No keys.txt in the keys dir → fail before invoking Cemu at all,
    so the user gets an actionable message instead of a 139 segfault."""
    fake = _FakeRun(returncode=0, stdout=_META_XML)
    monkeypatch.setattr(subprocess, "run", fake)

    # tmp_path has no keys.txt planted.
    title = _retrodeck_tool().extract_title_id(tmp_path / "rom.wux", keys_dir=tmp_path)
    assert title is None
    assert fake.calls == []  # Cemu never invoked — segfault avoided


def test_extract_title_id_dangling_keys_symlink_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RetroDECK pre-creates keys.txt as a symlink into its BIOS dir; if
    the target isn't populated the symlink dangles. `.is_file()` follows
    symlinks, so a dangling one reads as absent — fail fast, don't crash."""
    fake = _FakeRun(returncode=0, stdout=_META_XML)
    monkeypatch.setattr(subprocess, "run", fake)
    (tmp_path / "keys.txt").symlink_to(tmp_path / "nonexistent-bios" / "keys.txt")

    title = _retrodeck_tool().extract_title_id(tmp_path / "rom.wux", keys_dir=tmp_path)
    assert title is None
    assert fake.calls == []


def test_extract_title_id_exit0_but_unable_to_open_is_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cemu's silent-failure path: exit 0, `Unable to open "%s"` on stdout.
    Exit code alone proves nothing — no <title_id>, so None."""
    fake = _FakeRun(returncode=0, stdout=_UNABLE_TO_OPEN)
    monkeypatch.setattr(subprocess, "run", fake)

    title = _retrodeck_tool().extract_title_id(tmp_path / "rom.wux", keys_dir=_keys_dir(tmp_path))
    assert title is None


def test_extract_title_id_segfault_is_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SIGSEGV (exit 139) — e.g. Cemu's data-dir walk overflowing — is
    a hard failure even though the pre-flight keys.txt check passed."""
    fake = _FakeRun(returncode=139, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake)

    title = _retrodeck_tool().extract_title_id(tmp_path / "rom.wux", keys_dir=_keys_dir(tmp_path))
    assert title is None


def test_extract_title_id_cwd_guard_exit_is_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shell snippet's `cd` guard exits 91 when the keys dir is gone."""
    fake = _FakeRun(returncode=91)
    monkeypatch.setattr(subprocess, "run", fake)

    title = _retrodeck_tool().extract_title_id(tmp_path / "rom.wux", keys_dir=_keys_dir(tmp_path))
    assert title is None


def test_extract_title_id_timeout_is_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeRun(raises=subprocess.TimeoutExpired(cmd="cemu", timeout=120.0))
    monkeypatch.setattr(subprocess, "run", fake)

    title = _retrodeck_tool().extract_title_id(tmp_path / "rom.wux", keys_dir=_keys_dir(tmp_path))
    assert title is None


# ---------------------------------------------------------------------------
# discover_cemu_tool
# ---------------------------------------------------------------------------


def test_discover_prefers_in_sandbox(tmp_path: Path) -> None:
    """`/.flatpak-info` naming RetroDECK → in-sandbox direct invocation."""
    info = tmp_path / "flatpak-info"
    info.write_text("[Application]\nname=net.retrodeck.retrodeck\n")

    tool = discover_cemu_tool(
        flatpak_info_path=info, flatpak_dirs=(tmp_path / "no-flatpak",), path_env={"PATH": ""}
    )
    assert tool is not None
    assert tool.source == "retrodeck-in-sandbox"
    assert tool.cwd_via_snippet is True


def test_discover_finds_retrodeck_flatpak(tmp_path: Path) -> None:
    flatpak_root = tmp_path / "flatpak" / "app"
    (flatpak_root / "net.retrodeck.retrodeck").mkdir(parents=True)
    missing_info = tmp_path / "no-flatpak-info"

    tool = discover_cemu_tool(
        flatpak_info_path=missing_info,
        flatpak_dirs=(flatpak_root,),
        path_env={"PATH": ""},
    )
    assert tool is not None
    assert tool.source == "retrodeck-flatpak"
    assert tool.cwd_via_snippet is True
    assert "flatpak" in tool.argv_prefix[0]


def test_discover_falls_back_to_system_path(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cemu_bin = bin_dir / "cemu"
    cemu_bin.write_text("#!/bin/sh\n")
    cemu_bin.chmod(0o755)
    missing_info = tmp_path / "no-flatpak-info"

    tool = discover_cemu_tool(
        flatpak_info_path=missing_info,
        flatpak_dirs=(tmp_path / "no-flatpak",),
        path_env={"PATH": str(bin_dir)},
    )
    assert tool is not None
    assert tool.source == "system-path"
    assert tool.cwd_via_snippet is False
    assert tool.argv_prefix == (str(cemu_bin),)


def test_discover_returns_none_when_nothing_available(tmp_path: Path) -> None:
    tool = discover_cemu_tool(
        flatpak_info_path=tmp_path / "no-flatpak-info",
        flatpak_dirs=(tmp_path / "no-flatpak",),
        path_env={"PATH": ""},
    )
    assert tool is None


# ---------------------------------------------------------------------------
# WiiUTitleCache
# ---------------------------------------------------------------------------


def _plant_rom(tmp_path: Path, content: bytes = b"fake wux") -> Path:
    rom = tmp_path / "game.wux"
    rom.write_bytes(content)
    return rom


def test_cache_put_then_get_hit(tmp_path: Path) -> None:
    rom = _plant_rom(tmp_path)
    cache = WiiUTitleCache(tmp_path / "cache.json")
    title = WiiUTitle(title_id="00050000101C9400")

    cache.put(rom, title)
    assert cache.get(rom) == title


def test_cache_persists_across_instances(tmp_path: Path) -> None:
    rom = _plant_rom(tmp_path)
    cache_path = tmp_path / "cache.json"
    WiiUTitleCache(cache_path).put(rom, WiiUTitle(title_id="00050000101C9400"))

    fresh = WiiUTitleCache(cache_path)
    assert fresh.get(rom) == WiiUTitle(title_id="00050000101C9400")


def test_cache_miss_on_size_change(tmp_path: Path) -> None:
    rom = _plant_rom(tmp_path)
    cache = WiiUTitleCache(tmp_path / "cache.json")
    cache.put(rom, WiiUTitle(title_id="00050000101C9400"))

    rom.write_bytes(b"different content entirely")
    assert cache.get(rom) is None


def test_cache_miss_on_mtime_change(tmp_path: Path) -> None:
    rom = _plant_rom(tmp_path)
    cache = WiiUTitleCache(tmp_path / "cache.json")
    cache.put(rom, WiiUTitle(title_id="00050000101C9400"))

    import os

    future = rom.stat().st_mtime + 100
    os.utime(rom, (future, future))
    assert cache.get(rom) is None


def test_cache_miss_for_unknown_rom(tmp_path: Path) -> None:
    cache = WiiUTitleCache(tmp_path / "cache.json")
    assert cache.get(tmp_path / "never-cached.wux") is None


def test_cache_malformed_file_treated_as_empty(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    cache_path.write_text("{not valid json")
    rom = _plant_rom(tmp_path)

    cache = WiiUTitleCache(cache_path)
    assert cache.get(rom) is None  # no crash


# ---------------------------------------------------------------------------
# lookup_wiiu_title
# ---------------------------------------------------------------------------


def test_lookup_uses_cache_and_skips_invoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rom = _plant_rom(tmp_path)
    cache = WiiUTitleCache(tmp_path / "cache.json")
    cache.put(rom, WiiUTitle(title_id="00050000101C9400"))

    fake = _FakeRun(raises=AssertionError("should not invoke cemu on a cache hit"))
    monkeypatch.setattr(subprocess, "run", fake)

    title = lookup_wiiu_title(rom, _retrodeck_tool(), cache, keys_dir=tmp_path)
    assert title == WiiUTitle(title_id="00050000101C9400")
    assert fake.calls == []


def test_lookup_populates_cache_on_fresh_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rom = _plant_rom(tmp_path)
    cache = WiiUTitleCache(tmp_path / "cache.json")
    fake = _FakeRun(returncode=0, stdout=_META_XML)
    monkeypatch.setattr(subprocess, "run", fake)

    keys_dir = _keys_dir(tmp_path)
    title = lookup_wiiu_title(rom, _retrodeck_tool(), cache, keys_dir=keys_dir)
    assert title == WiiUTitle(title_id="00050000101C9400")
    # Second lookup is a cache hit — invoke count stays at 1.
    lookup_wiiu_title(rom, _retrodeck_tool(), cache, keys_dir=keys_dir)
    assert len(fake.calls) == 1


def test_lookup_without_cache_invokes_every_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rom = _plant_rom(tmp_path)
    fake = _FakeRun(returncode=0, stdout=_META_XML)
    monkeypatch.setattr(subprocess, "run", fake)

    keys_dir = _keys_dir(tmp_path)
    lookup_wiiu_title(rom, _retrodeck_tool(), None, keys_dir=keys_dir)
    lookup_wiiu_title(rom, _retrodeck_tool(), None, keys_dir=keys_dir)
    assert len(fake.calls) == 2
