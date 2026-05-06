"""Pure-domain tests for `services.reconcile` — walker, classifier,
state synthesis. CLI-level behaviour (RomM fetch, adoption side
effects, dry-run output) lives in test_cli_reconcile.py."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

from ferry.adapters.sidecar import (
    SIDECAR_PREFIX,
    SIDECAR_SUFFIX,
    write_sidecar,
)
from ferry.domain.state import LibraryState, RomState, TransformedOutput
from ferry.services.reconcile import (
    Ambiguous,
    Confident,
    HashOnly,
    NameOnly,
    NoMatch,
    OrphanCandidate,
    build_index,
    classify,
    find_orphans,
    synthesize_state,
)


def _rom_payload(
    rom_id: int,
    name: str,
    *,
    fs_name: str,
    platform_slug: str = "gba",
    files: list[dict[str, Any]] | None = None,
    updated_at: str = "2026-01-01T00:00:00Z",
    fs_size_bytes: int = 1234,
) -> dict[str, Any]:
    return {
        "id": rom_id,
        "name": name,
        "platform_slug": platform_slug,
        "fs_name": fs_name,
        "fs_size_bytes": fs_size_bytes,
        "updated_at": updated_at,
        "files": files or [],
    }


def _file_payload(
    file_id: int, file_name: str, md5: str | None, size: int = 1024
) -> dict[str, Any]:
    return {
        "id": file_id,
        "file_name": file_name,
        "file_size_bytes": size,
        "md5_hash": md5,
    }


# ---------------------------------------------------------------------------
# find_orphans — walker
# ---------------------------------------------------------------------------


def test_find_orphans_returns_empty_when_roms_base_missing(tmp_path: Path) -> None:
    assert (
        find_orphans(
            roms_base=tmp_path / "missing",
            sidecars_root=tmp_path / "sidecars",
            state=LibraryState(),
        )
        == []
    )


def test_find_orphans_skips_tracked_files(tmp_path: Path, make_rom) -> None:
    """A file listed in `state.roms[*].outputs[*].path` is NOT an orphan."""
    roms_base = tmp_path / "ROMs"
    sidecars_root = tmp_path / "state" / "sidecars"
    primary = roms_base / "gba" / "Tracked.gba"
    primary.parent.mkdir(parents=True)
    primary.write_bytes(b"x")
    rom = make_rom(
        rom_id=1,
        outputs=(TransformedOutput(path="gba/Tracked.gba", md5="abc", size=1),),
        primary_output_index=0,
    )

    orphans = find_orphans(
        roms_base=roms_base,
        sidecars_root=sidecars_root,
        state=LibraryState(roms={1: rom}),
    )
    assert orphans == []


def test_find_orphans_returns_untracked_files(tmp_path: Path) -> None:
    roms_base = tmp_path / "ROMs"
    sidecars_root = tmp_path / "state" / "sidecars"
    target = roms_base / "gba" / "Loose.gba"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")

    orphans = find_orphans(roms_base=roms_base, sidecars_root=sidecars_root, state=LibraryState())
    assert len(orphans) == 1
    assert orphans[0].abs_path == target
    assert orphans[0].rel_path == Path("gba/Loose.gba")
    assert orphans[0].platform_dir == "gba"


def test_find_orphans_skips_files_with_canonical_sidecar(tmp_path: Path, make_rom) -> None:
    """A file with a canonical sidecar but no state entry is mid-recovery —
    don't double-claim it as an orphan."""
    roms_base = tmp_path / "ROMs"
    sidecars_root = tmp_path / "state" / "sidecars"
    target = roms_base / "gba" / "Recovering.gba"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")
    rom = make_rom(
        rom_id=1,
        outputs=(TransformedOutput(path="gba/Recovering.gba", md5="abc", size=1),),
        primary_output_index=0,
    )
    write_sidecar(target, rom, roms_base=roms_base, sidecars_root=sidecars_root)

    # Empty state — sidecar exists but state is unaware.
    orphans = find_orphans(roms_base=roms_base, sidecars_root=sidecars_root, state=LibraryState())
    assert orphans == []


