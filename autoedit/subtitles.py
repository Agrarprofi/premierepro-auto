"""Phase 6: Untertitel als SRT (Reel-Format) + Claude-Korrekturlauf.

Regeln: max. 2 Zeilen, max. 20 Zeichen pro Zeile, Timing auf Wortgrenzen –
mit den NEUEN Timeline-Zeiten der geschnittenen Segmente.

Die SRT wird bewusst NICHT in die FCPXML eingebettet (Premiere-Captions lassen
sich per XML nicht sauber mit Stilvorlagen verbinden). Stattdessen liegt im
Output-Ordner ein README mit dem Import-Hinweis.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import claude_client, config, cutting, paths, transcribe

SRT_FILE = "reel.srt"
SRT_KORRIGIERT_FILE = "reel_korrigiert.srt"
DIFF_FILE = "untertitel_diff.json"
README_FILE = "README_UNTERTITEL.txt"

MAX_ZEILEN = 2
MAX_ZEICHEN = 20
MAX_WORT_LUECKE = 1.0   # neue Cue bei Sprechpausen > 1 s
MIN_CUE_DAUER = 0.5

README_TEXT = """Untertitel für Premiere Pro
===========================

1. In Premiere: Datei > Importieren > reel_korrigiert.srt
   (oder reel.srt, falls kein Korrekturlauf gemacht wurde)
2. Die SRT auf die Untertitel-Spur der Sequenz ziehen.
3. Gespeicherten Track Style anwenden (einmalig als Vorlage in
   Premiere unter Essential Graphics > Untertitel speichern).

