"""Phase 1a: Input-Ordner scannen und Medieninfos erfassen."""

from __future__ import annotations

import json
from pathlib import Path

from . import ffmpeg_utils, paths

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mxf"}
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aif", ".aiff", ".flac"}

MEDIA_INFO_FILE = "media_info.json"
CFR_DIR = "cfr"
# Version der Encoder-Einstellungen: Erhöhung erzwingt eine einmalige
# Neu-Wandlung vorhandener Kopien (v2 = keine B-Frames, kurze GOPs)
CFR_VERSION = 2
# Automatisch gewandelt werden nur die Interview-Kameras: dort zerstört
# VFR-Drift den Lippensync über die lange Laufzeit. B-Roll (kurze
# Ausschnitte, kein Sync-Bezug) wäre verschwendete Rechenzeit - bei
# Bedarf gibt es den manuellen "-> CFR"-Knopf.
CFR_ROLLEN = {"cam_a", "cam_b"}
VIDEO_ROLLEN = {"cam_a", "cam_b", "broll"}


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
    seq_fps = _sequenz_fps(project)

    clips: list[dict] = []
    zu_wandeln: list[int] = []
    for i, (role, f) in enumerate(all_files):
        if progress:
            progress(0.4 * i / max(1, len(all_files)), f"Analysiere {f.name}")
        info = ffmpeg_utils.media_info(f)
        info["rolle"] = role
        info["name"] = f.name
        info["relpfad"] = str(f.relative_to(paths.project_dir(project)))
        clips.append(info)
        # Kameras werden gewandelt bei VFR-Verdacht ODER wenn ihre Rate
        # nicht zur Sequenz-Framerate passt: alle Timeline-Clips laufen
        # dann in EINER Rate - Premieres Misch-Raten-Import entfällt.
        rate_passt = (seq_fps is None or not info.get("fps")
                      or abs(info["fps"] - seq_fps) < 0.01)
        if role in VIDEO_ROLLEN and (
                info["relpfad"] in erzwungen
                or (role in CFR_ROLLEN
                    and (info.get("vfr_verdacht") or not rate_passt))):
            zu_wandeln.append(i)

    if zu_wandeln:
        _convert_all_cfr(project, clips, zu_wandeln, erzwungen, progress)

    result = {"projekt": project, "clips": clips}
    out = paths.output_dir(project)
    out.mkdir(parents=True, exist_ok=True)
    (out / MEDIA_INFO_FILE).write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return result


def _convert_all_cfr(project: str, clips: list[dict], indizes: list[int],
                     erzwungen: set[str], progress=None) -> None:
    """Alle nötigen CFR-Wandlungen PARALLEL ausführen (mehrere ffmpeg-
    Prozesse; auf dem Mac teilen sich die Hardware-Encoder-Engines die
    Arbeit). Gesamtfortschritt gewichtet nach Dateidauer."""
    import os
    import threading
    from concurrent.futures import ThreadPoolExecutor

    workers = min(3, max(1, (os.cpu_count() or 4) // 4), len(indizes))
    gesamt = sum(float(clips[i]["dauer"] or 0.0) for i in indizes) or 1.0
    stand = {i: 0.0 for i in indizes}
    fertig = [0]
    lock = threading.Lock()

    def melde(i: int, frac: float, text: str) -> None:
        if not progress:
            return
        with lock:
            stand[i] = frac * float(clips[i]["dauer"] or 0.0)
            done = sum(stand.values())
            n_fertig = fertig[0]
        progress(0.4 + 0.6 * done / gesamt,
                 f"CFR-Wandlung ({n_fertig}/{len(indizes)} fertig): {text}")

    def wandle(i: int) -> None:
        info = clips[i]
        src = paths.project_dir(project) / info["relpfad"]

        def sub(frac: float, meldung: str = "", _i=i) -> None:
            melde(_i, frac, meldung)

        neu = _ensure_cfr(project, src, info, progress=sub)
        if info["relpfad"] in erzwungen:
            neu["cfr_erzwungen"] = True
        with lock:
            fertig[0] += 1
        clips[i] = neu

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(wandle, i) for i in indizes]
        for fu in futures:
            fu.result()  # Fehler/Abbruch weiterreichen


def _sequenz_fps(project: str) -> float | None:
    """Ziel-Framerate der Premiere-Sequenz (None = wie Kamera A)."""
    from . import config

    fps = float(config.load_config(project).get("sequenz_fps") or 0)
    return fps if fps > 0 else None


def _ensure_cfr(project: str, src: Path, info: dict, progress=None,
                frac0: float = 0.0, frac1: float = 1.0) -> dict:
    """Datei einmalig nach CFR wandeln und die Medieninfo der Kopie
    übernehmen. Alle weiteren Schritte (Vorschau, Export) nutzen dann die
    CFR-Kopie - Bild und Ton bleiben in Premiere fest verbunden.

    Kameras (cam_a/cam_b) werden auf die SEQUENZ-Framerate normalisiert,
    B-Roll auf die nächstliegende Standardrate der Quelle. Eine
    vorhandene Kopie mit falscher Rate wird neu gewandelt.

    Schlägt die Wandlung fehl, bleibt das Original mit `cfr_fehler`
    markiert; der Export warnt dann. Der Kodier-Fortschritt wird in den
    Bereich [frac0, frac1] des Gesamtfortschritts gemappt.
    """
    seq_fps = _sequenz_fps(project) if info["rolle"] in CFR_ROLLEN else None
    ziel = seq_fps if seq_fps else (info["fps"] or 25.0)
    fps_f, fps_bruch = ffmpeg_utils.nearest_standard_fps(ziel)
    dst = paths.output_dir(project) / CFR_DIR / info["rolle"] / src.name
    meta_datei = dst.with_name(dst.name + ".meta.json")

    aktuell = None
    if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
        try:
            meta = json.loads(meta_datei.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        if meta.get("version") == CFR_VERSION:
            aktuell = ffmpeg_utils.media_info(dst)
            if not aktuell.get("fps") or abs(aktuell["fps"] - fps_f) >= 0.01:
                aktuell = None  # Kopie hat die falsche Rate -> neu wandeln

    if aktuell is None:
        if progress:
            progress(frac0, f"{src.name}: wandle nach {fps_f:g} fps (CFR)")

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
        meta_datei.write_text(
            json.dumps({"version": CFR_VERSION, "fps": fps_f}),
            encoding="utf-8")
        aktuell = ffmpeg_utils.media_info(dst)
    neu = aktuell
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
        if clip["rolle"] not in VIDEO_ROLLEN:
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
