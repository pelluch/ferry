"""Shared pytest fixtures."""

from collections.abc import Callable
from pathlib import Path

import pytest

from ferry.domain.state import RomState, TransformedOutput


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip RommHttpAdapter's retry backoff so tests don't pay 1s+3s+9s."""
    monkeypatch.setattr("ferry.adapters.romm.http.time.sleep", lambda *_: None)


@pytest.fixture(autouse=True)
def _isolated_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Redirect $HOME to a fresh tmp dir so tests never read or write the developer's
    real state.json, scratch cache, or `~/.config/ferry/`. XDG_* vars are *unset* so
    that HOME-derived fallback paths apply — tests that override HOME via their own
    monkeypatch automatically redirect XDG-style paths through the new HOME.
    """
    home = tmp_path_factory.mktemp("isolated-home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("FERRY_CONFIG", raising=False)
    monkeypatch.delenv("FERRY_ROMM_API_KEY", raising=False)
    return home


@pytest.fixture
def make_output() -> Callable[..., TransformedOutput]:
    def _make(path: str = "gc/Pikmin.iso") -> TransformedOutput:
        return TransformedOutput(path=path, md5="d41d8cd98f00b204e9800998ecf8427e", size=1024)

    return _make


@pytest.fixture
def make_rom(make_output) -> Callable[..., RomState]:
    def _make(rom_id: int = 26085, **overrides) -> RomState:
        defaults: dict = {
            "rom_id": rom_id,
            "platform_slug": "gc",
            "name": "Pikmin",
            "source_filename": "Pikmin.zip",
            "source_md5": "0123456789abcdef0123456789abcdef",
            "source_size": 2048,
            "source_updated_at": "2026-04-25T12:00:00Z",
            # RomM-style hash: matches the `md5_hash` default in
            # `tests.test_sync_plan.romm_rom` so the by-default state vs.
            # API-rom pairing classifies as unchanged (the typical
            # "nothing changed" baseline).
            "source_romm_md5": "11111111111111111111111111111111",
            "transforms": ("unzip",),
            "outputs": (make_output(),),
            "primary_output_index": 0,
            "synced_at": "2026-04-25T12:01:00Z",
        }
        defaults.update(overrides)
        return RomState(**defaults)

    return _make
