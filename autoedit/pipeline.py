"""Phase 8: Pipeline-Kette Ingest -> Transkript -> Sync -> Schnitt -> B-Roll
-> Untertitel -> Musik -> Export, mit Status (Ampel), Log und Kosten-Schätzung.
"""

from __future__ import annotations

import datetime as _dt
import json
import traceback
from typing import Callable

from . import (broll, claude_client, config, cutting, fcpxml, ingest, jobs,
               music, paths, subtitles, sync_audio, transcribe)

STATUS_FILE = "status.json"
LOG_FILE = "log.txt"

STEPS: list[str] = [
    "ingest", "transkript", "sync", "schnitt", "broll",
    "untertitel", "musik", "export",
]

# Fehler in diesen Schritten brechen die Kette NICHT ab (werden übersprungen)
OPTIONAL_STEPS = {"broll", "untertitel", "musik"}

STEP_LABELS = {
    "ingest": "Ingest",
    "transkript": "Transkript",
    "sync": "Sync",
    "schnitt": "Schnitt",
    "broll": "B-Roll",
    "untertitel": "Untertitel",
    "musik": "Musik",
    "export": "Export",
}


def log(project: str, message: str) -> None:
    out = paths.output_dir(project)
    out.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with (out / LOG_FILE).open("a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {message}\n")


def read_log(project: str, tail: int = 200) -> str:
    f = paths.output_dir(project) / LOG_FILE
    if not f.is_file():
        return ""
    lines = f.read_text(encoding="utf-8").splitlines()
    return "\n".join(lines[-tail:])


def load_status(project: str) -> dict:
    f = paths.output_dir(project) / STATUS_FILE
    status = {step: {"status": "offen", "detail": "", "zeit": None}
              for step in STEPS}
    if f.is_file():
        saved = json.loads(f.read_text(encoding="utf-8"))
        for step in STEPS:
            if step in saved:
                status[step] = saved[step]
    return status


