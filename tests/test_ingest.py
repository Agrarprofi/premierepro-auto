import pytest

from autoedit import ingest, paths


def test_scan_project(projekt):
    result = ingest.scan_project(projekt)
    by_role = ingest.clips_by_role(result)

    assert [c["name"] for c in by_role["cam_a"]] == ["cam_a_001.mov"]
    assert [c["name"] for c in by_role["cam_b"]] == ["cam_b_001.mp4"]
    assert [c["name"] for c in by_role["audio_dji"]] == ["dji.wav"]
    assert len(by_role["broll"]) == 2

    cam_a = by_role["cam_a"][0]
    assert abs(cam_a["dauer"] - 24.0) < 0.3
    assert cam_a["breite"] == 640 and cam_a["hoehe"] == 360
    assert abs(cam_a["fps"] - 25.0) < 0.01
    assert cam_a["audio_kanaele"] == 1
    # Synthetische Testdateien sind sauber CFR
    assert cam_a["vfr_verdacht"] is False

    dji = by_role["audio_dji"][0]
    assert abs(dji["dauer"] - 30.0) < 0.2
    assert dji["audio_samplerate"] == 16000

    assert (paths.output_dir(projekt) / "media_info.json").is_file()
    assert ingest.load_media_info(projekt)["clips"]


def test_scan_ignores_hidden_and_foreign_files(projekt):
    cam_dir = paths.input_dir(projekt, "cam_a")
    (cam_dir / ".DS_Store").write_bytes(b"\x00")
    (cam_dir / "notizen.txt").write_text("x")
    result = ingest.scan_project(projekt)
    names = [c["name"] for c in result["clips"] if c["rolle"] == "cam_a"]
    assert names == ["cam_a_001.mov"]


def test_scan_converts_vfr_to_cfr(projekt, media):
    """VFR-Datei wird beim Ingest automatisch nach CFR gewandelt; alle
    Zeitangaben in media_info stammen von der Kopie."""
    from autoedit import ffmpeg_utils
    from tests.conftest import make_vfr

    cam_dir = paths.input_dir(projekt, "cam_b")
    make_vfr(media["root"] / "cam_b_001.mp4", cam_dir / "cam_b_vfr.mp4")

    calls: list[tuple[float, str]] = []
    result = ingest.scan_project(
        projekt, progress=lambda f, m="": calls.append((f, m)))
    by_name = {c["name"]: c for c in result["clips"]}

    # Fortschritt der Wandlung wird gemeldet (inkl. Prozent aus ffmpeg)
    assert any("wandle nach" in m and "%" in m for _, m in calls)
    assert all(0.0 <= f <= 1.0 for f, _ in calls)

    clip = by_name["cam_b_vfr.mp4"]
    assert clip["vfr_original"] is True
    assert clip["cfr_pfad"].startswith("output/cfr/cam_b/")
    assert clip["relpfad"] == "input/cam_b/cam_b_vfr.mp4"

    cfr = ingest.clip_datei(projekt, clip)
    assert cfr.is_file()
    info = ffmpeg_utils.media_info(cfr)
    assert info["vfr_verdacht"] is False          # Kopie ist sauber CFR
    assert clip["fps"] == pytest.approx(info["fps"])
    # Ton wurde 1:1 kopiert: Dauer bleibt (im Rahmen der Framerate) gleich
    assert clip["dauer"] == pytest.approx(22.0, abs=0.5)

    # Saubere Dateien bleiben unangetastet
    sauber = by_name["cam_b_001.mp4"]
    assert "cfr_pfad" not in sauber
    assert ingest.clip_datei(projekt, sauber).name == "cam_b_001.mp4"

    # Zweiter Scan wandelt nicht erneut (Cache über mtime)
    mtime = cfr.stat().st_mtime
    ingest.scan_project(projekt)
    assert cfr.stat().st_mtime == mtime


def test_vfr_detection_catches_sparse_drops(projekt, media):
    """Der LKOE-Fall: wenige gedroppte Frames (99.9x statt 100 fps) sind
    relativ winzig, summieren sich aber über die Dateilänge zu sichtbarem
    Bild-Drift - die Erkennung muss auf den DRIFT schauen."""
    from autoedit import ffmpeg_utils
    from tests.conftest import _run

    cam_dir = paths.input_dir(projekt, "cam_a")
    # jeden 250. Frame verwerfen: nur 0.4% Abweichung, aber Drift wächst
    _run([
        "ffmpeg", "-y", "-v", "error", "-i", str(media["root"] / "cam_a_001.mov"),
        "-vf", "select='not(eq(mod(n\\,250)\\,0))'", "-fps_mode", "vfr",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "pcm_s16le", str(cam_dir / "cam_a_drop.mov"),
    ])
    info = ffmpeg_utils.media_info(cam_dir / "cam_a_drop.mov")
    # relative Abweichung klein, Drift über 24 s aber ~0.1 s -> Verdacht!
    assert info["vfr_verdacht"] is True
    assert info["vfr_drift_sek"] > 0.05


