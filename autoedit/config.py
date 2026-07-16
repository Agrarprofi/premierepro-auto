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
    # Sprechpausen INNERHALB eines Segments ab dieser Länge automatisch
    # rausschneiden (am echten Audio gemessen); 0 = aus
    "pausen_schnitt_sek": 1.0,
    # Füllwort-Entfernung: Pausen über ab_sek werden auf ziel_sek gekürzt;
    # optionale Liste = Sprechstil-Wörter wie "halt"/"eben" mit erkennen
    "fuellwort_pause_ab_sek": 1.5,
    "fuellwort_pause_ziel_sek": 0.4,
    "fuellwort_optionale_liste": False,
    # Freitext-Vorgaben für die Reel-Auswahl (z.B. "Fokus auf Bodengesundheit")
    "schnitt_hinweise": "",
    "musik_aktiv": False,
    "untertitel_aktiv": True,
    # "quelle" = Auflösung/Framerate der Kamera A, "9:16" = 1080x1920 (Reels)
    "export_format": "quelle",
    # Auto-Zoom im Export: "wechsel" = statischer Punch-In auf jedem
    # 2. Segment (kaschiert Jump-Cuts, klassischer Reel-Look),
    # "sanft" = langsame Zoom-Fahrt in jedem Segment (Keyframes),
    # "aus" = keine Zooms
    "zoom_modus": "wechsel",
    "zoom_staerke_prozent": 8,
    # Claude-Modell für Schnitt/B-Roll/Untertitel/Musik
    "claude_modell": "claude-sonnet-4-6",
    # WhisperX-Modell; "large-v3-turbo" ist ~4x schneller bei fast
    # gleicher Qualität (braucht aktuelles whisperx/faster-whisper)
    "whisper_modell": "large-v3",
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
    if not (0 <= float(cfg["pausen_schnitt_sek"]) <= 10):
        raise ValueError("pausen_schnitt_sek muss zwischen 0 und 10 liegen")
    if cfg["export_format"] not in ("quelle", "9:16"):
        raise ValueError('export_format muss "quelle" oder "9:16" sein')
    if cfg["zoom_modus"] not in ("aus", "wechsel", "sanft"):
        raise ValueError('zoom_modus muss "aus", "wechsel" oder "sanft" sein')
    if not (0 <= float(cfg["zoom_staerke_prozent"]) <= 40):
        raise ValueError("zoom_staerke_prozent muss zwischen 0 und 40 liegen")
    if not isinstance(cfg["schnitt_hinweise"], str) or len(cfg["schnitt_hinweise"]) > 2000:
        raise ValueError("schnitt_hinweise muss ein Text (max. 2000 Zeichen) sein")
    if not (0.5 <= float(cfg["fuellwort_pause_ab_sek"]) <= 10):
        raise ValueError("fuellwort_pause_ab_sek muss zwischen 0.5 und 10 liegen")
    if not (0.1 <= float(cfg["fuellwort_pause_ziel_sek"]) <= 2):
        raise ValueError("fuellwort_pause_ziel_sek muss zwischen 0.1 und 2 liegen")
    for key in ("musik_aktiv", "untertitel_aktiv", "fuellwort_optionale_liste"):
        if not isinstance(cfg[key], bool):
            raise ValueError(f"{key} muss true/false sein")
    for key in ("claude_modell", "whisper_modell"):
        if not isinstance(cfg[key], str) or not cfg[key].strip():
            raise ValueError(f"{key} darf nicht leer sein")


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


def delete_preset(name: str) -> dict[str, dict[str, Any]]:
    """Preset löschen; Rückgabe: aktualisierte Preset-Liste."""
    if not paths.valid_project_name(name):
        raise ValueError(f"Ungültiger Preset-Name: {name!r}")
    f = paths.presets_dir() / f"{name}.yaml"
    if not f.is_file():
        raise KeyError(f"Preset nicht gefunden: {name!r}")
    f.unlink()
    return list_presets()


def apply_preset(project: str, preset_name: str) -> dict[str, Any]:
    presets = list_presets()
    if preset_name not in presets:
        raise ValueError(f"Preset nicht gefunden: {preset_name!r}")
    return save_config(project, presets[preset_name])
