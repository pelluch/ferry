"""Tests for ferry.adapters.dolphin.dolphin_config.parse_dolphin_ini."""

from __future__ import annotations

from pathlib import Path

from ferry.adapters.dolphin.dolphin_config import (
    SLOT_TYPE_MEMORY_CARD,
    SLOT_TYPE_MEMORY_CARD_FOLDER,
    SLOT_TYPE_NONE,
    parse_dolphin_ini,
)


def _write_ini(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


# ---------------------------------------------------------------------------
# Defaults / missing inputs
# ---------------------------------------------------------------------------


def test_missing_file_returns_none(tmp_path: Path) -> None:
    assert parse_dolphin_ini(tmp_path / "Dolphin.ini") is None


def test_empty_ini_uses_modern_defaults(tmp_path: Path) -> None:
    """Modern Dolphin defaults: SlotA=GCI Folder (8), SlotB=None (255)."""
    ini = _write_ini(tmp_path / "Dolphin.ini", "")
    result = parse_dolphin_ini(ini)
    assert result is not None
    assert result.slot_a_raw == SLOT_TYPE_MEMORY_CARD_FOLDER
    assert result.slot_b_raw == SLOT_TYPE_NONE
    assert result.slot_a_mode == "gci_folder"
    assert result.slot_b_mode == "none"


def test_core_section_without_slot_keys_uses_defaults(tmp_path: Path) -> None:
    """A [Core] section that doesn't mention SlotA/SlotB falls through to defaults."""
    ini = _write_ini(
        tmp_path / "Dolphin.ini",
        "[Core]\nGFXBackend = Vulkan\nSIDevice0 = 6\n",
    )
    result = parse_dolphin_ini(ini)
    assert result is not None
    assert result.slot_a_raw == SLOT_TYPE_MEMORY_CARD_FOLDER


# ---------------------------------------------------------------------------
# Slot value parsing
# ---------------------------------------------------------------------------


def test_slot_a_gci_folder_explicitly(tmp_path: Path) -> None:
    ini = _write_ini(tmp_path / "Dolphin.ini", "[Core]\nSlotA = 8\nSlotB = 255\n")
    result = parse_dolphin_ini(ini)
    assert result is not None
    assert result.slot_a_mode == "gci_folder"
    assert result.slot_b_mode == "none"


def test_slot_a_raw_memcard_classified(tmp_path: Path) -> None:
    """SlotA = 1 (raw .raw memcard) is the v3-unsupported case."""
    ini = _write_ini(tmp_path / "Dolphin.ini", "[Core]\nSlotA = 1\n")
    result = parse_dolphin_ini(ini)
    assert result is not None
    assert result.slot_a_raw == SLOT_TYPE_MEMORY_CARD
    assert result.slot_a_mode == "raw_memcard"


def test_slot_a_unknown_value_classified_other(tmp_path: Path) -> None:
    """Other EXIDeviceType values (microphone, AGP, ethernet, modem) → 'other'."""
    ini = _write_ini(tmp_path / "Dolphin.ini", "[Core]\nSlotA = 6\n")
    result = parse_dolphin_ini(ini)
    assert result is not None
    assert result.slot_a_raw == 6
    assert result.slot_a_mode == "other"


def test_slot_b_can_also_be_gci_folder(tmp_path: Path) -> None:
    """User's RetroDECK install showed SlotB = 8 in flatpak Dolphin.ini —
    valid configuration (two GCI Folder slots)."""
    ini = _write_ini(tmp_path / "Dolphin.ini", "[Core]\nSlotA = 8\nSlotB = 8\n")
    result = parse_dolphin_ini(ini)
    assert result is not None
    assert result.slot_b_mode == "gci_folder"


# ---------------------------------------------------------------------------
# Section scoping — keys outside [Core] must be ignored
# ---------------------------------------------------------------------------


def test_slotA_outside_core_section_is_ignored(tmp_path: Path) -> None:
    """A key named `SlotA` under [Display] or any non-Core section is some
    other Dolphin setting (or junk); ignore."""
    ini = _write_ini(
        tmp_path / "Dolphin.ini",
        "[Display]\nSlotA = 1\n[Core]\nSlotA = 8\n",
    )
    result = parse_dolphin_ini(ini)
    assert result is not None
    assert result.slot_a_mode == "gci_folder"


def test_slotA_only_outside_core_uses_default(tmp_path: Path) -> None:
    ini = _write_ini(tmp_path / "Dolphin.ini", "[Display]\nSlotA = 1\n")
    result = parse_dolphin_ini(ini)
    assert result is not None
    assert result.slot_a_raw == SLOT_TYPE_MEMORY_CARD_FOLDER  # default


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_comments_are_ignored(tmp_path: Path) -> None:
    ini = _write_ini(
        tmp_path / "Dolphin.ini",
        "# SlotA = 1\n; SlotA = 1\n[Core]\nSlotA = 8\n",
    )
    result = parse_dolphin_ini(ini)
    assert result is not None
    assert result.slot_a_mode == "gci_folder"


def test_non_integer_slot_value_falls_through_to_default(tmp_path: Path) -> None:
    ini = _write_ini(tmp_path / "Dolphin.ini", "[Core]\nSlotA = potato\n")
    result = parse_dolphin_ini(ini)
    assert result is not None
    assert result.slot_a_raw == SLOT_TYPE_MEMORY_CARD_FOLDER  # default
