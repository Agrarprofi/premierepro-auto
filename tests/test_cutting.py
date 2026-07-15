import pytest

from autoedit import cutting, ffmpeg_utils, ingest, paths, sync_audio, transcribe
from tests.conftest import FakeClaude, fake_transcriber


def _prep(projekt):
    ingest.scan_project(projekt)
    transcribe.transcribe_project(projekt, transcriber=fake_transcriber)
    sync_audio.compute_offsets(projekt)


def test_snap_to_words():
    words = [
        {"word": "eins", "start": 1.0, "end": 1.3},
        {"word": "zwei", "start": 1.4, "end": 1.7},
        {"word": "drei", "start": 2.0, "end": 2.4},
    ]
    # Bereich schneidet "zwei" und "drei" an -> auf deren Grenzen snappen
    s, e = cutting.snap_to_words(words, 1.5, 2.2)
    assert s == pytest.approx(1.4 - 0.15)
    assert e == pytest.approx(2.4 + 0.15)
    # Bereich ohne Wörter
    assert cutting.snap_to_words(words, 5.0, 6.0) is None
    # Padding klemmt nicht unter 0
    s, _ = cutting.snap_to_words(words, 0.9, 1.1, padding=2.0)
    assert s == 0.0


def test_select_segments_snaps_and_stores(projekt, fake_claude):
    _prep(projekt)
    result = cutting.select_segments(projekt, client=fake_claude)
    assert "reel_auswahl" in fake_claude.calls
    segs = result["segmente"]
    assert len(segs) == 2
    for seg in segs:
        assert seg["aktiv"] is True
        assert seg["ende"] > seg["start"]
        assert seg["text"]
    # Segment 1: Claude wollte 5.5–10.0; gesnappt auf Wortgrenzen ±0.15
    assert segs[0]["start"] == pytest.approx(5.5, abs=0.4)
    assert segs[0]["ende"] == pytest.approx(10.0, abs=0.5)
    assert cutting.load_segments(projekt)["segmente"] == segs


def test_update_segments_and_timeline(projekt, fake_claude):
    _prep(projekt)
    cutting.select_segments(projekt, client=fake_claude)
    data = cutting.load_segments(projekt)
    id1, id2 = (s["id"] for s in data["segmente"])

    # Reihenfolge tauschen, zweites (jetzt erstes) deaktivieren
    cutting.update_segments(projekt, order=[id2, id1], aktiv={id2: False})
    segs = cutting.enabled_segments(projekt)
    assert len(segs) == 1
    assert segs[0]["id"] == id1
    assert segs[0]["timeline_start"] == 0.0
    assert cutting.reel_dauer(projekt) == pytest.approx(segs[0]["dauer"])

    # Wieder aktivieren: Timeline hängt Segmente lückenlos aneinander
    cutting.update_segments(projekt, aktiv={id2: True})
    segs = cutting.enabled_segments(projekt)
    assert len(segs) == 2
    assert segs[1]["timeline_start"] == pytest.approx(segs[0]["dauer"])


def test_select_segments_requires_script_in_script_mode(projekt, fake_claude):
    _prep(projekt)
    with pytest.raises(ValueError):
        cutting.select_segments(projekt, modus="skript", skript="  ",
                                client=fake_claude)


def test_render_cut_preview(projekt, fake_claude):
    _prep(projekt)
    cutting.select_segments(projekt, client=fake_claude)
    out = cutting.render_cut_preview(projekt)
    assert out.is_file()
    info = ffmpeg_utils.media_info(out)
    erwartet = cutting.reel_dauer(projekt)
    assert abs(info["dauer"] - erwartet) < 0.6
    assert info["hoehe"] == 720
