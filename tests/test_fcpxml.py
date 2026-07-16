import xml.etree.ElementTree as ET

import pytest

from autoedit import config, cutting, fcpxml, music, paths
from tests.conftest import prepared_project


def test_rate_for_fps():
    assert fcpxml.rate_for_fps(25.0) == (25, False)
    assert fcpxml.rate_for_fps(23.976) == (24, True)
    assert fcpxml.rate_for_fps(29.97) == (30, True)
    assert fcpxml.rate_for_fps(30.0) == (30, False)
    assert fcpxml.rate_for_fps(50.0) == (50, False)
    assert fcpxml.rate_for_fps(59.94) == (60, True)
    assert fcpxml.exact_fps(30, True) == pytest.approx(29.97, abs=0.001)


def _clipitems(track):
    return track.findall("clipitem")


def test_generate_fcpxml_frame_accuracy(projekt, fake_claude):
    prepared_project(projekt, fake_claude)
    out = fcpxml.generate_fcpxml(projekt)
    assert out.name.startswith("testprojekt_premiere_")
    assert out.name.endswith(".xml")

    tree = ET.parse(out)
    seq = tree.getroot().find(".//sequence")
    assert seq.find("rate/timebase").text == "25"
    assert seq.find("rate/ntsc").text == "FALSE"
    fmt = seq.find("media/video/format/samplecharacteristics")
    assert fmt.find("width").text == "640"
    assert fmt.find("height").text == "360"

    vtracks = seq.findall("media/video/track")
    atracks = seq.findall("media/audio/track")
    assert len(vtracks) == 3 and len(atracks) == 3

    segs = cutting.enabled_segments(projekt)
    v1 = _clipitems(vtracks[0])
    assert len(v1) == len(segs) == 2

    for item, seg in zip(v1, segs):
        start = int(item.find("start").text)
        end = int(item.find("end").text)
        src_in = int(item.find("in").text)
        src_out = int(item.find("out").text)
        # Framegenau: Timeline-Position = round(sek * 25)
        assert start == round(seg["timeline_start"] * 25)
        assert end == round((seg["timeline_start"] + seg["dauer"]) * 25)
        assert src_out - src_in == end - start
        # Quell-In = Segmentstart minus Kamera-Offset (~3 s -> 25 fps)
        assert src_in == pytest.approx(round((seg["start"] - 3.0) * 25), abs=1)

    # Segmente stoßen lückenlos aneinander
    assert int(v1[1].find("start").text) == int(v1[0].find("end").text)

    # V2 = Kamera B synchron, gleiche Timeline-Schnitte
    v2 = _clipitems(vtracks[1])
    assert len(v2) == 2
    assert [i.find("start").text for i in v2] == [i.find("start").text for i in v1]

    # V3 = B-Roll an der zugeordneten Stelle (t=4.0 -> Frame 100, 2 s = 50 Frames)
    v3 = _clipitems(vtracks[2])
    assert len(v3) == 1
    assert int(v3[0].find("start").text) == 100
    assert int(v3[0].find("end").text) == 150

    # A1 = DJI (src_in == Referenzzeit), A2 = Kamera-Ton, Spur deaktiviert
    a1 = _clipitems(atracks[0])
    assert len(a1) == 2
    assert int(a1[0].find("in").text) == round(segs[0]["start"] * 25)
    assert atracks[1].find("enabled").text == "FALSE"
    assert len(_clipitems(atracks[1])) == 2

    # Pfade absolut als file://localhost
    for pathurl in tree.getroot().iter("pathurl"):
        assert pathurl.text.startswith("file://localhost/")


def test_fcpxml_9_16_scaling(projekt, fake_claude):
    prepared_project(projekt, fake_claude, with_broll=False)
    config.save_config(projekt, {"export_format": "9:16"})
    out = fcpxml.generate_fcpxml(projekt)
    tree = ET.parse(out)
    seq = tree.getroot().find(".//sequence")
    fmt = seq.find("media/video/format/samplecharacteristics")
    assert fmt.find("width").text == "1080"
    assert fmt.find("height").text == "1920"

    item = seq.find("media/video/track/clipitem")
    params = {p.find("parameterid").text: p.find("value").text
              for p in item.iter("parameter")}
    # 640x360 -> 1080x1920: Skalierung = max(1080/640, 1920/360)*100
    assert float(params["scale"]) == pytest.approx(1920 / 360 * 100, abs=0.1)


