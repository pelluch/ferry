import zipfile
from pathlib import Path

import pytest

from ferry.services.pipeline import run_pipeline
from ferry.transforms import TRANSFORMS, UnknownTransformError
from ferry.transforms.errors import TransformError


def make_zip(path: Path, members: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


# ---------------------------------------------------------------------------
# Passthrough (empty pipeline)
# ---------------------------------------------------------------------------


def test_empty_pipeline_moves_source_to_final_dir(tmp_path: Path) -> None:
    source = tmp_path / "scratch" / "Game.iso"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"iso-bytes")
    final_dir = tmp_path / "roms" / "gc"
    scratch = tmp_path / "scratch" / "pipeline"

    outputs = run_pipeline(
        source_path=source,
        transforms=[],
        final_dir=final_dir,
        scratch_dir=scratch,
    )
    assert outputs == [final_dir / "Game.iso"]
    assert outputs[0].read_bytes() == b"iso-bytes"
    assert not source.exists()  # moved, not copied


# ---------------------------------------------------------------------------
# Single-step pipeline (unzip)
# ---------------------------------------------------------------------------


def test_unzip_step_produces_extracted_files_in_final_dir(tmp_path: Path) -> None:
    source = make_zip(tmp_path / "scratch" / "Pikmin.zip", {"Pikmin.iso": b"iso"})
    final_dir = tmp_path / "roms" / "gc"
    scratch = tmp_path / "scratch" / "pipeline"

    outputs = run_pipeline(
        source_path=source,
        transforms=["unzip"],
        final_dir=final_dir,
        scratch_dir=scratch,
    )
    assert outputs == [final_dir / "Pikmin.iso"]
    assert outputs[0].read_bytes() == b"iso"
    # Scratch is cleaned up on success.
    assert not scratch.exists()


def test_unzip_step_with_multi_file_archive(tmp_path: Path) -> None:
    source = make_zip(
        tmp_path / "scratch" / "Multi.zip",
        {"CD1.cue": b"c1", "CD1.bin": b"d1", "CD2.cue": b"c2", "CD2.bin": b"d2"},
    )
    final_dir = tmp_path / "roms" / "psx"
    scratch = tmp_path / "scratch" / "pipeline"

    outputs = run_pipeline(
        source_path=source,
        transforms=["unzip"],
        final_dir=final_dir,
        scratch_dir=scratch,
    )
    assert {p.name for p in outputs} == {"CD1.cue", "CD1.bin", "CD2.cue", "CD2.bin"}
    for p in outputs:
        assert p.parent == final_dir


# ---------------------------------------------------------------------------
# Multi-step pipeline (chained transforms)
# ---------------------------------------------------------------------------


def test_chained_transforms_pass_outputs_forward(tmp_path: Path, monkeypatch) -> None:
    """Use a fake second transform to verify the pipeline chains correctly."""
    calls: list[tuple[list[Path], Path]] = []

    def renamer(inputs: list[Path], output_dir: Path) -> list[Path]:
        """Rename each input to <name>.processed and copy to output_dir."""
        calls.append((list(inputs), output_dir))
        output_dir.mkdir(parents=True, exist_ok=True)
        outputs = []
        for src in inputs:
            target = output_dir / f"{src.name}.processed"
            target.write_bytes(src.read_bytes())
            outputs.append(target)
        return outputs

    monkeypatch.setitem(TRANSFORMS, "rename", renamer)

    source = make_zip(tmp_path / "scratch" / "Game.zip", {"a.iso": b"data"})
    final_dir = tmp_path / "roms" / "gc"
    scratch = tmp_path / "scratch" / "pipeline"

    outputs = run_pipeline(
        source_path=source,
        transforms=["unzip", "rename"],
        final_dir=final_dir,
        scratch_dir=scratch,
    )
    assert outputs == [final_dir / "a.iso.processed"]
    assert outputs[0].read_bytes() == b"data"
    # Two steps were called: unzip first, then rename.
    assert len(calls) == 1  # only `rename` was registered through monkeypatch
    # Rename's input was unzip's output (a.iso under step-0-unzip/).
    rename_inputs = calls[0][0]
    assert [p.name for p in rename_inputs] == ["a.iso"]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_unknown_transform_name_raises_before_touching_filesystem(
    tmp_path: Path,
) -> None:
    source = tmp_path / "scratch" / "Game.iso"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"x")
    final_dir = tmp_path / "roms" / "gc"
    scratch = tmp_path / "scratch" / "pipeline"

    with pytest.raises(UnknownTransformError):
        run_pipeline(
            source_path=source,
            transforms=["never-heard-of"],
            final_dir=final_dir,
            scratch_dir=scratch,
        )
    # Source is intact; final_dir has nothing.
    assert source.exists()
    assert not (final_dir / "Game.iso").exists()


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_failure_mid_pipeline_leaves_final_dir_empty(tmp_path: Path, monkeypatch) -> None:
    def boom(inputs: list[Path], output_dir: Path) -> list[Path]:
        raise TransformError("boom")

    monkeypatch.setitem(TRANSFORMS, "boom", boom)

    source = make_zip(tmp_path / "scratch" / "Game.zip", {"a.iso": b"x"})
    final_dir = tmp_path / "roms" / "gc"
    scratch = tmp_path / "scratch" / "pipeline"

    with pytest.raises(TransformError, match="boom"):
        run_pipeline(
            source_path=source,
            transforms=["unzip", "boom"],
            final_dir=final_dir,
            scratch_dir=scratch,
        )
    # final_dir created (via mkdir) but empty.
    assert final_dir.exists()
    assert list(final_dir.iterdir()) == []
    # Scratch survives for debugging.
    assert scratch.exists()


def test_transform_returning_no_outputs_raises(tmp_path: Path, monkeypatch) -> None:
    def empties(inputs: list[Path], output_dir: Path) -> list[Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        return []

    monkeypatch.setitem(TRANSFORMS, "empties", empties)

    source = tmp_path / "scratch" / "Game.iso"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"x")
    final_dir = tmp_path / "roms" / "gc"
    scratch = tmp_path / "scratch" / "pipeline"

    with pytest.raises(TransformError, match="no outputs"):
        run_pipeline(
            source_path=source,
            transforms=["empties"],
            final_dir=final_dir,
            scratch_dir=scratch,
        )
