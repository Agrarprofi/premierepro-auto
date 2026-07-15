import pytest

from autoedit import cutting, paths, subtitles
from tests.conftest import FakeClaude, prepared_project


def test_build_cues_limits():
    words = [{"word": f"wort{i}", "start": i * 0.4, "end": i * 0.4 + 0.3,
              "segment_ende": 100.0} for i in range(20)]
    cues = subtitles.build_cues(words)
    assert cues
    for cue in cues:
        assert 1 <= len(cue["zeilen"]) <= 2
        for line in cue["zeilen"]:
            assert len(line) <= 20
        assert cue["end"] > cue["start"]
    # Keine Überlappungen
    for a, b in zip(cues, cues[1:]):
        assert a["end"] <= b["start"]


def test_build_cues_splits_on_gap():
    words = [
        {"word": "vor", "start": 0.0, "end": 0.3, "segment_ende": 10.0},
        {"word": "pause", "start": 0.4, "end": 0.7, "segment_ende": 10.0},
        {"word": "danach", "start": 3.0, "end": 3.3, "segment_ende": 10.0},
    ]
    cues = subtitles.build_cues(words)
    assert len(cues) == 2
    assert cues[1]["start"] == pytest.approx(3.0)


def test_srt_time_format():
    assert subtitles._srt_time(0.0) == "00:00:00,000"
    assert subtitles._srt_time(83.415) == "00:01:23,415"
    assert subtitles._srt_time(3600.5) == "01:00:00,500"


def test_generate_subtitles_with_correction(projekt):
    def korrigiere(srt: str) -> str:
        # Textzeilen groß schreiben, Index-/Timing-Zeilen unangetastet lassen
        out = []
        for ln in srt.splitlines():
            if "-->" in ln or ln.strip().isdigit() or not ln.strip():
                out.append(ln)
            else:
                out.append(ln.upper())
        return "\n".join(out) + "\n"

    fake = FakeClaude(subtitle_transform=korrigiere)
    prepared_project(projekt, fake, with_broll=False)
    result = subtitles.generate_subtitles(projekt, client=fake)
    assert result["korrigiert"] is True
    out = paths.output_dir(projekt)
    assert (out / "reel.srt").is_file()
    assert (out / "reel_korrigiert.srt").is_file()
    assert (out / "README_UNTERTITEL.txt").is_file()

    # Timing beginnt auf der Reel-Timeline (Segment 1 startet bei 0)
    srt = (out / "reel.srt").read_text(encoding="utf-8")
    assert srt.startswith("1\n00:00:00,")
    geaendert = [d for d in result["diff"] if d["geaendert"]]
    assert geaendert, "Korrektur sollte Änderungen ausweisen"


def test_correction_rejected_if_timing_changed(projekt):
    def kaputt(srt: str) -> str:
        return srt.replace("00:00:00,", "00:00:01,", 1)

    fake = FakeClaude(subtitle_transform=kaputt)
    prepared_project(projekt, fake, with_broll=False)
    result = subtitles.generate_subtitles(projekt, client=fake)
    assert result["korrigiert"] is False
    assert "verworfen" in result["fehler"]
    assert not (paths.output_dir(projekt) / "reel_korrigiert.srt").is_file()


def test_subtitle_times_match_timeline(projekt, fake_claude):
    prepared_project(projekt, fake_claude, with_broll=False)
    subtitles.generate_subtitles(projekt, client=fake_claude, korrektur=False)
    srt = (paths.output_dir(projekt) / "reel.srt").read_text(encoding="utf-8")
    cues = subtitles.parse_srt(srt)
    # Letzte Cue endet vor dem Reel-Ende
    reel_ende = cutting.reel_dauer(projekt)
    last_end = cues[-1]["timing"].split(" --> ")[1]
    h, m, rest = last_end.split(":")
    s = float(rest.replace(",", "."))
    assert int(h) * 3600 + int(m) * 60 + s <= reel_ende + 0.05
