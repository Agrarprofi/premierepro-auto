"""Phase 1a: Input-Ordner scannen und Medieninfos erfassen."""

from __future__ import annotations

import json
from pathlib import Path

from . import ffmpeg_utils, paths

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mxf"}
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aif", ".aiff", ".flac"}

MEDIA_INFO_FILE = "media_info.json"
CFR_DIR = "cfr"
# Rollen, deren Bild in Premiere landet - nur dort ist VFR ein Problem
CFR_ROLLEN = {"cam_a", "cam_b", "broll"}


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

    # Manuell erzwungene CFR-Wandlungen aus dem letzten Scan übernehmen
    vorher = load_media_info(project) or {}
    erzwungen = {c["relpfad"] for c in vorher.get("clips", [])
                 if c.get("cfr_erzwungen")}

    clips = []
    for i, (role, f) in enumerate(all_files):
        if progress:
            progress(i / max(1, len(all_files)), f"Analysiere {f.name}")
        info = ffmpeg_utils.media_info(f)
        info["rolle"] = role
        info["name"] = f.name
        info["relpfad"] = str(f.relative_to(paths.project_dir(project)))
        if role in CFR_ROLLEN and (info.get("vfr_verdacht")
                                   or info["relpfad"] in erzwungen):
            n = max(1, len(all_files))
            info = _ensure_cfr(project, f, info, progress=progress,
                               frac0=i / n, frac1=(i + 1) / n)
            if info["relpfad"] in erzwungen:
                info["cfr_erzwungen"] = True
        clips.append(info)

    result = {"projekt": project, "clips": clips}
    out = paths.output_dir(project)
    out.mkdir(parents=True, exist_ok=True)
    (out / MEDIA_INFO_FILE).write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return result


def _ensure_cfr(project: str, src: Path, info: dict, progress=None,
                frac0: float = 0.0, frac1: float = 1.0) -> dict:
    """VFR-Datei einmalig nach CFR wandeln und die Medieninfo der Kopie
    übernehmen. Alle weiteren Schritte (Vorschau, Export) nutzen dann die
    CFR-Kopie - Bild und Ton bleiben in Premiere fest verbunden.

    Schlägt die Wandlung fehl, bleibt das Original mit `cfr_fehler`
    markiert; der Export warnt dann. Der Kodier-Fortschritt wird in den
    Bereich [frac0, frac1] des Gesamtfortschritts gemappt.
    """
    fps_f, fps_bruch = ffmpeg_utils.nearest_standard_fps(info["fps"] or 25.0)
    dst = paths.output_dir(project) / CFR_DIR / info["rolle"] / src.name
    if not dst.is_file() or dst.stat().st_mtime < src.stat().st_mtime:
        if progress:
            progress(frac0, f"{src.name}: variable Framerate erkannt – "
                            f"wandle nach {fps_f:g} fps")

        def _cb(f: float) -> None:
            if progress:
                progress(frac0 + f * (frac1 - frac0),
                         f"{src.name}: wandle nach {fps_f:g} fps (CFR) – "
                         f"{f * 100:.0f} %")

        try:
            ffmpeg_utils.convert_to_cfr(src, dst, fps_bruch,
                                        dauer=info["dauer"], progress=_cb,
                                        breite=info.get("breite"),
                                        hoehe=info.get("hoehe"),
                                        fps=info.get("fps"))
        except ffmpeg_utils.FfmpegError as exc:
            info["cfr_fehler"] = str(exc)[-300:]
            return info
    neu = ffmpeg_utils.media_info(dst)
    for key in ("rolle", "name", "relpfad"):
        neu[key] = info[key]
    neu["vfr_original"] = True
    neu["cfr_pfad"] = str(dst.relative_to(paths.project_dir(project)))
    return neu


def force_cfr(project: str, relpfad: str, progress=None) -> dict:
    """Eine Datei auf Nutzerwunsch nach CFR wandeln (Dashboard-Knopf
    "→ CFR"), auch wenn die automatische Erkennung nicht angeschlagen
    hat. Die Entscheidung übersteht erneute Ingest-Läufe."""
    media = load_media_info(project)
    if media is None:
        raise RuntimeError("Erst Ingest ausführen.")
    src = paths.project_dir(project) / relpfad
    if not src.is_file():
        raise KeyError(f"Datei nicht gefunden: {relpfad}")
    for i, clip in enumerate(media["clips"]):
        if clip["relpfad"] != relpfad:
            continue
        if clip["rolle"] not in CFR_ROLLEN:
            raise RuntimeError("CFR-Wandlung gibt es nur für Videodateien.")
        neu = _ensure_cfr(project, src, dict(clip), progress=progress)
        if "cfr_pfad" not in neu:
            raise RuntimeError(
                f"Wandlung fehlgeschlagen: {neu.get('cfr_fehler', '?')}")
        neu["cfr_erzwungen"] = True
        media["clips"][i] = neu
        (paths.output_dir(project) / MEDIA_INFO_FILE).write_text(
            json.dumps(media, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return neu
    raise KeyError(f"Unbekannte Datei: {relpfad}")


def clip_datei(project: str, clip: dict) -> Path:
    """Absoluter Pfad der tatsächlich zu verwendenden Datei
    (CFR-Kopie, falls das Original variable Framerate hatte)."""
    return paths.project_dir(project) / (clip.get("cfr_pfad") or clip["relpfad"])


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
