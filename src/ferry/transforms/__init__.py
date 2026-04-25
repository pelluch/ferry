"""Transform pipeline framework.

Each transform takes a list of input file paths and an output directory, and
returns the list of output paths it produced. Transforms run out-of-place —
they never mutate inputs in place — so a failed pipeline leaves the original
inputs untouched (DESIGN.md §5.5).

The registry below maps user-facing names (TOML config strings) to callables.
Adding a new transform is one entry plus its module.
"""

from ferry.transforms.errors import TransformError, UnknownTransformError
from ferry.transforms.types import TransformFn
from ferry.transforms.unzip import unzip

TRANSFORMS: dict[str, TransformFn] = {
    "unzip": unzip,
}


def get_transform(name: str) -> TransformFn:
    if name not in TRANSFORMS:
        known = ", ".join(sorted(TRANSFORMS))
        raise UnknownTransformError(f"unknown transform {name!r}; known: {known}")
    return TRANSFORMS[name]


def known_transforms() -> frozenset[str]:
    return frozenset(TRANSFORMS)


__all__ = [
    "TRANSFORMS",
    "TransformError",
    "TransformFn",
    "UnknownTransformError",
    "get_transform",
    "known_transforms",
    "unzip",
]
