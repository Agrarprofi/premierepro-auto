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


def _kaputter_transcriber(*args, **kwargs):
    raise AssertionError("Transkription darf beim Fortsetzen nicht erneut laufen")


def test_run_all_resumes_after_failure(projekt, music_lib):
    """'Alles ausführen' merkt sich den Letztstand: fertige Schritte werden
    übersprungen, die Kette setzt beim ersten offenen Schritt fort."""
    config.save_config(projekt, {"musik_aktiv": True})
    fake = FakeClaude()
    pipeline.run_all(projekt, client=fake, transcriber=fake_transcriber)

    # Simulierter Abbruch beim B-Roll-Schritt (wie der ffmpeg-Fehler):
    # B-Roll + alles danach zurücksetzen
    for step in ("broll", "untertitel", "musik", "export"):
        pipeline._set_status(projekt, step, "fehler", "simulierter Abbruch")

    fake2 = FakeClaude()
    results = pipeline.run_all(projekt, client=fake2,
                               transcriber=_kaputter_transcriber)

    # Fertige Schritte übersprungen, offene neu gelaufen
    for step in ("ingest", "transkript", "sync", "schnitt"):
        assert "bereits erledigt" in results[step], results
    assert "Zuordnungen" in results["broll"]
    assert results["export"].startswith("testprojekt_premiere.xml")
    # Claude wurde nur für die nachgeholten Schritte gebraucht
    assert "reel_auswahl" not in fake2.calls
    assert "broll_matching" in fake2.calls

    status = pipeline.load_status(projekt)
    assert all(status[s]["status"] == "ok" for s in pipeline.STEPS)


def test_run_all_resume_with_everything_done(projekt, music_lib):
    config.save_config(projekt, {"musik_aktiv": True})
    fake = FakeClaude()
    pipeline.run_all(projekt, client=fake, transcriber=fake_transcriber)
    results = pipeline.run_all(projekt, client=FakeClaude(),
                               transcriber=_kaputter_transcriber)
    assert all("bereits erledigt" in r for r in results.values())


def test_run_all_force_recomputes(projekt, music_lib):
    config.save_config(projekt, {"musik_aktiv": True})
    pipeline.run_all(projekt, client=FakeClaude(), transcriber=fake_transcriber)
    fake2 = FakeClaude()
    results = pipeline.run_all(projekt, fortsetzen=False, client=fake2,
                               transcriber=fake_transcriber)
    assert not any("bereits erledigt" in r for r in results.values())
    assert "reel_auswahl" in fake2.calls
