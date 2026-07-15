import pytest

from autoedit import config, paths


def test_create_project_structure(env):
    base = paths.create_project("demo")
    for sub in ["input/cam_a", "input/cam_b", "input/audio_dji",
                "input/broll", "output"]:
        assert (base / sub).is_dir()
    assert paths.list_projects() == ["demo"]


def test_invalid_project_names(env):
    for bad in ["", "../x", "a b", ".hidden", "ä"]:
        assert not paths.valid_project_name(bad)
    with pytest.raises(ValueError):
        paths.project_dir("../x")


def test_config_defaults_and_save(env):
    paths.create_project("demo")
    cfg = config.load_config("demo")
    assert cfg["reel_laenge_sek"] == 60
    assert cfg["broll_dauer_sek"] == 2.0
    assert cfg["sprache"] == "de"
    assert cfg["musik_aktiv"] is False
    assert cfg["untertitel_aktiv"] is True

    saved = config.save_config("demo", {"reel_laenge_sek": 90, "musik_aktiv": True})
    assert saved["reel_laenge_sek"] == 90
    assert config.load_config("demo")["musik_aktiv"] is True


def test_config_validation(env):
    paths.create_project("demo")
    with pytest.raises(ValueError):
        config.save_config("demo", {"reel_laenge_sek": 1})
    with pytest.raises(ValueError):
        config.save_config("demo", {"export_format": "16:9"})
    with pytest.raises(ValueError):
        config.save_config("demo", {"unbekannt": 1})


def test_presets_roundtrip(env):
    paths.create_project("demo")
    config.save_config("demo", {"reel_laenge_sek": 45, "musik_aktiv": True})
    config.save_preset("reel45", config.load_config("demo"))
    assert "reel45" in config.list_presets()

    paths.create_project("zwei")
    cfg = config.apply_preset("zwei", "reel45")
    assert cfg["reel_laenge_sek"] == 45
    assert cfg["musik_aktiv"] is True
