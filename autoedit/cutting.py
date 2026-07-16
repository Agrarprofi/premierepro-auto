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
# Stille-Trimmer: Segmente dürfen nicht mit Stille beginnen/enden, auch wenn
# Transkript-Timestamps daneben liegen (WhisperX dehnt Wörter über Pausen).
STILLE_TOLERANZ = 0.35   # ab so viel führender/folgender Stille wird gestutzt
STILLE_MAX_TRIM = 10.0   # maximal so viel wegschneiden
STILLE_NACHLAUF = 0.25   # Luft nach dem letzten gesprochenen Laut
PAUSE_MIN_TEIL_SEC = 0.8  # Mindestlänge der Teilstücke beim Pausen-Schnitt
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
    raw = claude_client.ensure_list(
        client.complete_json("aussagen_analyse", prompt, max_tokens=8192))

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
        "Du bist ein erfahrener Video-Editor. Der Nutzer hat ein Skript bzw. "
        "Stichworte als INHALTLICHEN LEITFADEN vorgegeben. Baue daraus ein "
        f"schlüssiges Reel von maximal {reel_laenge:.0f} Sekunden.\n\n"
        "So gehst du vor:\n"
        "- Gleiche das Transkript mit dem Leitfaden ab: welche Aussagen im "
        "Material transportieren die gewünschten Botschaften am besten?\n"
        "- Nimm dabei IMMER die stärkste, sauberste und schönste Formulierung "
        "aus dem Material (siehe Vorab-Analyse) – NICHT die wörtlichste "
        "Entsprechung zum Skript. Der Leitfaden sagt WAS, das Material "
        "entscheidet WIE.\n"
        "- Dramaturgie geht vor Skript-Reihenfolge: packender Hook am Anfang, "
        "dann Kernaussagen, sauberer Abschluss. Weiche von der Reihenfolge "
        "des Leitfadens ab, wenn das Reel dadurch besser wird.\n"
        "- Gibt das Material zu einem Punkt des Leitfadens nichts Gutes her, "
        "lass ihn weg. Umgekehrt darfst du eine herausragende Aussage "
        "aufnehmen, die nicht im Leitfaden steht, wenn sie zur Botschaft "
        "passt.\n"
        + _ROHMATERIAL_REGELN +
        "Vollständige Sätze, keine Schnitte mitten im Wort; die Summe der "
        "Segmentdauern darf die Maximallänge nicht überschreiten.\n\n"
        f"Leitfaden des Nutzers:\n{skript}\n"
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


# ------------------------------------------------------------ Stille-Trimmer

def speech_bounds(wav, sr: int, von: float, bis: float) -> tuple[float, float] | None:
    """Erster und letzter Sprach-Zeitpunkt im Fenster [von, bis] (Sekunden).

    Energie-basiert (20-ms-Frames, Schwelle relativ zum Fenster-Peak);
    None, wenn im Fenster keine Sprache gefunden wird.
    """
    import numpy as np

    a, b = max(0, int(von * sr)), min(len(wav), int(bis * sr))
    seg = wav[a:b]
    frame = int(0.02 * sr)
    n = len(seg) // frame
    if n < 3:
        return None
    env = np.abs(seg[: n * frame]).reshape(n, frame).mean(axis=1)
    peak = float(np.percentile(env, 95))
    if peak <= 1e-5:
        return None
    above = env > 0.12 * peak
    # zwei aufeinanderfolgende aktive Frames = Sprache (gegen Klicks robust)
    aktiv = above[:-1] & above[1:]
    idx = np.flatnonzero(aktiv)
    if len(idx) == 0:
        return None
    erste = von + idx[0] * frame / sr
    letzte = von + (idx[-1] + 2) * frame / sr
    return float(erste), float(letzte)


def _trim_silence(project: str, segmente: list[dict]) -> None:
    """Segmentgrenzen am echten Audio nachmessen und Stille wegstutzen.

    Schützt Take-Nahtstellen (fortsetzung): dort wird nicht getrimmt.
    """
    try:
        ref_file, _ = sync_audio.reference_source(project)
        wav = sync_audio._load_wav_mono(sync_audio._tmp_wav(project, ref_file))
    except Exception:  # noqa: BLE001 - Trimmen ist Verbesserung, kein Muss
        return
    sr = sync_audio.SYNC_SR
    for i, seg in enumerate(segmente):
        naht_davor = bool(seg.get("fortsetzung"))
        naht_danach = (i + 1 < len(segmente)
                       and bool(segmente[i + 1].get("fortsetzung")))
        bounds = speech_bounds(wav, sr, seg["start"],
                               min(seg["ende"], seg["start"] + 3600))
        if bounds is None:
            continue
        erste, letzte = bounds
        neu_start, neu_ende = seg["start"], seg["ende"]
        if not naht_davor and erste - seg["start"] > STILLE_TOLERANZ:
            neu_start = min(seg["start"] + STILLE_MAX_TRIM,
                            max(seg["start"], erste - PADDING_SEC))
        if not naht_danach and seg["ende"] - letzte > STILLE_TOLERANZ:
            neu_ende = max(seg["ende"] - STILLE_MAX_TRIM,
                           min(seg["ende"], letzte + STILLE_NACHLAUF))
        if neu_ende - neu_start < 0.4:
            continue
        if neu_start != seg["start"] or neu_ende != seg["ende"]:
            seg["start"] = round(neu_start, 3)
            seg["ende"] = round(neu_ende, 3)
            seg["dauer"] = round(neu_ende - neu_start, 3)
            seg["stille_getrimmt"] = True


