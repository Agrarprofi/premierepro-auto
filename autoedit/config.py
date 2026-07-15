"""Projekt-Konfiguration (config.yaml) und Presets."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

from . import paths

DEFAULT_CONFIG: dict[str, Any] = {
    "reel_laenge_sek": 60,
    "broll_dauer_sek": 2.0,
    # 0 = automatisch (max. 40% Abdeckung); >0 = so viele Schnittbilder
    # anpeilen (z.B. 15 bei einem 60s-Reel), Clips dürfen mehrfach vorkommen
    "broll_ziel_anzahl": 0,
    # Mindestabstand zwischen zwei B-Roll-Einblendungen
    "broll_min_abstand_sek": 1.0,
    "sprache": "de",
    "musik_aktiv": False,
    "untertitel_aktiv": True,
    # "quelle" = Auflösung/Framerate der Kamera A, "9:16" = 1080x1920 (Reels)
    "export_format": "quelle",
    # Claude-Modell für Schnitt/B-Roll/Untertitel/Musik
    "claude_modell": "claude-sonnet-4-6",
}

CONFIG_FILENAME = "config.yaml"


def _config_path(project: str) -> Path:
    return paths.project_dir(project) / CONFIG_FILENAME


def load_config(project: str) -> dict[str, Any]:
    """Config mit Defaults gemergt laden; fehlende Datei => Defaults."""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    path = _config_path(project)
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"config.yaml von {project} ist kein Mapping")
        cfg.update(loaded)
    return cfg


def save_config(project: str, updates: dict[str, Any]) -> dict[str, Any]:
    """Teil-Updates übernehmen, validieren und speichern."""
    cfg = load_config(project)
    unknown = set(updates) - set(DEFAULT_CONFIG)
    if unknown:
        raise ValueError(f"Unbekannte Config-Schlüssel: {sorted(unknown)}")
    cfg.update(updates)
    _validate(cfg)
    _config_path(project).write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return cfg


def _validate(cfg: dict[str, Any]) -> None:
    if not (5 <= float(cfg["reel_laenge_sek"]) <= 3600):
        raise ValueError("reel_laenge_sek muss zwischen 5 und 3600 liegen")
    if not (0.5 <= float(cfg["broll_dauer_sek"]) <= 30):
        raise ValueError("broll_dauer_sek muss zwischen 0.5 und 30 liegen")
    if not (0 <= int(cfg["broll_ziel_anzahl"]) <= 100):
        raise ValueError("broll_ziel_anzahl muss zwischen 0 und 100 liegen")
    if not (0 <= float(cfg["broll_min_abstand_sek"]) <= 30):
        raise ValueError("broll_min_abstand_sek muss zwischen 0 und 30 liegen")
    if cfg["export_format"] not in ("quelle", "9:16"):
        raise ValueError('export_format muss "quelle" oder "9:16" sein')
    for key in ("musik_aktiv", "untertitel_aktiv"):
        if not isinstance(cfg[key], bool):
            raise ValueError(f"{key} muss true/false sein")


# ---------------------------------------------------------------- Presets

def list_presets() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    pdir = paths.presets_dir()
    if not pdir.is_dir():
        return result
    for f in sorted(pdir.glob("*.yaml")):
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        if isinstance(data, dict):
            result[f.stem] = data
    return result


def save_preset(name: str, cfg: dict[str, Any]) -> None:
    if not paths.valid_project_name(name):
        raise ValueError(f"Ungültiger Preset-Name: {name!r}")
    known = {k: v for k, v in cfg.items() if k in DEFAULT_CONFIG}
    pdir = paths.presets_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / f"{name}.yaml").write_text(
        yaml.safe_dump(known, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def apply_preset(project: str, preset_name: str) -> dict[str, Any]:
    presets = list_presets()
    if preset_name not in presets:
        raise ValueError(f"Preset nicht gefunden: {preset_name!r}")
    return save_config(project, presets[preset_name])