def test_force_cfr_survives_rescan(projekt, media):
    """Manueller "→ CFR"-Knopf: wandelt auch ohne VFR-Verdacht und die
    Entscheidung übersteht einen erneuten Ingest-Lauf."""
    ingest.scan_project(projekt)
    rel = "input/cam_b/cam_b_001.mp4"

    clip = ingest.force_cfr(projekt, rel)
    assert clip["cfr_erzwungen"] is True
    assert clip["cfr_pfad"].startswith("output/cfr/cam_b/")
    assert ingest.clip_datei(projekt, clip).is_file()

    # Rescan behält die Wandlung bei (Cache, keine Neukodierung)
    mtime = ingest.clip_datei(projekt, clip).stat().st_mtime
    result = ingest.scan_project(projekt)
    neu = [c for c in result["clips"] if c["relpfad"] == rel][0]
    assert neu.get("cfr_erzwungen") is True
    assert neu["cfr_pfad"] == clip["cfr_pfad"]
    assert ingest.clip_datei(projekt, neu).stat().st_mtime == mtime

    with pytest.raises(KeyError):
        ingest.force_cfr(projekt, "input/cam_b/gibtsnicht.mp4")


def test_scan_converts_multiple_vfr_parallel(projekt, media):
    """Mehrere VFR-Kameradateien werden in EINEM Scan (parallel)
    gewandelt; der Gesamtfortschritt bleibt in [0, 1]."""
    from tests.conftest import make_vfr

    make_vfr(media["root"] / "cam_a_001.mov",
             paths.input_dir(projekt, "cam_a") / "cam_a_vfr.mov")
    make_vfr(media["root"] / "cam_b_001.mp4",
             paths.input_dir(projekt, "cam_b") / "cam_b_vfr.mp4")

    calls = []
    result = ingest.scan_project(
        projekt, progress=lambda f, m="": calls.append(f))
    by_name = {c["name"]: c for c in result["clips"]}
    for name in ("cam_a_vfr.mov", "cam_b_vfr.mp4"):
        assert by_name[name].get("cfr_pfad"), name
        assert ingest.clip_datei(projekt, by_name[name]).is_file()
    assert all(0.0 <= f <= 1.0 for f in calls)


def test_broll_vfr_not_auto_converted(projekt, media):
    """B-Roll wird NICHT automatisch gewandelt (kein Sync-Bezug, kurze
    Ausschnitte - verschwendete Rechenzeit); manuell geht es weiterhin."""
    from tests.conftest import make_vfr

    broll_dir = paths.input_dir(projekt, "broll")
    make_vfr(media["root"] / "broll_traktor.mp4", broll_dir / "vfr_b.mp4")

    result = ingest.scan_project(projekt)
    clip = [c for c in result["clips"] if c["name"] == "vfr_b.mp4"][0]
    assert clip["vfr_verdacht"] is True     # erkannt ...
    assert "cfr_pfad" not in clip           # ... aber nicht gewandelt

    # manueller Knopf wandelt trotzdem, und das übersteht den Rescan
    neu = ingest.force_cfr(projekt, "input/broll/vfr_b.mp4")
    assert neu["cfr_pfad"]
    result = ingest.scan_project(projekt)
    clip = [c for c in result["clips"] if c["name"] == "vfr_b.mp4"][0]
    assert clip.get("cfr_erzwungen") is True and clip.get("cfr_pfad")


def test_kamera_wird_auf_sequenz_rate_normalisiert(projekt, media):
    """Kamera mit 50 fps bei Sequenz-Rate 25: wird auch OHNE VFR-Verdacht
    gewandelt, damit alle Timeline-Clips in einer Rate laufen. Eine alte
    Kopie mit falscher Rate wird erkannt und neu gewandelt."""
    from autoedit import config, ffmpeg_utils
    from tests.conftest import _run

    cam_dir = paths.input_dir(projekt, "cam_a")
    # saubere CFR-50p-Kamera erzeugen (aus der 25p-Testdatei)
    _run([
        "ffmpeg", "-y", "-v", "error", "-i", str(media["root"] / "cam_a_001.mov"),
        "-vf", "fps=50", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le",
        str(cam_dir / "cam_50p.mov"),
    ])

    result = ingest.scan_project(projekt)   # Default sequenz_fps = 25
    clip = [c for c in result["clips"] if c["name"] == "cam_50p.mov"][0]
    assert clip.get("cfr_pfad"), "50p-Kamera muss auf 25p normalisiert werden"
    info = ffmpeg_utils.media_info(ingest.clip_datei(projekt, clip))
    assert info["fps"] == pytest.approx(25.0, abs=0.01)

    # 25p-Kamera bleibt unangetastet (Rate passt schon)
    sauber = [c for c in result["clips"] if c["name"] == "cam_a_001.mov"][0]
    assert "cfr_pfad" not in sauber

    # Sequenz-Rate umgestellt -> vorhandene Kopie hat falsche Rate und
    # wird beim nächsten Scan neu gewandelt
    config.save_config(projekt, {"sequenz_fps": 50})
    result = ingest.scan_project(projekt)
    clip = [c for c in result["clips"] if c["name"] == "cam_50p.mov"][0]
    info = ffmpeg_utils.media_info(ingest.clip_datei(projekt, clip))
    assert info["fps"] == pytest.approx(50.0, abs=0.01)
    # die 25p-Kamera braucht jetzt ihrerseits eine 50p-Kopie
    sauber = [c for c in result["clips"] if c["name"] == "cam_a_001.mov"][0]
    assert sauber.get("cfr_pfad")