def test_find_orphans_skips_dotfiles_and_sidecar_suffixes(tmp_path: Path) -> None:
    roms_base = tmp_path / "ROMs"
    sidecars_root = tmp_path / "state" / "sidecars"
    gba = roms_base / "gba"
    gba.mkdir(parents=True)
    (gba / ".directory").write_text("[Desktop Entry]")  # KDE droppings
    (gba / f"{SIDECAR_PREFIX}Foo.gba{SIDECAR_SUFFIX}").write_text("{}")  # legacy sidecar
    (gba / f"Foo.gba{SIDECAR_SUFFIX}").write_text("{}")  # very-legacy plain sidecar
    (gba / "Loose.gba").write_bytes(b"x")  # the only real orphan

    orphans = find_orphans(roms_base=roms_base, sidecars_root=sidecars_root, state=LibraryState())
    assert len(orphans) == 1
    assert orphans[0].abs_path.name == "Loose.gba"


def test_find_orphans_skips_top_level_files_in_roms_base(tmp_path: Path) -> None:
    """A file directly in roms_base (no platform subdir) is too ambiguous to
    classify; reconcile only walks platform-shaped subdirs."""
    roms_base = tmp_path / "ROMs"
    sidecars_root = tmp_path / "state" / "sidecars"
    roms_base.mkdir()
    (roms_base / "stray.bin").write_bytes(b"x")  # top-level — skipped

    (roms_base / "gba").mkdir()
    (roms_base / "gba" / "in-platform.gba").write_bytes(b"y")  # included

    orphans = find_orphans(roms_base=roms_base, sidecars_root=sidecars_root, state=LibraryState())
    assert [o.abs_path.name for o in orphans] == ["in-platform.gba"]


def test_find_orphans_respects_platform_filter(tmp_path: Path) -> None:
    roms_base = tmp_path / "ROMs"
    sidecars_root = tmp_path / "state" / "sidecars"
    (roms_base / "gba").mkdir(parents=True)
    (roms_base / "snes").mkdir(parents=True)
    (roms_base / "gba" / "A.gba").write_bytes(b"x")
    (roms_base / "snes" / "B.smc").write_bytes(b"y")

    orphans = find_orphans(
        roms_base=roms_base,
        sidecars_root=sidecars_root,
        state=LibraryState(),
        platform_filter="gba",
    )
    assert [o.platform_dir for o in orphans] == ["gba"]


def test_find_orphans_walks_nested_subdirs(tmp_path: Path) -> None:
    """Multi-disc ROMs land in nested subdirs (e.g., `psx/Game/CD1.cue`).
    Walker must recurse."""
    roms_base = tmp_path / "ROMs"
    sidecars_root = tmp_path / "state" / "sidecars"
    deep = roms_base / "psx" / "Game"
    deep.mkdir(parents=True)
    (deep / "CD1.cue").write_bytes(b"x")
    (deep / "CD1.bin").write_bytes(b"y")

    orphans = find_orphans(roms_base=roms_base, sidecars_root=sidecars_root, state=LibraryState())
    assert {o.rel_path.name for o in orphans} == {"CD1.cue", "CD1.bin"}


# ---------------------------------------------------------------------------
# build_index
# ---------------------------------------------------------------------------


def test_build_index_indexes_each_file_by_name_and_hash() -> None:
    rom = _rom_payload(
        1,
        "Pikmin",
        fs_name="Pikmin (USA).zip",
        files=[
            _file_payload(10, "Pikmin (USA).iso", md5="abc"),
        ],
    )
    by_name, by_hash, by_stem = build_index([rom])
    assert "Pikmin (USA).iso" in by_name
    assert "abc" in by_hash
    assert by_name["Pikmin (USA).iso"][0].rom_id == 1
    assert by_hash["abc"][0].file_id == 10


def test_build_index_lowercases_md5() -> None:
    rom = _rom_payload(1, "X", fs_name="x", files=[_file_payload(1, "x.iso", md5="ABCDEF")])
    _, by_hash, _ = build_index([rom])
    assert "abcdef" in by_hash
    assert "ABCDEF" not in by_hash


def test_build_index_skips_files_without_md5_in_hash_index() -> None:
    """Platforms without RomM hashes (NON_HASHABLE_PLATFORMS) still get
    name-indexed so name-only classification works."""
    rom = _rom_payload(
        1,
        "Switch Game",
        fs_name="game.nsp",
        files=[_file_payload(1, "game.nsp", md5=None)],
    )
    by_name, by_hash, by_stem = build_index([rom])
    assert "game.nsp" in by_name
    assert by_hash == {}


