import json

import pytest

from ferry.domain.state import (
    CURRENT_SCHEMA_VERSION,
    BiosRecord,
    LibraryState,
    StateDecodeError,
    StateSchemaError,
    from_json,
    rom_from_json,
    rom_to_json,
    to_json,
)


def make_bios(firmware_id: int = 7, **overrides) -> BiosRecord:
    fields = {
        "firmware_id": firmware_id,
        "platform_slug": "ps2",
        "file_name": "ps2-0230a-20080220.bin",
        "path": "ps2-0230a-20080220.bin",
        "md5": "a" * 32,
        "size": 4194304,
    }
    fields.update(overrides)
    return BiosRecord(**fields)


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


# ---------------------------------------------------------------------------
# SaveRecord schema (v2)
# ---------------------------------------------------------------------------


def _save_record_dict() -> dict:
    return {
        "emulator": "retroarch-snes9x",
        "slot": "default",
        "save_filename": "Mario.srm",
        "last_sync_md5": "a" * 32,
        "last_sync_server_size": 1024,
        "last_sync_server_updated_at": "2026-04-25T12:00:00Z",
        "last_synced_at": "2026-04-25T12:00:01Z",
        "server_save_id": 5,
    }


def test_rom_with_saves_round_trips(make_rom) -> None:
    """RomState.saves serializes and deserializes via from_json/to_json."""
    rom_dict = json.loads(rom_to_json(make_rom()))
    rom_dict["saves"] = [_save_record_dict()]
    payload = {
        "schema_version": 2,
        "roms": {str(rom_dict["rom_id"]): rom_dict},
    }
    state = from_json(json.dumps(payload))
    rom = next(iter(state.roms.values()))
    assert len(rom.saves) == 1
    sr = rom.saves[0]
    assert sr.emulator == "retroarch-snes9x"
    assert sr.server_save_id == 5


def test_v1_state_loads_with_default_empty_saves(make_rom) -> None:
    """A v1 state document (no `saves` key) loads cleanly with empty saves tuple."""
    rom_dict = json.loads(rom_to_json(make_rom()))
    rom_dict.pop("saves", None)  # ensure no saves key
    payload = {
        "schema_version": 1,
        "roms": {str(rom_dict["rom_id"]): rom_dict},
    }
    state = from_json(json.dumps(payload))
    rom = next(iter(state.roms.values()))
    assert rom.saves == ()


def test_save_record_missing_required_string_raises() -> None:
    rom_dict = {
        "rom_id": 1,
        "platform_slug": "snes",
        "name": "X",
        "source_filename": "X.zip",
        "source_md5": "a" * 32,
        "source_size": 1,
        "source_updated_at": "2026-01-01T00:00:00Z",
        "transforms": [],
        "outputs": [{"path": "x", "md5": "b" * 32, "size": 1}],
        "primary_output_index": 0,
        "synced_at": "2026-01-01T00:00:01Z",
        "saves": [{**_save_record_dict(), "emulator": 42}],  # type: ignore[dict-item]
    }
    payload = {"schema_version": 2, "roms": {"1": rom_dict}}
    with pytest.raises(StateDecodeError, match="emulator"):
        from_json(json.dumps(payload))


def test_save_record_missing_int_field_raises() -> None:
    sr = _save_record_dict()
    sr["server_save_id"] = "five"  # type: ignore[assignment]
    rom_dict = {
        "rom_id": 1,
        "platform_slug": "snes",
        "name": "X",
        "source_filename": "X.zip",
        "source_md5": "a" * 32,
        "source_size": 1,
        "source_updated_at": "2026-01-01T00:00:00Z",
        "transforms": [],
        "outputs": [{"path": "x", "md5": "b" * 32, "size": 1}],
        "primary_output_index": 0,
        "synced_at": "2026-01-01T00:00:01Z",
        "saves": [sr],
    }
    payload = {"schema_version": 2, "roms": {"1": rom_dict}}
    with pytest.raises(StateDecodeError, match="server_save_id"):
        from_json(json.dumps(payload))


def test_state_round_trips_device_id(make_rom) -> None:
    state = from_json(json.dumps({"schema_version": 2, "device_id": "uuid-abc", "roms": {}}))
    assert state.device_id == "uuid-abc"


def test_state_device_id_optional() -> None:
    state = from_json(json.dumps({"schema_version": 2, "roms": {}}))
    assert state.device_id is None


def test_state_device_id_must_be_string_when_present() -> None:
    with pytest.raises(StateDecodeError, match="device_id"):
        from_json(json.dumps({"schema_version": 2, "device_id": 42, "roms": {}}))


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
# Single-RomState roundtrips
# ---------------------------------------------------------------------------


def test_rom_roundtrips_via_per_rom_helpers(make_rom) -> None:
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


# ---------------------------------------------------------------------------
# BIOS state (schema 3 — v5.5)
# ---------------------------------------------------------------------------


def test_state_with_bios_roundtrips() -> None:
    s = LibraryState(
        bios={
            7: make_bios(7),
            3: make_bios(3, platform_slug="dc", file_name="dc_boot.bin", path="dc/dc_boot.bin"),
        },
    )
    decoded = from_json(to_json(s))
    assert decoded == s


def test_bios_keys_sorted_in_output() -> None:
    s = LibraryState(bios={9: make_bios(9), 2: make_bios(2)})
    parsed = json.loads(to_json(s))
    assert list(parsed["bios"]) == ["2", "9"]


def test_v2_state_loads_with_default_empty_bios() -> None:
    """A pre-v5.5 (schema 2) document loads with an empty bios map."""
    decoded = from_json(json.dumps({"schema_version": 2, "roms": {}}))
    assert decoded.bios == {}


def test_v1_state_loads_with_default_empty_bios() -> None:
    decoded = from_json(json.dumps({"schema_version": 1, "roms": {}}))
    assert decoded.bios == {}


def test_bios_key_value_id_mismatch_raises() -> None:
    payload = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "roms": {},
        "bios": {"99": _bios_dict(firmware_id=7)},
    }
    with pytest.raises(StateDecodeError, match="firmware_id"):
        from_json(json.dumps(payload))


def test_bios_must_be_object() -> None:
    payload = {"schema_version": CURRENT_SCHEMA_VERSION, "roms": {}, "bios": []}
    with pytest.raises(StateDecodeError, match="bios must be an object"):
        from_json(json.dumps(payload))


def test_bios_record_missing_required_string_raises() -> None:
    entry = _bios_dict(firmware_id=7)
    del entry["file_name"]
    payload = {"schema_version": CURRENT_SCHEMA_VERSION, "roms": {}, "bios": {"7": entry}}
    with pytest.raises(StateDecodeError, match="bios.file_name"):
        from_json(json.dumps(payload))


def test_bios_record_size_must_be_int_not_bool() -> None:
    entry = _bios_dict(firmware_id=7)
    entry["size"] = True  # bool is an int subclass — must still be rejected
    payload = {"schema_version": CURRENT_SCHEMA_VERSION, "roms": {}, "bios": {"7": entry}}
    with pytest.raises(StateDecodeError, match="bios.size"):
        from_json(json.dumps(payload))


def _bios_dict(*, firmware_id: int) -> dict:
    from dataclasses import asdict

    return asdict(make_bios(firmware_id))
