"""Per-user directory resolution for state, cache, data, and config.

OS-neutral surface (`state_dir`, `cache_dir`, `data_dir`, `config_dir`)
implemented today against the XDG Base Directory spec — the only
platform ferry targets. A future port to macOS / Windows would fork the
implementation here while leaving callers untouched.

Pure functions, no side effects. `env=None` reads `os.environ`; pass a
`Mapping[str, str]` (or plain dict) to inject in tests.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def state_dir(env: Mapping[str, str] | None = None) -> Path:
    """Base dir for persistent program state — survives reboots; not user-edited.

    Linux: `$XDG_STATE_HOME` or `~/.local/state`.
    """
    return _resolve(env, "XDG_STATE_HOME", Path(".local") / "state")


def cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """Base dir for non-essential cached data — safe to delete.

    Linux: `$XDG_CACHE_HOME` or `~/.cache`.
    """
    return _resolve(env, "XDG_CACHE_HOME", Path(".cache"))


def data_dir(env: Mapping[str, str] | None = None) -> Path:
    """Base dir for user-specific application data — installable artifacts.

    Linux: `$XDG_DATA_HOME` or `~/.local/share`.
    """
    return _resolve(env, "XDG_DATA_HOME", Path(".local") / "share")


def config_dir(env: Mapping[str, str] | None = None) -> Path:
    """Base dir for user-specific configuration — hand-edited by the user.

    Linux: `$XDG_CONFIG_HOME` or `~/.config`.
    """
    return _resolve(env, "XDG_CONFIG_HOME", Path(".config"))


def _resolve(env: Mapping[str, str] | None, var: str, home_relative_default: Path) -> Path:
    env = env if env is not None else os.environ
    base = env.get(var)
    return Path(base) if base else Path.home() / home_relative_default
