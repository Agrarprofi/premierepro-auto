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
    # Segment 1: Claude wollte 5.5–10.0; gesnappt auf Wortgrenzen ±0.15.
    # Das Ende darf kürzer ausfallen: der Stille-Trimmer stutzt Lücken im
    # synthetischen Burst-Audio (Detailtests in test_trim_silence_*).
    assert segs[0]["start"] == pytest.approx(5.5, abs=0.4)
    assert 7.5 < segs[0]["ende"] <= 10.2
    assert segs[0]["dauer"] == pytest.approx(
        segs[0]["ende"] - segs[0]["start"], abs=0.01)
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
    assert "⟳ weiterer Anlauf" in text
    assert "⟳ letzter Take" in text


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
    assert "SAUBERSTEN Take" in fake_claude.prompts["aussagen_analyse"]


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
    assert "SAUBERSTEN Take" in prompt          # Take-Regel
    assert "Fokus auf Bodengesundheit" in prompt  # Nutzer-Hinweise
    assert cutting.load_statements(projekt) is not None


def test_select_segments_without_analysis(projekt, fake_claude):
    _prep(projekt)
    cutting.select_segments(projekt, client=fake_claude, analyse=False)
    assert "aussagen_analyse" not in fake_claude.calls


def _take_kombi_fake():
    from tests.conftest import FakeClaude
    return FakeClaude(reel_segments=[
        {"start": 5.5, "ende": 10.0, "text": "Teil 1", "begruendung": "",
         "fortsetzung": False},
        {"start": 15.0, "ende": 18.0, "text": "Teil 2 aus anderem Take",
         "begruendung": "Satzende sauberer", "fortsetzung": True},
    ])


def test_take_combination_tightens_join(projekt):
    """Zwei Takes verschnitten (fortsetzung=true): an der Naht wird das
    Padding entfernt, die Grenzen liegen exakt auf Wortgrenzen."""
    fake = _take_kombi_fake()
    _prep(projekt)
    cutting.select_segments(projekt, client=fake)
    s1, s2 = cutting.load_segments(projekt)["segmente"]
    assert s2["fortsetzung"] is True

    transcript = transcribe.load_transcript(projekt)
    word_starts = {w["start"] for w in transcript["woerter"]}
    word_ends = {w["end"] for w in transcript["woerter"]}
    # Nahtstelle ohne Padding: exakt auf Wortgrenzen
    assert s1["ende"] in word_ends
    assert s2["start"] in word_starts
    # Normale Kanten behalten ihr Padding (liegen NICHT auf der Wortgrenze)
    assert s1["start"] not in word_starts
    assert s2["ende"] not in word_ends

    # Take-Übergang liegt am Timeline-Start des zweiten Teils
    joins = cutting.take_joins(projekt)
    assert len(joins) == 1
    assert joins[0] == pytest.approx(s1["dauer"], abs=0.01)


def test_prompt_allows_take_combination(projekt, fake_claude):
    _prep(projekt)
    cutting.select_segments(projekt, client=fake_claude)
    prompt = fake_claude.prompts["reel_auswahl"]
    assert "KOMBINIERT" in prompt
    assert '"fortsetzung"' in prompt
    assert "SAUBERSTEN Take" in prompt


def test_broll_prompt_covers_take_joins(projekt):
    from autoedit import broll
    from tests.conftest import prepared_project

    fake = _take_kombi_fake()
    prepared_project(projekt, fake, with_broll=True)
    prompt = fake.prompts["broll_matching"]
    assert "Take-Übergänge" in prompt
    assert "verdecken" in prompt


# ------------------------------------------------------------ Stille-Trimmer