def test_build_index_handles_multi_file_roms() -> None:
    rom = _rom_payload(
        1,
        "Multi",
        fs_name="multi-disc",
        files=[
            _file_payload(1, "CD1.cue", md5="aaa"),
            _file_payload(2, "CD1.bin", md5="bbb"),
            _file_payload(3, "CD2.cue", md5="ccc"),
            _file_payload(4, "CD2.bin", md5="ddd"),
        ],
    )
    by_name, by_hash, by_stem = build_index([rom])
    assert set(by_name) == {"CD1.cue", "CD1.bin", "CD2.cue", "CD2.bin"}
    assert set(by_hash) == {"aaa", "bbb", "ccc", "ddd"}


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------


def _orphan(path: Path, *, roms_base: Path, platform_dir: str = "gba") -> OrphanCandidate:
    return OrphanCandidate(
        abs_path=path,
        rel_path=path.relative_to(roms_base),
        platform_dir=platform_dir,
    )


def test_classify_confident_when_name_and_hash_both_match(tmp_path: Path) -> None:
    """Common cartridge case: pass-through .gba file matches RomM directly."""
    roms_base = tmp_path / "ROMs"
    target = roms_base / "gba" / "Game.gba"
    target.parent.mkdir(parents=True)
    payload = b"GBA bytes"
    target.write_bytes(payload)

    import hashlib

    md5 = hashlib.md5(payload, usedforsecurity=False).hexdigest()
    rom = _rom_payload(
        1,
        "Game",
        fs_name="Game.gba",
        files=[_file_payload(10, "Game.gba", md5=md5)],
    )
    by_name, by_hash, by_stem = build_index([rom])

    result = classify(_orphan(target, roms_base=roms_base), by_name, by_hash)
    assert isinstance(result, Confident)
    assert result.match.rom_id == 1
    assert result.local_md5 == md5


def test_classify_confident_for_zip_pass_through(tmp_path: Path) -> None:
    """The DOS / multi-file-zip case: orphan is a .zip; ferry hashes the
    largest inner file. RomM stores that same hash."""
    roms_base = tmp_path / "ROMs"
    target = roms_base / "dos" / "Game.zip"
    target.parent.mkdir(parents=True)
    inner_payload = b"BIG INNER FILE" * 100
    with zipfile.ZipFile(target, "w") as z:
        z.writestr("small.exe", b"sm")
        z.writestr("big.bin", inner_payload)

    import hashlib

    inner_md5 = hashlib.md5(inner_payload, usedforsecurity=False).hexdigest()
    rom = _rom_payload(
        1,
        "Game",
        fs_name="Game.zip",
        platform_slug="dos",
        files=[_file_payload(10, "Game.zip", md5=inner_md5)],
    )
    by_name, by_hash, by_stem = build_index([rom])

    result = classify(
        _orphan(target, roms_base=roms_base, platform_dir="dos"),
        by_name,
        by_hash,
    )
    assert isinstance(result, Confident)
    assert result.local_md5 == inner_md5


def test_classify_name_only_when_hash_differs(tmp_path: Path) -> None:
    roms_base = tmp_path / "ROMs"
    target = roms_base / "gba" / "Game.gba"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"local bytes")  # different from RomM's md5

    rom = _rom_payload(
        1,
        "Game",
        fs_name="Game.gba",
        files=[_file_payload(10, "Game.gba", md5="ffffffff" * 4)],
    )
    by_name, by_hash, by_stem = build_index([rom])

    result = classify(_orphan(target, roms_base=roms_base), by_name, by_hash)
    assert isinstance(result, NameOnly)
    assert len(result.candidates) == 1


def test_classify_hash_only_when_name_differs(tmp_path: Path) -> None:
    roms_base = tmp_path / "ROMs"
    target = roms_base / "gba" / "renamed-by-user.gba"
    target.parent.mkdir(parents=True)
    payload = b"matching bytes"
    target.write_bytes(payload)

    import hashlib

    md5 = hashlib.md5(payload, usedforsecurity=False).hexdigest()
    rom = _rom_payload(
        1,
        "Game",
        fs_name="Game.gba",
        files=[_file_payload(10, "Game (USA).gba", md5=md5)],
    )
    by_name, by_hash, by_stem = build_index([rom])

    result = classify(_orphan(target, roms_base=roms_base), by_name, by_hash)
    assert isinstance(result, HashOnly)
    assert result.candidates[0].rom_id == 1


