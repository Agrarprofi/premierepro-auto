"""Phase 3: Transkriptbasierter Schnitt (Reel-Auswahl via Claude).

Ablauf: Transkript -> Claude wählt Passagen -> Schnittpunkte werden lokal auf
Wortgrenzen gesnappt (+150 ms Padding) -> segments.json. Das Dashboard kann
Segmente ab-/anwählen und umsortieren, bevor exportiert wird.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from pathlib import Path

from . import claude_client, config, ffmpeg_utils, ingest, paths, sync_audio, transcribe

SEGMENTS_FILE = "segments.json"
STATEMENTS_FILE = "statements.json"
PADDING_SEC = 0.15
PAUSE_MARKER_SEC = 1.5   # ab dieser Sprechpause wird sie im Prompt markiert
TAKE_FENSTER = 4         # wie viele Folgepassagen auf Wiederholung geprüft werden
TAKE_AEHNLICHKEIT = 0.7  # Token-Überlappung, ab der zwei Passagen als Takes gelten


# --------------------------------------------------- Take-/Pausen-Annotation

def _norm_tokens(text: str) -> list[str]:
    return re.findall(r"[a-zäöüß0-9]+", text.lower())


def _same_statement(a: str, b: str) -> bool:
    """Erkennt Wiederholungen inkl. abgebrochener erster Anläufe."""
    ta, tb = _norm_tokens(a), _norm_tokens(b)
    if len(ta) < 3 or len(tb) < 3:
        return False
    common = sum((Counter(ta) & Counter(tb)).values())
    return common / min(len(ta), len(tb)) >= TAKE_AEHNLICHKEIT


def find_take_groups(segmente: list[dict]) -> dict[int, dict]:
    """Wiederholte Aussagen (mehrere Anläufe) im Transkript finden.

    Interviews enthalten typischerweise Falschstarts: Sprechpause, dann sagt
    die Person den Satz noch einmal – und der letzte Take ist der beste.
    Rückgabe: {segment_index: {"gruppe": id, "final": bool}}.
    """
    gruppe_von: dict[int, int] = {}
    next_gruppe = 0
    for i in range(len(segmente)):
        for j in range(i + 1, min(i + 1 + TAKE_FENSTER, len(segmente))):
            if _same_statement(segmente[i]["text"], segmente[j]["text"]):
                g = gruppe_von.get(i)
                if g is None:
                    g = next_gruppe
                    next_gruppe += 1
                    gruppe_von[i] = g
                gruppe_von[j] = g
    result: dict[int, dict] = {}
    for g in set(gruppe_von.values()):
        mitglieder = sorted(i for i, gi in gruppe_von.items() if gi == g)
        for i in mitglieder:
            result[i] = {"gruppe": g, "final": i == mitglieder[-1]}
    return result


def annotate_transcript(transcript: dict) -> str:
    """Transkript-Zeilen mit Sprechpausen- und Take-Markern für den Prompt."""
    segmente = transcript["segmente"]
    takes = find_take_groups(segmente)
    lines: list[str] = []
    for i, s in enumerate(segmente):
        if i > 0:
            pause = s["start"] - segmente[i - 1]["end"]
            if pause >= PAUSE_MARKER_SEC:
                lines.append(f"⏸ Sprechpause {pause:.1f} s")
        marker = ""
        info = takes.get(i)
        if info is not None:
            if info["final"]:
                marker = "  ⟳ letzter Take dieser Aussage"
            else:
                letzte = max(j for j, t in takes.items()
                             if t["gruppe"] == info["gruppe"])
                marker = (f"  ⟳ weiterer Anlauf derselben Aussage folgt bei "
                          f"[{segmente[letzte]['start']:.2f}]")
        lines.append(f"[{s['start']:.2f} – {s['end']:.2f}] {s['text']}{marker}")
    return "\n".join(lines)


_ROHMATERIAL_REGELN = (
    "Das Transkript ist ROHMATERIAL: Es enthält Sprechpausen (⏸), "
    "Versprecher und wiederholte Anläufe/Takes (⟳). Regeln dafür:\n"
    "- Wenn eine Aussage mehrfach vorkommt (die Person setzt nach einer "
    "Pause neu an), wähle den SAUBERSTEN Take – meistens ist das der "
    "letzte, aber entscheide nach Qualität und Inhalt. Ein und derselbe "
    "Inhalt darf nie doppelt im Reel landen.\n"
    "- Takes dürfen KOMBINIERT werden: z.B. erste Satzhälfte aus Take 1, "
    "zweite aus Take 2, wenn der Übergang an einer Wortgrenze liegt und "
    "zusammen ein flüssiger, grammatikalisch sauberer Satz entsteht. Gib "
    "die Teile dann als aufeinanderfolgende Segmente aus und setze beim "
    'zweiten Teil "fortsetzung": true.\n'
    "- Meide Passagen mit Versprechern, Satzabbrüchen oder Füllwort-Ketten.\n"
)


# ------------------------------------------------------------ Aussagen-Analyse

def analyze_statements(project: str, progress=None, client=None) -> dict:
    """Analyse-Lauf: ALLE brauchbaren Aussagen bewerten (Punkte, Kategorie,
    Qualität). Ergebnis wird gespeichert und fließt in die Auswahl ein."""
    cfg = config.load_config(project)
    transcript = transcribe.load_transcript(project)
    if transcript is None:
        raise RuntimeError("Erst transkribieren (Phase 1).")
    if client is None:
        client = claude_client.ClaudeClient(model=cfg["claude_modell"], project=project)

    if progress:
        progress(0.1, "Analysiere Aussagen …")
    prompt = (
        "Du bist ein erfahrener Interview-Editor. Analysiere dieses "
        "Interview-Transkript (Rohmaterial) und bewerte JEDE eigenständige, "
        "brauchbare Aussage – nicht nur die besten.\n"
        + _ROHMATERIAL_REGELN +
        "\nBewerte pro Aussage:\n"
        "- punkte: 0-10 (Prägnanz, Aussagekraft, Emotion, Zitierfähigkeit)\n"
        '- kategorie: "hook" (starker Einstieg), "kern" (Kernaussage), '
        '"abschluss" (rundes Ende) oder "detail"\n'
        '- qualitaet: "sauber", "versprecher" oder "abgebrochen"\n'
        "- kommentar: 1 kurzer Satz, warum (nicht) stark\n\n"
        f"Transkript (Zeiten in Sekunden):\n{annotate_transcript(transcript)}\n\n"
        'Antworte NUR als JSON-Array: [{"start": <sek>, "ende": <sek>, '
        '"text": "...", "punkte": <0-10>, "kategorie": "...", '
        '"qualitaet": "...", "kommentar": "..."}]'
    )
    raw = client.complete_json("aussagen_analyse", prompt, max_tokens=8192)
    if not isinstance(raw, list):
        raise ValueError(f"Unerwartete Claude-Antwort (kein Array): {raw!r}")

    aussagen = []
    for item in raw:
        try:
            aussagen.append({
                "start": float(item["start"]),
                "ende": float(item["ende"]),
                "text": str(item.get("text", "")),
                "punkte": max(0, min(10, int(item.get("punkte", 0)))),
                "kategorie": str(item.get("kategorie", "detail")),
                "qualitaet": str(item.get("qualitaet", "sauber")),
                "kommentar": str(item.get("kommentar", "")),
            })
        except (KeyError, TypeError, ValueError):
            continue
    aussagen.sort(key=lambda a: a["punkte"], reverse=True)
    result = {"projekt": project, "aussagen": aussagen}
    (paths.output_dir(project) / STATEMENTS_FILE).write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if progress:
        progress(0.4, f"{len(aussagen)} Aussagen bewertet")
    return result


def load_statements(project: str) -> dict | None:
    f = paths.output_dir(project) / STATEMENTS_FILE
    if not f.is_file():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


def _statements_block(statements: dict | None, limit: int = 15) -> str:
    if not statements or not statements.get("aussagen"):
        return ""
    zeilen = "\n".join(
        f"- [{a['start']:.2f} – {a['ende']:.2f}] ({a['punkte']} P., "
        f"{a['kategorie']}, {a['qualitaet']}) \"{a['text']}\""
        + (f" – {a['kommentar']}" if a["kommentar"] else "")
        for a in statements["aussagen"][:limit]
    )
    return (
        "\nVorab-Analyse der stärksten Aussagen (nutze sie als Grundlage; "
        "bevorzuge hohe Punkte und qualitaet 'sauber'):\n" + zeilen + "\n"
    )


# ------------------------------------------------------------ Claude-Auswahl

def _hinweise_block(cfg: dict) -> str:
    hinweise = str(cfg.get("schnitt_hinweise", "")).strip()
    if not hinweise:
        return ""
    return f"\nZusätzliche Vorgaben des Nutzers:\n{hinweise}\n"


def _prompt_auto(transcript: dict, reel_laenge: float, cfg: dict,
                 statements: dict | None) -> str:
    return (
        "Du bist ein erfahrener Video-Editor. Wähle aus diesem Interview-"
        f"Transkript die Passagen aus, die zusammen ein schlüssiges Reel von "
        f"maximal {reel_laenge:.0f} Sekunden ergeben.\n"
        + _ROHMATERIAL_REGELN +
        "Kriterien:\n"
        "- inhaltlicher Bogen: Hook am Anfang, Kernaussage, Abschluss\n"
        "- vollständige Sätze, keine Schnitte mitten im Wort\n"
        "- die Summe der Segmentdauern darf die Maximallänge nicht überschreiten\n"
        + _statements_block(statements)
        + _hinweise_block(cfg) +
        f"\nTranskript (Zeiten in Sekunden):\n{annotate_transcript(transcript)}\n\n"
        + _AUSGABE_FORMAT
    )


_AUSGABE_FORMAT = (
    'Antworte NUR als JSON-Array: [{"start": <sek>, "ende": <sek>, '
    '"text": "...", "begruendung": "...", "fortsetzung": <true, wenn dieses '
    "Segment den Satz des vorherigen Segments direkt fortsetzt "
    "(Take-Kombination), sonst false>}]"
)


def _prompt_script(transcript: dict, skript: str, reel_laenge: float,
                   cfg: dict, statements: dict | None) -> str:
    return (
        "Du bist ein erfahrener Video-Editor. Der Nutzer hat ein eigenes "
        "Skript bzw. Stichworte vorgegeben. Suche im Transkript die Passagen, "
        "die diesem Skript am besten entsprechen, in der Reihenfolge des "
        f"Skripts. Ziel-Gesamtlänge: maximal {reel_laenge:.0f} Sekunden.\n"
        + _ROHMATERIAL_REGELN +
        "Vollständige Sätze, keine Schnitte mitten im Wort.\n\n"
        f"Skript/Stichworte des Nutzers:\n{skript}\n"
        + _statements_block(statements)
        + _hinweise_block(cfg) +
        f"\nTranskript (Zeiten in Sekunden):\n{annotate_transcript(transcript)}\n\n"
        + _AUSGABE_FORMAT
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
                    progress=None, client=None, analyse: bool = True) -> dict:
    cfg = config.load_config(project)
    transcript = transcribe.load_transcript(project)
    if transcript is None:
        raise RuntimeError("Erst transkribieren (Phase 1).")
    if modus == "skript" and not (skript or "").strip():
        raise ValueError("Skript-Modus gewählt, aber kein Skript angegeben.")

    reel_laenge = float(cfg["reel_laenge_sek"])
    if client is None:
        client = claude_client.ClaudeClient(model=cfg["claude_modell"], project=project)

    # Stufe 1: Aussagen bewerten (getrennt von der Auswahl -> bessere Treffer)
    statements = None
    if analyse:
        statements = analyze_statements(project, progress=progress, client=client)

    if progress:
        progress(0.5, "Frage Claude nach der Segment-Auswahl …")
    prompt = (_prompt_script(transcript, skript, reel_laenge, cfg, statements)
              if modus == "skript"
              else _prompt_auto(transcript, reel_laenge, cfg, statements))
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
            "fortsetzung": bool(item.get("fortsetzung", False)),
            "aktiv": True,
        })

    if not segmente:
        raise RuntimeError("Claude hat keine verwertbaren Segmente geliefert.")

    # Take-Kombination: an der Nahtstelle das Padding entfernen, damit der
    # zusammengesetzte Satz ohne doppelten Atmer/Pause fließt.
    for i, seg in enumerate(segmente):
        if i == 0 or not seg["fortsetzung"]:
            continue
        prev = segmente[i - 1]
        if prev["ende"] - prev["start"] > 2 * PADDING_SEC:
            prev["ende"] = round(prev["ende"] - PADDING_SEC, 3)
            prev["dauer"] = round(prev["ende"] - prev["start"], 3)
        if seg["ende"] - seg["start"] > 2 * PADDING_SEC:
            seg["start"] = round(seg["start"] + PADDING_SEC, 3)
            seg["dauer"] = round(seg["ende"] - seg["start"], 3)

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


def take_joins(project: str) -> list[float]:
    """Reel-Zeitpunkte, an denen zwei Takes zusammengeschnitten sind
    (Jump-Cut auf derselben Kamera) – Kandidaten für B-Roll-Abdeckung."""
    segs = enabled_segments(project)
    return [seg["timeline_start"] for i, seg in enumerate(segs)
            if i > 0 and seg.get("fortsetzung")]


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
        pieces, _ = sync_audio.cover_range(media, sync, "cam_a",
                                           seg["start"], seg["ende"])
        if not pieces:
            raise RuntimeError(
                f"Kein Kamera-A-Clip deckt Segment bei {seg['start']:.2f}s ab."
            )
        for j, p in enumerate(pieces):
            ref_start = seg["start"] + p["rel_start"]
            part = tmp / f"part_{i:03d}_{j:02d}.mp4"
            ffmpeg_utils.run([
                "ffmpeg", "-y", "-v", "error",
                "-ss", f"{p['src_in']:.3f}", "-t", f"{p['dauer']:.3f}",
                "-i", str(base / p["clip"]["relpfad"]),
                "-ss", f"{ref_start:.3f}", "-t", f"{p['dauer']:.3f}",
                "-i", str(ref_path),
                "-map", "0:v:0", "-map", "1:a:0",
                "-vf", "scale=-2:720,fps=25", "-c:v", "libx264",
                "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-ar", "48000", "-shortest", str(part),
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