def test_fcpxml_music_track_with_level(projekt, fake_claude, music_lib):
    prepared_project(projekt, fake_claude, with_broll=False)
    config.save_config(projekt, {"musik_aktiv": True})
    music.set_track(projekt, "ruhig_akustik.wav")
    out = fcpxml.generate_fcpxml(projekt)
    tree = ET.parse(out)
    atracks = tree.getroot().findall(".//sequence/media/audio/track")
    a3 = _clipitems(atracks[2])
    assert len(a3) == 1
    assert int(a3[0].find("start").text) == 0
    params = {p.find("parameterid").text: p.find("value").text
              for p in a3[0].iter("parameter")}
    # -18 dB -> 10^(-18/20) ≈ 0.1259
    assert float(params["level"]) == pytest.approx(0.12589, abs=0.001)


def test_build_timeline_warns_on_missing_coverage(projekt, fake_claude):
    from tests.conftest import FakeClaude
    fake = FakeClaude(reel_segments=[
        {"start": 5.5, "ende": 10.0, "text": "x", "begruendung": ""}])
    prepared_project(projekt, fake, with_broll=False)
    # Kamera B beginnt erst bei 5.0 -> Segment ab 5.35 liegt drin; kein Fehler
    timeline = fcpxml.build_timeline(projekt)
    assert timeline["dauer"] == pytest.approx(cutting.reel_dauer(projekt))
    assert (paths.output_dir(projekt) / "timeline.json").is_file()


def test_fcpxml_stereo_audio_becomes_track_pair(projekt, fake_claude, media):
    """Stereo-DJI (2 Sender!) muss als zwei Spuren mit trackindex 1/2
    exportiert werden, sonst verliert Premiere den rechten Kanal."""
    import numpy as np
    from scipy.io import wavfile
    from tests.conftest import SR

    base = paths.project_dir(projekt)
    ref = media["ref"]
    stereo = np.stack([ref, ref * 0.5], axis=1)
    wavfile.write(base / "input/audio_dji/dji.wav", SR,
                  (stereo * 32767).astype(np.int16))

    prepared_project(projekt, fake_claude, with_broll=False)
    out = fcpxml.generate_fcpxml(projekt)
    tree = ET.parse(out)
    atracks = tree.getroot().findall(".//sequence/media/audio/track")
    # A1-Gruppe = 2 Spuren (stereo), A2 = 1 (Kamera mono), A3 = 1 (leer)
    assert len(atracks) == 4

    def indices(track):
        return [ci.find("sourcetrack/trackindex").text
                for ci in track.findall("clipitem")]

    assert set(indices(atracks[0])) == {"1"}
    assert set(indices(atracks[1])) == {"2"}
    assert len(indices(atracks[0])) == len(indices(atracks[1])) == 2
    # A2 (Kamera-Backup) bleibt deaktiviert
    assert atracks[2].find("enabled").text == "FALSE"
    # Datei deklariert die echten 2 Kanäle
    for f in tree.getroot().iter("file"):
        name = f.find("name")
        if name is not None and name.text == "dji.wav":
            assert f.find("media/audio/channelcount").text == "2"
            break
    else:
        raise AssertionError("dji.wav-Fileblock nicht gefunden")


def test_fcpxml_video_without_audio_declares_no_audio(projekt, fake_claude):
    """B-Roll ohne Tonspur darf im <file>-Block kein Audio deklarieren."""
    prepared_project(projekt, fake_claude)  # B-Roll-Clips sind tonlos
    out = fcpxml.generate_fcpxml(projekt)
    tree = ET.parse(out)
    for f in tree.getroot().iter("file"):
        name = f.find("name")
        if name is not None and name.text == "broll_traktor.mp4":
            assert f.find("media/audio") is None
            assert f.find("media/video") is not None
            return
    raise AssertionError("broll_traktor.mp4-Fileblock nicht gefunden")