def find_pauses(wav, sr: int, von: float, bis: float,
                min_pause: float) -> list[tuple[float, float]]:
    """Sprechpausen (start, ende) INNERHALB von [von, bis], die mindestens
    min_pause Sekunden dauern - am echten Audio gemessen.

    Läufe, die direkt am Fensteranfang beginnen oder am Fensterende noch
    offen sind, zählen nicht (Rand-Stille erledigt der Stille-Trimmer).
    """
    import numpy as np

    a, b = max(0, int(von * sr)), min(len(wav), int(bis * sr))
    seg = wav[a:b]
    frame = int(0.02 * sr)
    n = len(seg) // frame
    if n < 3:
        return []
    env = np.abs(seg[: n * frame]).reshape(n, frame).mean(axis=1)
    peak = float(np.percentile(env, 95))
    if peak <= 1e-5:
        return []
    still = env <= 0.12 * peak
    pausen: list[tuple[float, float]] = []
    start: int | None = None
    for i, s in enumerate(still):
        if s and start is None:
            start = i
        elif not s and start is not None:
            if start > 0 and (i - start) * frame / sr >= min_pause:
                pausen.append((von + start * frame / sr,
                               von + i * frame / sr))
            start = None
    return pausen


def _split_pauses(project: str, segmente: list[dict], words: list[dict],
                  min_pause: float) -> list[dict]:
    """Lange Sprechpausen INNERHALB eines Segments rausschneiden.

    Das Segment wird an der Pause geteilt; das zweite Teilstück wird als
    fortsetzung markiert, damit der Jump-Cut (gleiche Kamera, Position
    springt) im B-Roll-Matching als Kandidat zum Verdecken auftaucht.
    min_pause <= 0 schaltet den Pausen-Schnitt ab.
    """
    if min_pause <= 0:
        return segmente
    try:
        ref_file, _ = sync_audio.reference_source(project)
        wav = sync_audio._load_wav_mono(sync_audio._tmp_wav(project, ref_file))
    except Exception:  # noqa: BLE001 - Pausen-Schnitt ist Kür, kein Muss
        return segmente
    sr = sync_audio.SYNC_SR

    ergebnis: list[dict] = []
    for seg in segmente:
        pausen = find_pauses(wav, sr, seg["start"], seg["ende"], min_pause)
        teile: list[tuple[float, float]] = []
        cursor = seg["start"]
        for p_start, p_ende in pausen:
            teile.append((cursor, p_start + STILLE_NACHLAUF))
            cursor = max(cursor, p_ende - PADDING_SEC)
        teile.append((cursor, seg["ende"]))
        # Winzige Teilstücke (einzelner Füller zwischen zwei Pausen) fliegen
        # mit der Pause raus.
        gueltig = [(s, e) for s, e in teile if e - s >= PAUSE_MIN_TEIL_SEC]
        if len(gueltig) <= 1:
            ergebnis.append(seg)
            continue
        for j, (s, e) in enumerate(gueltig):
            teil_words = words_in_range(words, s, e)
            neu = dict(seg)
            neu.update({
                "id": seg["id"] if j == 0 else uuid.uuid4().hex[:8],
                "start": round(s, 3),
                "ende": round(e, 3),
                "dauer": round(e - s, 3),
                "text": " ".join(w["word"] for w in teil_words) or seg["text"],
                "fortsetzung": bool(seg.get("fortsetzung")) if j == 0 else True,
                "pause_entfernt": j > 0,
            })
            ergebnis.append(neu)
    return ergebnis


# ------------------------------------------------------------ Skript-Datei

SKRIPT_ENDUNGEN = (".txt", ".md")


