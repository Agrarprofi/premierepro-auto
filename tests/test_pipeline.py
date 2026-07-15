import xml.etree.ElementTree as ET

from autoedit import claude_client, config, paths, pipeline
from tests.conftest import FakeClaude, fake_transcriber


def test_run_all_end_to_end(projekt, music_lib):
    """Komplette Kette mit Fake-Claude/Fake-WhisperX: am Ende liegen
    FCPXML + SRT in output/, alle Schritte auf 'ok'."""
    config.save_config(projekt, {"musik_aktiv": True})
    fake = FakeClaude()
    progress_meldungen = []

    results = pipeline.run_all(
        projekt,
        progress=lambda f, m="": progress_meldungen.append((f, m)),
        client=fake, transcriber=fake_transcriber,
    )

    out = paths.output_dir(projekt)
    assert (out / "testprojekt_premiere.xml").is_file()
    assert (out / "reel.srt").is_file()
    assert (out / "reel_korrigiert.srt").is_file()
    assert (out / "log.txt").is_file()

    status = pipeline.load_status(projekt)
    assert all(status[s]["status"] == "ok" for s in pipeline.STEPS), status

    # XML enthält alle 6 Spuren und mind. je 1 Clipitem auf V1/A1/A3
    tree = ET.parse(out / "testprojekt_premiere.xml")
    seq = tree.getroot().find("sequence")
    assert len(seq.findall("media/video/track")) == 3
    atracks = seq.findall("media/audio/track")
    assert len(atracks) == 3
    assert seq.find("media/video/track/clipitem") is not None
    assert atracks[0].find("clipitem") is not None
    assert atracks[2].find("clipitem") is not None  # Musik

    assert progress_meldungen and progress_meldungen[-1][0] == 1.0
    assert "export" in results
    log = pipeline.read_log(projekt)
    assert "Schritt 'export' fertig" in log


def test_run_all_skips_optional_steps(projekt):
    """Ohne B-Roll-Dateien und mit deaktivierter Musik/Untertitel wird
    übersprungen statt zu scheitern."""
    for f in paths.input_dir(projekt, "broll").iterdir():
        f.unlink()
    config.save_config(projekt, {"musik_aktiv": False, "untertitel_aktiv": False})
    fake = FakeClaude()
    results = pipeline.run_all(projekt, client=fake, transcriber=fake_transcriber)
    assert "übersprungen" in results["broll"]
    assert "übersprungen" in results["untertitel"]
    assert "übersprungen" in results["musik"]
    assert (paths.output_dir(projekt) / "testprojekt_premiere.xml").is_file()


def test_step_error_sets_status(projekt):
    """Schnitt ohne Transkript -> Fehlerstatus + Log-Eintrag."""
    try:
        pipeline.run_step(projekt, "schnitt", client=FakeClaude())
    except RuntimeError:
        pass
    status = pipeline.load_status(projekt)
    assert status["schnitt"]["status"] == "fehler"
    assert "transkribieren" in status["schnitt"]["detail"].lower()


def test_estimate_costs(projekt):
    est = pipeline.estimate_costs(projekt)
    assert est["modell"] == "claude-sonnet-4-6"
    assert est["summe_usd"] > 0
    zwecke = [p["zweck"] for p in est["posten"]]
    assert "Reel-Auswahl" in zwecke
    assert any("Vision" in z for z in zwecke)


def test_record_usage_accumulates(projekt):
    claude_client.record_usage(projekt, "test", "claude-sonnet-4-6", 1000, 500)
    claude_client.record_usage(projekt, "test2", "claude-sonnet-4-6", 2000, 100)
    costs = claude_client.load_costs(projekt)
    assert len(costs["aufrufe"]) == 2
    # 1000 in + 500 out: 1000/1e6*3 + 500/1e6*15 = 0.003 + 0.0075
    assert costs["aufrufe"][0]["kosten_usd"] == 0.0105
    assert costs["summe_usd"] > 0.01
