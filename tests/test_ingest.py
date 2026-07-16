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
