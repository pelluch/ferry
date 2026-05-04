"""Tests for ferry.domain.save_conflicts.

Ported from decky-romm-sync's `tests/domain/test_save_conflicts.py`,
adapted for ferry's primitive-arg signatures and the trimmed surface
(no `ask_me` mode, no `SaveConflict` dataclass).
"""

from __future__ import annotations

from datetime import UTC, datetime

from ferry.domain.save_conflicts import (
    determine_action,
    local_changed,
    resolve_newest,
    server_changed_fast,
)

# ---------------------------------------------------------------------------
# local_changed
# ---------------------------------------------------------------------------


class TestLocalChanged:
    def test_same_hash_returns_false(self) -> None:
        assert local_changed("abc123", "abc123") is False

    def test_different_hash_returns_true(self) -> None:
        assert local_changed("abc123", "def456") is True

    def test_empty_local_hash_differs_from_baseline(self) -> None:
        assert local_changed("", "abc123") is True

    def test_both_empty_returns_false(self) -> None:
        assert local_changed("", "") is False

    def test_none_local_hash_differs_from_baseline(self) -> None:
        """File disappeared since last sync."""
        assert local_changed(None, "abc123") is True

    def test_baseline_none_means_first_sync(self) -> None:
        """No baseline + present local file = changed (newly added)."""
        assert local_changed("abc123", None) is True

    def test_both_none_returns_false(self) -> None:
        """File was missing and is still missing — no change."""
        assert local_changed(None, None) is False


# ---------------------------------------------------------------------------
# server_changed_fast
# ---------------------------------------------------------------------------


class TestServerChangedFast:
    def test_matching_timestamp_and_size_returns_false(self) -> None:
        assert (
            server_changed_fast(
                stored_updated_at="2026-02-17T06:00:00Z",
                stored_size=1024,
                server_updated_at="2026-02-17T06:00:00Z",
                server_size=1024,
            )
            is False
        )

    def test_matching_timestamp_size_differs_returns_true(self) -> None:
        """Same RomM record, different content — possible if upstream silently rewrites."""
        assert (
            server_changed_fast(
                stored_updated_at="2026-02-17T06:00:00Z",
                stored_size=1024,
                server_updated_at="2026-02-17T06:00:00Z",
                server_size=2048,
            )
            is True
        )

    def test_timestamp_differs_returns_indeterminate(self) -> None:
        assert (
            server_changed_fast(
                stored_updated_at="2026-02-17T06:00:00Z",
                stored_size=1024,
                server_updated_at="2026-02-17T12:00:00Z",
                server_size=1024,
            )
            is None
        )

    def test_no_stored_timestamp_returns_indeterminate(self) -> None:
        assert (
            server_changed_fast(
                stored_updated_at=None,
                stored_size=1024,
                server_updated_at="2026-02-17T06:00:00Z",
                server_size=1024,
            )
            is None
        )

    def test_empty_stored_timestamp_returns_indeterminate(self) -> None:
        assert (
            server_changed_fast(
                stored_updated_at="",
                stored_size=1024,
                server_updated_at="2026-02-17T06:00:00Z",
                server_size=1024,
            )
            is None
        )

    def test_stored_size_none_with_matching_timestamp_returns_false(self) -> None:
        """Legacy state without recorded size — assume unchanged when ts matches."""
        assert (
            server_changed_fast(
                stored_updated_at="2026-02-17T06:00:00Z",
                stored_size=None,
                server_updated_at="2026-02-17T06:00:00Z",
                server_size=2048,
            )
            is False
        )

    def test_server_size_none_with_matching_timestamp_returns_false(self) -> None:
        assert (
            server_changed_fast(
                stored_updated_at="2026-02-17T06:00:00Z",
                stored_size=1024,
                server_updated_at="2026-02-17T06:00:00Z",
                server_size=None,
            )
            is False
        )


# ---------------------------------------------------------------------------
# determine_action
# ---------------------------------------------------------------------------