def test_classify_ambiguous_when_two_roms_share_name_and_hash(tmp_path: Path) -> None:
    """Two RomM rom_ids both contain a file with the same name AND same md5
    (e.g., duplicate uploads). Adoption can't pick one — Ambiguous."""
    roms_base = tmp_path / "ROMs"
    target = roms_base / "gba" / "Dup.gba"
    target.parent.mkdir(parents=True)
    payload = b"shared"
    target.write_bytes(payload)

    import hashlib

    md5 = hashlib.md5(payload, usedforsecurity=False).hexdigest()
    rom_a = _rom_payload(
        1,
        "A",
        fs_name="Dup.gba",
        files=[_file_payload(10, "Dup.gba", md5=md5)],
    )
    rom_b = _rom_payload(
        2,
        "B",
        fs_name="Dup.gba",
        files=[_file_payload(20, "Dup.gba", md5=md5)],
    )
    by_name, by_hash, by_stem = build_index([rom_a, rom_b])

    result = classify(_orphan(target, roms_base=roms_base), by_name, by_hash)
    assert isinstance(result, Ambiguous)
    assert {m.rom_id for m in result.matches} == {1, 2}


def test_classify_no_match_when_neither_index_matches(tmp_path: Path) -> None:
    roms_base = tmp_path / "ROMs"
    target = roms_base / "gba" / "Stranger.gba"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"unknown")

    rom = _rom_payload(
        1,
        "Other",
        fs_name="Other.gba",
        files=[_file_payload(10, "Other.gba", md5="00" * 16)],
    )
    by_name, by_hash, by_stem = build_index([rom])

    result = classify(_orphan(target, roms_base=roms_base), by_name, by_hash)
    assert isinstance(result, NoMatch)


def test_classify_no_match_when_rom_listing_is_empty(tmp_path: Path) -> None:
    """Platform with zero RomM ROMs (e.g., user has local files but never
    uploaded) — every orphan is NoMatch."""
    roms_base = tmp_path / "ROMs"
    target = roms_base / "gba" / "Solo.gba"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")

    result = classify(_orphan(target, roms_base=roms_base), by_name={}, by_hash={})
    assert isinstance(result, NoMatch)


# ---------------------------------------------------------------------------
# Stem-equivalence — Confident match when extension differs (the unzip case)
# ---------------------------------------------------------------------------


def test_classify_stem_match_with_hash_match_is_confident(tmp_path: Path) -> None:
    """The unzip case from live testing: server file is `Game.zip`, local
    file is `Game.rvz` (Dolphin-compressed disc image post-unzip). RomM's
    md5 is over the zip's largest inner file (== the local .rvz); stems
    are equal. Should adopt confidently."""
    roms_base = tmp_path / "ROMs"
    target = roms_base / "gc" / "Eternal Darkness - Sanity's Requiem (USA).rvz"
    target.parent.mkdir(parents=True)
    payload = b"RVZ disc bytes" * 1000
    target.write_bytes(payload)

    import hashlib

    md5 = hashlib.md5(payload, usedforsecurity=False).hexdigest()
    rom = _rom_payload(
        1,
        "Eternal Darkness: Sanity's Requiem",
        fs_name="Eternal Darkness - Sanity's Requiem (USA).zip",
        platform_slug="gc",
        files=[_file_payload(10, "Eternal Darkness - Sanity's Requiem (USA).zip", md5=md5)],
    )
    by_name, by_hash, by_stem = build_index([rom])
    result = classify(
        _orphan(target, roms_base=roms_base, platform_dir="gc"),
        by_name,
        by_hash,
        by_stem,
    )
    assert isinstance(result, Confident)
    assert result.match.rom_id == 1
    assert result.local_md5 == md5


def test_classify_stem_match_without_hash_match_is_name_only(tmp_path: Path) -> None:
    """Stem matches but hash doesn't — should NOT promote to Confident.
    Falls through to NameOnly (via the orphan having a different
    revision/region of the same name)."""
    roms_base = tmp_path / "ROMs"
    target = roms_base / "gc" / "Game.iso"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"local revision")  # hash differs from server

    rom = _rom_payload(
        1,
        "Game",
        fs_name="Game.zip",
        platform_slug="gc",
        files=[_file_payload(10, "Game.zip", md5="ff" * 16)],
    )
    by_name, by_hash, by_stem = build_index([rom])
    result = classify(
        _orphan(target, roms_base=roms_base, platform_dir="gc"),
        by_name,
        by_hash,
        by_stem,
    )
    # by_stem has `Game` → server's `Game.zip`. Stem matches but hash
    # differs — so the stem-match upgrade doesn't fire. The orphan's
    # full filename `Game.iso` doesn't appear in by_name. Result:
    # neither name nor hash match → NoMatch (not NameOnly, since
    # NameOnly requires the FULL filename to be in by_name).
    assert isinstance(result, NoMatch)


