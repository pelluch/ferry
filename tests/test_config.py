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


# ---------------------------------------------------------------------------
# [destination] section
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin Path.home() to a fresh tmp dir so preset path resolution is deterministic."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_no_destination_section_yields_none(tmp_path: Path) -> None:
    cfg_file = write(tmp_path / "config.toml", minimal_toml())
    loaded = load_config(cfg_file, env={})
    assert loaded.config.destination is None


def test_destination_preset_resolves_under_home(tmp_path: Path, fake_home: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[destination]\npreset = "retrodeck-flatpak"\n',
    )
    loaded = load_config(cfg_file, env={})
    dest = loaded.config.destination
    assert dest is not None
    assert dest.preset == "retrodeck-flatpak"
    assert dest.roms_base == fake_home / "retrodeck/roms"
    assert dest.bios_base == fake_home / "retrodeck/bios"


def test_destination_preset_with_bios_override(tmp_path: Path, fake_home: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml()
        + '\n[destination]\npreset = "retrodeck-flatpak"\nbios_base = "/mnt/sd/bios"\n',
    )
    loaded = load_config(cfg_file, env={})
    dest = loaded.config.destination
    assert dest is not None
    assert dest.preset == "retrodeck-flatpak"
    assert dest.roms_base == fake_home / "retrodeck/roms"  # preset default
    assert dest.bios_base == Path("/mnt/sd/bios")  # explicit override


def test_destination_explicit_paths_no_preset(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[destination]\nroms_base = "/data/roms"\nbios_base = "/data/bios"\n',
    )
    loaded = load_config(cfg_file, env={})
    dest = loaded.config.destination
    assert dest is not None
    assert dest.preset is None
    assert dest.roms_base == Path("/data/roms")
    assert dest.bios_base == Path("/data/bios")


def test_destination_path_expanduser_is_applied(tmp_path: Path, fake_home: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml()
        + '\n[destination]\nroms_base = "~/games/roms"\nbios_base = "~/games/bios"\n',
    )
    loaded = load_config(cfg_file, env={})
    dest = loaded.config.destination
    assert dest is not None
    assert dest.roms_base == fake_home / "games/roms"
    assert dest.bios_base == fake_home / "games/bios"


def test_destination_unknown_preset_raises(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[destination]\npreset = "nintendo-switch-classic"\n',
    )
    with pytest.raises(ConfigInvalidError, match="unknown preset"):
        load_config(cfg_file, env={})


def test_destination_no_preset_and_no_roms_raises(tmp_path: Path) -> None:
    cfg_file = write(tmp_path / "config.toml", minimal_toml() + "\n[destination]\n")
    with pytest.raises(ConfigInvalidError, match="preset.*roms_base"):
        load_config(cfg_file, env={})


def test_destination_explicit_roms_only_is_valid(tmp_path: Path) -> None:
    """bios_base is optional — bare ES-DE has no centralized BIOS root."""
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[destination]\nroms_base = "/data/roms"\n',
    )
    loaded = load_config(cfg_file, env={})
    dest = loaded.config.destination
    assert dest is not None
    assert dest.roms_base == Path("/data/roms")
    assert dest.bios_base is None


def test_destination_bios_without_roms_raises(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[destination]\nbios_base = "/data/bios"\n',
    )
    with pytest.raises(ConfigInvalidError, match="preset.*roms_base"):
        load_config(cfg_file, env={})


def test_destination_esde_native_preset_has_none_bios(tmp_path: Path, fake_home: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[destination]\npreset = "esde-native"\n',
    )
    loaded = load_config(cfg_file, env={})
    dest = loaded.config.destination
    assert dest is not None
    assert dest.preset == "esde-native"
    assert dest.roms_base == fake_home / "ROMs"
    assert dest.bios_base is None


def test_destination_esde_native_can_override_bios(tmp_path: Path, fake_home: Path) -> None:
    """User can opt into a centralized BIOS even when the preset doesn't have one."""
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[destination]\npreset = "esde-native"\nbios_base = "/srv/bios"\n',
    )
    loaded = load_config(cfg_file, env={})
    dest = loaded.config.destination
    assert dest is not None
    assert dest.bios_base == Path("/srv/bios")


# ---------------------------------------------------------------------------
# [sync] section
# ---------------------------------------------------------------------------


def test_no_sync_section_yields_none(tmp_path: Path) -> None:
    cfg_file = write(tmp_path / "config.toml", minimal_toml())
    loaded = load_config(cfg_file, env={})
    assert loaded.config.sync is None


