"""Phase 7: Musik-Auswahl aus der lokalen music_library.

Tags kommen aus music_library/tags.yaml (optional) oder aus dem Dateinamen.
Optional analysiert librosa Tempo/Energie (nur wenn installiert).
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from . import claude_client, config, cutting, ffmpeg_utils, paths

MUSIC_FILE = "music.json"
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aif", ".aiff", ".flac", ".ogg"}
DEFAULT_GAIN_DB = -18.0


def scan_library() -> list[dict]:
    """Alle Tracks der Library mit Tags und Dauer."""
    lib = paths.music_dir()
    if not lib.is_dir():
        return []
    tags_file = lib / "tags.yaml"
    tags: dict = {}
    if tags_file.is_file():
        tags = yaml.safe_load(tags_file.read_text(encoding="utf-8")) or {}

    tracks = []
    for f in sorted(lib.iterdir()):
        if not f.is_file() or f.suffix.lower() not in AUDIO_EXTS:
            continue
        try:
            dauer = ffmpeg_utils.media_info(f)["dauer"]
        except ffmpeg_utils.FfmpegError:
            continue
        entry_tags = tags.get(f.name) or tags.get(f.stem) or {}
        if not entry_tags:
            # Dateinamen-Tokens als Behelfs-Tags ("ruhig_akustik_feld.mp3")
            tokens = [t for t in f.stem.replace("-", "_").split("_") if t]
            entry_tags = {"stichworte": tokens}
        track = {
            "datei": str(f),
            "name": f.name,
            "dauer": round(dauer, 2),
            "tags": entry_tags,
        }
        track.update(_analyze_optional(f))
        tracks.append(track)
    return tracks


def _analyze_optional(f: Path) -> dict:
    """Tempo/Energie mit librosa, falls installiert – sonst leer."""
    try:
        import librosa  # optional
    except ImportError:
        return {}
    try:
        y, sr = librosa.load(str(f), duration=60, mono=True)
        tempo = librosa.beat.tempo(y=y, sr=sr)
        rms = float(librosa.feature.rms(y=y).mean())
        return {"tempo_bpm": round(float(tempo[0]), 1), "energie": round(rms, 4)}
    except Exception:  # noqa: BLE001 - Analyse ist optional
        return {}


def select_track(project: str, progress=None, client=None) -> dict:
    """Claude wählt den zum Reel-Inhalt passendsten Track."""
    cfg = config.load_config(project)
    tracks = scan_library()
    if not tracks:
        raise RuntimeError(
            f"Keine Tracks in {paths.music_dir()} gefunden "
            "(Ordner music_library/ anlegen und Tracks hineinlegen)."
        )
    segs = cutting.enabled_segments(project)
    if not segs:
        raise RuntimeError("Keine aktiven Segmente (Phase 3).")
    if client is None:
        client = claude_client.ClaudeClient(model=cfg["claude_modell"], project=project)

    reel_text = " ".join(s["text"] for s in segs)
    liste = "\n".join(
        f"- {t['name']}: Tags {json.dumps(t['tags'], ensure_ascii=False)}"
        + (f", Tempo {t['tempo_bpm']} BPM" if t.get("tempo_bpm") else "")
        + f", Dauer {t['dauer']:.0f} s"
        for t in tracks
    )
    prompt = (
        "Wähle aus dieser Musikliste den Track, dessen Stimmung am besten zum "
        "Inhalt des folgenden Reels passt (Hintergrundmusik unter einem "
        "Interview).\n\n"
        f"Reel-Inhalt:\n{reel_text}\n\n"
        f"Verfügbare Tracks:\n{liste}\n\n"
        'Antworte NUR als JSON: {"datei": "<name aus der Liste>", '
        '"begruendung": "..."}'
    )
    if progress:
        progress(0.3, "Frage Claude nach der Musikauswahl …")
    data = client.complete_json("musik_auswahl", prompt, max_tokens=512)
    name = str(data.get("datei", ""))
    chosen = next((t for t in tracks if t["name"] == name or t["datei"] == name), None)
    if chosen is None:
        chosen = tracks[0]
    result = {
        "datei": chosen["datei"],
        "name": chosen["name"],
        "begruendung": str(data.get("begruendung", "")),
        "gain_db": DEFAULT_GAIN_DB,
        "quelle": "claude",
    }
    save_selection(project, result)
    if progress:
        progress(1.0, f"Track gewählt: {chosen['name']}")
    return result


def set_track(project: str, name: str | None) -> dict | None:
    """Manueller Override aus dem Dashboard (None = Auswahl löschen)."""
    f = paths.output_dir(project) / MUSIC_FILE
    if name is None:
        if f.is_file():
            f.unlink()
        return None
    tracks = scan_library()
    chosen = next((t for t in tracks if t["name"] == name), None)
    if chosen is None:
        raise KeyError(f"Track nicht in der Library: {name!r}")
    result = {
        "datei": chosen["datei"],
        "name": chosen["name"],
        "begruendung": "manuell gewählt",
        "gain_db": DEFAULT_GAIN_DB,
        "quelle": "manuell",
    }
    save_selection(project, result)
    return result


def save_selection(project: str, data: dict) -> None:
    (paths.output_dir(project) / MUSIC_FILE).write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def load_selection(project: str) -> dict | None:
    f = paths.output_dir(project) / MUSIC_FILE
    if not f.is_file():
        return None
    return json.loads(f.read_text(encoding="utf-8"))
