from dataclasses import dataclass, field

from ferry.domain.destination import Destination


@dataclass(frozen=True, slots=True, kw_only=True)
class RommConfig:
    url: str
    api_key: str = field(repr=False)
    allow_insecure_ssl: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncConfig:
    """Settings for `ferry sync`. Required for sync; ignored by other commands."""

    collection: str
    primary_version_only: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    romm: RommConfig
    destination: Destination | None = None
    sync: SyncConfig | None = None
