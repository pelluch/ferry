"""Pick the "active" install when multiple are detected.

Three adapters (RetroArch, Dolphin, ES-DE) each carried an identical
decision table for resolving "which install does the user actually
work with?":

  - 0 installs                              → None
  - 1 install                               → that one
  - 2+ installs:
      - exactly one with active-use signal  → that one
      - 0 with active-use signal            → first by priority order
      - 2+ with active-use signal           → None (caller asks the user)

The signal differs by adapter (RetroArch & Dolphin use "has saves",
ES-DE uses "has a custom systems file"), so the predicate is injected.
The priority order is the input list's order — adapters return their
discovery results sorted by preference (RetroDECK first, then libretro
flatpak / EmuDeck, then native), so `installs[0]` is the conservative
fallback when nothing distinguishes the candidates.

`resolve_install` layers the configured-override logic on top:
discover, honour `[saves].<backend>_install` if set, else auto-select.
Returns the install plus an enum saying *why* — callers render
different messages for "no installs" / "configured but mismatched" /
"ambiguous" so the dispatch logic stays in one place.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum


def select_active[T](installs: list[T], *, has_active: Callable[[T], bool]) -> T | None:
    """Resolve the user-active install from a list, or None when ambiguous.

    Returning None on ambiguity (2+ installs with the active-use signal)
    is the safe default — picking arbitrarily would polish off the wrong
    save tree at conflict time.
    """
    if not installs:
        return None
    if len(installs) == 1:
        return installs[0]
    with_active = [i for i in installs if has_active(i)]
    if len(with_active) == 1:
        return with_active[0]
    if not with_active:
        return installs[0]
    return None


class ResolutionReason(Enum):
    """Why `resolve_install` picked (or refused to pick) an install.

    Each call site renders a backend- and tense-specific message per
    reason ("skipped" vs "would skip", "(not detected)" vs "no install
    detected"); the resolution itself is centralized.
    """

    EXPLICIT_MATCH = "explicit_match"
    AUTO_ACTIVE = "auto_active"
    NO_INSTALLS = "no_installs"
    EXPLICIT_MISMATCH = "explicit_mismatch"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True, kw_only=True)
class InstallResolution[T]:
    """Outcome of `resolve_install`. `install` is None unless reason is
    EXPLICIT_MATCH or AUTO_ACTIVE."""

    install: T | None
    reason: ResolutionReason


def resolve_install[T](
    installs: list[T],
    *,
    configured_source: str | None,
    source_of: Callable[[T], str],
    has_active: Callable[[T], bool],
) -> InstallResolution[T]:
    """Discover-then-pick: honour configured override, else auto-select.

    `configured_source` is the user's `[saves].<backend>_install` value
    (None when unset). `source_of(install)` extracts the comparable
    source string from each install. `has_active(install)` is the
    auto-selector's active-use predicate.
    """
    if not installs:
        return InstallResolution(install=None, reason=ResolutionReason.NO_INSTALLS)
    if configured_source is not None:
        match = next((i for i in installs if source_of(i) == configured_source), None)
        if match is not None:
            return InstallResolution(install=match, reason=ResolutionReason.EXPLICIT_MATCH)
        return InstallResolution(install=None, reason=ResolutionReason.EXPLICIT_MISMATCH)
    active = select_active(installs, has_active=has_active)
    if active is None:
        return InstallResolution(install=None, reason=ResolutionReason.AMBIGUOUS)
    return InstallResolution(install=active, reason=ResolutionReason.AUTO_ACTIVE)
