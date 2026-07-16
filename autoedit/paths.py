"""Ordnerstruktur und Projektverwaltung."""

from __future__ import annotations

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

INPUT_SUBDIRS = ["input/cam_a", "input/cam_b", "input/audio_dji", "input/broll"]
PROJECT_SUBDIRS = INPUT_SUBDIRS + ["output"]

# Rollen entsprechen den Input-Unterordnern
ROLES = ["cam_a", "cam_b", "audio_dji", "broll"]

_NAME_RE = re.compile(r"^[A-Za-z0-9._\-]+$")


def projects_dir() -> Path:
    return Path(os.environ.get("AUTOEDIT_PROJECTS", REPO_ROOT / "projects"))


def music_dir() -> Path:
    return Path(os.environ.get("AUTOEDIT_MUSIC", REPO_ROOT / "music_library"))


def presets_dir() -> Path:
    return Path(os.environ.get("AUTOEDIT_PRESETS", REPO_ROOT / "presets"))


def trash_dir() -> Path:
    """Papierkorb für gelöschte Projekte (neben dem projects/-Ordner);
    wird vom Nutzer manuell geleert."""
    return Path(os.environ.get("AUTOEDIT_TRASH",
                               projects_dir().parent / "papierkorb"))


def valid_project_name(name: str) -> bool:
    return bool(name) and bool(_NAME_RE.match(name)) and not name.startswith(".")


def project_dir(name: str) -> Path:
    if not valid_project_name(name):
        raise ValueError(f"Ungültiger Projektname: {name!r}")
    return projects_dir() / name


def output_dir(name: str) -> Path:
    return project_dir(name) / "output"


def input_dir(name: str, role: str) -> Path:
    if role not in ROLES:
        raise ValueError(f"Unbekannte Rolle: {role!r}")
    return project_dir(name) / "input" / role


def create_project(name: str) -> Path:
    """Legt die komplette Ordnerstruktur eines Projekts an."""
    base = project_dir(name)
    for sub in PROJECT_SUBDIRS:
        (base / sub).mkdir(parents=True, exist_ok=True)
    return base


def list_projects() -> list[str]:
    root = projects_dir()
    if not root.is_dir():
        return []
    return sorted(
        p.name for p in root.iterdir() if p.is_dir() and valid_project_name(p.name)
    )


def project_exists(name: str) -> bool:
    return valid_project_name(name) and project_dir(name).is_dir()


def delete_project(name: str) -> Path:
    """Projekt in den Papierkorb VERSCHIEBEN (nichts wird zerstört).

    project_dir() validiert den Namen - kein Pfad außerhalb von
    projects/ erreichbar. Rückgabe: Zielordner im Papierkorb.
    Wiederherstellen = Ordner von Hand zurück nach projects/ schieben;
    endgültig löschen = Papierkorb manuell leeren.
    """
    import shutil
    from datetime import datetime

    d = project_dir(name)
    trash = trash_dir()
    trash.mkdir(parents=True, exist_ok=True)
    ziel = trash / name
    if ziel.exists():
        stempel = datetime.now().strftime("%Y%m%d-%H%M%S")
        ziel = trash / f"{name}_{stempel}"
        i = 1
        while ziel.exists():
            ziel = trash / f"{name}_{stempel}_{i}"
            i += 1
    shutil.move(str(d), str(ziel))
    return ziel
