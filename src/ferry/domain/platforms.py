"""Map RomM `platform_slug` values to ES-DE on-disk directory names.

RomM's slugs (`game-boy-advance`, `nintendo-switch`, `playstation-2`) don't
always match what ES-DE expects on disk (`gba`, `switch`, `ps2`). The mapping
data is shipped as JSON next to this module — lifted from decky-romm-sync,
which curated it for RetroDECK; bare ES-DE agrees for every platform we've
spot-checked, but the JSON is editable if a divergence surfaces.

`resolve_platform_dir(slug)` returns the mapped name, or the slug unchanged
if no mapping exists. Unknown platforms still get a directory — they just
keep the RomM slug — so a sync that includes a freshly-added RomM platform
won't fail; it'll just land at a possibly-non-canonical path until we update
the map.
"""

from __future__ import annotations

import json
from functools import cache
from importlib import resources

_MAP_RESOURCE = "ferry.data"
_MAP_FILENAME = "platform_map.json"


@cache
def _load_map() -> dict[str, str]:
    text = resources.files(_MAP_RESOURCE).joinpath(_MAP_FILENAME).read_text()
    raw = json.loads(text)
    mapping = raw.get("platform_map")
    if not isinstance(mapping, dict):
        raise RuntimeError(f"{_MAP_FILENAME} is missing the platform_map object")
    return {k: v for k, v in mapping.items() if isinstance(v, str)}


def resolve_platform_dir(slug: str) -> str:
    """Return the on-disk directory name for a RomM platform slug.

    Falls back to *slug* unchanged when no mapping exists, so unknown
    platforms still get a stable directory.
    """
    return _load_map().get(slug, slug)


def known_platforms() -> frozenset[str]:
    """Return the set of RomM slugs we have an explicit mapping for."""
    return frozenset(_load_map())
