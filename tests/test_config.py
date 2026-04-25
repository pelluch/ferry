from pathlib import Path

import pytest

from ferry.config import (
    ApiKeySource,
    ConfigInvalidError,
    ConfigNotFoundError,
    default_config_path,
    load_config,
)


def write(path: Path, content: str) -> Path:
    path.write_text(content)
    return path


def minimal_toml(api_key: str = "rmm_abcdef0123456789") -> str:
    return f'[romm]\nurl = "https://romm.example.tld"\napi_key = "{api_key}"\n'


def test_loads_minimal_valid_config(tmp_path: Path) -> None:
    cfg_file = write(tmp_path / "config.toml", minimal_toml())
    loaded = load_config(cfg_file, env={})
    assert loaded.config.romm.url == "https://romm.example.tld"
    assert loaded.config.romm.api_key == "rmm_abcdef0123456789"
    assert loaded.config.romm.allow_insecure_ssl is False
    assert loaded.api_key_source == ApiKeySource.TOML
    assert loaded.config_path == cfg_file


def test_url_strips_trailing_slash(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        '[romm]\nurl = "https://romm.example.tld/"\napi_key = "rmm_xyz1234567"\n',
    )
    loaded = load_config(cfg_file, env={})
    assert loaded.config.romm.url == "https://romm.example.tld"


def test_allow_insecure_ssl_is_read(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        '[romm]\nurl = "https://r"\napi_key = "rmm_xyz1234567"\nallow_insecure_ssl = true\n',
    )
    loaded = load_config(cfg_file, env={})
    assert loaded.config.romm.allow_insecure_ssl is True


def test_env_var_overrides_toml_api_key(tmp_path: Path) -> None:
    cfg_file = write(tmp_path / "config.toml", minimal_toml("rmm_from_toml"))
    loaded = load_config(cfg_file, env={"FERRY_ROMM_API_KEY": "rmm_from_env"})
    assert loaded.config.romm.api_key == "rmm_from_env"
    assert loaded.api_key_source == ApiKeySource.ENV


def test_env_var_supplies_key_when_toml_omits_it(tmp_path: Path) -> None:
    cfg_file = write(tmp_path / "config.toml", '[romm]\nurl = "https://r"\n')
    loaded = load_config(cfg_file, env={"FERRY_ROMM_API_KEY": "rmm_only_env"})
    assert loaded.config.romm.api_key == "rmm_only_env"
    assert loaded.api_key_source == ApiKeySource.ENV


def test_missing_api_key_raises(tmp_path: Path) -> None:
    cfg_file = write(tmp_path / "config.toml", '[romm]\nurl = "https://r"\n')
    with pytest.raises(ConfigInvalidError, match="missing RomM API key"):
        load_config(cfg_file, env={})


def test_missing_url_raises(tmp_path: Path) -> None:
    cfg_file = write(tmp_path / "config.toml", '[romm]\napi_key = "rmm_xyz1234567"\n')
    with pytest.raises(ConfigInvalidError, match=r"\[romm\]\.url"):
        load_config(cfg_file, env={})


def test_url_must_have_scheme(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        '[romm]\nurl = "romm.example.tld"\napi_key = "rmm_xyz1234567"\n',
    )
    with pytest.raises(ConfigInvalidError, match="http://"):
        load_config(cfg_file, env={})


def test_unknown_key_raises(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        '[romm]\nurl = "https://r"\napi_key = "rmm_xyz1234567"\napi-key = "typo"\n',
    )
    with pytest.raises(ConfigInvalidError, match="unknown keys"):
        load_config(cfg_file, env={})


def test_unknown_top_level_section_raises(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + "\n[strange]\nx = 1\n",
    )
    with pytest.raises(ConfigInvalidError, match="unknown top-level"):
        load_config(cfg_file, env={})


def test_missing_romm_section_raises(tmp_path: Path) -> None:
    cfg_file = write(tmp_path / "config.toml", "")
    with pytest.raises(ConfigInvalidError, match=r"\[romm\] section is required"):
        load_config(cfg_file, env={})


def test_missing_file_raises_not_found(tmp_path: Path) -> None:
    with pytest.raises(ConfigNotFoundError):
        load_config(tmp_path / "nope.toml", env={})


def test_invalid_toml_raises(tmp_path: Path) -> None:
    cfg_file = write(tmp_path / "config.toml", "[romm\nurl = ")
    with pytest.raises(ConfigInvalidError, match="invalid TOML"):
        load_config(cfg_file, env={})


def test_allow_insecure_ssl_must_be_bool(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        '[romm]\nurl = "https://r"\napi_key = "rmm_xyz1234567"\nallow_insecure_ssl = "yes"\n',
    )
    with pytest.raises(ConfigInvalidError, match="allow_insecure_ssl"):
        load_config(cfg_file, env={})


def test_default_path_uses_xdg_config_home(tmp_path: Path) -> None:
    p = default_config_path(env={"XDG_CONFIG_HOME": str(tmp_path)})
    assert p == tmp_path / "ferry" / "config.toml"


def test_default_path_falls_back_to_home_config() -> None:
    p = default_config_path(env={})
    assert p == Path.home() / ".config" / "ferry" / "config.toml"


def test_env_config_path_used_when_arg_omitted(tmp_path: Path) -> None:
    cfg_file = write(tmp_path / "elsewhere.toml", minimal_toml())
    loaded = load_config(env={"FERRY_CONFIG": str(cfg_file)})
    assert loaded.config_path == cfg_file


def test_repr_does_not_leak_api_key(tmp_path: Path) -> None:
    cfg_file = write(tmp_path / "config.toml", minimal_toml("rmm_secret_value"))
    loaded = load_config(cfg_file, env={})
    assert "rmm_secret_value" not in repr(loaded.config.romm)
