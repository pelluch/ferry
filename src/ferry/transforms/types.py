from collections.abc import Callable
from pathlib import Path

# A transform: (inputs, output_dir) -> outputs. The transform writes any new
# files into output_dir, returns their paths, and never mutates inputs in place.
TransformFn = Callable[[list[Path], Path], list[Path]]