def test_fcpxml_mixed_framerate_and_resolution_broll(projekt, fake_claude, media):
    """50p-B-Roll in 320x180 in einer 25p/640x360-Sequenz:
    Quell-In/Out in der nativen 50p-Rate, Scale-to-fill auch im
    'quelle'-Export."""
    from autoedit import broll, ingest
    from tests.conftest import make_broll

    prepared_project(projekt, fake_claude, with_broll=False)
    base = paths.project_dir(projekt)
    make_broll(base / "input/broll/broll50.mp4", rate=50, size="320x180")
    ingest.scan_project(projekt)  # neue Datei erfassen
    broll.save_matches(projekt, {"projekt": projekt, "matches": [{
        "id": "m50", "transkript_zeit": 4.0,
        "broll_datei": "input/broll/broll50.mp4",
        "broll_einstieg": 1.0, "dauer": 2.0,
        "begruendung": "", "thumbnail": None,
    }]})

    out = fcpxml.generate_fcpxml(projekt)
    tree = ET.parse(out)
    v3 = tree.getroot().findall(".//sequence/media/video/track")[2]
    item = v3.find("clipitem")
    assert item is not None
    # Timeline-Frames in Sequenzrate (25), Quell-Frames in Dateirate (50)
    assert item.find("rate/timebase").text == "50"
    assert int(item.find("start").text) == 100   # 4.0 s * 25
    assert int(item.find("end").text) == 150     # 6.0 s * 25
    assert int(item.find("in").text) == 50       # 1.0 s * 50
    assert int(item.find("out").text) == 150     # (1.0+2.0) s * 50
    # Scale-to-fill trotz export_format="quelle": max(640/320, 360/180)*100
    params = {p.find("parameterid").text: p.find("value").text
              for p in item.iter("parameter")}
    assert float(params["scale"]) == pytest.approx(200.0, abs=0.1)
    # Datei-Block traegt die native Rate (volle Definition liegt im
    # Masterclip, der Sequenz-Clip referenziert sie nur per id)
    fid = item.find("file").get("id")
    volle = [f for f in tree.getroot().iter("file")
             if f.get("id") == fid and f.find("rate") is not None]
    assert len(volle) == 1
    assert volle[0].find("rate/timebase").text == "50"


def test_av_versatz_compensation(env, media, tmp_path):
    """Kameradatei mit versetzter Audiospur im Container (Edit-List):
    der Ton-Offset stimmt, aber Premiere zählt Frames ab dem ersten
    Videobild – ohne Kompensation zeigen V1/V2 verschiedene Momente."""
    import json
    import shutil
    from autoedit import ffmpeg_utils, ingest, sync_audio
    from tests.conftest import (CAM_A_START, FakeClaude, fake_transcriber,
                                make_camera)
    from autoedit import cutting, transcribe

    name = "avversatz"
    paths.create_project(name)
    base = paths.project_dir(name)
    shutil.copy(media["root"] / "dji.wav", base / "input/audio_dji/dji.wav")
    # Kamera A: Audiospur im Container 0.5 s nach hinten versetzt
    make_camera(base / "input/cam_a/cam_delay.mp4", media["ref"],
                CAM_A_START, 24.0, "aac", audio_delay=0.5)

    info = ffmpeg_utils.media_info(base / "input/cam_a/cam_delay.mp4")
    # ~0.5 s itsoffset minus AAC-Encoder-Priming (~64 ms) = Container-Wert
    assert -0.55 < info["av_versatz"] < -0.35

    ingest.scan_project(name)
    transcribe.transcribe_project(name, transcriber=fake_transcriber)
    sync_audio.compute_offsets(name)
    fake = FakeClaude(reel_segments=[
        {"start": 6.0, "ende": 10.0, "text": "x", "begruendung": ""}])
    cutting.select_segments(name, client=fake)
    timeline = fcpxml.build_timeline(name)

    seg = cutting.enabled_segments(name)[0]
    sync = sync_audio.load_sync(name)
    offset = sync["offsets"]["input/cam_a/cam_delay.mp4"]["offset_sekunden"]
    ev = timeline["tracks"]["V1"][0]
    # src_in = Audio-Zeit MINUS AV-Versatz (hier -0.5 -> +0.5 s später)
    erwartet = (seg["start"] - offset) - info["av_versatz"]
    assert ev["src_in"] == pytest.approx(erwartet, abs=0.02)
    assert ev["av_versatz"] == pytest.approx(info["av_versatz"], abs=0.001)
    # A2 (Kamera-Ton) bekommt dieselbe Korrektur -> bleibt bildsynchron
    a2 = timeline["tracks"]["A2"][0]
    assert a2["src_in"] == pytest.approx(erwartet, abs=0.02)