def test_classify_different_stem_same_hash_stays_hash_only(tmp_path: Path) -> None:
    """Hash match without stem match: user renamed the file. Stays
    HashOnly (won't be auto-adopted by the stem-equivalence path)."""
    roms_base = tmp_path / "ROMs"
    target = roms_base / "gba" / "renamed-by-user.gba"
    target.parent.mkdir(parents=True)
    payload = b"matching bytes"
    target.write_bytes(payload)

    import hashlib

    md5 = hashlib.md5(payload, usedforsecurity=False).hexdigest()
    rom = _rom_payload(
        1,
        "Game",
        fs_name="Pristine (USA).gba",
        files=[_file_payload(10, "Pristine (USA).gba", md5=md5)],
    )
    by_name, by_hash, by_stem = build_index([rom])
    result = classify(_orphan(target, roms_base=roms_base), by_name, by_hash, by_stem)
    assert isinstance(result, HashOnly)


def test_build_index_indexes_stems(tmp_path: Path) -> None:
    """Verify the new `by_stem` map is populated with extension-stripped
    keys, skipping files where stem == filename (no extension)."""
    rom = _rom_payload(
        1,
        "Game",
        fs_name="Game.zip",
        files=[
            _file_payload(10, "Game.zip", md5="abc"),
            _file_payload(11, "Game (no extension)", md5="def"),  # stem == name → skipped
        ],
    )
    _, _, by_stem = build_index([rom])
    assert "Game" in by_stem
    assert by_stem["Game"][0].file_id == 10
    assert "Game (no extension)" not in by_stem  # filename has no extension


# ---------------------------------------------------------------------------
# synthesize_state
# ---------------------------------------------------------------------------


def test_synthesize_state_for_pass_through_orphan(tmp_path: Path) -> None:
    roms_base = tmp_path / "ROMs"
    target = roms_base / "gba" / "Game.gba"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"GBA cart")

    import hashlib

    bytes_md5 = hashlib.md5(b"GBA cart", usedforsecurity=False).hexdigest()
    file_data = _file_payload(10, "Game.gba", md5=bytes_md5)
    rom_data = _rom_payload(
        1,
        "Game",
        fs_name="Game.gba",
        platform_slug="gba",
        files=[file_data],
        updated_at="2026-04-25T12:00:00Z",
        fs_size_bytes=8,
    )
    confident = Confident(
        orphan=_orphan(target, roms_base=roms_base),
        match=__import__("ferry.services.reconcile", fromlist=["MatchedFile"]).MatchedFile(
            rom_id=1,
            file_id=10,
            rom_name="Game",
            file_name="Game.gba",
            file_md5=bytes_md5,
            rom_data=rom_data,
            file_data=file_data,
        ),
        local_md5=bytes_md5,
    )

    state = synthesize_state(
        confident,
        roms_base=roms_base,
        transforms_for_platform=(),
        now_iso="2026-05-14T00:00:00Z",
    )
    assert state.rom_id == 1
    assert state.platform_slug == "gba"
    assert state.name == "Game"
    assert state.source_filename == "Game.gba"
    assert state.source_md5 == bytes_md5
    assert state.source_updated_at == "2026-04-25T12:00:00Z"
    assert state.transforms == ()
    assert len(state.outputs) == 1
    assert state.outputs[0].path == "gba/Game.gba"
    assert state.outputs[0].md5 == bytes_md5
    assert state.outputs[0].size == len(b"GBA cart")
    assert state.synced_at == "2026-05-14T00:00:00Z"