Das Timing ist bereits auf die geschnittene Reel-Timeline gerechnet.
"""


# ------------------------------------------------------------ Cue-Erzeugung

def _timeline_words(project: str) -> list[dict]:
    """Wörter der aktiven Segmente mit Timeline-Zeiten (Reel-Zeitachse)."""
    transcript = transcribe.load_transcript(project)
    segs = cutting.enabled_segments(project)
    if transcript is None:
        raise RuntimeError("Kein Transkript vorhanden (Phase 1).")
    if not segs:
        raise RuntimeError("Keine aktiven Segmente (Phase 3).")
    words = []
    for seg in segs:
        for w in cutting.words_in_range(transcript["woerter"], seg["start"], seg["ende"]):
            words.append({
                "word": w["word"],
                "start": seg["timeline_start"] + (w["start"] - seg["start"]),
                "end": min(seg["timeline_start"] + (w["end"] - seg["start"]),
                           seg["timeline_ende"]),
                "segment_ende": seg["timeline_ende"],
            })
    return words


def build_cues(words: list[dict]) -> list[dict]:
    """Wörter zu Cues gruppieren: max 2 Zeilen à 20 Zeichen, Wortgrenzen-Timing."""
    cues: list[dict] = []
    lines: list[list[str]] = []
    cue_words: list[dict] = []

    def flush() -> None:
        if not cue_words:
            return
        text_lines = [" ".join(line) for line in lines if line]
        cues.append({
            "start": cue_words[0]["start"],
            "end": max(cue_words[-1]["end"], cue_words[0]["start"] + MIN_CUE_DAUER),
            "zeilen": text_lines,
        })
        lines.clear()
        cue_words.clear()

    prev: dict | None = None
    for w in words:
        token = w["word"].strip()
        if not token:
            continue
        if prev is not None and (
            w["start"] - prev["end"] > MAX_WORT_LUECKE
            or prev.get("segment_ende") != w.get("segment_ende")
        ):
            flush()
        # Wort in Zeilen einsortieren
        placed = False
        if lines and len(" ".join(lines[-1] + [token])) <= MAX_ZEICHEN:
            lines[-1].append(token)
            placed = True
        elif len(lines) < MAX_ZEILEN:
            lines.append([token])
            placed = True
        if not placed:
            flush()
            lines.append([token])
        cue_words.append(w)
        prev = w
    flush()

    # Überlappungen vermeiden (Cue endet spätestens beim nächsten Cue-Start)
    for i in range(len(cues) - 1):
        cues[i]["end"] = min(cues[i]["end"], cues[i + 1]["start"] - 0.001)
        cues[i]["end"] = max(cues[i]["end"], cues[i]["start"] + 0.2)
    return cues


def _srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, rest = divmod(ms, 3600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def cues_to_srt(cues: list[dict]) -> str:
    blocks = []
    for i, cue in enumerate(cues, start=1):
        text = "\n".join(cue["zeilen"])
        blocks.append(
            f"{i}\n{_srt_time(cue['start'])} --> {_srt_time(cue['end'])}\n{text}"
        )
    return "\n\n".join(blocks) + "\n"


_SRT_TIME_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})"
)


def parse_srt(text: str) -> list[dict]:
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [ln.rstrip("﻿").strip() for ln in block.strip().splitlines()]
        if len(lines) < 2:
            continue
        m = _SRT_TIME_RE.search(lines[1]) or _SRT_TIME_RE.search(lines[0])
        if not m:
            continue
        text_start = 2 if _SRT_TIME_RE.search(lines[1]) else 1
        cues.append({
            "timing": f"{m.group(1)} --> {m.group(2)}",
            "text": "\n".join(lines[text_start:]).strip(),
        })
    return cues


# ------------------------------------------------------------ Hauptlauf

def generate_subtitles(project: str, progress=None, client=None,
                       korrektur: bool = True) -> dict:
    cfg = config.load_config(project)
    out = paths.output_dir(project)
    if progress:
        progress(0.1, "Erzeuge SRT aus geschnittenen Segmenten")
    words = _timeline_words(project)
    if not words:
        raise RuntimeError("Keine Wörter in den aktiven Segmenten gefunden.")
    cues = build_cues(words)
    srt = cues_to_srt(cues)
    (out / SRT_FILE).write_text(srt, encoding="utf-8")
    (out / README_FILE).write_text(README_TEXT, encoding="utf-8")

    result = {"cues": len(cues), "korrigiert": False, "diff": []}
    if korrektur:
        if client is None:
            client = claude_client.ClaudeClient(model=cfg["claude_modell"],
                                                project=project)
        if progress:
            progress(0.5, "Korrekturlauf mit Claude …")
        prompt = (
            "Korrigiere in dieser SRT-Datei Rechtschreibung, Groß-/Klein-"
            "schreibung und Fachbegriffe (Landwirtschaft, österreichisches "
            "Deutsch: Kartoffel- und Agrar-Fachvokabular).\n"
            "Ändere NICHTS am Timing, NICHTS an der Cue-Anzahl und nichts am "
            "Sinn. Behalte die Zeilenaufteilung bei (max. 20 Zeichen pro "
            "Zeile). Gib NUR die korrigierte SRT zurück, ohne Erklärungen.\n\n"
            + srt
        )
        corrected_text = client.complete_text("untertitel_korrektur", prompt,
                                              max_tokens=8192)
        corrected_text = corrected_text.strip()
        if corrected_text.startswith("```"):
            corrected_text = re.sub(r"^```[a-z]*\n?|```$", "",
                                    corrected_text).strip()
        orig = parse_srt(srt)
        corr = parse_srt(corrected_text)
        if len(corr) == len(orig) and all(
            a["timing"] == b["timing"] for a, b in zip(orig, corr)
        ):
            (out / SRT_KORRIGIERT_FILE).write_text(corrected_text + "\n",
                                                   encoding="utf-8")
            diff = [
                {"nr": i + 1, "timing": a["timing"], "vorher": a["text"],
                 "nachher": b["text"], "geaendert": a["text"] != b["text"]}
                for i, (a, b) in enumerate(zip(orig, corr))
            ]
            result.update(korrigiert=True, diff=diff)
        else:
            result["fehler"] = (
                "Korrektur verworfen: Claude hat Timing oder Cue-Anzahl "
                f"verändert ({len(orig)} -> {len(corr)} Cues)."
            )
    (out / DIFF_FILE).write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if progress:
        progress(1.0, f"{len(cues)} Untertitel-Cues geschrieben")
    return result


def load_subtitles(project: str) -> dict:
    out = paths.output_dir(project)
    data: dict = {"srt": None, "korrigiert": None, "diff": None}
    if (out / SRT_FILE).is_file():
        data["srt"] = (out / SRT_FILE).read_text(encoding="utf-8")
    if (out / SRT_KORRIGIERT_FILE).is_file():
        data["korrigiert"] = (out / SRT_KORRIGIERT_FILE).read_text(encoding="utf-8")
    if (out / DIFF_FILE).is_file():
        data["diff"] = json.loads((out / DIFF_FILE).read_text(encoding="utf-8"))
    return data
