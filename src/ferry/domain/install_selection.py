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
"""

from __future__ import annotations

from collections.abc import Callable


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
