"""Phase 3: Transkriptbasierter Schnitt (Reel-Auswahl via Claude).

Ablauf: Transkript -> Claude wählt Passagen -> Schnittpunkte werden lokal auf
Wortgrenzen gesnappt (+150 ms Padding) -> segments.json. Das Dashboard kann
Segmente ab-/anwählen und umsortieren, bevor exportiert wird.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from . import claude_client, config, ffmpeg_utils, ingest, paths, sync_audio, transcribe

SEGMENTS_FILE = "segments.json"
PADDING_SEC = 0.15


# ------------------------------------------------------------ Claude-Auswahl

def _transcript_lines(transcript: dict) -> str:
    return "\n".join(
        f"[{s['start']:.2f} – {s['end']:.2f}] {s['text']}"
        for s in transcript["segmente"]
    )


def _prompt_auto(transcript: dict, reel_laenge: float) -> str:
    return (
        "Du bist ein erfahrener Video-Editor. Wähle aus diesem Interview-"
        f"Transkript die Passagen aus, die zusammen ein schlüssiges Reel von "
        f"maximal {reel_laenge:.0f} Sekunden ergeben.\n"
        "Kriterien:\n"
        "- inhaltlicher Bogen: Hook am Anfang, Kernaussage, Abschluss\n"
        "- vollständige Sätze, keine Schnitte mitten im Wort\n"
        "- die Summe der Segmentdauern darf die Maximallänge nicht überschreiten\n\n"
        f"Transkript (Zeiten in Sekunden):\n{_transcript_lines(transcript)}\n\n"
        'Antworte NUR als JSON-Array: '
        '[{"start": <sek>, "ende": <sek>, "text": "...", "begruendung": "..."}]'
    )


def _prompt_script(transcript: dict, skript: str, reel_laenge: float) -> str:
    return (
        "Du bist ein erfahrener Video-Editor. Der Nutzer hat ein eigenes "
        "Skript bzw. Stichworte vorgegeben. Suche im Transkript die Passagen, "
        "die diesem Skript am besten entsprechen, in der Reihenfolge des "
        f"Skripts. Ziel-Gesamtlänge: maximal {reel_laenge:.0f} Sekunden.\n"
        "Vollständige Sätze, keine Schnitte mitten im Wort.\n\n"
        f"Skript/Stichworte des Nutzers:\n{skript}\n\n"
        f"Transkript (Zeiten in Sekunden):\n{_transcript_lines(transcript)}\n\n"
        'Antworte NUR als JSON-Array: '
        '[{"start": <sek>, "ende": <sek>, "text": "...", "begruendung": "..."}]'
    )


# ------------------------------------------------------------ Snapping

def snap_to_words(words: list[dict], start: float, ende: float,
                  padding: float = PADDING_SEC) -> tuple[float, float] | None:
    """Schnittpunkte auf Wortgrenzen legen und Padding anwenden.

    start -> Anfang des ersten Worts, das (teilweise) im Bereich liegt;
    ende  -> Ende des letzten Worts, das (teilweise) im Bereich liegt.
    """
    inside = [w for w in words if w["end"] > start and w["start"] < ende]
    if not inside:
        return None
    s = min(w["start"] for w in inside)
    e = max(w["end"] for w in inside)
    if e <= s:
        return None
    return max(0.0, s - padding), e + padding


def words_in_range(words: list[dict], start: float, ende: float) -> list[dict]:
    return [w for w in words if w["start"] >= start - 1e-6 and w["end"] <= ende + 1e-6]


# ------------------------------------------------------------ Auswahl-Lauf

def select_segments(project: str, modus: str = "auto", skript: str | None = None,
                    progress=None, client=None) -> dict:
    cfg = config.load_config(project)
    transcript = transcribe.load_transcript(project)
    if transcript is None:
        raise RuntimeError("Erst transkribieren (Phase 1).")
    if modus == "skript" and not (skript or "").strip():
        raise ValueError("Skript-Modus gewählt, aber kein Skript angegeben.")

    reel_laenge = float(cfg["reel_laenge_sek"])
    if client is None:
        client = claude_client.ClaudeClient(model=cfg["claude_modell"], project=project)

    if progress:
        progress(0.1, "Frage Claude nach der Segment-Auswahl …")
    prompt = (_prompt_script(transcript, skript, reel_laenge) if modus == "skript"
              else _prompt_auto(transcript, reel_laenge))
    raw = client.complete_json("reel_auswahl", prompt, max_tokens=4096)
    if not isinstance(raw, list):
        raise ValueError(f"Unerwartete Claude-Antwort (kein Array): {raw!r}")

    words = transcript["woerter"]
    segmente = []
    for item in raw:
        try:
            start = float(item["start"])
            ende = float(item["ende"])
        except (KeyError, TypeError, ValueError):
            continue
        snapped = snap_to_words(words, start, ende)
        if snapped is None:
            continue
        s, e = snapped
        seg_words = words_in_range(words, s, e)
        text = " ".join(w["word"] for w in seg_words) or str(item.get("text", ""))
        segmente.append({
            "id": uuid.uuid4().hex[:8],
            "start": round(s, 3),
            "ende": round(e, 3),
            "dauer": round(e - s, 3),
            "text": text,
            "begruendung": str(item.get("begruendung", "")),
            "aktiv": True,
        })

    if not segmente:
        raise RuntimeError("Claude hat keine verwertbaren Segmente geliefert.")

    result = {
        "projekt": project,
        "modus": modus,
        "skript": skript,
        "reel_laenge_sek": reel_laenge,
        "segmente": segmente,
    }
    save_segments(project, result)
    if progress:
        progress(1.0, f"{len(segmente)} Segmente ausgewählt "
                      f"({sum(s['dauer'] for s in segmente):.1f} s gesamt)")
    return result


# ------------------------------------------------------------ Persistenz

def save_segments(project: str, data: dict) -> None:
    f = paths.output_dir(project) / SEGMENTS_FILE
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_segments(project: str) -> dict | None:
    f = paths.output_dir(project) / SEGMENTS_FILE
    if not f.is_file():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


def update_segments(project: str, order: list[str] | None = None,
                    aktiv: dict[str, bool] | None = None) -> dict:
    """Dashboard-Änderungen übernehmen: Reihenfolge und Aktiv-Status."""
    data = load_segments(project)
    if data is None:
        raise RuntimeError("Keine Segmente vorhanden.")
    by_id = {s["id"]: s for s in data["segmente"]}
    if aktiv:
        for sid, flag in aktiv.items():
            if sid in by_id:
                by_id[sid]["aktiv"] = bool(flag)
    if order:
        missing = [sid for sid in by_id if sid not in order]
        data["segmente"] = [by_id[sid] for sid in order if sid in by_id]
        data["segmente"] += [by_id[sid] for sid in missing]
    save_segments(project, data)
    return data


def enabled_segments(project: str) -> list[dict]:
    """Aktive Segmente in Reihenfolge, mit Timeline-Startzeiten angereichert."""
    data = load_segments(project)
    if data is None:
        return []
    result = []
    t = 0.0
    for seg in data["segmente"]:
        if not seg.get("aktiv", True):
            continue
        s = dict(seg)
        s["timeline_start"] = round(t, 3)
        s["timeline_ende"] = round(t + seg["dauer"], 3)
        result.append(s)
        t += seg["dauer"]
    return result


def reel_dauer(project: str) -> float:
    segs = enabled_segments(project)
    return segs[-1]["timeline_ende"] if segs else 0.0


# ------------------------------------------------------------ Vorschau

def render_cut_preview(project: str, progress=None) -> Path:
    """Schnelle 720p-Vorschau: Kamera A + Referenz-Ton, Segmente hintereinander."""
    media = ingest.load_media_info(project)
    sync = sync_audio.load_sync(project)
    segs = enabled_segments(project)
    if media is None or sync is None:
        raise RuntimeError("Erst Ingest und Sync ausführen.")
    if not segs:
        raise RuntimeError("Keine aktiven Segmente.")

    base = paths.project_dir(project)
    ref_path = base / sync["referenz"]
    tmp = paths.output_dir(project) / "tmp" / "preview"
    tmp.mkdir(parents=True, exist_ok=True)

    parts: list[Path] = []
    for i, seg in enumerate(segs):
        if progress:
            progress(i / max(1, len(segs)), f"Rendere Segment {i + 1}/{len(segs)}")
        hit = sync_audio.clip_for_ref_time(project, media, sync, "cam_a", seg["start"])
        if hit is None:
            raise RuntimeError(
                f"Kein Kamera-A-Clip deckt Segment bei {seg['start']:.2f}s ab."
            )
        clip, src_t = hit
        dauer = min(seg["dauer"], float(clip["dauer"]) - src_t)
        part = tmp / f"part_{i:03d}.mp4"
        ffmpeg_utils.run([
            "ffmpeg", "-y", "-v", "error",
            "-ss", f"{src_t:.3f}", "-t", f"{dauer:.3f}",
            "-i", str(base / clip["relpfad"]),
            "-ss", f"{seg['start']:.3f}", "-t", f"{dauer:.3f}", "-i", str(ref_path),
            "-map", "0:v:0", "-map", "1:a:0",
            "-vf", "scale=-2:720,fps=25", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "23", "-c:a", "aac", "-ar", "48000", "-shortest", str(part),
        ])
        parts.append(part)

    concat_list = tmp / "concat.txt"
    concat_list.write_text(
        "".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8"
    )
    out = paths.output_dir(project) / "preview_reel.mp4"
    ffmpeg_utils.run([
        "ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
        "-i", str(concat_list), "-c", "copy", str(out),
    ])
    if progress:
        progress(1.0, "Vorschau fertig")
    return out
