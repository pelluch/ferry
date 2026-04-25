import json

import pytest

from ferry.domain.state import (
    CURRENT_SCHEMA_VERSION,
    LibraryState,
    StateDecodeError,
    StateSchemaError,
    from_json,
    rom_from_json,
    rom_to_json,
    to_json,
)

# ---------------------------------------------------------------------------
# LibraryState roundtrips
# ---------------------------------------------------------------------------


def test_empty_state_roundtrips() -> None:
    s = LibraryState()
    decoded = from_json(to_json(s))
    assert decoded == s
    assert decoded.schema_version == CURRENT_SCHEMA_VERSION
    assert decoded.last_updated_after is None
    assert decoded.roms == {}


def test_state_with_roms_roundtrips(make_rom) -> None:
    s = LibraryState(
        last_updated_after="2026-04-25T12:00:00Z",
        roms={
            26085: make_rom(26085),
            18335: make_rom(18335, name="Custom Robo", platform_slug="gc"),
        },
    )
    decoded = from_json(to_json(s))
    assert decoded == s


def test_to_json_is_deterministic(make_rom) -> None:
    s = LibraryState(roms={2: make_rom(2), 1: make_rom(1)})
    a = to_json(s)
    b = to_json(s)
    assert a == b
    # roms keys must be sorted in output
    parsed = json.loads(a)
    assert list(parsed["roms"]) == ["1", "2"]


# ---------------------------------------------------------------------------
# Schema version handling
# ---------------------------------------------------------------------------


def test_future_schema_version_raises() -> None:
    payload = json.dumps({"schema_version": CURRENT_SCHEMA_VERSION + 1, "roms": {}})
    with pytest.raises(StateSchemaError, match="schema_version"):
        from_json(payload)


def test_zero_or_negative_schema_version_raises() -> None:
    with pytest.raises(StateDecodeError):
        from_json(json.dumps({"schema_version": 0, "roms": {}}))
    with pytest.raises(StateDecodeError):
        from_json(json.dumps({"schema_version": -1, "roms": {}}))


def test_missing_schema_version_raises() -> None:
    with pytest.raises(StateDecodeError, match="schema_version"):
        from_json(json.dumps({"roms": {}}))


# ---------------------------------------------------------------------------
# Decode error cases
# ---------------------------------------------------------------------------


def test_invalid_json_raises_decode_error() -> None:
    with pytest.raises(StateDecodeError, match="invalid JSON"):
        from_json("{not json")


def test_root_must_be_object() -> None:
    with pytest.raises(StateDecodeError):
        from_json("[]")


def test_rom_key_value_id_mismatch_raises(make_rom) -> None:
    payload = json.dumps(
        {
            "schema_version": 1,
            "roms": {"99": json.loads(rom_to_json(make_rom(rom_id=42)))},
        }
    )
    with pytest.raises(StateDecodeError, match="key/value mismatch"):
        from_json(payload)


def test_outputs_must_be_non_empty(make_rom) -> None:
    rom_dict = json.loads(rom_to_json(make_rom()))
    rom_dict["outputs"] = []
    with pytest.raises(StateDecodeError, match="non-empty"):
        rom_from_json(json.dumps(rom_dict))


def test_primary_output_index_out_of_range_raises(make_rom) -> None:
    rom_dict = json.loads(rom_to_json(make_rom()))
    rom_dict["primary_output_index"] = 5
    with pytest.raises(StateDecodeError, match="primary_output_index"):
        rom_from_json(json.dumps(rom_dict))


def test_wrong_field_type_raises(make_rom) -> None:
    rom_dict = json.loads(rom_to_json(make_rom()))
    rom_dict["rom_id"] = "not-an-int"
    with pytest.raises(StateDecodeError, match="rom_id"):
        rom_from_json(json.dumps(rom_dict))


def test_transforms_wrong_shape_raises(make_rom) -> None:
    rom_dict = json.loads(rom_to_json(make_rom()))
    rom_dict["transforms"] = "unzip"  # should be a list
    with pytest.raises(StateDecodeError, match="transforms"):
        rom_from_json(json.dumps(rom_dict))


# ---------------------------------------------------------------------------
# Sidecar (single-RomState) roundtrips
# ---------------------------------------------------------------------------


def test_rom_roundtrips_for_sidecar_use(make_rom) -> None:
    rom = make_rom()
    decoded = rom_from_json(rom_to_json(rom))
    assert decoded == rom


def test_multi_output_rom_roundtrips(make_rom, make_output) -> None:
    rom = make_rom(
        outputs=(
            make_output("psx/CD1.cue"),
            make_output("psx/CD1.bin"),
            make_output("psx/CD2.cue"),
            make_output("psx/CD2.bin"),
            make_output("psx/Game.m3u"),
        ),
        primary_output_index=4,
        transforms=("unzip", "m3u_generate"),
    )
    decoded = rom_from_json(rom_to_json(rom))
    assert decoded == rom
    assert decoded.primary_output.path == "psx/Game.m3u"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_primary_output_property(make_rom, make_output) -> None:
    rom = make_rom(
        outputs=(make_output("a.iso"), make_output("b.iso")),
        primary_output_index=1,
    )
    assert rom.primary_output.path == "b.iso"
