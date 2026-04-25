"""Library state — what ferry knows about each installed ROM.

State is the durable record that ties RomM's view of a ROM (rom_id, source
file, last-modified time) to ferry's on-disk artifact(s) (the outputs of the
transform pipeline). It is the source of truth for incremental sync,
delete-on-remove, and reconcile — every other v1 feature consumes it.

Hashes are computed locally on download / transform output, not pulled from
RomM. RomM's `md5_hash` is optional (server admins can disable hashing) and
unreliable (in-flight scans, missing CRCs); driving change detection off
`updated_at` and verifying with our own hash sidesteps both. RomM's hash, when
present, is used at download time as an integrity cross-check only.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

CURRENT_SCHEMA_VERSION = 1


class StateSchemaError(Exception):
    """The state document's schema_version is not supported by this ferry."""


class StateDecodeError(Exception):
    """The state document is malformed — wrong types, missing fields, bad JSON."""


@dataclass(frozen=True, slots=True, kw_only=True)
class TransformedOutput:
    """A single file produced by the transform pipeline.

    Path is relative to `Destination.roms_base` so state is portable when the
    ROM tree moves to a new disk. Hash and size are of the on-disk artifact,
    not the upstream source.
    """

    path: str
    md5: str
    size: int


@dataclass(frozen=True, slots=True, kw_only=True)
class RomState:
    """Everything ferry knows about one installed ROM."""

    rom_id: int
    platform_slug: str
    name: str

    # Source provenance — what RomM had when we last fetched.
    source_filename: str
    source_md5: str  # always our locally computed hash
    source_size: int
    source_updated_at: str  # ISO 8601 from RomM; primary change-detection signal

    # Transform pipeline applied to the source file.
    transforms: tuple[str, ...]

    # On-disk artifacts. `outputs[primary_output_index]` is the launchable file.
    outputs: tuple[TransformedOutput, ...]
    primary_output_index: int

    # When ferry last reconciled this entry against RomM.
    synced_at: str  # ISO 8601

    @property
    def primary_output(self) -> TransformedOutput:
        return self.outputs[self.primary_output_index]


@dataclass(frozen=True, slots=True, kw_only=True)
class LibraryState:
    """The whole state document. Persisted as a single JSON file."""

    schema_version: int = CURRENT_SCHEMA_VERSION
    last_updated_after: str | None = None
    roms: dict[int, RomState] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# JSON (de)serialization
# ---------------------------------------------------------------------------


def to_json(state: LibraryState) -> str:
    """Render *state* as deterministic, human-readable JSON."""
    payload = {
        "schema_version": state.schema_version,
        "last_updated_after": state.last_updated_after,
        "roms": {str(rid): asdict(r) for rid, r in sorted(state.roms.items())},
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def from_json(text: str) -> LibraryState:
    """Parse a state document. Raises StateSchemaError or StateDecodeError."""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise StateDecodeError(f"invalid JSON: {e}") from e
    if not isinstance(raw, dict):
        raise StateDecodeError("state root must be an object")
    return _state_from_dict(raw)


def rom_to_json(rom: RomState) -> str:
    """Render a single RomState as JSON — used for sidecar files."""
    return json.dumps(asdict(rom), indent=2, sort_keys=True)


def rom_from_json(text: str) -> RomState:
    """Parse a single RomState — used for sidecar files."""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise StateDecodeError(f"invalid JSON: {e}") from e
    if not isinstance(raw, dict):
        raise StateDecodeError("rom state root must be an object")
    return _rom_from_dict(raw)


# ---------------------------------------------------------------------------
# Internal: dict -> dataclass with explicit validation
# ---------------------------------------------------------------------------


def _state_from_dict(raw: dict[str, Any]) -> LibraryState:
    schema_version = raw.get("schema_version")
    if not isinstance(schema_version, int):
        raise StateDecodeError("schema_version must be an integer")
    if schema_version > CURRENT_SCHEMA_VERSION:
        raise StateSchemaError(
            f"state file uses schema_version {schema_version}, "
            f"but this ferry only knows up to {CURRENT_SCHEMA_VERSION}. "
            f"Upgrade ferry or hand-edit the file."
        )
    if schema_version < 1:
        raise StateDecodeError(f"schema_version {schema_version} is not valid")

    last = raw.get("last_updated_after")
    if last is not None and not isinstance(last, str):
        raise StateDecodeError("last_updated_after must be a string or null")

    roms_raw = raw.get("roms", {})
    if not isinstance(roms_raw, dict):
        raise StateDecodeError("roms must be an object keyed by rom_id")

    roms: dict[int, RomState] = {}
    for key, value in roms_raw.items():
        try:
            rom_id = int(key)
        except (TypeError, ValueError) as e:
            raise StateDecodeError(f"rom key {key!r} must be an integer") from e
        if not isinstance(value, dict):
            raise StateDecodeError(f"roms[{rom_id}] must be an object")
        rom = _rom_from_dict(value)
        if rom.rom_id != rom_id:
            raise StateDecodeError(f"key/value mismatch: roms[{rom_id}] has rom_id={rom.rom_id}")
        roms[rom_id] = rom

    return LibraryState(
        schema_version=schema_version,
        last_updated_after=last,
        roms=roms,
    )


def _rom_from_dict(raw: dict[str, Any]) -> RomState:
    required_str = (
        "platform_slug",
        "name",
        "source_filename",
        "source_md5",
        "source_updated_at",
        "synced_at",
    )
    for field_name in required_str:
        if not isinstance(raw.get(field_name), str):
            raise StateDecodeError(f"rom.{field_name} must be a string")
    for field_name in ("rom_id", "source_size", "primary_output_index"):
        if not isinstance(raw.get(field_name), int):
            raise StateDecodeError(f"rom.{field_name} must be an integer")

    transforms_raw = raw.get("transforms")
    if not isinstance(transforms_raw, list) or not all(isinstance(t, str) for t in transforms_raw):
        raise StateDecodeError("rom.transforms must be a list of strings")

    outputs_raw = raw.get("outputs")
    if not isinstance(outputs_raw, list) or not outputs_raw:
        raise StateDecodeError("rom.outputs must be a non-empty list")
    outputs = tuple(_output_from_dict(o) for o in outputs_raw)

    primary = raw["primary_output_index"]
    if not 0 <= primary < len(outputs):
        raise StateDecodeError(
            f"rom.primary_output_index {primary} out of range for {len(outputs)} outputs"
        )

    return RomState(
        rom_id=raw["rom_id"],
        platform_slug=raw["platform_slug"],
        name=raw["name"],
        source_filename=raw["source_filename"],
        source_md5=raw["source_md5"],
        source_size=raw["source_size"],
        source_updated_at=raw["source_updated_at"],
        transforms=tuple(transforms_raw),
        outputs=outputs,
        primary_output_index=primary,
        synced_at=raw["synced_at"],
    )


def _output_from_dict(raw: Any) -> TransformedOutput:
    if not isinstance(raw, dict):
        raise StateDecodeError("output entry must be an object")
    if not isinstance(raw.get("path"), str):
        raise StateDecodeError("output.path must be a string")
    if not isinstance(raw.get("md5"), str):
        raise StateDecodeError("output.md5 must be a string")
    if not isinstance(raw.get("size"), int):
        raise StateDecodeError("output.size must be an integer")
    return TransformedOutput(path=raw["path"], md5=raw["md5"], size=raw["size"])