def test_no_av_versatz_for_clean_files(projekt, fake_claude):
    """Normale Dateien (Spuren starten gemeinsam): keine Korrektur."""
    prepared_project(projekt, fake_claude, with_broll=False)
    timeline = fcpxml.build_timeline(projekt)
    for ev in timeline["tracks"]["V1"]:
        assert "av_versatz" not in ev or abs(ev["av_versatz"]) < 0.05


def test_export_warns_on_stale_broll_matches(projekt, fake_claude):
    """Schnitt nach dem B-Roll-Matching geändert -> Zeiten passen nicht
    mehr, der Export warnt."""
    prepared_project(projekt, fake_claude, with_broll=True)
    timeline = fcpxml.build_timeline(projekt)
    assert not any("Schnitt-Stand" in w for w in timeline["warnungen"])

    # Segment deaktivieren -> Timeline ändert sich
    data = cutting.load_segments(projekt)
    cutting.update_segments(projekt, aktiv={data["segmente"][0]["id"]: False})
    timeline = fcpxml.build_timeline(projekt)
    assert any("Schnitt-Stand" in w for w in timeline["warnungen"])


def test_video_korrektur_shifts_only_video(projekt, fake_claude):
    """Manuelle Bild-Korrektur: das Bild (V1) rückt in der Quelle nach
    hinten, der Kamera-Ton (A2) und Kamera B bleiben unverändert."""
    from autoedit import sync_audio

    prepared_project(projekt, fake_claude, with_broll=False)
    vorher = fcpxml.build_timeline(projekt)

    sync_audio.set_video_korrektur(projekt, "input/cam_a/cam_a_001.mov", 0.25)
    nachher = fcpxml.build_timeline(projekt)

    assert len(nachher["tracks"]["V1"]) == len(vorher["tracks"]["V1"]) > 0
    for alt, neu in zip(vorher["tracks"]["V1"], nachher["tracks"]["V1"]):
        assert neu["src_in"] == pytest.approx(alt["src_in"] + 0.25, abs=0.001)
        assert neu["video_korrektur"] == 0.25
        assert neu["timeline_start"] == alt["timeline_start"]
    for alt, neu in zip(vorher["tracks"]["A2"], nachher["tracks"]["A2"]):
        assert neu["src_in"] == pytest.approx(alt["src_in"], abs=0.001)
    for alt, neu in zip(vorher["tracks"]["V2"], nachher["tracks"]["V2"]):
        assert neu["src_in"] == pytest.approx(alt["src_in"], abs=0.001)


def test_export_warns_on_vfr(projekt, fake_claude):
    """VFR-Verdacht auf Kamera B -> genau eine Warnung im Export (nicht
    pro Segment wiederholt)."""
    import json

    prepared_project(projekt, fake_claude, with_broll=False)
    info_file = paths.output_dir(projekt) / "media_info.json"
    media = json.loads(info_file.read_text(encoding="utf-8"))
    for clip in media["clips"]:
        if clip["relpfad"] == "input/cam_b/cam_b_001.mp4":
            clip["vfr_verdacht"] = True
    info_file.write_text(json.dumps(media, ensure_ascii=False),
                         encoding="utf-8")

    timeline = fcpxml.build_timeline(projekt)
    vfr = [w for w in timeline["warnungen"] if "VARIABLE Framerate" in w]
    assert len(vfr) == 1
    assert vfr[0].startswith("input/cam_b/cam_b_001.mp4")


