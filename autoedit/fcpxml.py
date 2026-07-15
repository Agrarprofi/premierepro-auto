"""Phase 5: Export als FCP7-XML (xmeml v4) – importierbar in Premiere Pro
über Datei > Importieren.

Sequenzaufbau:
  V1: Kamera A (nach Segmenten geschnitten)
  V2: Kamera B (synchron, gleiche Schnitte)
  V3: B-Rolls an den zugeordneten Stellen
  A1: Referenz-Audio (DJI, synchron geschnitten)
  A2: Kamera-A-Ton, Spur deaktiviert (Backup)
  A3: Musik, -18 dB Startpegel

Alle Zeiten werden erst als Sekunden-Timeline gebaut (build_timeline) und dann
framegenau in die Sequenz-Timebase umgerechnet – das hält die Logik testbar.
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
    cam_a = by_role.get("cam_a", [])
    if not cam_a:
        raise RuntimeError("Kein Kamera-A-Material.")

    # Sequenzeinstellungen aus Kamera A ableiten
    src = cam_a[0]
    fps = float(src["fps"] or 25.0)
    timebase, ntsc = rate_for_fps(fps)
    if cfg["export_format"] == "9:16":
        seq_w, seq_h = 1080, 1920
    else:
        seq_w, seq_h = int(src["breite"] or 1920), int(src["hoehe"] or 1080)

    tracks: dict[str, list[dict]] = {
        "V1": [], "V2": [], "V3": [], "A1": [], "A2": [], "A3": [],
    }
    warnungen: list[str] = []
    ref_path = base / sync["referenz"]

    def _event(clip: dict, timeline_start: float, dauer: float, src_in: float,
               **extra) -> dict:
        return {
            "datei": str(base / clip["relpfad"]),
            "name": clip["name"],
            "timeline_start": round(timeline_start, 6),
            "dauer": round(dauer, 6),
            "src_in": round(src_in, 6),
            "datei_dauer": float(clip["dauer"]),
            "breite": clip.get("breite"),
            "hoehe": clip.get("hoehe"),
            **extra,
        }

    for seg in segs:
        t0 = seg["timeline_start"]
        for role, vtrack, atrack in (("cam_a", "V1", "A2"), ("cam_b", "V2", None)):
            hit = sync_audio.clip_for_ref_time(project, media, sync, role, seg["start"])
            if hit is None:
                if role == "cam_a":
                    warnungen.append(
                        f"Kein {role}-Clip für Segment ab {seg['start']:.2f} s"
                    )
                continue
            clip, src_t = hit
            dauer = min(seg["dauer"], float(clip["dauer"]) - src_t)
            if dauer < seg["dauer"] - 0.04:
                warnungen.append(
                    f"{role}-Clip {clip['name']} endet {seg['dauer'] - dauer:.2f} s "
                    f"vor Segmentende (Segment ab {seg['start']:.2f} s)"
                )
            ev = _event(clip, t0, dauer, src_t)
            tracks[vtrack].append(ev)
            if atrack and clip.get("audio_kanaele"):
                tracks[atrack].append(dict(ev))

        # A1: Referenz-Audio – Referenzzeit == Dateizeit der Referenzdatei
        ref_info = ffmpeg_utils.media_info(ref_path)
        a_dauer = min(seg["dauer"], max(0.0, ref_info["dauer"] - seg["start"]))
        if a_dauer > 0:
            tracks["A1"].append({
                "datei": str(ref_path),
                "name": ref_path.name,
                "timeline_start": round(t0, 6),
                "dauer": round(a_dauer, 6),
                "src_in": round(seg["start"], 6),
                "datei_dauer": ref_info["dauer"],
                "breite": None, "hoehe": None,
            })

    reel_ende = segs[-1]["timeline_ende"]

    # V3: B-Roll
    matches = broll.load_matches(project)
    if matches:
        broll_infos = {c["datei"]: c for c in (broll.load_broll_index(project)
                                               or {"clips": []})["clips"]}
        for m in matches["matches"]:
            info = broll_infos.get(m["broll_datei"])
            clip_path = base / m["broll_datei"]
            mi = ffmpeg_utils.media_info(clip_path) if info is None else None
            w = info.get("breite") if info else (mi or {}).get("breite")
            h = info.get("hoehe") if info else (mi or {}).get("hoehe")
            file_dauer = float(info["dauer"] if info else mi["dauer"])
            dauer = min(float(m["dauer"]), file_dauer - float(m["broll_einstieg"]))
            if info and (w is None or h is None):
                probe = ffmpeg_utils.media_info(clip_path)
                w, h = probe.get("breite"), probe.get("hoehe")
            tracks["V3"].append({
                "datei": str(clip_path),
                "name": Path(m["broll_datei"]).name,
                "timeline_start": float(m["transkript_zeit"]),
                "dauer": round(dauer, 6),
                "src_in": float(m["broll_einstieg"]),
                "datei_dauer": file_dauer,
                "breite": w, "hoehe": h,
            })

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
                    "breite": None, "hoehe": None,
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


def _scale_filter(scale_pct: float) -> str:
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
\t\t\t\t\t\t\t\t\t<value>{scale_pct:.4f}</value>
\t\t\t\t\t\t\t\t</parameter>
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


class _FileRegistry:
    """Jede Datei bekommt eine ID; der volle <file>-Block nur beim ersten Mal."""

    def __init__(self, timebase: int, ntsc: bool, fps: float):
        self.timebase, self.ntsc, self.fps = timebase, ntsc, fps
        self.ids: dict[str, str] = {}

    def xml(self, ev: dict, mediatype: str) -> str:
        path = ev["datei"]
        if path in self.ids:
            return f'\t\t\t\t\t\t<file id="{self.ids[path]}"/>\n'
        fid = f"file-{len(self.ids) + 1}"
        self.ids[path] = fid
        dur = to_frames(float(ev["datei_dauer"]), self.fps)
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
        audio_xml = (
            "\t\t\t\t\t\t\t\t<audio>\n"
            "\t\t\t\t\t\t\t\t\t<samplecharacteristics>\n"
            "\t\t\t\t\t\t\t\t\t\t<depth>16</depth>\n"
            "\t\t\t\t\t\t\t\t\t\t<samplerate>48000</samplerate>\n"
            "\t\t\t\t\t\t\t\t\t</samplecharacteristics>\n"
            "\t\t\t\t\t\t\t\t\t<channelcount>2</channelcount>\n"
            "\t\t\t\t\t\t\t\t</audio>\n"
        )
        return (
            f'\t\t\t\t\t\t<file id="{fid}">\n'
            f"\t\t\t\t\t\t\t<name>{escape(ev['name'])}</name>\n"
            f"\t\t\t\t\t\t\t<pathurl>{escape(_pathurl(path))}</pathurl>\n"
            + _rate_xml(self.timebase, self.ntsc, "\t\t\t\t\t\t\t")
            + f"\t\t\t\t\t\t\t<duration>{dur}</duration>\n"
            "\t\t\t\t\t\t\t<media>\n"
            + video_xml + audio_xml +
            "\t\t\t\t\t\t\t</media>\n"
            "\t\t\t\t\t\t</file>\n"
        )


def _clipitem(ev: dict, idx: int, files: _FileRegistry, fps: float,
              mediatype: str, seq_w: int, seq_h: int,
              scale_video: bool) -> str:
    start = to_frames(ev["timeline_start"], fps)
    end = to_frames(ev["timeline_start"] + ev["dauer"], fps)
    src_in = to_frames(ev["src_in"], fps)
    src_out = src_in + (end - start)
    filters = ""
    if mediatype == "video" and scale_video and ev.get("breite") and ev.get("hoehe"):
        pct = max(seq_w / float(ev["breite"]), seq_h / float(ev["hoehe"])) * 100.0
        if abs(pct - 100.0) > 0.01:
            filters += _scale_filter(pct)
    if mediatype == "audio" and ev.get("gain_db") is not None:
        filters += _levels_filter(float(ev["gain_db"]))
    sourcetrack = ""
    if mediatype == "audio":
        sourcetrack = (
            "\t\t\t\t\t\t<sourcetrack>\n"
            "\t\t\t\t\t\t\t<mediatype>audio</mediatype>\n"
            "\t\t\t\t\t\t\t<trackindex>1</trackindex>\n"
            "\t\t\t\t\t\t</sourcetrack>\n"
        )
    return (
        f'\t\t\t\t\t<clipitem id="clipitem-{idx}">\n'
        f"\t\t\t\t\t\t<name>{escape(ev['name'])}</name>\n"
        "\t\t\t\t\t\t<enabled>TRUE</enabled>\n"
        f"\t\t\t\t\t\t<duration>{end - start}</duration>\n"
        + _rate_xml(files.timebase, files.ntsc, "\t\t\t\t\t\t")
        + f"\t\t\t\t\t\t<start>{start}</start>\n"
        f"\t\t\t\t\t\t<end>{end}</end>\n"
        f"\t\t\t\t\t\t<in>{src_in}</in>\n"
        f"\t\t\t\t\t\t<out>{src_out}</out>\n"
        + files.xml(ev, mediatype)
        + sourcetrack + filters +
        "\t\t\t\t\t</clipitem>\n"
    )


def generate_fcpxml(project: str, progress=None) -> Path:
    timeline = build_timeline(project)
    fps = timeline["fps"]
    timebase, ntsc = timeline["timebase"], timeline["ntsc"]
    seq_w, seq_h = timeline["breite"], timeline["hoehe"]
    cfg = config.load_config(project)
    scale_video = cfg["export_format"] == "9:16"

    files = _FileRegistry(timebase, ntsc, fps)
    idx = 0

    def track_xml(events: list[dict], mediatype: str, enabled: bool = True,
                  locked: bool = False) -> str:
        nonlocal idx
        items = ""
        for ev in sorted(events, key=lambda e: e["timeline_start"]):
            idx += 1
            items += _clipitem(ev, idx, files, fps, mediatype, seq_w, seq_h,
                               scale_video)
        en = "TRUE" if enabled else "FALSE"
        lo = "TRUE" if locked else "FALSE"
        return (f"\t\t\t\t<track>\n{items}"
                f"\t\t\t\t\t<enabled>{en}</enabled>\n"
                f"\t\t\t\t\t<locked>{lo}</locked>\n"
                f"\t\t\t\t</track>\n")

    t = timeline["tracks"]
    video_tracks = (track_xml(t["V1"], "video")
                    + track_xml(t["V2"], "video")
                    + track_xml(t["V3"], "video"))
    audio_tracks = (track_xml(t["A1"], "audio")
                    + track_xml(t["A2"], "audio", enabled=False)  # Backup stumm
                    + track_xml(t["A3"], "audio"))

    duration = to_frames(timeline["dauer"], fps)
    ntsc_str = "TRUE" if ntsc else "FALSE"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="4">
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
</xmeml>
"""
    out = paths.output_dir(project) / f"{project}{FCPXML_SUFFIX}"
    out.write_text(xml, encoding="utf-8")
    if progress:
        progress(1.0, f"FCPXML geschrieben: {out.name}")
    return out
