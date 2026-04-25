"""Transform pipeline runner.

Chains transforms together, lands the final outputs in `final_dir` atomically,
and cleans up scratch on success. On failure, scratch is left in place so the
operator can inspect what happened.

Contract:
- The caller provides a `scratch_dir` that the pipeline can use as workspace.
  The pipeline will create per-step subdirectories inside it.
- The source file may be moved or consumed — the pipeline does not promise
  to leave it untouched. Callers (the download path) put the source in scratch
  so this is fine.
- On success: outputs are atomically present in `final_dir`; intermediate
  scratch is removed.
- On failure: `final_dir` is untouched; scratch is kept for debugging.
"""

import shutil
from pathlib import Path

from ferry.transforms import get_transform


def run_pipeline(
    *,
    source_path: Path,
    transforms: list[str] | tuple[str, ...],
    final_dir: Path,
    scratch_dir: Path,
) -> list[Path]:
    """Run *transforms* on *source_path*; return absolute paths in final_dir."""
    final_dir.mkdir(parents=True, exist_ok=True)
    scratch_dir.mkdir(parents=True, exist_ok=True)

    # Pre-validate every named transform exists before we touch the filesystem.
    fns = [(name, get_transform(name)) for name in transforms]

    if not fns:
        return [_move_into(source_path, final_dir)]

    succeeded = False
    try:
        current_inputs: list[Path] = [source_path]
        for i, (name, fn) in enumerate(fns):
            step_dir = scratch_dir / f"step-{i}-{name}"
            current_inputs = fn(current_inputs, step_dir)
            if not current_inputs:
                from ferry.transforms.errors import TransformError

                raise TransformError(f"transform {name!r} produced no outputs at step {i}")

        final_outputs = [_move_into(p, final_dir) for p in current_inputs]
        succeeded = True
        return final_outputs
    finally:
        if succeeded:
            shutil.rmtree(scratch_dir, ignore_errors=True)


def _move_into(path: Path, dest_dir: Path) -> Path:
    """Move *path* into *dest_dir*, returning the final location."""
    target = dest_dir / path.name
    shutil.move(str(path), str(target))
    return target