def test_timeline_uses_cfr_copy(projekt, fake_claude):
    """Hat eine Datei eine CFR-Kopie, zeigen alle Export-Events (Bild UND
    Ton) auf die Kopie statt auf das VFR-Original."""
    import json
    import shutil

    prepared_project(projekt, fake_claude, with_broll=False)

    # CFR-Kopie simulieren (inhaltlich identische Datei genügt)
    base = paths.project_dir(projekt)
    cfr_rel = "output/cfr/cam_b/cam_b_001.mp4"
    (base / cfr_rel).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(base / "input/cam_b/cam_b_001.mp4", base / cfr_rel)

    info_file = paths.output_dir(projekt) / "media_info.json"
    media = json.loads(info_file.read_text(encoding="utf-8"))
    for clip in media["clips"]:
        if clip["relpfad"] == "input/cam_b/cam_b_001.mp4":
            clip["cfr_pfad"] = cfr_rel
            clip["vfr_original"] = True
    info_file.write_text(json.dumps(media, ensure_ascii=False),
                         encoding="utf-8")

    timeline = fcpxml.build_timeline(projekt)
    for ev in timeline["tracks"]["V2"]:
        assert ev["datei"].endswith(cfr_rel)
    for ev in timeline["tracks"]["V1"]:          # Kamera A unverändert
        assert ev["datei"].endswith("input/cam_a/cam_a_001.mov")
    # keine VFR-Warnung, die Datei ist ja gewandelt
    assert not any("VARIABLE Framerate" in w for w in timeline["warnungen"])

    # und die XML referenziert die Kopie
    out = fcpxml.generate_fcpxml(projekt)
    assert "output/cfr/cam_b/cam_b_001.mp4" in out.read_text(encoding="utf-8")


def test_fcpxml_bin_structure_and_masterclips(projekt, fake_claude):
    """Der Import landet in einer eigenen Ablage: Projekt > Bin
    '<projekt> – autoedit' > (Bin 'Material' mit einem Masterclip pro
    Datei + Sequenz); alle Sequenz-Clips verweisen auf die Masterclips."""
    prepared_project(projekt, fake_claude)
    out = fcpxml.generate_fcpxml(projekt)
    tree = ET.parse(out)
    root = tree.getroot()

    proj = root.find("project")
    assert proj is not None
    bins = proj.findall(".//bin")
    names = [b.findtext("name") for b in bins]
    assert f"{projekt} – autoedit" in names
    assert "Material" in names

    # ein Masterclip pro Mediendatei, mit ismasterclip
    clips = proj.findall(".//bin//clip")
    clip_namen = {c.findtext("name") for c in clips}
    assert {"cam_a_001.mov", "cam_b_001.mp4", "dji.wav",
            "broll_traktor.mp4"} <= clip_namen
    for c in clips:
        assert c.findtext("ismasterclip") == "TRUE"
        assert c.findtext("masterclipid") == c.get("id")

    # Sequenz liegt in derselben Ablage; jeder Sequenz-Clip verweist auf
    # einen Masterclip
    seq = proj.find(".//bin/children/sequence")
    assert seq is not None
    master_ids = {c.get("id") for c in clips}
    for ci in seq.iter("clipitem"):
        assert ci.findtext("masterclipid") in master_ids

    # volle <file>-Definition (mit pathurl) existiert genau einmal je Datei
    volle = [f for f in root.iter("file") if f.find("pathurl") is not None]
    urls = [f.findtext("pathurl") for f in volle]
    assert len(urls) == len(set(urls))


def _scale_param(item):
    for p in item.iter("parameter"):
        if p.findtext("parameterid") == "scale":
            return p
    return None


