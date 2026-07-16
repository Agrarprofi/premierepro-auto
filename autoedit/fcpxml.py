"""Phase 5: Export als FCP7-XML (xmeml v4) – importierbar in Premiere Pro
über Datei > Importieren.

Sequenzaufbau:
  V1: Kamera A (nach Segmenten geschnitten, Clip-Grenzen werden überbrückt)
  V2: Kamera B (synchron, gleiche Schnitte)
  V3: B-Rolls an den zugeordneten Stellen
  A1: Referenz-Audio (DJI, synchron geschnitten)
  A2: Kamera-A-Ton, Spur deaktiviert (Backup)
  A3: Musik, -18 dB Startpegel

Stereo-Quellen werden als zwei verknüpfte Spuren exportiert (trackindex 1/2),
sonst verwirft Premiere den rechten Kanal. Jede Datei deklariert ihre native
Framerate; Quell-In/Out werden in der Datei-Framerate gerechnet, Timeline-
Positionen in der Sequenz-Framerate. Clips, deren Auflösung von der Sequenz
abweicht, bekommen einen zentrierten Scale-to-fill-Filter (Basic Motion).

Alle Zeiten werden erst als Sekunden-Timeline gebaut (build_timeline) und dann
framegenau umgerechnet – das hält die Logik testbar.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from xml.sax.saxutils import escape

from . import broll, config, cutting, ffmpeg_utils, ingest, music, paths, sync_audio

FCPXML_SUFFIX = "_premiere.xml"
TIMELINE_FILE = "timeline.json"
MUSIK_GAIN_DB = -18.0

_NTSC_RATES = {23.976: 24, 29.97: 30, 59.94: 60, 47.952: 48}


def rate_for_fps(fps: float) -> tuple[int, bool]:
    """fps -> (timebase, ntsc). 29.97 -> (30, True), 25 -> (25, False)."""
    for ntsc_fps, timebase in _NTSC_RATES.items():
        if abs(fps - ntsc_fps) < 0.01:
            return timebase, True
    return int(round(fps)), False


def exact_fps(timebase: int, ntsc: bool) -> float:
    return timebase * 1000.0 / 1001.0 if ntsc else float(timebase)


def to_frames(seconds: float, fps: float) -> int:
    return int(round(seconds * fps))


# ------------------------------------------------------------ Timeline-Modell

def _media_lookup(media: dict) -> dict[str, dict]:
    return {c["relpfad"]: c for c in media.get("clips", [])}


def _apply_av_versatz(ev: dict, clip: dict, warnungen: list[str]) -> None:
    """Container-Startversatz Video vs. Audio kompensieren.

    Unsere src_in-Zeiten stammen aus der Audio-Korrelation (Zeitachse ab dem
    ersten Audiosample). Premiere adressiert Frames ab dem ersten Videobild.
    Starten die Spuren im Container versetzt, muss der Versatz abgezogen
    werden – sonst stimmen A- und B-Kamera im Bild nicht überein, obwohl
    der Ton passt.
    """
    versatz = float(clip.get("av_versatz") or 0.0)
    if abs(versatz) < 0.001:
        return
    neu = ev["src_in"] - versatz
    if neu < 0:
        warnungen.append(
            f"AV-Versatz-Korrektur bei {ev['name']} am Dateianfang gekappt "
            f"({neu:.3f} s)"
        )
        neu = 0.0
    if neu + ev["dauer"] > ev["datei_dauer"]:
        alt = ev["dauer"]
        ev["dauer"] = round(max(0.0, ev["datei_dauer"] - neu), 6)
        warnungen.append(
            f"AV-Versatz-Korrektur bei {ev['name']} am Dateiende gekappt "
            f"({alt - ev['dauer']:.3f} s)"
        )
    ev["src_in"] = round(neu, 6)
    ev["av_versatz"] = versatz


def _event_from_clip(base: Path, clip: dict, timeline_start: float, dauer: float,
                     src_in: float, **extra) -> dict:
    return {
        # CFR-Kopie verwenden, falls das Original variable Framerate hatte
        "datei": str(base / (clip.get("cfr_pfad") or clip["relpfad"])),
        "name": clip["name"],
        "timeline_start": round(timeline_start, 6),
        "dauer": round(dauer, 6),
        "src_in": round(src_in, 6),
        "datei_dauer": float(clip["dauer"]),
        "breite": clip.get("breite"),
        "hoehe": clip.get("hoehe"),
        "fps": clip.get("fps"),
        "audio_kanaele": clip.get("audio_kanaele"),
        "audio_samplerate": clip.get("audio_samplerate"),
        **extra,
    }


def build_timeline(project: str) -> dict:
    """Sekundengenaues Timeline-Modell aus Segmenten/Sync/B-Roll/Musik."""
    cfg = config.load_config(project)
    media = ingest.load_media_info(project)
    sync = sync_audio.load_sync(project)
    segs = cutting.enabled_segments(project)
    if media is None or sync is None:
        raise RuntimeError("Erst Ingest und Sync ausführen.")
    if not segs:
        raise RuntimeError("Keine aktiven Segmente (Phase 3).")

    base = paths.project_dir(project)
    by_role = ingest.clips_by_role(media)
    lookup = _media_lookup(media)
    cam_a = by_role.get("cam_a", [])
    if not cam_a:
        raise RuntimeError("Kein Kamera-A-Material.")

    # Sequenz-Framerate: fest aus der Config (Default 25); 0 = wie
    # Kamera A. Die Kameras sind vom Ingest auf diese Rate normalisiert.
    src = cam_a[0]
    seq_fps_cfg = float(cfg.get("sequenz_fps") or 0)
    timebase, ntsc = rate_for_fps(seq_fps_cfg if seq_fps_cfg > 0
                                  else float(src["fps"] or 25.0))
    if cfg["export_format"] == "9:16":
        seq_w, seq_h = 1080, 1920
    else:
        seq_w, seq_h = int(src["breite"] or 1920), int(src["hoehe"] or 1080)

    tracks: dict[str, list[dict]] = {
        "V1": [], "V2": [], "V3": [], "A1": [], "A2": [], "A3": [],
    }
    warnungen: list[str] = []

    seq_fps_exakt = exact_fps(timebase, ntsc)
    for role in ("cam_a", "cam_b"):
        for clip in by_role.get(role, []):
            if clip.get("fps") and abs(clip["fps"] - seq_fps_exakt) >= 0.01:
                warnungen.append(
                    f"{clip['relpfad']}: Framerate {clip['fps']:.2f} passt "
                    f"nicht zur Sequenz ({seq_fps_exakt:g}) – Ingest neu "
                    "ausführen, damit die Kamera auf die Sequenz-Rate "
                    "normalisiert wird (Misch-Raten sind in Premiere "
                    "fehleranfällig)."
                )

    # Referenz-Audio-Info einmalig (steht i.d.R. schon in media_info.json)
    ref_rel = sync["referenz"]
    ref_path = base / ref_rel
    ref_clip = lookup.get(ref_rel)
    if ref_clip is None:
        info = ffmpeg_utils.media_info(ref_path)
        ref_clip = {**info, "relpfad": ref_rel, "name": ref_path.name}

    # Auto-Zoom: pro Segment ein Punch-In (wechsel) oder eine Zoom-Fahrt
    # (sanft) - direkt als Motion-Scale auf den Kamera-Clips, in Premiere
    # pro Clip nachjustierbar. Einstellungsebenen kennt FCP7-XML nicht.
    zoom_modus = str(cfg.get("zoom_modus", "aus"))
    zoom_faktor = 1.0 + float(cfg.get("zoom_staerke_prozent", 0) or 0) / 100.0
    if zoom_faktor <= 1.0:
        zoom_modus = "aus"

    for si, seg in enumerate(segs):
        t0 = seg["timeline_start"]
        for role, vtrack, atrack in (("cam_a", "V1", "A2"), ("cam_b", "V2", None)):
            pieces, uncovered = sync_audio.cover_range(
                media, sync, role, seg["start"], seg["ende"]
            )
            if role == "cam_a" and not pieces:
                warnungen.append(
                    f"Kein {role}-Clip für Segment ab {seg['start']:.2f} s"
                )
            if uncovered > 0.04 and (role == "cam_a" or pieces):
                warnungen.append(
                    f"{role}: {uncovered:.2f} s des Segments ab "
                    f"{seg['start']:.2f} s nicht abgedeckt"
                )
            for p in pieces:
                clip = p["clip"]
                ev = _event_from_clip(base, clip, t0 + p["rel_start"],
                                      p["dauer"], p["src_in"])
                _apply_av_versatz(ev, clip, warnungen)
                audio_ev = dict(ev)  # Kamera-Ton VOR der Bild-Korrektur
                if zoom_modus == "wechsel" and si % 2 == 1:
                    ev["zoom"] = round(zoom_faktor, 4)
                elif zoom_modus == "sanft":
                    von, bis = ((1.0, zoom_faktor) if si % 2 == 0
                                else (zoom_faktor, 1.0))
                    a = p["rel_start"] / seg["dauer"]
                    b = (p["rel_start"] + p["dauer"]) / seg["dauer"]
                    ev["zoom_fahrt"] = [round(von + (bis - von) * a, 4),
                                        round(von + (bis - von) * b, 4)]
                entry = sync["offsets"].get(clip["relpfad"]) or {}
                video_korr = float(entry.get("video_korrektur_sekunden") or 0.0)
                if abs(video_korr) >= 0.001:
                    ev["src_in"] = round(
                        max(0.0, min(ev["src_in"] + video_korr,
                                     ev["datei_dauer"] - ev["dauer"])), 6)
                    ev["video_korrektur"] = video_korr
                if clip.get("vfr_verdacht") and clip["relpfad"] not in \
                        [w.split(" ", 1)[0] for w in warnungen]:
                    warnungen.append(
                        f"{clip['relpfad']} hat vermutlich VARIABLE Framerate "
                        "und wurde noch nicht nach CFR gewandelt – Bild "
                        "driftet in Premiere. Ingest neu ausführen (wandelt "
                        "automatisch)"
                        + (f"; letzter Fehler: {clip['cfr_fehler']}"
                           if clip.get("cfr_fehler") else ".")
                    )
                tracks[vtrack].append(ev)
                if atrack and clip.get("audio_kanaele"):
                    tracks[atrack].append(audio_ev)

        # A1: Referenz-Audio – Referenzzeit == Dateizeit der Referenzdatei
        a_dauer = min(seg["dauer"], max(0.0, float(ref_clip["dauer"]) - seg["start"]))
        if a_dauer > 0:
            ev_ref = _event_from_clip(base, ref_clip, t0, a_dauer, seg["start"])
            _apply_av_versatz(ev_ref, ref_clip, warnungen)
            tracks["A1"].append(ev_ref)
        if a_dauer < seg["dauer"] - 0.04:
            warnungen.append(
                f"Referenz-Audio endet {seg['dauer'] - a_dauer:.2f} s vor "
                f"Segmentende (Segment ab {seg['start']:.2f} s)"
            )

    reel_ende = segs[-1]["timeline_ende"]

    # V3: B-Roll
    matches = broll.load_matches(project)
    if matches and matches.get("reel_stand") and \
            matches["reel_stand"] != cutting.segment_stand(project):
        warnungen.append(
            "B-Roll-Zuordnung stammt von einem älteren Schnitt-Stand – "
            "die Zeiten passen nicht mehr zur aktuellen Timeline. "
            "Bitte den B-Roll-Schritt neu ausführen."
        )
    if matches:
        for m in matches["matches"]:
            clip = lookup.get(m["broll_datei"])
            if clip is None:
                info = ffmpeg_utils.media_info(base / m["broll_datei"])
                clip = {**info, "relpfad": m["broll_datei"],
                        "name": Path(m["broll_datei"]).name}
            dauer = min(float(m["dauer"]),
                        float(clip["dauer"]) - float(m["broll_einstieg"]))
            if dauer <= 0:
                warnungen.append(f"B-Roll-Einstieg hinter Clipende: {m['broll_datei']}")
                continue
            tracks["V3"].append(
                _event_from_clip(base, clip, float(m["transkript_zeit"]), dauer,
                                 float(m["broll_einstieg"]))
            )

    # A3: Musik
    if cfg["musik_aktiv"]:
        track = music.load_selection(project)
        if track and track.get("datei"):
            mpath = Path(track["datei"])
            if mpath.is_file():
                minfo = ffmpeg_utils.media_info(mpath)
                tracks["A3"].append({
                    "datei": str(mpath),
                    "name": mpath.name,
                    "timeline_start": 0.0,
                    "dauer": round(min(reel_ende, minfo["dauer"]), 6),
                    "src_in": 0.0,
                    "datei_dauer": minfo["dauer"],
                    "breite": None, "hoehe": None, "fps": None,
                    "audio_kanaele": minfo.get("audio_kanaele"),
                    "audio_samplerate": minfo.get("audio_samplerate"),
                    "gain_db": float(track.get("gain_db", MUSIK_GAIN_DB)),
                })
            else:
                warnungen.append(f"Musikdatei nicht gefunden: {mpath}")

    timeline = {
        "projekt": project,
        "name": f"{project}_reel",
        "timebase": timebase,
        "ntsc": ntsc,
        "fps": exact_fps(timebase, ntsc),
        "breite": seq_w,
        "hoehe": seq_h,
        "dauer": reel_ende,
        "tracks": tracks,
        "warnungen": warnungen,
    }
    (paths.output_dir(project) / TIMELINE_FILE).write_text(
        json.dumps(timeline, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return timeline


# ------------------------------------------------------------ XML-Generierung

def _pathurl(path: str) -> str:
    return "file://localhost" + urllib.parse.quote(str(Path(path).resolve()))


def _rate_xml(timebase: int, ntsc: bool, indent: str) -> str:
    n = "TRUE" if ntsc else "FALSE"
    return (f"{indent}<rate>\n{indent}\t<timebase>{timebase}</timebase>\n"
            f"{indent}\t<ntsc>{n}</ntsc>\n{indent}</rate>\n")


def _scale_filter(scale_pct: float | None = None,
                  keyframes: list[tuple[int, float]] | None = None) -> str:
    """Basic-Motion-Scale: fester Wert ODER Keyframes (Zoom-Fahrt).

    Keyframe-Zeiten (<when>) laufen in Quell-Frames der Datei, also in
    derselben Zeitachse wie <in>/<out> des Clipitems.
    """
    if keyframes:
        wert = "".join(
            "\t\t\t\t\t\t\t\t\t<keyframe>\n"
            f"\t\t\t\t\t\t\t\t\t\t<when>{when}</when>\n"
            f"\t\t\t\t\t\t\t\t\t\t<value>{val:.4f}</value>\n"
            "\t\t\t\t\t\t\t\t\t</keyframe>\n"
            for when, val in keyframes
        )
    else:
        wert = f"\t\t\t\t\t\t\t\t\t<value>{scale_pct:.4f}</value>\n"
    return f"""\t\t\t\t\t\t<filter>