def test_sync_requires_at_least_one_source(tmp_path: Path) -> None:
    """`[sync]` with neither collections nor platforms is rejected."""
    cfg_file = write(
        tmp_path / "config.toml", minimal_toml() + "\n[sync]\nprimary_version_only = true\n"
    )
    with pytest.raises(ConfigInvalidError, match="collections.*platforms|at least one"):
        load_config(cfg_file, env={})


def test_sync_collections_must_be_string_list(tmp_path: Path) -> None:
    cfg_file = write(tmp_path / "config.toml", minimal_toml() + "\n[sync]\ncollections = [12]\n")
    with pytest.raises(ConfigInvalidError, match="collections"):
        load_config(cfg_file, env={})


def test_sync_loads_with_collections(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[sync]\ncollections = ["Steam Deck", "Quick Picks"]\n',
    )
    loaded = load_config(cfg_file, env={})
    assert loaded.config.sync is not None
    assert loaded.config.sync.collections == ("Steam Deck", "Quick Picks")
    assert loaded.config.sync.platforms == ()
    assert loaded.config.sync.primary_version_only is False


def test_sync_loads_with_platforms_only(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[sync]\nplatforms = ["gba", "snes"]\n',
    )
    loaded = load_config(cfg_file, env={})
    assert loaded.config.sync is not None
    assert loaded.config.sync.collections == ()
    assert loaded.config.sync.platforms == ("gba", "snes")


def test_sync_loads_with_collections_and_platforms(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + "\n[sync]\n" + 'collections = ["Steam Deck"]\n' + 'platforms = ["gba"]\n',
    )
    loaded = load_config(cfg_file, env={})
    assert loaded.config.sync is not None
    assert loaded.config.sync.collections == ("Steam Deck",)
    assert loaded.config.sync.platforms == ("gba",)


def test_sync_dedups_string_lists(tmp_path: Path) -> None:
    """User listing the same source twice → silently dedup; preserve first-seen order."""
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[sync]\nplatforms = ["gba", "snes", "gba"]\n',
    )
    loaded = load_config(cfg_file, env={})
    assert loaded.config.sync is not None
    assert loaded.config.sync.platforms == ("gba", "snes")


def test_sync_primary_version_only_parses(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[sync]\ncollections = ["Steam Deck"]\nprimary_version_only = true\n',
    )
    loaded = load_config(cfg_file, env={})
    assert loaded.config.sync is not None
    assert loaded.config.sync.primary_version_only is True


def test_sync_delete_on_remove_defaults_false(tmp_path: Path) -> None:
    """Less-surprising default — first sync against stale state can't silently trash."""
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[sync]\ncollections = ["Steam Deck"]\n',
    )
    loaded = load_config(cfg_file, env={})
    assert loaded.config.sync is not None
    assert loaded.config.sync.delete_on_remove is False
    assert loaded.config.sync.trash_retention_days == 14


def test_sync_delete_on_remove_opt_in(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml()
        + "\n[sync]\n"
        + 'collections = ["Steam Deck"]\n'
        + "delete_on_remove = true\n"
        + "trash_retention_days = 30\n",
    )
    loaded = load_config(cfg_file, env={})
    assert loaded.config.sync is not None
    assert loaded.config.sync.delete_on_remove is True
    assert loaded.config.sync.trash_retention_days == 30


def test_sync_trash_retention_must_be_non_negative(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[sync]\ncollections = ["Steam Deck"]\ntrash_retention_days = -1\n',
    )
    with pytest.raises(ConfigInvalidError, match="non-negative"):
        load_config(cfg_file, env={})


def test_sync_trash_retention_must_be_int_not_bool(tmp_path: Path) -> None:
    """Booleans are int subclasses in Python — guard against `true` as days."""
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[sync]\ncollections = ["Steam Deck"]\ntrash_retention_days = true\n',
    )
    with pytest.raises(ConfigInvalidError, match="non-negative"):
        load_config(cfg_file, env={})


def test_sync_unknown_key_raises(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[sync]\ncollections = ["Steam Deck"]\nbreed = "labradoodle"\n',
    )
    with pytest.raises(ConfigInvalidError, match=r"unknown keys under \[sync\]"):
        load_config(cfg_file, env={})


