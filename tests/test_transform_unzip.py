import zipfile
from pathlib import Path

import pytest

from ferry.transforms.errors import TransformError
from ferry.transforms.unzip import unzip


def make_zip(path: Path, members: dict[str, bytes]) -> Path:
    """Create a zip at *path* containing *members* (filename -> bytes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


# ---------------------------------------------------------------------------
# Happy-path extraction
# ---------------------------------------------------------------------------


def test_single_file_zip_extracts(tmp_path: Path) -> None:
    src = make_zip(tmp_path / "src" / "Pikmin.zip", {"Pikmin.iso": b"iso-bytes"})
    out_dir = tmp_path / "out"
    outputs = unzip([src], out_dir)
    assert outputs == [out_dir / "Pikmin.iso"]
    assert outputs[0].read_bytes() == b"iso-bytes"


def test_multi_file_zip_extracts_all_sorted(tmp_path: Path) -> None:
    src = make_zip(
        tmp_path / "src" / "Game.zip",
        {"CD2.bin": b"d2", "CD1.cue": b"c1", "CD1.bin": b"d1", "CD2.cue": b"c2"},
    )
    out_dir = tmp_path / "out"
    outputs = unzip([src], out_dir)
    assert [p.name for p in outputs] == ["CD1.bin", "CD1.cue", "CD2.bin", "CD2.cue"]
    for p in outputs:
        assert p.exists()


def test_preserves_internal_directory_structure(tmp_path: Path) -> None:
    src = make_zip(
        tmp_path / "src" / "MultiDisc.zip",
        {"Disc1/CD.cue": b"c1", "Disc1/CD.bin": b"d1", "Disc2/CD.cue": b"c2"},
    )
    out_dir = tmp_path / "out"
    outputs = unzip([src], out_dir)
    assert (out_dir / "Disc1" / "CD.cue") in outputs
    assert (out_dir / "Disc2" / "CD.cue") in outputs


def test_creates_output_dir_if_missing(tmp_path: Path) -> None:
    src = make_zip(tmp_path / "src" / "X.zip", {"x.iso": b"x"})
    out_dir = tmp_path / "deep" / "nested" / "out"
    assert not out_dir.exists()
    unzip([src], out_dir)
    assert out_dir.is_dir()


# ---------------------------------------------------------------------------
# Passthrough for non-zip
# ---------------------------------------------------------------------------


def test_non_zip_input_is_passthrough(tmp_path: Path) -> None:
    src = tmp_path / "Game.iso"
    src.write_bytes(b"already-extracted")
    out_dir = tmp_path / "out"
    outputs = unzip([src], out_dir)
    assert outputs == [src]
    # Output dir should NOT have been touched in passthrough mode.
    assert not out_dir.exists() or not list(out_dir.iterdir())


def test_uppercase_zip_extension_is_recognized(tmp_path: Path) -> None:
    src = make_zip(tmp_path / "src" / "Game.zip", {"a.iso": b"a"})
    upper = src.rename(src.parent / "Game.ZIP")
    outputs = unzip([upper], tmp_path / "out")
    assert outputs == [tmp_path / "out" / "a.iso"]


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_zero_inputs_raises(tmp_path: Path) -> None:
    with pytest.raises(TransformError, match="exactly 1 input"):
        unzip([], tmp_path / "out")


def test_multiple_inputs_raises(tmp_path: Path) -> None:
    a = tmp_path / "a.zip"
    b = tmp_path / "b.zip"
    a.write_bytes(b"")
    b.write_bytes(b"")
    with pytest.raises(TransformError, match="exactly 1 input"):
        unzip([a, b], tmp_path / "out")


def test_corrupt_zip_raises(tmp_path: Path) -> None:
    src = tmp_path / "broken.zip"
    src.write_bytes(b"not-a-zip-file")
    with pytest.raises(TransformError, match="corrupt zip"):
        unzip([src], tmp_path / "out")


def test_empty_zip_raises(tmp_path: Path) -> None:
    src = make_zip(tmp_path / "empty.zip", {})
    with pytest.raises(TransformError, match="contains no files"):
        unzip([src], tmp_path / "out")


def test_path_traversal_member_refused(tmp_path: Path) -> None:
    src = tmp_path / "evil.zip"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("../escape.txt", b"pwned")
    with pytest.raises(TransformError, match="unsafe path"):
        unzip([src], tmp_path / "out")


def test_absolute_path_member_refused(tmp_path: Path) -> None:
    src = tmp_path / "abs.zip"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("/etc/passwd", b"pwned")
    with pytest.raises(TransformError, match="unsafe path"):
        unzip([src], tmp_path / "out")


def test_file_with_dotdot_in_middle_refused(tmp_path: Path) -> None:
    src = tmp_path / "evil2.zip"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("legit/../escape.txt", b"pwned")
    with pytest.raises(TransformError, match="unsafe path"):
        unzip([src], tmp_path / "out")