def test_synthesize_state_uses_direct_bytes_md5_for_zip_orphan(
    tmp_path: Path,
) -> None:
    """For pass-through `.zip` orphans: `output.md5` is md5 of the zip BYTES
    (matches what ferry's executor would store after a real download), NOT
    the largest-inner-file md5 used for matching."""
    roms_base = tmp_path / "ROMs"
    target = roms_base / "gba" / "Game.zip"
    target.parent.mkdir(parents=True)
    inner = b"GBA INNER" * 50
    with zipfile.ZipFile(target, "w") as z:
        z.writestr("Game.gba", inner)

    import hashlib

    inner_md5 = hashlib.md5(inner, usedforsecurity=False).hexdigest()
    zip_bytes_md5 = hashlib.md5(target.read_bytes(), usedforsecurity=False).hexdigest()

    file_data = _file_payload(10, "Game.zip", md5=inner_md5)
    rom_data = _rom_payload(
        1,
        "Game",
        fs_name="Game.zip",
        platform_slug="gba",
        files=[file_data],
    )
    confident = Confident(
        orphan=_orphan(target, roms_base=roms_base),
        match=__import__("ferry.services.reconcile", fromlist=["MatchedFile"]).MatchedFile(
            rom_id=1,
            file_id=10,
            rom_name="Game",
            file_name="Game.zip",
            file_md5=inner_md5,
            rom_data=rom_data,
            file_data=file_data,
        ),
        local_md5=inner_md5,  # the matching hash is inner-content
    )

    state = synthesize_state(
        confident,
        roms_base=roms_base,
        transforms_for_platform=(),
        now_iso="2026-05-14T00:00:00Z",
    )
    # source_md5 reflects the matching server-side md5 (largest inner).
    assert state.source_md5 == inner_md5
    # output.md5 reflects the local file's BYTES — the ZIP bytes here.
    assert state.outputs[0].md5 == zip_bytes_md5
    assert zip_bytes_md5 != inner_md5  # they're genuinely different


def test_synthesize_state_records_unzip_pipeline_transforms(tmp_path: Path) -> None:
    """Unzipped local ISO with an unzip transform — output.md5 collapses to
    file-bytes (no archive interpretation), which equals RomM's md5."""
    roms_base = tmp_path / "ROMs"
    target = roms_base / "gc" / "Pikmin.iso"
    target.parent.mkdir(parents=True)
    payload = b"ISO content " * 100
    target.write_bytes(payload)

    import hashlib

    iso_md5 = hashlib.md5(payload, usedforsecurity=False).hexdigest()
    file_data = _file_payload(10, "Pikmin.zip", md5=iso_md5)
    rom_data = _rom_payload(
        1, "Pikmin", fs_name="Pikmin.zip", platform_slug="gc", files=[file_data]
    )
    confident = Confident(
        orphan=_orphan(target, roms_base=roms_base, platform_dir="gc"),
        match=__import__("ferry.services.reconcile", fromlist=["MatchedFile"]).MatchedFile(
            rom_id=1,
            file_id=10,
            rom_name="Pikmin",
            file_name="Pikmin.zip",
            file_md5=iso_md5,
            rom_data=rom_data,
            file_data=file_data,
        ),
        local_md5=iso_md5,
    )
    state = synthesize_state(
        confident,
        roms_base=roms_base,
        transforms_for_platform=("unzip",),
        now_iso="2026-05-14T00:00:00Z",
    )
    assert state.transforms == ("unzip",)
    assert state.outputs[0].md5 == iso_md5  # file IS the ISO; bytes-md5 == iso_md5
    assert state.source_md5 == iso_md5


def test_synthesize_state_used_now_default_when_now_iso_omitted(tmp_path: Path) -> None:
    roms_base = tmp_path / "ROMs"
    target = roms_base / "gba" / "Game.gba"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")

    import hashlib

    md5 = hashlib.md5(b"x", usedforsecurity=False).hexdigest()
    file_data = _file_payload(10, "Game.gba", md5=md5)
    rom_data = _rom_payload(1, "G", fs_name="Game.gba", files=[file_data])
    confident = Confident(
        orphan=_orphan(target, roms_base=roms_base),
        match=__import__("ferry.services.reconcile", fromlist=["MatchedFile"]).MatchedFile(
            rom_id=1,
            file_id=10,
            rom_name="G",
            file_name="Game.gba",
            file_md5=md5,
            rom_data=rom_data,
            file_data=file_data,
        ),
        local_md5=md5,
    )

    state = synthesize_state(confident, roms_base=roms_base, transforms_for_platform=())
    # Just verify it produced a Z-suffixed ISO timestamp; exact value drifts.
    assert state.synced_at.endswith("Z")
    assert "T" in state.synced_at


def _make_rom(
    rom_id: int = 1,
    *,
    outputs: tuple[TransformedOutput, ...] | None = None,
    primary_output_index: int = 0,
) -> RomState:
    return RomState(
        rom_id=rom_id,
        platform_slug="gba",
        name=f"Rom {rom_id}",
        source_filename=f"Rom{rom_id}.zip",
        source_md5="abc",
        source_size=10,
        source_updated_at="2026-01-01T00:00:00Z",
        transforms=(),
        outputs=outputs or (TransformedOutput(path=f"gba/Rom{rom_id}.gba", md5="d", size=1),),
        primary_output_index=primary_output_index,
        synced_at="2026-01-01T00:00:01Z",
    )