def test_sync_section_must_be_a_table(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        f'sync = "Steam Deck"\n{minimal_toml()}',
    )
    with pytest.raises(ConfigInvalidError, match=r"\[sync\] must be a table"):
        load_config(cfg_file, env={})


# ---------------------------------------------------------------------------
# [transforms] section
# ---------------------------------------------------------------------------


def test_no_transforms_section_yields_empty_config(tmp_path: Path) -> None:
    cfg_file = write(tmp_path / "config.toml", minimal_toml())
    loaded = load_config(cfg_file, env={})
    assert loaded.config.transforms.pipelines == {}
    # Unconfigured platforms get no pipeline.
    assert loaded.config.transforms.for_platform("gc") == ()


def test_transforms_pipeline_parses(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[transforms.gc]\npipeline = ["unzip"]\n',
    )
    loaded = load_config(cfg_file, env={})
    assert loaded.config.transforms.for_platform("gc") == ("unzip",)


def test_transforms_multiple_platforms(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml()
        + '\n[transforms.gc]\npipeline = ["unzip"]\n'
        + '\n[transforms.wii]\npipeline = ["unzip"]\n',
    )
    loaded = load_config(cfg_file, env={})
    assert loaded.config.transforms.for_platform("gc") == ("unzip",)
    assert loaded.config.transforms.for_platform("wii") == ("unzip",)
    assert loaded.config.transforms.for_platform("nes") == ()


def test_transforms_unknown_name_raises(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[transforms.gc]\npipeline = ["unzipp"]\n',
    )
    with pytest.raises(ConfigInvalidError, match="unknown transform 'unzipp'"):
        load_config(cfg_file, env={})


def test_transforms_pipeline_must_be_list(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[transforms.gc]\npipeline = "unzip"\n',
    )
    with pytest.raises(ConfigInvalidError, match="must be a list"):
        load_config(cfg_file, env={})


def test_transforms_unknown_subkey_raises(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[transforms.gc]\npipeline = ["unzip"]\nbogus = true\n',
    )
    with pytest.raises(ConfigInvalidError, match=r"unknown keys under \[transforms\.gc\]"):
        load_config(cfg_file, env={})


def test_transforms_empty_pipeline_is_valid(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + "\n[transforms.gc]\npipeline = []\n",
    )
    loaded = load_config(cfg_file, env={})
    assert loaded.config.transforms.for_platform("gc") == ()


def test_transforms_section_must_be_a_table(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        f'transforms = "unzip"\n{minimal_toml()}',
    )
    with pytest.raises(ConfigInvalidError, match=r"\[transforms\] must be a table"):
        load_config(cfg_file, env={})


def test_destination_unknown_key_raises(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[destination]\npreset = "esde-native"\nsave_base = "/etc"\n',
    )
    with pytest.raises(ConfigInvalidError, match="unknown keys under \\[destination\\]"):
        load_config(cfg_file, env={})


def test_destination_path_must_be_string(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + "\n[destination]\nroms_base = 1\nbios_base = 2\n",
    )
    with pytest.raises(ConfigInvalidError, match="roms_base"):
        load_config(cfg_file, env={})


def test_destination_section_must_be_a_table(tmp_path: Path) -> None:
    # `destination` at top level (before any [section] header) parses as a
    # bare top-level value, not a table — exactly the "[destination] preset = X
    # mistakenly written as one line" error mode we want to catch.
    cfg_file = write(
        tmp_path / "config.toml",
        f'destination = "retrodeck"\n{minimal_toml()}',
    )
    with pytest.raises(ConfigInvalidError, match="\\[destination\\] must be a table"):
        load_config(cfg_file, env={})


# ---------------------------------------------------------------------------
# [saves] section (v2)
# ---------------------------------------------------------------------------


def test_no_saves_section_yields_none(tmp_path: Path) -> None:
    cfg_file = write(tmp_path / "config.toml", minimal_toml())
    loaded = load_config(cfg_file, env={})
    assert loaded.config.saves is None


def test_empty_saves_section_defaults_to_enabled(tmp_path: Path) -> None:
    """Bare `[saves]` opts in — `enabled` defaults to true."""
    cfg_file = write(tmp_path / "config.toml", minimal_toml() + "\n[saves]\n")
    loaded = load_config(cfg_file, env={})
    assert loaded.config.saves is not None
    assert loaded.config.saves.enabled is True
    assert loaded.config.saves.retroarch_install is None
    assert loaded.config.saves.dolphin_install is None


