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
