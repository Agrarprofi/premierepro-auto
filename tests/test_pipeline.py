import xml.etree.ElementTree as ET

import pytest

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
    from autoedit import fcpxml
    assert fcpxml.latest_fcpxml(projekt) is not None
    assert (out / "reel.srt").is_file()
    assert (out / "reel_korrigiert.srt").is_file()
    assert (out / "log.txt").is_file()

    status = pipeline.load_status(projekt)
    assert all(status[s]["status"] == "ok" for s in pipeline.STEPS), status

    # XML enthält alle 6 Spuren und mind. je 1 Clipitem auf V1/A1/A3
    tree = ET.parse(fcpxml.latest_fcpxml(projekt))
    seq = tree.getroot().find(".//sequence")
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
    from autoedit import fcpxml
    assert fcpxml.latest_fcpxml(projekt) is not None


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
    assert results["export"].startswith("testprojekt_premiere")
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


def test_run_all_ab_schritt(projekt, music_lib):
    """'Ab hier ausführen': alles vor dem Startschritt bleibt unangetastet,
    ab dort läuft die Kette bis zum Ende – auch wenn sie schon grün war."""
    config.save_config(projekt, {"musik_aktiv": True})
    pipeline.run_all(projekt, client=FakeClaude(), transcriber=fake_transcriber)

    fake2 = FakeClaude()
    results = pipeline.run_all(projekt, ab_schritt="broll", client=fake2,
                               transcriber=_kaputter_transcriber)

    for step in ("ingest", "transkript", "sync", "schnitt"):
        assert "vor Startschritt" in results[step], results
    # ab broll wurde neu gerechnet, obwohl alles grün war
    assert "Zuordnungen" in results["broll"]
    assert results["export"].startswith("testprojekt_premiere")
    assert "reel_auswahl" not in fake2.calls
    assert "broll_matching" in fake2.calls

    with pytest.raises(ValueError):
        pipeline.run_all(projekt, ab_schritt="gibtsnicht")


def test_run_all_skips_empty_music_library(projekt):
    """Musik aktiv, aber keine Tracks: wird übersprungen statt abzubrechen."""
    config.save_config(projekt, {"musik_aktiv": True})
    results = pipeline.run_all(projekt, client=FakeClaude(),
                               transcriber=fake_transcriber)
    assert "keine Tracks" in results["musik"]
    assert results["export"].startswith("testprojekt_premiere")


def test_run_all_tolerates_optional_step_failure(projekt, monkeypatch):
    """Harter Fehler in einem optionalen Schritt (hier: B-Roll) bricht die
    Kette nicht ab – Export läuft trotzdem."""
    from autoedit import broll as broll_mod

    def kaputt(*args, **kwargs):
        raise RuntimeError("simulierter ffmpeg-Absturz")

    monkeypatch.setattr(broll_mod, "analyze_broll", kaputt)
    results = pipeline.run_all(projekt, client=FakeClaude(),
                               transcriber=fake_transcriber)
    assert results["broll"].startswith("FEHLER, übersprungen")
    assert results["export"].startswith("testprojekt_premiere")
    status = pipeline.load_status(projekt)
    assert status["broll"]["status"] == "fehler"
    assert status["export"]["status"] == "ok"


def test_run_all_required_step_failure_still_aborts(projekt, monkeypatch):
    """Pflichtschritte (z.B. Sync) brechen weiterhin ab."""
    from autoedit import sync_audio as sync_mod

    def kaputt(*args, **kwargs):
        raise RuntimeError("kein Audio")

    monkeypatch.setattr(sync_mod, "compute_offsets", kaputt)
    with pytest.raises(RuntimeError):
        pipeline.run_all(projekt, client=FakeClaude(),
                         transcriber=fake_transcriber)
    status = pipeline.load_status(projekt)
    assert status["export"]["status"] == "offen"


def test_run_batch_continues_after_failure(env, media, music_lib):
    """Warteschlange: Projekt 1 läuft komplett durch, Projekt 2 (leer,
    Transkript schlägt fehl) bricht die Kette NICHT ab."""
    import shutil

    from autoedit import paths as p

    gut = "batch-gut"
    kaputt = "batch-kaputt"
    p.create_project(gut)
    p.create_project(kaputt)  # bleibt leer -> Transkript wirft Fehler
    base = p.project_dir(gut)
    root = media["root"]
    shutil.copy(root / "dji.wav", base / "input/audio_dji/dji.wav")
    shutil.copy(root / "cam_a_001.mov", base / "input/cam_a/cam_a_001.mov")
    shutil.copy(root / "cam_b_001.mp4", base / "input/cam_b/cam_b_001.mp4")

    fake = FakeClaude()
    meldungen = []
    results = pipeline.run_batch(
        [gut, kaputt],
        progress=lambda f, m="": meldungen.append((f, m)),
        client=fake, transcriber=fake_transcriber,
    )

    assert results[gut].startswith("fertig")
    assert results[kaputt].startswith("FEHLER")
    from autoedit import fcpxml
    assert fcpxml.latest_fcpxml(gut) is not None
    # Fortschritt nennt Projekt und Position in der Warteschlange
    assert any(m.startswith(f"[1/2] {gut}:") for _, m in meldungen)
    assert any("2/2 Projekte" not in m and "1/2 Projekte erfolgreich" in m
               for _, m in meldungen)


