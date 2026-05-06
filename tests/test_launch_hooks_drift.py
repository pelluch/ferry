"""Tests for the drift-snapshot helpers in services.launch_hooks.

Covers: file SHA, managed-block extraction + SHA, snapshot serialization
round-trip, drift detection across all interesting state combinations.
End-to-end CLI behavior (install --force gating, status output, sync
warning) lives in test_cli_launch_hooks.py and test_cli_*.py.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ferry.services.launch_hooks import (
    MANAGED_BLOCK_BEGIN,
    MANAGED_BLOCK_END,
    Snapshot,
    compute_file_sha256,
    compute_managed_block_sha256,
    default_snapshot_path,
    delete_snapshot,
    detect_drift,
    extract_managed_block,
    install_managed_block,
    make_snapshot,
    read_snapshot,
    write_snapshot,
)

# ---------------------------------------------------------------------------
# compute_file_sha256
# ---------------------------------------------------------------------------


def test_compute_file_sha256_matches_hashlib(tmp_path: Path) -> None:
    target = tmp_path / "file.bin"
    payload = b"hello ferry" * 1000
    target.write_bytes(payload)
    assert compute_file_sha256(target) == hashlib.sha256(payload).hexdigest()


def test_compute_file_sha256_streams_large_files(tmp_path: Path) -> None:
    """Larger-than-chunk file still hashes correctly (proves the chunked loop)."""
    target = tmp_path / "big.bin"
    chunk = b"\xab\xcd" * 32_768
    target.write_bytes(chunk * 4)  # ~256KB, > 64KB chunk size
    assert compute_file_sha256(target) == hashlib.sha256(chunk * 4).hexdigest()


# ---------------------------------------------------------------------------
# extract_managed_block / compute_managed_block_sha256
# ---------------------------------------------------------------------------


def _wrap_block(body: str) -> str:
    """File-content shape: leading-2-space markers + body."""
    return (
        f'<?xml version="1.0"?>\n<systemList>\n'
        f"  {MANAGED_BLOCK_BEGIN}\n"
        f"{body}\n"
        f"  {MANAGED_BLOCK_END}\n"
        f"</systemList>\n"
    )


def test_extract_managed_block_returns_markers_plus_body(tmp_path: Path) -> None:
    custom = tmp_path / "custom.xml"
    body = "    <system><name>gba</name></system>"
    custom.write_text(_wrap_block(body))

    extracted = extract_managed_block(custom)
    assert extracted is not None
    assert extracted.startswith(MANAGED_BLOCK_BEGIN)
    assert extracted.endswith(MANAGED_BLOCK_END)
    assert body in extracted


def test_extract_managed_block_returns_none_when_no_block(tmp_path: Path) -> None:
    custom = tmp_path / "custom.xml"
    custom.write_text('<?xml version="1.0"?><systemList></systemList>')
    assert extract_managed_block(custom) is None


def test_extract_managed_block_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert extract_managed_block(tmp_path / "nope.xml") is None


def test_managed_block_sha_is_stable_across_round_trip(tmp_path: Path) -> None:
    """Hash of what install_managed_block writes == hash extract returns
    from the file. This guarantees make_snapshot / detect_drift agree."""
    custom = tmp_path / "custom.xml"
    body = "    <system><name>gba</name></system>"
    install_managed_block(custom, body)

    sha_post_install = compute_managed_block_sha256(custom)
    assert sha_post_install is not None
    # Verify we hash exactly what extract returns, with no reformatting.
    extracted = extract_managed_block(custom)
    assert sha_post_install == hashlib.sha256(extracted.encode("utf-8")).hexdigest()


def test_managed_block_sha_changes_when_block_edited(tmp_path: Path) -> None:
    custom = tmp_path / "custom.xml"
    install_managed_block(custom, "    <system><name>gba</name></system>")
    sha_before = compute_managed_block_sha256(custom)

    # Simulate a hand-edit inside the block (extension list change).
    text = custom.read_text()
    edited = text.replace("<name>gba</name>", "<name>gba</name><extension>.foo</extension>")
    custom.write_text(edited)

    sha_after = compute_managed_block_sha256(custom)
    assert sha_after is not None
    assert sha_after != sha_before


# ---------------------------------------------------------------------------
# Snapshot serialization round-trip
# ---------------------------------------------------------------------------


def test_snapshot_round_trip(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "snap.json"
    snap = Snapshot(
        bundled_path=Path("/var/lib/flatpak/.../es_systems.xml"),
        bundled_sha256="a" * 64,
        custom_systems_path=Path("/home/user/ES-DE/custom_systems/es_systems.xml"),
        managed_block_sha256="b" * 64,
        installed_at="2026-05-05T12:34:56Z",
    )
    write_snapshot(snapshot_path, snap)
    loaded = read_snapshot(snapshot_path)
    assert loaded == snap


def test_read_snapshot_returns_none_when_missing(tmp_path: Path) -> None:
    assert read_snapshot(tmp_path / "missing.json") is None


def test_read_snapshot_returns_none_on_invalid_json(tmp_path: Path) -> None:
    target = tmp_path / "snap.json"
    target.write_text("{this isn't json}")
    assert read_snapshot(target) is None


def test_read_snapshot_returns_none_when_required_keys_missing(tmp_path: Path) -> None:
    target = tmp_path / "snap.json"
    target.write_text(json.dumps({"version": 1, "bundled_path": "/x"}))
    assert read_snapshot(target) is None


def test_read_snapshot_returns_none_for_unknown_version(tmp_path: Path) -> None:
    target = tmp_path / "snap.json"
    target.write_text(
        json.dumps(
            {
                "version": 99,
                "bundled_path": "/x",
                "bundled_sha256": "a" * 64,
                "custom_systems_path": "/y",
                "managed_block_sha256": "b" * 64,
                "installed_at": "2026-05-05T00:00:00Z",
            }
        )
    )
    assert read_snapshot(target) is None


def test_delete_snapshot_returns_true_when_existed(tmp_path: Path) -> None:
    target = tmp_path / "snap.json"
    target.write_text("{}")
    assert delete_snapshot(target) is True
    assert not target.exists()


def test_delete_snapshot_returns_false_when_missing(tmp_path: Path) -> None:
    assert delete_snapshot(tmp_path / "missing.json") is False


# ---------------------------------------------------------------------------
# default_snapshot_path
# ---------------------------------------------------------------------------


def test_default_snapshot_path_uses_xdg_state_home(tmp_path: Path) -> None:
    state = tmp_path / "state"
    assert default_snapshot_path({"XDG_STATE_HOME": str(state)}) == (
        state / "ferry" / "launch-hooks.snapshot.json"
    )


def test_default_snapshot_path_falls_back_to_home(monkeypatch: pytest.MonkeyPatch) -> None:
    home = Path("/tmp/fakehome")
    monkeypatch.setattr("ferry.services.launch_hooks.Path.home", lambda: home)
    assert (
        default_snapshot_path({})
        == home / ".local" / "state" / "ferry" / "launch-hooks.snapshot.json"
    )


# ---------------------------------------------------------------------------
# make_snapshot + detect_drift
# ---------------------------------------------------------------------------


def _setup_install(
    tmp_path: Path, *, body: str = "    <system><name>gba</name></system>"
) -> tuple[Path, Path, Snapshot]:
    """Create a bundled file + custom_systems file with managed block, return snapshot."""
    bundled = tmp_path / "bundled" / "es_systems.xml"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("<systemList><system><name>gba</name></system></systemList>")
    custom = tmp_path / "custom" / "es_systems.xml"
    install_managed_block(custom, body)
    snap = make_snapshot(bundled_path=bundled, custom_systems_path=custom)
    return bundled, custom, snap


def test_make_snapshot_records_current_shas(tmp_path: Path) -> None:
    bundled, custom, snap = _setup_install(tmp_path)
    assert snap.bundled_sha256 == compute_file_sha256(bundled)
    assert snap.managed_block_sha256 == compute_managed_block_sha256(custom)
    assert snap.bundled_path == bundled
    assert snap.custom_systems_path == custom
    assert snap.version == 1
    assert snap.installed_at.endswith("Z")


def test_make_snapshot_raises_when_no_managed_block(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled.xml"
    bundled.write_text("<systemList/>")
    custom = tmp_path / "custom.xml"
    custom.write_text('<?xml version="1.0"?><systemList></systemList>')  # no block
    with pytest.raises(ValueError, match="managed block not found"):
        make_snapshot(bundled_path=bundled, custom_systems_path=custom)


def test_detect_drift_clean_when_nothing_changed(tmp_path: Path) -> None:
    _, _, snap = _setup_install(tmp_path)
    drift = detect_drift(snap)
    assert drift.is_clean
    assert not drift.upstream_drift
    assert not drift.local_drift
    assert drift.bundled_present
    assert drift.block_present


def test_detect_drift_flags_upstream_when_bundled_changes(tmp_path: Path) -> None:
    bundled, _, snap = _setup_install(tmp_path)
    bundled.write_text(
        "<systemList><system><name>gba</name></system><system><name>snes</name></system></systemList>"
    )

    drift = detect_drift(snap)
    assert drift.upstream_drift
    assert not drift.local_drift
    assert not drift.is_clean


def test_detect_drift_flags_local_when_managed_block_edited(tmp_path: Path) -> None:
    _, custom, snap = _setup_install(tmp_path)
    text = custom.read_text()
    custom.write_text(
        text.replace("<name>gba</name>", "<name>gba</name><extension>.foo</extension>")
    )

    drift = detect_drift(snap)
    assert drift.local_drift
    assert not drift.upstream_drift
    assert not drift.is_clean


def test_detect_drift_flags_both_when_bundled_and_block_changed(tmp_path: Path) -> None:
    bundled, custom, snap = _setup_install(tmp_path)
    bundled.write_text("<systemList><system><name>snes</name></system></systemList>")
    text = custom.read_text()
    custom.write_text(text.replace("<name>gba</name>", "<name>gba-mod</name>"))

    drift = detect_drift(snap)
    assert drift.upstream_drift
    assert drift.local_drift
    assert not drift.is_clean


def test_detect_drift_when_bundled_file_disappears(tmp_path: Path) -> None:
    bundled, _, snap = _setup_install(tmp_path)
    bundled.unlink()

    drift = detect_drift(snap)
    assert not drift.bundled_present
    assert drift.current_bundled_sha is None
    # bundled "drift" is gated on present, so it stays False — caller
    # interprets bundled_present=False as a separate state.
    assert not drift.upstream_drift
    assert not drift.is_clean


def test_detect_drift_when_managed_block_removed(tmp_path: Path) -> None:
    _, custom, snap = _setup_install(tmp_path)
    # Strip the managed block, leaving only the systemList wrapper.
    text = custom.read_text()
    import re

    stripped = re.sub(
        re.escape(MANAGED_BLOCK_BEGIN) + r".*?" + re.escape(MANAGED_BLOCK_END),
        "",
        text,
        flags=re.DOTALL,
    )
    custom.write_text(stripped)

    drift = detect_drift(snap)
    assert not drift.block_present
    assert drift.current_block_sha is None
    assert not drift.local_drift  # gated on present
    assert not drift.is_clean


def test_detect_drift_when_custom_systems_file_deleted(tmp_path: Path) -> None:
    _, custom, snap = _setup_install(tmp_path)
    custom.unlink()

    drift = detect_drift(snap)
    assert not drift.block_present
    assert drift.current_block_sha is None
    assert not drift.is_clean