def test_saves_enabled_explicit_false(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + "\n[saves]\nenabled = false\n",
    )
    loaded = load_config(cfg_file, env={})
    assert loaded.config.saves is not None
    assert loaded.config.saves.enabled is False


def test_saves_retroarch_install_accepts_known_values(tmp_path: Path) -> None:
    for value in ("retrodeck-flatpak", "libretro-flatpak", "native"):
        cfg_file = write(
            tmp_path / "config.toml",
            minimal_toml() + f'\n[saves]\nretroarch_install = "{value}"\n',
        )
        loaded = load_config(cfg_file, env={})
        assert loaded.config.saves is not None
        assert loaded.config.saves.retroarch_install == value


def test_saves_retroarch_install_rejects_unknown_value(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[saves]\nretroarch_install = "esx"\n',
    )
    with pytest.raises(ConfigInvalidError, match="retroarch_install"):
        load_config(cfg_file, env={})


def test_saves_dolphin_install_accepts_known_values(tmp_path: Path) -> None:
    for value in ("retrodeck-flatpak", "emudeck-flatpak", "native"):
        cfg_file = write(
            tmp_path / "config.toml",
            minimal_toml() + f'\n[saves]\ndolphin_install = "{value}"\n',
        )
        loaded = load_config(cfg_file, env={})
        assert loaded.config.saves is not None
        assert loaded.config.saves.dolphin_install == value


def test_saves_dolphin_install_rejects_unknown_value(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[saves]\ndolphin_install = "libretro-flatpak"\n',
    )
    with pytest.raises(ConfigInvalidError, match="dolphin_install"):
        load_config(cfg_file, env={})


def test_saves_enabled_must_be_bool(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[saves]\nenabled = "yes"\n',
    )
    with pytest.raises(ConfigInvalidError, match="enabled"):
        load_config(cfg_file, env={})


def test_saves_section_must_be_table(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        f'saves = "on"\n{minimal_toml()}',
    )
    with pytest.raises(ConfigInvalidError, match=r"\[saves\] must be a table"):
        load_config(cfg_file, env={})


def test_saves_unknown_key_raises(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[saves]\nbogus = "x"\n',
    )
    with pytest.raises(ConfigInvalidError, match=r"unknown keys under \[saves\]"):
        load_config(cfg_file, env={})


# ---------------------------------------------------------------------------
# [bios] section (v5.5)
# ---------------------------------------------------------------------------


def test_no_bios_section_yields_none(tmp_path: Path) -> None:
    cfg_file = write(tmp_path / "config.toml", minimal_toml())
    loaded = load_config(cfg_file, env={})
    assert loaded.config.bios is None


def test_empty_bios_section_defaults_to_enabled(tmp_path: Path) -> None:
    """Bare `[bios]` opts in — `enabled` defaults to true."""
    cfg_file = write(tmp_path / "config.toml", minimal_toml() + "\n[bios]\n")
    loaded = load_config(cfg_file, env={})
    assert loaded.config.bios is not None
    assert loaded.config.bios.enabled is True
    assert loaded.config.bios.files == {}


def test_bios_enabled_explicit_false(tmp_path: Path) -> None:
    cfg_file = write(tmp_path / "config.toml", minimal_toml() + "\n[bios]\nenabled = false\n")
    loaded = load_config(cfg_file, env={})
    assert loaded.config.bios is not None
    assert loaded.config.bios.enabled is False


def test_bios_files_allowlist_parsed(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml()
        + "\n[bios]\n[bios.files]\n"
        + 'ps2 = ["ps2-0230a-20080220.bin"]\n'
        + 'dc = ["dc_boot.bin", "dc_flash.bin"]\n',
    )
    loaded = load_config(cfg_file, env={})
    assert loaded.config.bios is not None
    assert loaded.config.bios.files["ps2"] == ("ps2-0230a-20080220.bin",)
    assert loaded.config.bios.files["dc"] == ("dc_boot.bin", "dc_flash.bin")
    assert loaded.config.bios.allowlist_for("ps2") == ("ps2-0230a-20080220.bin",)
    assert loaded.config.bios.allowlist_for("snes") is None


def test_bios_files_dedups_within_a_platform(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[bios.files]\nps2 = ["a.bin", "a.bin", "b.bin"]\n',
    )
    loaded = load_config(cfg_file, env={})
    assert loaded.config.bios is not None
    assert loaded.config.bios.files["ps2"] == ("a.bin", "b.bin")


