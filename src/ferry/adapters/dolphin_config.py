"""Parse Dolphin's `Dolphin.ini` for memcard-mode-related settings.

`Dolphin.ini` is a Qt-style INI: `[Section]` headers + `key = value` lines,
case-sensitive section names. ferry only needs two keys from `[Core]`:

- `SlotA` — Memory Card Slot A device type. The `EXIDeviceType` enum
  (in Dolphin source `Source/Core/Core/HW/EXI/EXI_Device.h`) treats:
  `1 = MemoryCard` (raw `.raw` file, all games share),
  `8 = MemoryCardFolder` (GCI Folder mode — per-game `.gci` files),
  `255 = None` (slot empty/disabled). Other values exist (microphone,
  AGP, modem, ethernet) and aren't memcards.
- `SlotB` — same enum, the second Memory Card slot. Defaults to `None`
  on a fresh Dolphin install; users only populate it when running
  multi-card games (Animal Crossing's "Island Boy" travel save) or
  copying between cards. v3 doesn't sync Slot B (DESIGN.md §5.3).

When either key is absent, modern Dolphin's defaults apply:
`MAIN_SLOT_A = MemoryCardFolder` (8), `MAIN_SLOT_B = None` (255). These
defaults flipped from raw `.raw` to GCI-folder during Dolphin 5.0 →
mainline; older installs that haven't touched config may still default
to raw. We honor the modern defaults — users on ancient builds will see
their actual on-disk save layout regardless via `MemcardMode`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

# EXIDeviceType enum values relevant to memcards. See Dolphin source
# `Source/Core/Core/HW/EXI/EXI_Device.h::EXIDeviceType`.
SLOT_TYPE_MEMORY_CARD = 1
SLOT_TYPE_MEMORY_CARD_FOLDER = 8
SLOT_TYPE_NONE = 255

# Dolphin defaults (`MainSettings.cpp::MAIN_SLOT_A` / `MAIN_SLOT_B`) when
# the keys are absent from the INI.
_DEFAULT_SLOT_A = SLOT_TYPE_MEMORY_CARD_FOLDER
_DEFAULT_SLOT_B = SLOT_TYPE_NONE

MemcardMode = Literal["gci_folder", "raw_memcard", "none", "other"]


@dataclass(frozen=True, slots=True, kw_only=True)
class DolphinSettings:
    """Memcard-related settings parsed out of a single `Dolphin.ini`.

    `slot_a_raw` / `slot_b_raw` are the integer enum values as written
    (or defaulted) in the INI. `slot_a_mode` / `slot_b_mode` translate
    those into ferry's narrower vocabulary — useful for status display
    and ferry's own decisions, while the raw int stays available for
    diagnostics if a user encounters an unexpected value.
    """

    config_path: Path
    slot_a_raw: int
    slot_b_raw: int

    @property
    def slot_a_mode(self) -> MemcardMode:
        return _classify(self.slot_a_raw)

    @property
    def slot_b_mode(self) -> MemcardMode:
        return _classify(self.slot_b_raw)


def parse_dolphin_ini(config_path: Path) -> DolphinSettings | None:
    """Return memcard settings from `Dolphin.ini`, or None if it doesn't exist.

    Missing/unreadable INI returns None; the caller treats that as "this
    Dolphin user dir hasn't been initialized yet" (Dolphin writes the file
    on first launch). Malformed lines are tolerated — we only fail on
    fundamental I/O issues.
    """
    if not config_path.is_file():
        return None
    try:
        text = config_path.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("could not read %s: %s", config_path, exc)
        return None

    slot_a = _DEFAULT_SLOT_A
    slot_b = _DEFAULT_SLOT_B
    in_core = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_core = line == "[Core]"
            continue
        if not in_core or "=" not in line:
            continue
        key, _, raw_value = line.partition("=")
        key = key.strip()
        value = raw_value.strip()
        if key == "SlotA":
            parsed = _parse_int(value)
            if parsed is not None:
                slot_a = parsed
        elif key == "SlotB":
            parsed = _parse_int(value)
            if parsed is not None:
                slot_b = parsed

    return DolphinSettings(config_path=config_path, slot_a_raw=slot_a, slot_b_raw=slot_b)


def _parse_int(raw: str) -> int | None:
    try:
        return int(raw)
    except ValueError:
        return None


def _classify(raw: int) -> MemcardMode:
    if raw == SLOT_TYPE_MEMORY_CARD_FOLDER:
        return "gci_folder"
    if raw == SLOT_TYPE_MEMORY_CARD:
        return "raw_memcard"
    if raw == SLOT_TYPE_NONE:
        return "none"
    return "other"
