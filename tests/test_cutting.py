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


def test_find_take_groups_detects_retakes():
    """Falschstart + Wiederholung nach Pause -> gleiche Take-Gruppe,
    letzter Anlauf ist der finale."""
    segmente = [
        {"start": 0.0, "end": 2.0, "text": "Die Kartoffelernte war heuer"},
        {"start": 6.0, "end": 10.0,
         "text": "Die Kartoffelernte war heuer richtig gut weil der Boden passt"},
        {"start": 12.0, "end": 14.0, "text": "Der Traktor fährt mit GPS"},
    ]
    takes = cutting.find_take_groups(segmente)
    assert 0 in takes and 1 in takes
    assert takes[0]["gruppe"] == takes[1]["gruppe"]
    assert takes[0]["final"] is False
    assert takes[1]["final"] is True
    assert 2 not in takes  # inhaltlich anders -> keine Gruppe


def test_find_take_groups_ignores_short_fillers():
    segmente = [
        {"start": 0.0, "end": 0.5, "text": "ja genau"},
        {"start": 2.0, "end": 2.5, "text": "ja genau"},
    ]
    assert cutting.find_take_groups(segmente) == {}


def test_annotate_transcript_markers():
    transcript = {"segmente": [
        {"start": 0.0, "end": 2.0, "text": "Die Ernte war heuer sehr gut"},
        # 4 s Sprechpause, dann Wiederholung
        {"start": 6.0, "end": 9.0, "text": "Die Ernte war heuer sehr gut sag ich"},
    ]}
    text = cutting.annotate_transcript(transcript)
    assert "⏸ Sprechpause 4.0 s" in text
    assert "⟳ erster Anlauf" in text
    assert "⟳ finaler Take" in text


def test_analyze_statements(projekt, fake_claude):
    _prep(projekt)
    result = cutting.analyze_statements(projekt, client=fake_claude)
    assert "aussagen_analyse" in fake_claude.calls
    aussagen = result["aussagen"]
    assert len(aussagen) == 2
    # nach Punkten sortiert
    assert aussagen[0]["punkte"] >= aussagen[1]["punkte"]
    assert aussagen[0]["kategorie"] == "hook"
    gespeichert = cutting.load_statements(projekt)
    assert gespeichert["aussagen"] == aussagen
    # Analyse-Prompt enthält die Rohmaterial-Regeln
    assert "AUSSCHLIESSLICH" in fake_claude.prompts["aussagen_analyse"]


def test_select_segments_uses_analysis_and_hints(projekt, fake_claude):
    from autoedit import config

    _prep(projekt)
    config.save_config(projekt, {"schnitt_hinweise": "Fokus auf Bodengesundheit"})
    cutting.select_segments(projekt, client=fake_claude)

    # Analyse lief automatisch vor der Auswahl
    assert fake_claude.calls.index("aussagen_analyse") < \
        fake_claude.calls.index("reel_auswahl")
    prompt = fake_claude.prompts["reel_auswahl"]
    assert "Vorab-Analyse der stärksten Aussagen" in prompt
    assert "AUSSCHLIESSLICH" in prompt          # Take-Regel
    assert "Fokus auf Bodengesundheit" in prompt  # Nutzer-Hinweise
    assert cutting.load_statements(projekt) is not None


def test_select_segments_without_analysis(projekt, fake_claude):
    _prep(projekt)
    cutting.select_segments(projekt, client=fake_claude, analyse=False)
    assert "aussagen_analyse" not in fake_claude.calls
