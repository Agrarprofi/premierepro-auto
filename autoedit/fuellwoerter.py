"""Füllwort-Entfernung, Phase 1: Erkennung von Füllwörtern, Versprechern
und langen Pausen auf Basis des Word-Level-Transkripts.

Erzeugt eine REVIEW-Liste (output/fuellwoerter.json) mit Schnitt-
Kandidaten - angewendet wird erst nach Bestätigung im Dashboard
(Phase 2). Es wird kein Audio neu berechnet, nur Schnittpunkte gesetzt.

Grundsatz: KONSERVATIV erkennen. Im Zweifel bleibt eine Stelle drin und
taucht höchstens im Review auf; lieber ein Füllwort zu wenig entfernt
als ein Wortanfang abgeschnitten.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

import yaml

from . import config, paths, transcribe

STELLEN_FILE = "fuellwoerter.json"

# Sicherheits-Puffer: Schnitte bleiben so weit von behaltenen Wörtern weg
PAD_SEC = 0.10
# Schnitte, die näher beieinander liegen, werden zu einem zusammengefasst
MERGE_ABSTAND_SEC = 0.30
# Kürzere Schnittregionen lohnen nicht (Rundungs-/Timestamp-Rauschen)
MIN_SCHNITT_SEC = 0.12
# Versprecher: Neustart muss so schnell nach dem Abbruch kommen ...
VERSPRECHER_MAX_LUECKE_SEC = 3.0
# ... und mindestens so viele Wörter müssen exakt übereinstimmen
VERSPRECHER_MIN_MATCH = 2
# Abgebrochene Versuche länger als das gelten nicht als Versprecher
VERSPRECHER_MAX_WOERTER = 8
# Wort-Doppelungen: zweites Vorkommen muss so schnell folgen
DOPPEL_MAX_LUECKE_SEC = 1.0

_DEFAULT_LISTE = {
    # werden immer als Füllwort erkannt
    "immer": ["ähm", "äh", "ähh", "öhm", "öh", "mhm", "hm", "hmm", "em"],
    # nur aktiv, wenn fuellwort_optionale_liste=true - diese Wörter gehören
    # oft zum Sprechstil (Austriazismen!) und sind nicht immer Füllsel
    "optional": ["quasi", "eben", "halt", "sozusagen", "irgendwie",
                 "praktisch", "im prinzip"],
    # bewusste Verstärkungen: "sehr sehr gut" ist KEIN Stotterer
    "doppel_ok": ["sehr", "ganz", "wirklich", "immer", "schön"],
}

_PUNKT_RE = re.compile(r"^[\W_]+|[\W_]+$", re.UNICODE)


def _norm(wort: str) -> str:
    return _PUNKT_RE.sub("", wort.lower())


def _hat_satzende(wort: str) -> bool:
    return wort.rstrip().endswith((".", "!", "?", "…"))


# ------------------------------------------------------------ Wortliste

def liste_datei() -> Path:
    return Path(os.environ.get("AUTOEDIT_FUELLWOERTER",
                               paths.REPO_ROOT / "fuellwoerter.yaml"))


def lade_liste() -> dict:
    """Füllwortliste laden; beim ersten Aufruf wird die editierbare
    Default-Datei angelegt (fuellwoerter.yaml im Tool-Ordner)."""
    f = liste_datei()
    if not f.is_file():
        f.write_text(
            "# Füllwortliste für die automatische Erkennung.\n"
            "# immer:     wird immer als Füllwort markiert\n"
            "# optional:  nur bei aktivierter Option (Sprechstil-Wörter!)\n"
            "# doppel_ok: bewusste Verdopplungen, kein Stottern\n"
            + yaml.safe_dump(_DEFAULT_LISTE, allow_unicode=True,
                             sort_keys=False),
            encoding="utf-8",
        )
        return dict(_DEFAULT_LISTE)
    data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    liste = dict(_DEFAULT_LISTE)
    for key in liste:
        if isinstance(data.get(key), list):
            liste[key] = [str(w).lower() for w in data[key]]
    return liste


# ------------------------------------------------------------ Erkennung

def _kontext(words: list[dict], von_idx: int, bis_idx: int,
             umfang: int = 5) -> str:
    """±5 Wörter Kontext, die Fundstelle in ** hervorgehoben."""
    davor = " ".join(w["word"] for w in words[max(0, von_idx - umfang):von_idx])
    stelle = " ".join(w["word"] for w in words[von_idx:bis_idx + 1])
    danach = " ".join(
        w["word"] for w in words[bis_idx + 1:bis_idx + 1 + umfang])
    return f"{davor} **{stelle}** {danach}".strip()


def _stelle(typ: str, words: list[dict], von_idx: int, bis_idx: int,
            start: float, ende: float) -> dict:
    return {
        "id": uuid.uuid4().hex[:8],
        "typ": typ,
        "start": round(start, 3),
        "ende": round(ende, 3),
        "text": " ".join(w["word"] for w in words[von_idx:bis_idx + 1]),
        "kontext": _kontext(words, von_idx, bis_idx),
        "aktiv": True,
    }


def _finde_fuellwoerter(words: list[dict], liste: dict,
                        optionale: bool) -> list[dict]:
    aktiv = set(liste["immer"]) | (set(liste["optional"]) if optionale
                                   else set())
    stellen = []
    for i, w in enumerate(words):
        if _norm(w["word"]) in aktiv:
            stellen.append(_stelle("fuellwort", words, i, i,
                                   w["start"], w["end"]))
    return stellen


def _finde_doppelungen(words: list[dict], liste: dict) -> list[dict]:
    """Direkt wiederholte Wörter ("die die"): erstes Vorkommen entfernen.
    Bewusste Verstärkungen (doppel_ok) bleiben unangetastet."""
    ok = set(liste["doppel_ok"])
    stellen = []
    for i in range(len(words) - 1):
        a, b = _norm(words[i]["word"]), _norm(words[i + 1]["word"])
        if (a and a == b and a not in ok
                and words[i + 1]["start"] - words[i]["end"]
                <= DOPPEL_MAX_LUECKE_SEC
                and not _hat_satzende(words[i]["word"])):
            stellen.append(_stelle("doppelung", words, i, i,
                                   words[i]["start"], words[i]["end"]))
    return stellen


def _finde_versprecher(words: list[dict]) -> list[dict]:
    """Abgebrochene Sätze: der Satzanfang wird binnen 3 s fast wortgleich
    neu begonnen -> der erste Versuch fliegt raus.

    Konservativ: mindestens 2 exakt gleiche Anfangswörter, Versuch max.
    8 Wörter, und endet der Versuch mit Satzzeichen (fertiger Satz,
    evtl. bewusste rhetorische Wiederholung), wird NICHT geschnitten.
    """
    norm = [_norm(w["word"]) for w in words]
    n = len(words)
    stellen = []
    i = 0
    while i < n - VERSPRECHER_MIN_MATCH:
        treffer = None
        max_j = min(i + VERSPRECHER_MAX_WOERTER + 1,
                    n - VERSPRECHER_MIN_MATCH + 1)
        for j in range(i + VERSPRECHER_MIN_MATCH, max_j):
            if norm[j:j + VERSPRECHER_MIN_MATCH] == \
                    norm[i:i + VERSPRECHER_MIN_MATCH]:
                treffer = j
                break
        if treffer is not None:
            j = treffer
            # Versuch + Neustart müssen EIN zusammenhängender Anlauf sein:
            # keine Lücke über 3 s - weder vor dem Neustart noch mitten im
            # Versuch (sonst sind es zwei getrennte Aussagen).
            max_luecke = max(words[k + 1]["start"] - words[k]["end"]
                             for k in range(i, j))
            # Enthält der "Versuch" ein Satzende, war der Satz fertig -
            # dann ist die Wiederholung evtl. bewusste Rhetorik: drinlassen.
            satz_fertig = any(_hat_satzende(words[k]["word"])
                              for k in range(i, j))
            if max_luecke <= VERSPRECHER_MAX_LUECKE_SEC and not satz_fertig:
                stellen.append(_stelle("versprecher", words, i, j - 1,
                                       words[i]["start"],
                                       words[j - 1]["end"]))
                i = j
                continue
        i += 1
    return stellen


def _finde_pausen(words: list[dict], pause_ab: float,
                  pause_ziel: float) -> list[dict]:
    """Pausen über dem Schwellwert auf den Zielwert KÜRZEN (nicht ganz
    entfernen - sonst wirkt der Schnitt gehetzt). Die verbleibende Luft
    (je Zielwert/2 vor und nach dem Schnitt) ersetzt das Padding."""
    stellen = []
    for i in range(len(words) - 1):
        gap = words[i + 1]["start"] - words[i]["end"]
        if gap <= pause_ab:
            continue
        start = words[i]["end"] + pause_ziel / 2.0
        ende = words[i + 1]["start"] - pause_ziel / 2.0
        s = _stelle("stille", words, i, i + 1, start, ende)
        s["text"] = f"(Pause {gap:.1f} s → {pause_ziel:.1f} s)"
        s["pause_original_sek"] = round(gap, 2)
        stellen.append(s)
    return stellen


# ------------------------------------------------------ Padding & Merge

def _puffere(stellen: list[dict], words: list[dict]) -> list[dict]:
    """Sicherheits-Puffer: Schnittgrenzen bleiben PAD_SEC von behaltenen
    Wörtern entfernt (WhisperX-Timestamps sind nicht auf die ms genau).
    Der Schnitt darf dafür in die Lücken VOR/NACH der Fundstelle
    hineinwachsen. Zu kurz gewordene Schnitte fliegen raus."""
    enden = [w["end"] for w in words]
    starts = [w["start"] for w in words]
    ergebnis = []
    for s in stellen:
        if s["typ"] == "stille":
            ergebnis.append(s)  # Luft steckt schon im Zielwert
            continue
        # letztes behaltenes Wort vor dem Schnitt / erstes danach
        prev_ende = max((e for e in enden if e <= s["start"] + 1e-6),
                        default=None)
        next_start = min((a for a in starts if a >= s["ende"] - 1e-6),
                         default=None)
        start = s["start"]
        ende = s["ende"]
        if prev_ende is not None and prev_ende < start:
            start = max(prev_ende + PAD_SEC, start - 0.5)  # in die Lücke ziehen
            start = min(start, s["start"])                 # nie ins Wort hinein
        if next_start is not None and next_start > ende:
            ende = min(next_start - PAD_SEC, ende + 0.5)
            ende = max(ende, s["ende"])
        # Schutz: nie näher als PAD_SEC an Nachbar-Wörter heranschneiden
        if prev_ende is not None:
            start = max(start, prev_ende + PAD_SEC)
        if next_start is not None:
            ende = min(ende, next_start - PAD_SEC)
        if ende - start < MIN_SCHNITT_SEC:
            continue  # konservativ: lieber drinlassen
        s = dict(s)
        s["start"], s["ende"] = round(start, 3), round(ende, 3)
        ergebnis.append(s)
    return ergebnis


def _verschmelze(stellen: list[dict]) -> list[dict]:
    """Überlappende oder <300 ms benachbarte Schnitte zusammenfassen."""
    stellen = sorted(stellen, key=lambda s: s["start"])
    ergebnis: list[dict] = []
    for s in stellen:
        if ergebnis and s["start"] - ergebnis[-1]["ende"] < MERGE_ABSTAND_SEC:
            alt = ergebnis[-1]
            alt["ende"] = max(alt["ende"], s["ende"])
            if s["typ"] not in alt["typ"].split("+"):
                alt["typ"] = f"{alt['typ']}+{s['typ']}"
            if s["text"] != alt["text"]:
                alt["text"] = f"{alt['text']} {s['text']}"
            continue
        ergebnis.append(dict(s))
    for s in ergebnis:
        s["dauer"] = round(s["ende"] - s["start"], 3)
    return ergebnis


# ------------------------------------------------------------ Öffentlich

def finde_schnittstellen(words: list[dict], *, liste: dict | None = None,
                         optionale: bool = False, pause_ab: float = 1.5,
                         pause_ziel: float = 0.4) -> list[dict]:
    """Alle Schnitt-Kandidaten für ein Word-Level-Transkript.

    Reine Funktion (keine Projekt-/Dateizugriffe) - direkt testbar.
    Rückgabe: gepufferte, verschmolzene Stellen, sortiert nach Start.
    """
    if liste is None:
        liste = dict(_DEFAULT_LISTE)
    kandidaten = (
        _finde_fuellwoerter(words, liste, optionale)
        + _finde_doppelungen(words, liste)
        + _finde_versprecher(words)
        + _finde_pausen(words, pause_ab, pause_ziel)
    )
    return _verschmelze(_puffere(kandidaten, words))


def analysiere(project: str, progress=None) -> dict:
    """Projekt-Transkript analysieren -> output/fuellwoerter.json
    (Review-Grundlage fürs Dashboard, Phase 2)."""
    transcript = transcribe.load_transcript(project)
    if transcript is None:
        raise RuntimeError("Erst transkribieren (Phase 1).")
    cfg = config.load_config(project)
    if progress:
        progress(0.3, "Suche Füllwörter, Versprecher und Pausen …")

    words = transcript["woerter"]
    stellen = finde_schnittstellen(
        words,
        liste=lade_liste(),
        optionale=bool(cfg.get("fuellwort_optionale_liste", False)),
        pause_ab=float(cfg.get("fuellwort_pause_ab_sek", 1.5)),
        pause_ziel=float(cfg.get("fuellwort_pause_ziel_sek", 0.4)),
    )
    entfernt = sum(s["dauer"] for s in stellen)
    gesamt = (words[-1]["end"] - words[0]["start"]) if words else 0.0
    result = {
        "projekt": project,
        "stellen": stellen,
        "anzahl": len(stellen),
        "dauer_vorher_sek": round(gesamt, 2),
        "dauer_nachher_sek": round(gesamt - entfernt, 2),
        "entfernt_sek": round(entfernt, 2),
    }
    out = paths.output_dir(project)
    out.mkdir(parents=True, exist_ok=True)
    (out / STELLEN_FILE).write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if progress:
        progress(1.0, f"{len(stellen)} Stellen gefunden "
                      f"({entfernt:.1f} s entfernbar)")
    return result


def load_stellen(project: str) -> dict | None:
    f = paths.output_dir(project) / STELLEN_FILE
    if not f.is_file():
        return None
    return json.loads(f.read_text(encoding="utf-8"))