def test_auto_zoom_wechsel(projekt, fake_claude):
    """Default 'wechsel': Segment 1 ohne Zoom, Segment 2 mit Punch-In
    (108%) - auf V1 UND V2, damit der Kamerawechsel konsistent bleibt."""
    prepared_project(projekt, fake_claude, with_broll=False)
    out = fcpxml.generate_fcpxml(projekt)
    tree = ET.parse(out)
    for track_idx in (0, 1):
        items = tree.getroot().findall(
            ".//sequence/media/video/track")[track_idx].findall("clipitem")
        assert _scale_param(items[0]) is None          # Quelle == Sequenz
        p = _scale_param(items[1])
        assert p is not None
        assert float(p.findtext("value")) == pytest.approx(108.0, abs=0.1)


def test_auto_zoom_sanft_keyframes(projekt, fake_claude):
    """'sanft': Keyframe-Fahrt über das Segment, abwechselnd rein/raus;
    Keyframe-Zeiten liegen auf in/out des Clips (Quell-Frames)."""
    config.save_config(projekt, {"zoom_modus": "sanft",
                                 "zoom_staerke_prozent": 10})
    prepared_project(projekt, fake_claude, with_broll=False)
    out = fcpxml.generate_fcpxml(projekt)
    tree = ET.parse(out)
    items = tree.getroot().findall(
        ".//sequence/media/video/track")[0].findall("clipitem")

    def fahrt(item):
        p = _scale_param(item)
        kfs = [(int(k.findtext("when")), float(k.findtext("value")))
               for k in p.findall("keyframe")]
        assert kfs[0][0] == int(item.findtext("in"))
        assert kfs[-1][0] == int(item.findtext("out"))
        return kfs[0][1], kfs[-1][1]

    v1, b1 = fahrt(items[0])   # Segment 1: rein zoomen
    assert (v1, b1) == (pytest.approx(100.0, abs=0.1),
                        pytest.approx(110.0, abs=0.1))
    v2, b2 = fahrt(items[1])   # Segment 2: wieder raus
    assert (v2, b2) == (pytest.approx(110.0, abs=0.1),
                        pytest.approx(100.0, abs=0.1))


def test_auto_zoom_aus_and_916_kombination(projekt, fake_claude):
    """zoom 'aus' -> kein Filter; 9:16 + wechsel -> Zoom multipliziert
    sich in den Scale-to-fill."""
    config.save_config(projekt, {"zoom_modus": "aus"})
    prepared_project(projekt, fake_claude, with_broll=False)
    out = fcpxml.generate_fcpxml(projekt)
    tree = ET.parse(out)
    items = tree.getroot().findall(
        ".//sequence/media/video/track")[0].findall("clipitem")
    assert all(_scale_param(i) is None for i in items)

    config.save_config(projekt, {"zoom_modus": "wechsel",
                                 "zoom_staerke_prozent": 8,
                                 "export_format": "9:16"})
    out = fcpxml.generate_fcpxml(projekt)
    tree = ET.parse(out)
    items = tree.getroot().findall(
        ".//sequence/media/video/track")[0].findall("clipitem")
    fill = 1920 / 360 * 100
    assert float(_scale_param(items[0]).findtext("value")) == \
        pytest.approx(fill, abs=0.1)
    assert float(_scale_param(items[1]).findtext("value")) == \
        pytest.approx(fill * 1.08, abs=0.5)


def test_sequenz_rate_aus_config(projekt, fake_claude):
    """Die Sequenz läuft mit der konfigurierten Rate; Kameras, deren
    Rate nicht passt, lösen eine Export-Warnung aus."""
    prepared_project(projekt, fake_claude, with_broll=False)
    timeline = fcpxml.build_timeline(projekt)
    assert timeline["timebase"] == 25          # Default sequenz_fps=25
    assert not any("Misch-Raten" in w for w in timeline["warnungen"])

    # media_info manipulieren: Kamera B angeblich 50 fps -> Warnung
    import json
    f = paths.output_dir(projekt) / "media_info.json"
    media = json.loads(f.read_text(encoding="utf-8"))
    for clip in media["clips"]:
        if clip["rolle"] == "cam_b":
            clip["fps"] = 50.0
    f.write_text(json.dumps(media, ensure_ascii=False), encoding="utf-8")
    timeline = fcpxml.build_timeline(projekt)
    assert any("passt nicht zur Sequenz" in w for w in timeline["warnungen"])
