import pytest

from autoedit import paths, transcribe
from tests.conftest import fake_transcriber


def test_pick_audio_source_prefers_dji(projekt):
    src, role = transcribe.pick_audio_source(projekt)
    assert role == "audio_dji"
    assert src.name == "dji.wav"


def test_pick_audio_source_fallback_cam_a(projekt):
    (paths.input_dir(projekt, "audio_dji") / "dji.wav").unlink()
    src, role = transcribe.pick_audio_source(projekt)
    assert role == "cam_a"


def test_pick_audio_source_missing(env):
    paths.create_project("leer")
    with pytest.raises(FileNotFoundError):
        transcribe.pick_audio_source("leer")


def test_transcribe_project_writes_outputs(projekt):
    result = transcribe.transcribe_project(projekt, transcriber=fake_transcriber)
    assert result["sprache"] == "de"
    assert len(result["woerter"]) > 30
    assert all(w["end"] > w["start"] for w in result["woerter"])
    assert result["segmente"], "Segmente für die TXT-Vorschau fehlen"

    out = paths.output_dir(projekt)
    assert (out / "transcript.json").is_file()
    txt = (out / "transcript.txt").read_text(encoding="utf-8")
    assert "kartoffel" in txt
    assert transcribe.load_transcript(projekt)["woerter"] == result["woerter"]