class TestDetermineAction:
    def test_neither_changed_skips(self) -> None:
        assert determine_action(local_changed_=False, server_changed=False) == "skip"

    def test_only_server_changed_downloads(self) -> None:
        assert determine_action(local_changed_=False, server_changed=True) == "download"

    def test_only_local_changed_uploads(self) -> None:
        assert determine_action(local_changed_=True, server_changed=False) == "upload"

    def test_both_changed_conflict(self) -> None:
        assert determine_action(local_changed_=True, server_changed=True) == "conflict"


# ---------------------------------------------------------------------------
# resolve_newest
# ---------------------------------------------------------------------------


class TestResolveNewest:
    SERVER_TS = "2026-02-17T06:00:00Z"
    SERVER_DT = datetime(2026, 2, 17, 6, 0, 0, tzinfo=UTC)

    def test_local_clearly_newer_uploads(self) -> None:
        local_mtime = self.SERVER_DT.timestamp() + 3600  # +1h
        assert (
            resolve_newest(
                local_mtime=local_mtime,
                server_updated_at=self.SERVER_TS,
            )
            == "upload"
        )

    def test_server_clearly_newer_downloads(self) -> None:
        local_mtime = self.SERVER_DT.timestamp() - 3600  # -1h
        assert (
            resolve_newest(
                local_mtime=local_mtime,
                server_updated_at=self.SERVER_TS,
            )
            == "download"
        )

    def test_within_tolerance_returns_ambiguous(self) -> None:
        """30s drift, default 60s tolerance — too close to call."""
        local_mtime = self.SERVER_DT.timestamp() + 30
        assert (
            resolve_newest(
                local_mtime=local_mtime,
                server_updated_at=self.SERVER_TS,
            )
            == "ambiguous"
        )

    def test_exact_tolerance_boundary_returns_ambiguous(self) -> None:
        """Boundary case: diff == tolerance is ambiguous (defensive)."""
        local_mtime = self.SERVER_DT.timestamp() + 60
        assert (
            resolve_newest(
                local_mtime=local_mtime,
                server_updated_at=self.SERVER_TS,
                tolerance_sec=60,
            )
            == "ambiguous"
        )

    def test_just_outside_tolerance_resolves(self) -> None:
        local_mtime = self.SERVER_DT.timestamp() + 61
        assert (
            resolve_newest(
                local_mtime=local_mtime,
                server_updated_at=self.SERVER_TS,
                tolerance_sec=60,
            )
            == "upload"
        )

    def test_custom_tolerance_zero_resolves_one_second_diff(self) -> None:
        """Power user disables tolerance — 1s diff should win."""
        local_mtime = self.SERVER_DT.timestamp() + 1
        assert (
            resolve_newest(
                local_mtime=local_mtime,
                server_updated_at=self.SERVER_TS,
                tolerance_sec=0,
            )
            == "upload"
        )

    def test_unparseable_server_timestamp_returns_ambiguous(self) -> None:
        assert (
            resolve_newest(
                local_mtime=1000.0,
                server_updated_at="not-a-date",
            )
            == "ambiguous"
        )

    def test_empty_server_timestamp_returns_ambiguous(self) -> None:
        assert (
            resolve_newest(
                local_mtime=1000.0,
                server_updated_at="",
            )
            == "ambiguous"
        )

    def test_z_suffix_iso_timestamp_parses(self) -> None:
        """RomM serves timestamps with `Z` suffix; ensure we handle them."""
        local_mtime = self.SERVER_DT.timestamp() + 3600
        assert (
            resolve_newest(
                local_mtime=local_mtime,
                server_updated_at="2026-02-17T06:00:00Z",
            )
            == "upload"
        )

    def test_offset_iso_timestamp_parses(self) -> None:
        local_mtime = self.SERVER_DT.timestamp() + 3600
        assert (
            resolve_newest(
                local_mtime=local_mtime,
                server_updated_at="2026-02-17T06:00:00+00:00",
            )
            == "upload"
        )