def script_file_path(project: str) -> Path | None:
    """Pfad der Skript-Datei im Projektordner (Wurzel oder input/).

    Erste .txt/.md-Datei, 'skript*'/'script*'-Namen bevorzugt.
    """
    base = paths.project_dir(project)
    kandidaten: list[Path] = []
    for folder in (base, base / "input"):
        if folder.is_dir():
            kandidaten += sorted(
                f for f in folder.iterdir()
                if f.is_file() and f.suffix.lower() in SKRIPT_ENDUNGEN
                and not f.name.startswith(".")
            )
    if not kandidaten:
        return None
    kandidaten.sort(key=lambda f: (
        not f.stem.lower().startswith(("skript", "script")), f.name.lower()))
    return kandidaten[0]


def load_script_file(project: str) -> dict | None:
    """Skript-Datei einlesen: {"datei": name, "text": inhalt} oder None."""
    f = script_file_path(project)
    if f is None:
        return None
    text = f.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return None
    return {"datei": f.name, "text": text}


def save_script_file(project: str, text: str) -> dict | None:
    """Skript aus dem Dashboard-Feld dauerhaft speichern.

    Schreibt in die vorhandene skript*-Datei, sonst nach skript.txt im
    Projektordner (andere Textdateien, z.B. Notizen, werden nie
    überschrieben). Leerer Text löscht die Skript-Datei wieder.
    """
    text = (text or "").strip()
    f = script_file_path(project)
    if f is None or not f.stem.lower().startswith(("skript", "script")):
        f = paths.project_dir(project) / "skript.txt"
    if not text:
        f.unlink(missing_ok=True)
        return None
    f.write_text(text + "\n", encoding="utf-8")
    return {"datei": f.name, "text": text}


# ------------------------------------------------------------ Auswahl-Lauf

def select_segments(project: str, modus: str = "auto", skript: str | None = None,
                    progress=None, client=None, analyse: bool = True) -> dict:
    cfg = config.load_config(project)
    transcript = transcribe.load_transcript(project)
    if transcript is None:
        raise RuntimeError("Erst transkribieren (Phase 1).")
    if not (skript or "").strip():
        # Kein Skript übergeben: Skript-Datei aus dem Projektordner. Liegt
        # eine, gilt sie auch im Auto-Modus als Leitfaden (wichtig für die
        # Warteschlange: Datei reinlegen = Vorgabe zählt).
        datei = load_script_file(project)
        if datei:
            modus, skript = "skript", datei["text"]
        elif modus == "skript":
            raise ValueError(
                "Skript-Modus gewählt, aber kein Skript angegeben – ins "
                "Eingabefeld tippen oder eine skript.txt in den "
                "Projektordner legen.")

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
    raw = claude_client.ensure_list(
        client.complete_json("reel_auswahl", prompt, max_tokens=4096))

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

    # Stille am echten Audio nachmessen und wegstutzen (Transkript-Timestamps
    # sind bei Pausen nicht verlässlich -> "3-7 s Stille vor der Aussage")
    _trim_silence(project, segmente)

    # Lange Denk-/Sprechpausen MITTEN im Segment rausschneiden
    segmente = _split_pauses(project, segmente, words,
                             float(cfg.get("pausen_schnitt_sek", 0.0) or 0.0))

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


def timeline_words(project: str) -> list[dict]:
    """Wörter der aktiven Segmente mit Zeiten auf der Reel-Zeitachse.

    Grundlage für wortgenaue Untertitel UND wortgenaues B-Roll-Timing.
    """
    transcript = transcribe.load_transcript(project)
    segs = enabled_segments(project)
    if transcript is None:
        raise RuntimeError("Kein Transkript vorhanden (Phase 1).")
    if not segs:
        raise RuntimeError("Keine aktiven Segmente (Phase 3).")
    words = []
    for seg in segs:
        for w in words_in_range(transcript["woerter"], seg["start"], seg["ende"]):
            words.append({
                "word": w["word"],
                "start": seg["timeline_start"] + (w["start"] - seg["start"]),
                "end": min(seg["timeline_start"] + (w["end"] - seg["start"]),
                           seg["timeline_ende"]),
                "segment_ende": seg["timeline_ende"],
            })
    return words


def segment_stand(project: str) -> str:
    """Kurzer Fingerabdruck des aktuellen Schnitt-Stands (aktive Segmente +
    Reihenfolge). Damit erkennen nachgelagerte Schritte, ob ihre Daten von
    einem älteren Schnitt stammen."""
    import hashlib

    segs = enabled_segments(project)
    payload = json.dumps([(s["id"], s["timeline_start"]) for s in segs])
    return hashlib.md5(payload.encode()).hexdigest()[:10]


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
                "-i", str(ingest.clip_datei(project, p["clip"])),
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
