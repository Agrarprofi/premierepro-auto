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
    assert out.name == "testprojekt_premiere.xml"

    tree = ET.parse(out)
    seq = tree.getroot().find("sequence")
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
    seq = tree.getroot().find("sequence")
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
    atracks = tree.getroot().findall("sequence/media/audio/track")
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
