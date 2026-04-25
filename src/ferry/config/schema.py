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
class TransformsConfig:
    """Per-platform transform pipelines (DESIGN.md §5.5).

    Platforms not listed default to no pipeline (file flows through unchanged).
    """

    pipelines: dict[str, tuple[str, ...]]

    def for_platform(self, platform_slug: str) -> tuple[str, ...]:
        return self.pipelines.get(platform_slug, ())


def _empty_transforms() -> TransformsConfig:
    return TransformsConfig(pipelines={})


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    romm: RommConfig
    destination: Destination | None = None
    sync: SyncConfig | None = None
    transforms: TransformsConfig = field(default_factory=_empty_transforms)