def _set_status(project: str, step: str, state: str, detail: str = "") -> None:
    status = load_status(project)
    status[step] = {
        "status": state,
        "detail": detail,
        "zeit": _dt.datetime.now().isoformat(timespec="seconds"),
    }
    out = paths.output_dir(project)
    out.mkdir(parents=True, exist_ok=True)
    (out / STATUS_FILE).write_text(
        json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ------------------------------------------------------------ Schritte

def _step_ingest(project, progress, **kw):
    result = ingest.scan_project(project, progress=progress)
    return f"{len(result['clips'])} Dateien erfasst"


def _step_transkript(project, progress, **kw):
    result = transcribe.transcribe_project(
        project, progress=progress, transcriber=kw.get("transcriber")
    )
    return f"{len(result['woerter'])} Wörter"


def _step_sync(project, progress, **kw):
    result = sync_audio.compute_offsets(project, progress=progress)
    return f"{len(result['offsets'])} Offsets"


def _step_schnitt(project, progress, **kw):
    result = cutting.select_segments(
        project, modus=kw.get("modus", "auto"), skript=kw.get("skript"),
        progress=progress, client=kw.get("client"),
    )
    gesamt = sum(s["dauer"] for s in result["segmente"])
    return f"{len(result['segmente'])} Segmente, {gesamt:.1f} s"


def _step_broll(project, progress, **kw):
    if not ingest.clips_for_role(project, "broll"):
        return "übersprungen (keine B-Roll-Dateien)"
    broll.analyze_broll(project, progress=progress, client=kw.get("client"))
    result = broll.match_broll(project, progress=progress, client=kw.get("client"))
    return f"{len(result['matches'])} Zuordnungen"


def _step_untertitel(project, progress, **kw):
    cfg = config.load_config(project)
    if not cfg["untertitel_aktiv"]:
        return "übersprungen (untertitel_aktiv = false)"
    result = subtitles.generate_subtitles(project, progress=progress,
                                          client=kw.get("client"))
    note = " (Korrektur verworfen)" if result.get("fehler") else ""
    return f"{result['cues']} Cues{note}"


def _step_musik(project, progress, **kw):
    cfg = config.load_config(project)
    if not cfg["musik_aktiv"]:
        return "übersprungen (musik_aktiv = false)"
    if not music.scan_library():
        return (f"übersprungen (keine Tracks in {paths.music_dir()} – "
                "Ordner anlegen und Musik hineinlegen)")
    result = music.select_track(project, progress=progress, client=kw.get("client"))
    return f"Track: {result['name']}"


def _step_export(project, progress, **kw):
    path = fcpxml.generate_fcpxml(project, progress=progress)
    timeline = json.loads(
        (paths.output_dir(project) / fcpxml.TIMELINE_FILE).read_text(encoding="utf-8")
    )
    detail = path.name
    if timeline.get("warnungen"):
        detail += f" ({len(timeline['warnungen'])} Warnungen)"
    return detail


_STEP_FN: dict[str, Callable] = {
    "ingest": _step_ingest,
    "transkript": _step_transkript,
    "sync": _step_sync,
    "schnitt": _step_schnitt,
    "broll": _step_broll,
    "untertitel": _step_untertitel,
    "musik": _step_musik,
    "export": _step_export,
}


def run_step(project: str, step: str, progress=None, **kwargs) -> str:
    if step not in _STEP_FN:
        raise ValueError(f"Unbekannter Schritt: {step!r}")
    log(project, f"Schritt '{step}' gestartet")
    _set_status(project, step, "laeuft")
    try:
        detail = _STEP_FN[step](project, progress, **kwargs)
    except Exception as exc:
        _set_status(project, step, "fehler", f"{type(exc).__name__}: {exc}")
        log(project, f"Schritt '{step}' FEHLER: {exc}\n{traceback.format_exc()}")
        raise
    _set_status(project, step, "ok", detail)
    log(project, f"Schritt '{step}' fertig: {detail}")
    return detail


def run_all(project: str, progress=None, fortsetzen: bool = True,
            ab_schritt: str | None = None, **kwargs) -> dict:
    """Komplette Kette; am Ende liegen FCPXML + SRT in output/.

    fortsetzen=True (Default): Schritte, die bereits auf 'ok' stehen, werden
    übersprungen – die Kette setzt beim ersten offenen oder fehlgeschlagenen
    Schritt fort. fortsetzen=False rechnet alles neu.

    ab_schritt="broll": alles davor bleibt unangetastet, ab dem Schritt wird
    bis zum Ende durchgerechnet (auch wenn dahinter schon alles grün war).

    Fehler in optionalen Schritten (B-Roll/Untertitel/Musik) brechen die
    Kette nicht ab – sie werden protokolliert und übersprungen.
    """
    status = load_status(project)
    if ab_schritt is not None:
        if ab_schritt not in STEPS:
            raise ValueError(f"Unbekannter Schritt: {ab_schritt!r}")
        skip = set(STEPS[: STEPS.index(ab_schritt)])
        skip_grund = "vor Startschritt"
    else:
        skip = {s for s in STEPS if fortsetzen and status[s]["status"] == "ok"}
        skip_grund = "bereits erledigt"
        if len(skip) == len(STEPS):
            if progress:
                progress(1.0, "Alle Schritte bereits erledigt – für einen "
                              "kompletten Neustart 'Alles neu berechnen' wählen")
            return {s: "übersprungen (bereits erledigt)" for s in STEPS}

    results = {}
    fehler_uebersprungen: list[str] = []
    n = len(STEPS)
    for i, step in enumerate(STEPS):
        if step in skip:
            results[step] = f"übersprungen ({skip_grund})"
            log(project, f"Schritt '{step}' übersprungen ({skip_grund})")
            continue
        def sub_progress(frac: float, meldung: str = "", _i=i, _step=step):
            if progress:
                progress((_i + frac) / n,
                         f"{STEP_LABELS[_step]}: {meldung}" if meldung
                         else STEP_LABELS[_step])
        try:
            results[step] = run_step(project, step, progress=sub_progress,
                                     **kwargs)
        except jobs.JobAbgebrochen:
            raise  # Nutzer-Abbruch geht IMMER durch, auch bei B-Roll & Co.
        except Exception as exc:  # noqa: BLE001 - optionale Schritte tolerieren
            if step not in OPTIONAL_STEPS:
                raise
            results[step] = f"FEHLER, übersprungen: {exc}"
            fehler_uebersprungen.append(STEP_LABELS[step])
            log(project, f"Schritt '{step}' fehlgeschlagen – Kette läuft "
                         "weiter (optionaler Schritt)")
    if progress:
        meldung = "Pipeline fertig"
        if fehler_uebersprungen:
            meldung += (" – mit Fehlern übersprungen: "
                        + ", ".join(fehler_uebersprungen))
        progress(1.0, meldung)
    return results


# ------------------------------------------------------------ Warteschlange

def run_batch(projects: list[str], progress=None, fortsetzen: bool = True,
              **kwargs) -> dict:
    """Mehrere Projekte NACHEINANDER komplett durchrechnen (Nacht-Modus).

    Ein Fehler in einem Projekt stoppt die Warteschlange nicht – das
    nächste Projekt ist dran; Details stehen im Log des jeweiligen
    Projekts. Auf macOS hält `caffeinate` den Rechner währenddessen wach.
    Ergebnis: {projekt: "fertig" | "FEHLER: …"}.
    """
    import shutil
    import subprocess

    wachhalter = None
    if shutil.which("caffeinate"):
        # verhindert Ruhezustand, solange die Warteschlange läuft
        wachhalter = subprocess.Popen(["caffeinate", "-ims"])

    results: dict[str, str] = {}
    n = len(projects)
    try:
        for i, name in enumerate(projects):
            def sub_progress(frac: float, meldung: str = "",
                             _i=i, _name=name):
                if progress:
                    progress((_i + max(0.0, min(1.0, frac))) / n,
                             f"[{_i + 1}/{n}] {_name}: {meldung}")

            log(name, "Warteschlange: Projekt gestartet")
            try:
                schritte = run_all(name, progress=sub_progress,
                                   fortsetzen=fortsetzen, **kwargs)
                fehler = [s for s, d in schritte.items()
                          if str(d).startswith("FEHLER")]
                results[name] = ("fertig" if not fehler else
                                 "fertig – übersprungen: " + ", ".join(fehler))
            except jobs.JobAbgebrochen:
                log(name, "Warteschlange: vom Nutzer abgebrochen")
                raise  # bricht die GANZE Warteschlange ab
            except Exception as exc:  # noqa: BLE001 - Nacht-Modus: weiter!
                results[name] = f"FEHLER: {type(exc).__name__}: {exc}"
                log(name, f"Warteschlange: Projekt abgebrochen – {exc}")
    finally:
        if wachhalter is not None:
            wachhalter.terminate()

    if progress:
        ok = sum(1 for v in results.values() if not v.startswith("FEHLER"))
        progress(1.0, f"Warteschlange fertig: {ok}/{n} Projekte erfolgreich")
    return results


# ------------------------------------------------------------ Kosten-Schätzung

def estimate_costs(project: str) -> dict:
    """Grobe Schätzung der Claude-API-Kosten für einen kompletten Lauf."""
    cfg = config.load_config(project)
    model = cfg["claude_modell"]

    transcript = transcribe.load_transcript(project)
    n_words = len(transcript["woerter"]) if transcript else 3000
    broll_clips = len(ingest.clips_for_role(project, "broll"))
    # Vision-Kosten fallen nur für noch nicht analysierte Dateien an
    broll_neu = len(broll.clips_to_analyze(project))
    frames = broll_neu * broll.MAX_FRAMES_PER_CLIP

    posten = []

    def add(zweck: str, tin: int, tout: int) -> None:
        posten.append({
            "zweck": zweck, "input_tokens": tin, "output_tokens": tout,
            "kosten_usd": round(
                claude_client.estimate_cost_usd(model, tin, tout), 4),
        })

    add("Aussagen-Analyse", int(n_words * 2.0) + 500, 2500)
    add("Reel-Auswahl", int(n_words * 2.0) + 1500, 1200)
    if broll_neu:
        # ~1100 Tokens pro 768px-Frame plus Prompt/Antwort je Clip
        add("B-Roll-Analyse (Vision)", frames * 1100 + broll_neu * 300,
            broll_neu * 200)
    if broll_clips:
        add("B-Roll-Matching", int(n_words * 0.6) + broll_clips * 80 + 600, 500)
    if cfg["untertitel_aktiv"]:
        add("Untertitel-Korrektur", 2500, 2500)
    if cfg["musik_aktiv"]:
        add("Musik-Auswahl", 1200, 150)

    summe = round(sum(p["kosten_usd"] for p in posten), 4)
    return {
        "modell": model,
        "posten": posten,
        "summe_usd": summe,
        "hinweis": "Grobe Schätzung; tatsächliche Kosten siehe costs.json "
                   "nach dem Lauf.",
        "bisher_usd": claude_client.load_costs(project)["summe_usd"],
    }