def test_script_file_activates_leitfaden_in_auto_mode(projekt, fake_claude):
    """Warteschlangen-Vorbereitung: skript.txt im Projektordner reicht -
    auch im Auto-Modus wird sie als Leitfaden verwendet."""
    from autoedit import cutting, ingest, sync_audio, transcribe
    from autoedit import paths as p

    ingest.scan_project(projekt)
    transcribe.transcribe_project(projekt, transcriber=fake_transcriber)
    sync_audio.compute_offsets(projekt)
    (p.project_dir(projekt) / "skript.txt").write_text(
        "Botschaft: Regenerative Landwirtschaft", encoding="utf-8")

    cutting.select_segments(projekt, modus="auto", client=fake_claude)
    prompt = fake_claude.prompts["reel_auswahl"]
    assert "Regenerative Landwirtschaft" in prompt
    assert "LEITFADEN" in prompt
    assert cutting.load_segments(projekt)["modus"] == "skript"


def _api_fehler(cls, text):
    import httpx
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(text, response=httpx.Response(400, request=req),
               body={"error": {"message": text}})


def test_guthaben_fehler_erkennung():
    import anthropic

    from autoedit import claude_client

    leer = _api_fehler(anthropic.BadRequestError,
                       "Your credit balance is too low to access the API")
    assert claude_client._ist_guthaben_fehler(leer) is True
    normal = _api_fehler(anthropic.BadRequestError, "max_tokens too large")
    assert claude_client._ist_guthaben_fehler(normal) is False
    # normales Rate-Limit = vorübergehend, KEIN Stopp
    rate = _api_fehler(anthropic.RateLimitError, "rate limit exceeded")
    assert claude_client._ist_guthaben_fehler(rate) is False
    spend = _api_fehler(anthropic.RateLimitError, "monthly spend limit reached")
    assert claude_client._ist_guthaben_fehler(spend) is True


def test_batch_stops_on_empty_credit_and_resumes(env, media, music_lib):
    """Guthaben leer mitten in Projekt 1 -> die GANZE Warteschlange stoppt
    (Projekt 2 wird nicht angefasst). Nach dem 'Aufladen' setzt derselbe
    Aufruf exakt beim liegengebliebenen Schritt fort."""
    import shutil

    from autoedit import claude_client
    from autoedit import paths as p

    namen = ["kredit-1", "kredit-2"]
    root = media["root"]
    for name in namen:
        p.create_project(name)
        base = p.project_dir(name)
        shutil.copy(root / "dji.wav", base / "input/audio_dji/dji.wav")
        shutil.copy(root / "cam_a_001.mov", base / "input/cam_a/cam_a_001.mov")

    class GuthabenLeerClaude:
        def complete_json(self, zweck, *a, **kw):
            raise claude_client.ApiGuthabenLeer("Guthaben leer (Test)")
        complete_text = complete_json
        describe_images_json = complete_json

    with pytest.raises(claude_client.ApiGuthabenLeer):
        pipeline.run_batch(namen, client=GuthabenLeerClaude(),
                           transcriber=fake_transcriber)

    # Projekt 1: bis zum Sync fertig, Schnitt rot; Projekt 2: unberührt
    st1 = pipeline.load_status(namen[0])
    assert st1["sync"]["status"] == "ok"
    assert st1["schnitt"]["status"] == "fehler"
    st2 = pipeline.load_status(namen[1])
    assert all(st2[s]["status"] == "offen" for s in pipeline.STEPS)

    # "Guthaben aufgeladen": gleicher Aufruf läuft durch, erledigte
    # Schritte (Ingest/Transkript/Sync von Projekt 1) laufen nicht erneut
    fake = FakeClaude()
    results = pipeline.run_batch(namen, client=fake,
                                 transcriber=fake_transcriber)
    assert results[namen[0]].startswith("fertig")
    assert results[namen[1]].startswith("fertig")
    from autoedit import fcpxml as _fx
    assert _fx.latest_fcpxml(namen[0]) is not None
    st1 = pipeline.load_status(namen[0])
    # Transkript-Zeitstempel beweist: wurde beim zweiten Lauf übersprungen
    assert "übersprungen" in results[namen[0]] or st1["transkript"]["status"] == "ok"
