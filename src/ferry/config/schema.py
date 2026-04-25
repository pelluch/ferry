from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True, kw_only=True)
class RommConfig:
    url: str
    api_key: str = field(repr=False)
    allow_insecure_ssl: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    romm: RommConfig
