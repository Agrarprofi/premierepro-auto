"""Phase 1a: Input-Ordner scannen und Medieninfos erfassen."""

from __future__ import annotations

import json
from pathlib import Path

from . import ffmpeg_utils, paths

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mxf"}
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aif", ".aiff", ".flac"}

MEDIA_INFO_FILE = "media_info.json"


def _iter_media(folder: Path, exts: set[str]) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in exts and not p.name.startswith(".")
    )


def clips_for_role(project: str, role: str) -> list[Path]:
    """Dateien einer Rolle in Aufnahmereihenfolge (= Dateinamen-Sortierung)."""
    folder = paths.input_dir(project, role)
    exts = AUDIO_EXTS if role == "audio_dji" else VIDEO_EXTS | AUDIO_EXTS
    if role in ("cam_a", "cam_b", "broll"):
        exts = VIDEO_EXTS
    return _iter_media(folder, exts)


def scan_project(project: str, progress=None) -> dict:
    """Alle Inputdateien mit ffprobe erfassen und media_info.json schreiben."""
    ffmpeg_utils.require_ffmpeg()
    all_files: list[tuple[str, Path]] = []
    for role in paths.ROLES:
        for f in clips_for_role(project, role):
            all_files.append((role, f))

    clips = []
    for i, (role, f) in enumerate(all_files):
        if progress:
            progress(i / max(1, len(all_files)), f"Analysiere {f.name}")
        info = ffmpeg_utils.media_info(f)
        info["rolle"] = role
        info["name"] = f.name
        info["relpfad"] = str(f.relative_to(paths.project_dir(project)))
        clips.append(info)

    result = {"projekt": project, "clips": clips}
    out = paths.output_dir(project)
    out.mkdir(parents=True, exist_ok=True)
    (out / MEDIA_INFO_FILE).write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return result


def load_media_info(project: str) -> dict | None:
    f = paths.output_dir(project) / MEDIA_INFO_FILE
    if not f.is_file():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


def clips_by_role(media: dict) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {r: [] for r in paths.ROLES}
    for clip in media.get("clips", []):
        grouped.setdefault(clip["rolle"], []).append(clip)
    for lst in grouped.values():
        lst.sort(key=lambda c: c["name"])
    return grouped
