"""Shared pytest fixtures."""

from collections.abc import Callable

import pytest

from ferry.domain.state import RomState, TransformedOutput


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip RommHttpAdapter's retry backoff so tests don't pay 1s+3s+9s."""
    monkeypatch.setattr("ferry.adapters.romm.http.time.sleep", lambda *_: None)


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
            "transforms": ("unzip",),
            "outputs": (make_output(),),
            "primary_output_index": 0,
            "synced_at": "2026-04-25T12:01:00Z",
        }
        defaults.update(overrides)
        return RomState(**defaults)

    return _make
