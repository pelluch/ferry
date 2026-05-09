"""Tests for `domain.iso_time`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from ferry.domain.iso_time import parse_iso, parse_iso_to_epoch, same_iso_instant

# ---------------------------------------------------------------------------
# parse_iso
# ---------------------------------------------------------------------------


def test_parses_z_suffix() -> None:
    """Trailing `Z` is parsed as UTC. Python 3.11+ handles this natively."""
    dt = parse_iso("2026-05-05T10:21:07Z")
    assert dt == datetime(2026, 5, 5, 10, 21, 7, tzinfo=UTC)


def test_parses_explicit_utc_offset() -> None:
    dt = parse_iso("2026-05-05T10:21:07+00:00")
    assert dt == datetime(2026, 5, 5, 10, 21, 7, tzinfo=UTC)


def test_parses_microseconds() -> None:
    """RomM upload responses include microseconds; the list endpoint truncates."""
    dt = parse_iso("2026-05-05T10:21:07.058332+00:00")
    assert dt == datetime(2026, 5, 5, 10, 21, 7, 58332, tzinfo=UTC)


def test_parses_non_utc_offset() -> None:
    dt = parse_iso("2026-05-05T12:21:07+02:00")
    expected = datetime(2026, 5, 5, 12, 21, 7, tzinfo=timezone(timedelta(hours=2)))
    assert dt == expected


def test_empty_string_returns_none() -> None:
    assert parse_iso("") is None


def test_none_returns_none() -> None:
    assert parse_iso(None) is None


def test_garbage_returns_none() -> None:
    assert parse_iso("not a timestamp") is None


def test_invalid_components_return_none() -> None:
    assert parse_iso("2026-13-99T25:99:99Z") is None


# ---------------------------------------------------------------------------
# parse_iso_to_epoch
# ---------------------------------------------------------------------------


def test_z_suffix_to_epoch() -> None:
    assert parse_iso_to_epoch("1970-01-01T00:00:00Z") == 0.0


def test_equivalent_instants_compare_equal() -> None:
    """Same instant in different offsets → same epoch.

    Regression for the lexical-compare bug: `2026-05-05T10:00:00Z` and
    `2026-05-05T12:00:00+02:00` are the same instant; lexical compare
    would say the second is "newer," epoch compare correctly says equal.
    """
    a = parse_iso_to_epoch("2026-05-05T10:00:00Z")
    b = parse_iso_to_epoch("2026-05-05T12:00:00+02:00")
    assert a is not None
    assert b is not None
    assert a == b


def test_lexical_misordering_fixed() -> None:
    """Lexical and chronological order disagree when offsets vary."""
    z_form_str = "2026-05-05T10:00:00Z"  # 10:00 UTC (later instant)
    plus_form_str = "2026-05-05T11:00:00+02:00"  # 09:00 UTC (earlier instant)
    z_form = parse_iso_to_epoch(z_form_str)
    plus_form = parse_iso_to_epoch(plus_form_str)
    assert z_form is not None and plus_form is not None
    # Chronologically: the Z form is later (10:00 UTC > 09:00 UTC).
    assert z_form > plus_form
    # Lexically: the `+02:00` form sorts later (char 13: `1` > `0`). That's
    # the bug — lexical disagrees with chronological order.
    assert plus_form_str > z_form_str


def test_microseconds_preserved_in_epoch() -> None:
    a = parse_iso_to_epoch("2026-05-05T10:21:07.000000+00:00")
    b = parse_iso_to_epoch("2026-05-05T10:21:07.500000+00:00")
    assert a is not None and b is not None
    assert b - a == 0.5


def test_epoch_empty_returns_none() -> None:
    assert parse_iso_to_epoch("") is None


def test_epoch_none_returns_none() -> None:
    assert parse_iso_to_epoch(None) is None


def test_epoch_garbage_returns_none() -> None:
    assert parse_iso_to_epoch("not a timestamp") is None


def test_or_zero_pattern_sorts_unparseable_to_bottom() -> None:
    """The `parse_iso_to_epoch(...) or 0.0` idiom — unparseable sorts last."""
    valid = parse_iso_to_epoch("2026-05-05T10:00:00Z") or 0.0
    invalid = parse_iso_to_epoch("garbage") or 0.0
    assert valid > invalid


# ---------------------------------------------------------------------------
# same_iso_instant
# ---------------------------------------------------------------------------


def test_same_instant_when_strings_match() -> None:
    """Identical strings short-circuit to True without parsing."""
    assert same_iso_instant("2026-05-05T10:00:00Z", "2026-05-05T10:00:00Z")


def test_same_instant_z_vs_offset_form() -> None:
    """Regression for the live-test bug: state stored `+00:00` while
    a later API response returned `Z` for the same instant — `compute_plan`
    would lexically mismatch and flag every ROM for update."""
    assert same_iso_instant("2026-04-28T12:14:09+00:00", "2026-04-28T12:14:09Z")


def test_same_instant_microseconds_truncated() -> None:
    """RomM's list endpoint truncates microseconds while POST/PUT
    responses keep them; both forms are the same instant at second
    precision."""
    assert same_iso_instant("2026-05-05T10:21:07.058332+00:00", "2026-05-05T10:21:07+00:00")


def test_same_instant_across_offsets() -> None:
    """Same UTC instant in two different timezone offsets."""
    assert same_iso_instant("2026-05-05T10:00:00Z", "2026-05-05T12:00:00+02:00")


def test_different_instants_compare_unequal() -> None:
    assert not same_iso_instant("2026-05-05T10:00:00Z", "2026-05-05T10:00:01Z")


def test_both_none_compare_equal() -> None:
    """`None == None` short-circuits before parsing."""
    assert same_iso_instant(None, None)


def test_both_empty_strings_compare_equal() -> None:
    assert same_iso_instant("", "")


def test_one_none_one_set_compare_unequal() -> None:
    assert not same_iso_instant(None, "2026-05-05T10:00:00Z")
    assert not same_iso_instant("2026-05-05T10:00:00Z", None)


def test_one_unparseable_compares_unequal() -> None:
    """When one side is garbage, fall back to lexical inequality."""
    assert not same_iso_instant("garbage", "2026-05-05T10:00:00Z")
    assert not same_iso_instant("2026-05-05T10:00:00Z", "garbage")


def test_both_unparseable_but_equal_strings_compare_equal() -> None:
    """Equivalent strings stay equivalent even when neither parses
    — the lexical fast-path covers this before parse is attempted."""
    assert same_iso_instant("garbage", "garbage")