def test_bios_files_empty_list_is_valid(tmp_path: Path) -> None:
    """An empty allowlist means 'sync no firmware for this platform'."""
    cfg_file = write(tmp_path / "config.toml", minimal_toml() + "\n[bios.files]\nps2 = []\n")
    loaded = load_config(cfg_file, env={})
    assert loaded.config.bios is not None
    assert loaded.config.bios.allowlist_for("ps2") == ()


def test_bios_enabled_must_be_bool(tmp_path: Path) -> None:
    cfg_file = write(tmp_path / "config.toml", minimal_toml() + '\n[bios]\nenabled = "yes"\n')
    with pytest.raises(ConfigInvalidError, match=r"\[bios\].enabled must be a boolean"):
        load_config(cfg_file, env={})


def test_bios_files_must_be_list_of_strings(tmp_path: Path) -> None:
    cfg_file = write(tmp_path / "config.toml", minimal_toml() + "\n[bios.files]\nps2 = [1, 2]\n")
    with pytest.raises(ConfigInvalidError, match=r"\[bios.files\].ps2 must be a list"):
        load_config(cfg_file, env={})


def test_bios_section_must_be_table(tmp_path: Path) -> None:
    cfg_file = write(tmp_path / "config.toml", f'bios = "on"\n{minimal_toml()}')
    with pytest.raises(ConfigInvalidError, match=r"\[bios\] must be a table"):
        load_config(cfg_file, env={})


def test_bios_unknown_key_raises(tmp_path: Path) -> None:
    cfg_file = write(tmp_path / "config.toml", minimal_toml() + '\n[bios]\nbogus = "x"\n')
    with pytest.raises(ConfigInvalidError, match=r"unknown keys under \[bios\]"):
        load_config(cfg_file, env={})


# ---------------------------------------------------------------------------
# [launch_hooks] section (v8)
# ---------------------------------------------------------------------------


def test_no_launch_hooks_section_uses_defaults(tmp_path: Path) -> None:
    cfg_file = write(tmp_path / "config.toml", minimal_toml())
    loaded = load_config(cfg_file, env={})
    assert loaded.config.launch_hooks.log_enabled is True
    assert loaded.config.launch_hooks.log_path is None


def test_empty_launch_hooks_section_uses_defaults(tmp_path: Path) -> None:
    cfg_file = write(tmp_path / "config.toml", minimal_toml() + "\n[launch_hooks]\n")
    loaded = load_config(cfg_file, env={})
    assert loaded.config.launch_hooks.log_enabled is True


def test_launch_hooks_log_disabled(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + "\n[launch_hooks]\nlog_enabled = false\n",
    )
    loaded = load_config(cfg_file, env={})
    assert loaded.config.launch_hooks.log_enabled is False


def test_launch_hooks_custom_log_path(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[launch_hooks]\nlog_path = "/var/log/ferry/launch.log"\n',
    )
    loaded = load_config(cfg_file, env={})
    assert loaded.config.launch_hooks.log_path == Path("/var/log/ferry/launch.log")


def test_launch_hooks_log_path_expands_tilde(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[launch_hooks]\nlog_path = "~/custom-launch.log"\n',
    )
    loaded = load_config(cfg_file, env={})
    assert loaded.config.launch_hooks.log_path is not None
    assert not str(loaded.config.launch_hooks.log_path).startswith("~")


def test_launch_hooks_log_enabled_must_be_bool(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[launch_hooks]\nlog_enabled = "yes"\n',
    )
    with pytest.raises(ConfigInvalidError, match="log_enabled"):
        load_config(cfg_file, env={})


def test_launch_hooks_log_path_must_be_non_empty_string(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[launch_hooks]\nlog_path = ""\n',
    )
    with pytest.raises(ConfigInvalidError, match="log_path"):
        load_config(cfg_file, env={})


def test_launch_hooks_section_must_be_table(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        f'launch_hooks = "on"\n{minimal_toml()}',
    )
    with pytest.raises(ConfigInvalidError, match=r"\[launch_hooks\] must be a table"):
        load_config(cfg_file, env={})


def test_launch_hooks_unknown_key_raises(tmp_path: Path) -> None:
    cfg_file = write(
        tmp_path / "config.toml",
        minimal_toml() + '\n[launch_hooks]\nbogus = "x"\n',
    )
    with pytest.raises(ConfigInvalidError, match=r"unknown keys under \[launch_hooks\]"):
        load_config(cfg_file, env={})
