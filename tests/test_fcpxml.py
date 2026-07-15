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
    atracks = tree.getroot().findall("sequence/media/audio/track")
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
    v3 = tree.getroot().findall("sequence/media/video/track")[2]
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
    # Datei-Block traegt die native Rate
    fileblock = item.find("file")
    assert fileblock.find("rate/timebase").text == "50"