def test_speech_bounds():
    import numpy as np

    sr = 16000
    rng = np.random.default_rng(1)
    # Leises Grundrauschen, "Sprache" (lautes Rauschen) von 4.0 bis 6.5 s
    wav = rng.standard_normal(sr * 10).astype(np.float32) * 0.002
    wav[int(4.0 * sr):int(6.5 * sr)] += \
        rng.standard_normal(int(2.5 * sr)).astype(np.float32) * 0.3

    bounds = cutting.speech_bounds(wav, sr, 0.0, 10.0)
    assert bounds is not None
    erste, letzte = bounds
    assert erste == pytest.approx(4.0, abs=0.1)
    assert letzte == pytest.approx(6.5, abs=0.1)

    # Fenster ohne Sprache bzw. komplett still -> None
    assert cutting.speech_bounds(np.zeros(sr * 5, dtype=np.float32),
                                 sr, 0.0, 5.0) is None


def test_trim_silence_cuts_leading_and_trailing_pause(env, media):
    """Die Transkript-Timestamps behaupten Sprache ab 2 s, das echte Audio
    beginnt erst bei 6 s und endet bei 12 s -> der Trimmer misst am
    Referenz-Audio nach und stutzt die Stille an beiden Kanten."""
    import numpy as np
    from scipy.io import wavfile

    name = "stille"
    paths.create_project(name)
    base = paths.project_dir(name)
    sr = 16000
    rng = np.random.default_rng(3)
    wav = np.zeros(sr * 20, dtype=np.float32)
    wav[6 * sr:12 * sr] = rng.standard_normal(6 * sr).astype(np.float32) * 0.4
    wavfile.write(base / "input/audio_dji/dji.wav", sr,
                  (np.clip(wav, -0.99, 0.99) * 32767).astype(np.int16))

    def words_early(wav_path, language, progress=None):
        # Wörter angeblich von 2.0 bis ~13.1 s (WhisperX-typisch verrutscht)
        return [{"word": f"w{i}", "start": round(2.0 + 0.4 * i, 3),
                 "end": round(2.3 + 0.4 * i, 3)} for i in range(28)]

    ingest.scan_project(name)
    transcribe.transcribe_project(name, transcriber=words_early)
    fake = FakeClaude(reel_segments=[
        {"start": 2.0, "ende": 13.0, "text": "x", "begruendung": ""}])
    cutting.select_segments(name, client=fake)

    seg = cutting.load_segments(name)["segmente"][0]
    assert seg["stille_getrimmt"] is True
    # Start rückt von ~1.85 an den echten Sprachbeginn (6 s, minus Padding)
    assert 5.5 < seg["start"] < 6.05
    # Ende rückt von ~13.15 ans echte Sprachende (12 s, plus Nachlauf)
    assert 11.9 < seg["ende"] < 12.5
    assert seg["dauer"] == pytest.approx(seg["ende"] - seg["start"], abs=0.01)


def test_trim_silence_keeps_tight_segments(env, media):
    """Segmente, deren Grenzen schon am Sprachsignal liegen, bleiben
    unangetastet."""
    import numpy as np
    from scipy.io import wavfile

    name = "kein-trim"
    paths.create_project(name)
    base = paths.project_dir(name)
    sr = 16000
    rng = np.random.default_rng(4)
    wav = np.zeros(sr * 20, dtype=np.float32)
    wav[2 * sr:14 * sr] = rng.standard_normal(12 * sr).astype(np.float32) * 0.4
    wavfile.write(base / "input/audio_dji/dji.wav", sr,
                  (np.clip(wav, -0.99, 0.99) * 32767).astype(np.int16))

    def words_ok(wav_path, language, progress=None):
        return [{"word": f"w{i}", "start": round(2.2 + 0.4 * i, 3),
                 "end": round(2.5 + 0.4 * i, 3)} for i in range(28)]

    ingest.scan_project(name)
    transcribe.transcribe_project(name, transcriber=words_ok)
    fake = FakeClaude(reel_segments=[
        {"start": 3.0, "ende": 10.0, "text": "x", "begruendung": ""}])
    cutting.select_segments(name, client=fake)

    seg = cutting.load_segments(name)["segmente"][0]
    assert "stille_getrimmt" not in seg