\t\t\t\t\t\t\t<effect>
\t\t\t\t\t\t\t\t<name>Basic Motion</name>
\t\t\t\t\t\t\t\t<effectid>basic</effectid>
\t\t\t\t\t\t\t\t<effectcategory>motion</effectcategory>
\t\t\t\t\t\t\t\t<effecttype>motion</effecttype>
\t\t\t\t\t\t\t\t<mediatype>video</mediatype>
\t\t\t\t\t\t\t\t<parameter authoringApp="PremierePro">
\t\t\t\t\t\t\t\t\t<parameterid>scale</parameterid>
\t\t\t\t\t\t\t\t\t<name>Scale</name>
\t\t\t\t\t\t\t\t\t<valuemin>0</valuemin>
\t\t\t\t\t\t\t\t\t<valuemax>1000</valuemax>
{wert}\t\t\t\t\t\t\t\t</parameter>
\t\t\t\t\t\t\t</effect>
\t\t\t\t\t\t</filter>
"""


def _levels_filter(gain_db: float) -> str:
    level = 10.0 ** (gain_db / 20.0)
    return f"""\t\t\t\t\t\t<filter>
\t\t\t\t\t\t\t<effect>
\t\t\t\t\t\t\t\t<name>Audio Levels</name>
\t\t\t\t\t\t\t\t<effectid>audiolevels</effectid>
\t\t\t\t\t\t\t\t<effectcategory>audiolevels</effectcategory>
\t\t\t\t\t\t\t\t<effecttype>audiolevels</effecttype>
\t\t\t\t\t\t\t\t<mediatype>audio</mediatype>
\t\t\t\t\t\t\t\t<parameter authoringApp="PremierePro">
\t\t\t\t\t\t\t\t\t<parameterid>level</parameterid>
\t\t\t\t\t\t\t\t\t<name>Level</name>
\t\t\t\t\t\t\t\t\t<valuemin>0</valuemin>
\t\t\t\t\t\t\t\t\t<valuemax>3.98109</valuemax>
\t\t\t\t\t\t\t\t\t<value>{level:.6f}</value>
\t\t\t\t\t\t\t\t</parameter>
\t\t\t\t\t\t\t</effect>
\t\t\t\t\t\t</filter>
"""


def _file_rate(ev: dict, seq_timebase: int, seq_ntsc: bool) -> tuple[int, bool]:
    """Native Framerate der Datei; Audiodateien laufen in der Sequenzrate."""
    if ev.get("fps"):
        return rate_for_fps(float(ev["fps"]))
    return seq_timebase, seq_ntsc


class _FileRegistry:
    """Jede Datei bekommt eine ID; der volle <file>-Block nur beim ersten Mal."""

    def __init__(self, timebase: int, ntsc: bool):
        self.seq_timebase, self.seq_ntsc = timebase, ntsc
        self.ids: dict[str, str] = {}
        self.master_ids: dict[str, str] = {}

    def masterclip_id(self, path: str) -> str | None:
        return self.master_ids.get(path)

    def xml(self, ev: dict) -> str:
        path = ev["datei"]
        if path in self.ids:
            return f'\t\t\t\t\t\t<file id="{self.ids[path]}"/>\n'
        fid = f"file-{len(self.ids) + 1}"
        self.ids[path] = fid
        f_tb, f_ntsc = _file_rate(ev, self.seq_timebase, self.seq_ntsc)
        dur = to_frames(float(ev["datei_dauer"]), exact_fps(f_tb, f_ntsc))
        video_xml = ""
        if ev.get("breite"):
            video_xml = (
                "\t\t\t\t\t\t\t\t<video>\n"
                "\t\t\t\t\t\t\t\t\t<samplecharacteristics>\n"
                f"\t\t\t\t\t\t\t\t\t\t<width>{ev['breite']}</width>\n"
                f"\t\t\t\t\t\t\t\t\t\t<height>{ev['hoehe']}</height>\n"
                "\t\t\t\t\t\t\t\t\t</samplecharacteristics>\n"
                "\t\t\t\t\t\t\t\t</video>\n"
            )
        audio_xml = ""
        kanaele = ev.get("audio_kanaele")
        if kanaele:
            rate = ev.get("audio_samplerate") or 48000
            audio_xml = (
                "\t\t\t\t\t\t\t\t<audio>\n"
                "\t\t\t\t\t\t\t\t\t<samplecharacteristics>\n"
                "\t\t\t\t\t\t\t\t\t\t<depth>16</depth>\n"
                f"\t\t\t\t\t\t\t\t\t\t<samplerate>{rate}</samplerate>\n"
                "\t\t\t\t\t\t\t\t\t</samplecharacteristics>\n"
                f"\t\t\t\t\t\t\t\t\t<channelcount>{kanaele}</channelcount>\n"
                "\t\t\t\t\t\t\t\t</audio>\n"
            )
        return (
            f'\t\t\t\t\t\t<file id="{fid}">\n'
            f"\t\t\t\t\t\t\t<name>{escape(ev['name'])}</name>\n"
            f"\t\t\t\t\t\t\t<pathurl>{escape(_pathurl(path))}</pathurl>\n"
            + _rate_xml(f_tb, f_ntsc, "\t\t\t\t\t\t\t")
            + f"\t\t\t\t\t\t\t<duration>{dur}</duration>\n"
            "\t\t\t\t\t\t\t<media>\n"
            + video_xml + audio_xml +
            "\t\t\t\t\t\t\t</media>\n"
            "\t\t\t\t\t\t</file>\n"
        )


def _masterclip(ev: dict, mid: str, files: _FileRegistry) -> str:
    """Masterclip (Projektfenster-Eintrag) für eine Mediendatei.

    Alle Sequenz-Clips derselben Datei verweisen per <masterclipid> hierauf;
    Premiere legt dadurch pro Datei genau EINEN Clip in der Ablage an,
    statt die Importe lose im Projektfenster zu verstreuen.
    """
    f_tb, f_ntsc = _file_rate(ev, files.seq_timebase, files.seq_ntsc)
    dur = to_frames(float(ev["datei_dauer"]), exact_fps(f_tb, f_ntsc))
    name = escape(ev["name"])
    media = ""
    if ev.get("breite"):
        media += (
            "\t\t\t\t\t\t\t<video>\n"
            "\t\t\t\t\t\t\t\t<track>\n"
            f'\t\t\t\t\t\t\t\t\t<clipitem id="{mid}-v1">\n'
            f"\t\t\t\t\t\t\t\t\t\t<name>{name}</name>\n"
            f"\t\t\t\t\t\t\t\t\t\t<masterclipid>{mid}</masterclipid>\n"
            + files.xml(ev)
            + "\t\t\t\t\t\t\t\t\t</clipitem>\n"
            "\t\t\t\t\t\t\t\t</track>\n"
            "\t\t\t\t\t\t\t</video>\n"
        )
    kanaele = min(int(ev.get("audio_kanaele") or 0), 2)
    if kanaele:
        tracks = ""
        for ch in range(1, kanaele + 1):
            tracks += (
                "\t\t\t\t\t\t\t\t<track>\n"
                f'\t\t\t\t\t\t\t\t\t<clipitem id="{mid}-a{ch}">\n'
                f"\t\t\t\t\t\t\t\t\t\t<name>{name}</name>\n"
                f"\t\t\t\t\t\t\t\t\t\t<masterclipid>{mid}</masterclipid>\n"
                + files.xml(ev) +
                "\t\t\t\t\t\t\t\t\t\t<sourcetrack>\n"
                "\t\t\t\t\t\t\t\t\t\t\t<mediatype>audio</mediatype>\n"
                f"\t\t\t\t\t\t\t\t\t\t\t<trackindex>{ch}</trackindex>\n"
                "\t\t\t\t\t\t\t\t\t\t</sourcetrack>\n"
                "\t\t\t\t\t\t\t\t\t</clipitem>\n"
                "\t\t\t\t\t\t\t\t</track>\n"
            )
        media += f"\t\t\t\t\t\t\t<audio>\n{tracks}\t\t\t\t\t\t\t</audio>\n"
    return (
        f'\t\t\t\t\t<clip id="{mid}" explodedTracks="true">\n'
        f"\t\t\t\t\t\t<name>{name}</name>\n"
        f"\t\t\t\t\t\t<duration>{dur}</duration>\n"
        + _rate_xml(f_tb, f_ntsc, "\t\t\t\t\t\t")
        + "\t\t\t\t\t\t<in>0</in>\n"
        f"\t\t\t\t\t\t<out>{dur}</out>\n"
        f"\t\t\t\t\t\t<masterclipid>{mid}</masterclipid>\n"
        "\t\t\t\t\t\t<ismasterclip>TRUE</ismasterclip>\n"
        f"\t\t\t\t\t\t<media>\n{media}\t\t\t\t\t\t</media>\n"
        "\t\t\t\t\t</clip>\n"
    )


def _clipitem(ev: dict, idx: int, files: _FileRegistry, seq_fps: float,
              mediatype: str, seq_w: int, seq_h: int,
              trackindex: int = 1) -> str:
    start = to_frames(ev["timeline_start"], seq_fps)
    end = to_frames(ev["timeline_start"] + ev["dauer"], seq_fps)
    # Quell-In/Out in der NATIVEN Framerate der Datei (Premiere conformt die
    # Datei auf ihre echte Rate; Frames in der falschen Rate träfen die
    # falsche Quellzeit, z.B. 50p-B-Roll in einer 25p-Sequenz).
    f_tb, f_ntsc = _file_rate(ev, files.seq_timebase, files.seq_ntsc)
    f_fps = exact_fps(f_tb, f_ntsc)
    src_in = to_frames(ev["src_in"], f_fps)
    src_out = src_in + to_frames(ev["dauer"], f_fps)
    filters = ""
    if mediatype == "video" and ev.get("breite") and ev.get("hoehe"):
        # Scale-to-fill x Auto-Zoom in EINEM Basic-Motion-Filter
        pct = max(seq_w / float(ev["breite"]), seq_h / float(ev["hoehe"])) * 100.0
        fahrt = ev.get("zoom_fahrt")
        if fahrt:
            filters += _scale_filter(keyframes=[
                (src_in, pct * float(fahrt[0])),
                (src_out, pct * float(fahrt[1])),
            ])
        else:
            pct *= float(ev.get("zoom") or 1.0)
            if abs(pct - 100.0) > 0.01:
                filters += _scale_filter(pct)
    if mediatype == "audio" and ev.get("gain_db") is not None:
        filters += _levels_filter(float(ev["gain_db"]))
    sourcetrack = ""
    if mediatype == "audio":
        sourcetrack = (
            "\t\t\t\t\t\t<sourcetrack>\n"
            "\t\t\t\t\t\t\t<mediatype>audio</mediatype>\n"
            f"\t\t\t\t\t\t\t<trackindex>{trackindex}</trackindex>\n"
            "\t\t\t\t\t\t</sourcetrack>\n"
        )
    master = files.masterclip_id(ev["datei"])
    master_line = (f"\t\t\t\t\t\t<masterclipid>{master}</masterclipid>\n"
                   if master else "")
    return (
        f'\t\t\t\t\t<clipitem id="clipitem-{idx}">\n'
        + master_line +
        f"\t\t\t\t\t\t<name>{escape(ev['name'])}</name>\n"
        "\t\t\t\t\t\t<enabled>TRUE</enabled>\n"
        f"\t\t\t\t\t\t<duration>{src_out - src_in}</duration>\n"
        + _rate_xml(f_tb, f_ntsc, "\t\t\t\t\t\t")
        + f"\t\t\t\t\t\t<start>{start}</start>\n"
        f"\t\t\t\t\t\t<end>{end}</end>\n"
        f"\t\t\t\t\t\t<in>{src_in}</in>\n"
        f"\t\t\t\t\t\t<out>{src_out}</out>\n"
        + files.xml(ev)
        + sourcetrack + filters +
        "\t\t\t\t\t</clipitem>\n"
    )


def generate_fcpxml(project: str, progress=None) -> Path:
    timeline = build_timeline(project)
    fps = timeline["fps"]
    timebase, ntsc = timeline["timebase"], timeline["ntsc"]
    seq_w, seq_h = timeline["breite"], timeline["hoehe"]

    files = _FileRegistry(timebase, ntsc)
    idx = 0

    def video_track(events: list[dict]) -> str:
        nonlocal idx
        items = ""
        for ev in sorted(events, key=lambda e: e["timeline_start"]):
            idx += 1
            items += _clipitem(ev, idx, files, fps, "video", seq_w, seq_h)
        return (f"\t\t\t\t<track>\n{items}"
                "\t\t\t\t\t<enabled>TRUE</enabled>\n"
                "\t\t\t\t\t<locked>FALSE</locked>\n"
                "\t\t\t\t</track>\n")

    def audio_track_group(events: list[dict], enabled: bool = True) -> str:
        """Stereo-Quellen brauchen zwei Spuren (trackindex 1/2), sonst
        importiert Premiere nur den linken Kanal."""
        nonlocal idx
        channels = 1
        for ev in events:
            channels = max(channels, min(int(ev.get("audio_kanaele") or 1), 2))
        out = ""
        en = "TRUE" if enabled else "FALSE"
        for ch in range(1, channels + 1):
            items = ""
            for ev in sorted(events, key=lambda e: e["timeline_start"]):
                if int(ev.get("audio_kanaele") or 1) < ch:
                    continue
                idx += 1
                items += _clipitem(ev, idx, files, fps, "audio", seq_w, seq_h,
                                   trackindex=ch)
            out += (f"\t\t\t\t<track>\n{items}"
                    f"\t\t\t\t\t<enabled>{en}</enabled>\n"
                    "\t\t\t\t\t<locked>FALSE</locked>\n"
                    "\t\t\t\t</track>\n")
        return out

    t = timeline["tracks"]

    # Masterclips ZUERST bauen: die vollen <file>-Definitionen landen damit
    # in der Ablage "Material", die Sequenz referenziert sie nur noch.
    vertreter: dict[str, dict] = {}
    for key in ("V1", "V2", "V3", "A1", "A2", "A3"):
        for ev in sorted(t[key], key=lambda e: e["timeline_start"]):
            vertreter.setdefault(ev["datei"], ev)
    master_xml = ""
    for pfad, ev in vertreter.items():
        mid = f"masterclip-{len(files.master_ids) + 1}"
        files.master_ids[pfad] = mid
        master_xml += _masterclip(ev, mid, files)

    video_tracks = video_track(t["V1"]) + video_track(t["V2"]) + video_track(t["V3"])
    audio_tracks = (audio_track_group(t["A1"])
                    + audio_track_group(t["A2"], enabled=False)  # Backup stumm
                    + audio_track_group(t["A3"]))

    duration = to_frames(timeline["dauer"], fps)
    ntsc_str = "TRUE" if ntsc else "FALSE"
    # Projekt > Ablage "<projekt> – autoedit" > (Ablage "Material" + Sequenz):
    # Premiere legt beim Import alles in diese eine Ablage, statt die Clips
    # lose ins Projektfenster zu streuen.
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="4">
\t<project>
\t\t<name>{escape(project)} – autoedit</name>
\t\t<children>
\t\t\t<bin>
\t\t\t\t<name>{escape(project)} – autoedit</name>
\t\t\t\t<children>
\t\t\t\t\t<bin>
\t\t\t\t\t\t<name>Material</name>
\t\t\t\t\t\t<children>
{master_xml}\t\t\t\t\t\t</children>
\t\t\t\t\t</bin>
\t<sequence id="sequence-1">
\t\t<name>{escape(timeline["name"])}</name>
\t\t<duration>{duration}</duration>
\t\t<rate>
\t\t\t<timebase>{timebase}</timebase>
\t\t\t<ntsc>{ntsc_str}</ntsc>
\t\t</rate>
\t\t<media>
\t\t\t<video>
\t\t\t\t<format>
\t\t\t\t\t<samplecharacteristics>
\t\t\t\t\t\t<rate>
\t\t\t\t\t\t\t<timebase>{timebase}</timebase>
\t\t\t\t\t\t\t<ntsc>{ntsc_str}</ntsc>
\t\t\t\t\t\t</rate>
\t\t\t\t\t\t<width>{seq_w}</width>
\t\t\t\t\t\t<height>{seq_h}</height>
\t\t\t\t\t\t<anamorphic>FALSE</anamorphic>
\t\t\t\t\t\t<pixelaspectratio>square</pixelaspectratio>
\t\t\t\t\t\t<fielddominance>none</fielddominance>
\t\t\t\t\t</samplecharacteristics>
\t\t\t\t</format>
{video_tracks}\t\t\t</video>
\t\t\t<audio>
\t\t\t\t<numOutputChannels>2</numOutputChannels>
{audio_tracks}\t\t\t</audio>
\t\t</media>
\t</sequence>
\t\t\t\t</children>
\t\t\t</bin>
\t\t</children>
\t</project>
</xmeml>
"""
    # Zeitstempel im Namen: jeder Export bleibt erhalten und ist in
    # Premiere eindeutig zuordenbar (alte Exporte manuell löschen).
    from datetime import datetime

    stempel = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out = paths.output_dir(project) / f"{project}_premiere_{stempel}.xml"
    out.write_text(xml, encoding="utf-8")
    if progress:
        progress(1.0, f"FCPXML geschrieben: {out.name}")
    return out


def latest_fcpxml(project: str) -> Path | None:
    """Neueste exportierte XML des Projekts (fürs Dashboard/Download)."""
    files = sorted(
        paths.output_dir(project).glob(f"{project}_premiere*.xml"),
        key=lambda f: f.stat().st_mtime,
    )
    return files[-1] if files else None
