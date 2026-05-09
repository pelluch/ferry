"""`unzip` transform — extract .zip archives, pass through everything else.

This is the v1 default transform for zip-hostile platforms (GC, Wii, PS2, PS3,
3DS, Switch, Xbox, …). RetroArch reads zips natively, so platforms that route
through it pass through untransformed (`pipeline = []`).

Behavior:
- Single .zip input → extract members, preserving internal directory structure,
  return the list of extracted file paths sorted for determinism.
- Single non-.zip input → return [input] unchanged. This makes `unzip` safe to
  configure for platforms with mixed archive formats; non-archives flow through.
- Empty zip → raises (treat as a corrupt archive, not a no-op).
- Path traversal in zip members (`..`, absolute paths) → raises. We refuse to
  extract anywhere outside `output_dir`.
"""

import shutil
import zipfile
from pathlib import Path

from ferry.transforms.errors import TransformError


def unzip(inputs: list[Path], output_dir: Path) -> list[Path]:
    if len(inputs) != 1:
        raise TransformError(f"unzip expects exactly 1 input, got {len(inputs)}")
    src = inputs[0]
    if src.suffix.lower() != ".zip":
        return [src]

    output_dir.mkdir(parents=True, exist_ok=True)
    output_root = output_dir.resolve()

    extracted: list[Path] = []
    try:
        with zipfile.ZipFile(src) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if is_unsafe_zip_member(info.filename):
                    raise TransformError(
                        f"refusing to extract unsafe path from {src.name}: {info.filename!r}"
                    )
                target = (output_dir / info.filename).resolve()
                if not is_within_dir(target, output_root):
                    raise TransformError(
                        f"refusing to extract {info.filename!r} from {src.name}: "
                        f"escapes output directory"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src_f, target.open("wb") as dst_f:
                    shutil.copyfileobj(src_f, dst_f)
                extracted.append(target)
    except zipfile.BadZipFile as e:
        raise TransformError(f"corrupt zip {src}: {e}") from e

    if not extracted:
        raise TransformError(f"zip {src.name} contains no files")

    return sorted(extracted)


def is_unsafe_zip_member(filename: str) -> bool:
    """Reject obviously-malicious entries before we resolve the path."""
    if not filename:
        return True
    if filename.startswith("/") or filename.startswith("\\"):
        return True
    p = Path(filename)
    if p.is_absolute():
        return True
    return ".." in p.parts


def is_within_dir(target: Path, root: Path) -> bool:
    try:
        return target.is_relative_to(root)
    except ValueError:
        return False
