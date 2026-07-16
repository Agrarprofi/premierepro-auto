import numpy as np
import pytest

from autoedit import ffmpeg_utils, ingest, sync_audio
from tests.conftest import CAM_A_START, CAM_B_START, SR, make_reference


def test_find_offset_synthetic_shift(tmp_path):
    """Reiner Signal-Test: bekannte Verschiebung, Genauigkeit < 5 ms."""
    ref = make_reference(tmp_path / "ref.wav", seed=42)
    true_offset = 4.321
    start = int(true_offset * SR)
    clip = ref[start : start + 12 * SR] * 0.7
    rng = np.random.default_rng(1)
    clip = clip + rng.standard_normal(len(clip)).astype(np.float32) * 0.01

    offset, konfidenz = sync_audio.find_offset(ref, clip)
    assert abs(offset - true_offset) < 0.005
    assert konfidenz > 0.5


def test_find_offset_negative(tmp_path):
    """Clip beginnt vor der Referenz (negativer Offset)."""
    full = make_reference(tmp_path / "ref.wav", seed=9)
    ref = full[int(2.0 * SR):]          # Referenz startet 2 s später
    clip = full[: int(10 * SR)] * 0.8   # Clip = Anfang des Originals
    offset, _ = sync_audio.find_offset(ref, clip)
    assert abs(offset - (-2.0)) < 0.005


def test_compute_offsets_project(projekt):
    ingest.scan_project(projekt)
    result = sync_audio.compute_offsets(projekt)

    offsets = result["offsets"]
    assert result["referenz"] == "input/audio_dji/dji.wav"
    assert offsets["input/audio_dji/dji.wav"]["offset_sekunden"] == 0.0

    # PCM-Kamera: subframe-genau (< 1 Frame bei 25 fps = 40 ms; hier < 10 ms)
    a = offsets["input/cam_a/cam_a_001.mov"]
    assert abs(a["offset_sekunden"] - CAM_A_START) < 0.01
    assert a["konfidenz"] > 0.5

    # AAC-Kamera: Encoder-Delay erlaubt, aber unter einem Frame
    b = offsets["input/cam_b/cam_b_001.mp4"]
    assert abs(b["offset_sekunden"] - CAM_B_START) < 0.04


def test_clip_for_ref_time(projekt):
    media = ingest.scan_project(projekt)
    sync = sync_audio.compute_offsets(projekt)

    hit = sync_audio.clip_for_ref_time(projekt, media, sync, "cam_a", 10.0)
    assert hit is not None
    clip, src_t = hit
    assert clip["name"] == "cam_a_001.mov"
    assert abs(src_t - (10.0 - CAM_A_START)) < 0.02

    assert sync_audio.clip_for_ref_time(projekt, media, sync, "cam_a", 1.0) is None


def test_render_sync_preview(projekt):
    ingest.scan_project(projekt)
    sync_audio.compute_offsets(projekt)
    out = sync_audio.render_sync_preview(projekt, dauer=6.0)
    assert out.is_file()
    info = ffmpeg_utils.media_info(out)
    assert abs(info["dauer"] - 6.0) < 0.5
    assert info["hoehe"] == 720
    assert info["audio_kanaele"]


def test_cover_range_spans_clip_boundary():
    """4-GB-Split: Segment läuft über die Clip-Grenze -> zwei Stücke."""
    media = {"clips": [
        {"relpfad": "input/cam_a/c1.mp4", "name": "c1.mp4", "rolle": "cam_a",
         "dauer": 10.0},
        {"relpfad": "input/cam_a/c2.mp4", "name": "c2.mp4", "rolle": "cam_a",
         "dauer": 10.0},
    ]}
    sync = {"offsets": {
        "input/cam_a/c1.mp4": {"offset_sekunden": 0.0},
        "input/cam_a/c2.mp4": {"offset_sekunden": 10.0},
    }}
    pieces, uncovered = sync_audio.cover_range(media, sync, "cam_a", 7.0, 14.0)
    assert uncovered < 1e-6
    assert len(pieces) == 2
    assert pieces[0]["clip"]["name"] == "c1.mp4"
    assert pieces[0]["src_in"] == 7.0 and pieces[0]["dauer"] == 3.0
    assert pieces[0]["rel_start"] == 0.0
    assert pieces[1]["clip"]["name"] == "c2.mp4"
    assert pieces[1]["src_in"] == 0.0 and pieces[1]["dauer"] == 4.0
    assert pieces[1]["rel_start"] == 3.0


def test_cover_range_reports_gap():
    """Kamera-Pause zwischen zwei Clips -> Lücke wird ausgewiesen."""
    media = {"clips": [
        {"relpfad": "a/c1.mp4", "name": "c1.mp4", "rolle": "cam_a", "dauer": 10.0},
        {"relpfad": "a/c2.mp4", "name": "c2.mp4", "rolle": "cam_a", "dauer": 10.0},
    ]}
    sync = {"offsets": {"a/c1.mp4": {"offset_sekunden": 0.0},
                        "a/c2.mp4": {"offset_sekunden": 12.0}}}
    pieces, uncovered = sync_audio.cover_range(media, sync, "cam_a", 7.0, 14.0)
    assert len(pieces) == 2
    assert uncovered == pytest.approx(2.0)
    assert pieces[1]["rel_start"] == pytest.approx(5.0)


def test_tmp_wav_no_collision_for_same_filename(env, media):
    """Zwei baugleiche Kameras schreiben C0001.MP4 in cam_a UND cam_b:
    der Sync-Cache darf nicht kollidieren (sonst bekommt Kamera B still
    den Offset von Kamera A)."""
    import shutil
    from autoedit import ingest, paths
    from tests.conftest import CAM_A_START, CAM_B_START

    name = "kollision"
    paths.create_project(name)
    base = paths.project_dir(name)
    shutil.copy(media["root"] / "dji.wav", base / "input/audio_dji/dji.wav")
    shutil.copy(media["root"] / "cam_a_001.mov", base / "input/cam_a/C0001.mp4")
    shutil.copy(media["root"] / "cam_b_001.mp4", base / "input/cam_b/C0001.mp4")

    ingest.scan_project(name)
    result = sync_audio.compute_offsets(name)
    a = result["offsets"]["input/cam_a/C0001.mp4"]["offset_sekunden"]
    b = result["offsets"]["input/cam_b/C0001.mp4"]["offset_sekunden"]
    assert abs(a - CAM_A_START) < 0.04
    assert abs(b - CAM_B_START) < 0.04
    assert abs(a - b) > 1.0


def test_compute_offsets_fallback_without_overlap(env, media, tmp_path):
    """Clip ohne gemeinsames Audio mit der Referenz -> sequenzieller
    Fallback statt Zufallsoffset (Konfidenz-Schwelle)."""
    import shutil
    from autoedit import ingest, paths
    from tests.conftest import make_camera, make_reference

    name = "fallbackp"
    paths.create_project(name)
    base = paths.project_dir(name)
    shutil.copy(media["root"] / "dji.wav", base / "input/audio_dji/dji.wav")
    fremd = make_reference(tmp_path / "fremd.wav", seed=99)
    make_camera(tmp_path / "cam_x.mp4", fremd, 0.0, 12.0, "aac")
    shutil.copy(tmp_path / "cam_x.mp4", base / "input/cam_a/cam_x.mp4")

    ingest.scan_project(name)
    result = sync_audio.compute_offsets(name)
    entry = result["offsets"]["input/cam_a/cam_x.mp4"]
    assert entry["konfidenz"] == 0.0
    assert "sequenziell" in entry["hinweis"]
    assert entry["offset_sekunden"] == 0.0  # erster Clip der Rolle


def test_find_offset_overhang_falls_back_to_coarse(tmp_path):
    """Clip ragt über das Referenzende hinaus: die Feinsuche darf das
    korrekte Grobergebnis nicht durch einen Zufallspeak ersetzen."""
    ref = make_reference(tmp_path / "ref.wav", seed=21)
    true_offset = 30.0 - 10.0  # letzte 10 s der Referenz ...
    tail = ref[int(true_offset * SR):]
    rng = np.random.default_rng(3)
    extra = rng.standard_normal(int(5 * SR)).astype(np.float32) * 0.3
    clip = np.concatenate([tail, extra])  # ... plus 5 s fremdes Material
    offset, konf = sync_audio.find_offset(ref, clip)
    assert abs(offset - true_offset) < 0.02  # Grob-Auflösung 10 ms
    assert konf > sync_audio.MIN_KONFIDENZ


def test_find_offset_partial_overlap_majority_foreign(tmp_path):
    """Nur 8 von 25 s des Clips überlappen die Referenz (Rest fremdes
    Material): die normalisierte Grobsuche darf nicht auf einen
    Scheinpeak in der Mitte springen."""
    ref = make_reference(tmp_path / "ref.wav", seed=31)
    true_offset = 22.0
    tail = ref[int(true_offset * SR):]              # 8 s echte Überlappung
    rng = np.random.default_rng(5)
    fremd = rng.standard_normal(int(17 * SR)).astype(np.float32) * 0.3
    clip = np.concatenate([tail, fremd])            # 25 s gesamt
    offset, konf = sync_audio.find_offset(ref, clip)
    assert abs(offset - true_offset) < 0.02
    assert konf > sync_audio.MIN_KONFIDENZ


def test_measure_drift_detects_clock_drift(tmp_path):
    """Kamera-Uhr läuft 0.03% langsamer als die Referenz: Drift über
    einen langen Take wird erkannt und gemeldet."""
    from scipy import signal as sps
    from tests.conftest import _run  # noqa: F401

    rng = np.random.default_rng(11)
    dur, sr = 200.0, SR
    ref = rng.standard_normal(int(dur * sr)).astype(np.float32) * 0.05
    for start in rng.uniform(0.0, dur - 0.5, size=400):
        s = int(start * sr)
        burst = rng.standard_normal(int(0.2 * sr)).astype(np.float32) * 0.5
        ref[s:s + len(burst)] += burst

    seg = ref[int(30 * sr):int(190 * sr)]           # 160 s Take ab t=30
    faktor = 1.0003                                  # 0.03 % Drift
    clip = sps.resample(seg, int(round(len(seg) * faktor))).astype(np.float32)

    offset, _ = sync_audio.find_offset(ref, clip)
    drift = sync_audio.measure_drift(ref, clip, offset)
    assert drift is not None
    # Gedehnter Clip = Kamera-Uhr langsamer -> Offset sinkt zum Clip-Ende,
    # Drift ist negativ; erwartet ~0.75 * 160 s * 3e-4 ≈ 36 ms Betrag
    assert -0.07 < drift < -0.015


def test_set_manual_offset(projekt):
    ingest.scan_project(projekt)
    sync_audio.compute_offsets(projekt)
    rel = "input/cam_b/cam_b_001.mp4"
    result = sync_audio.set_manual_offset(projekt, rel, 5.25)
    assert result["offsets"][rel]["offset_sekunden"] == 5.25
    assert result["offsets"][rel]["hinweis"] == "manuell gesetzt"
    gespeichert = sync_audio.load_sync(projekt)
    assert gespeichert["offsets"][rel]["offset_sekunden"] == 5.25
    with pytest.raises(KeyError):
        sync_audio.set_manual_offset(projekt, "gibtsnicht.mp4", 1.0)


def test_render_sync_preview_cam_b(projekt):
    ingest.scan_project(projekt)
    sync_audio.compute_offsets(projekt)
    out = sync_audio.render_sync_preview(projekt, dauer=6.0, rolle="cam_b")
    assert out.name == "preview_sync_cam_b.mp4"
    assert out.is_file()
    with pytest.raises(ValueError):
        sync_audio.render_sync_preview(projekt, rolle="broll")


def test_offset_verification_rejects_repeated_take_alias():
    """Interview mit wiederholtem Take: dieselben 6 s Audio kommen an zwei
    Stellen der Referenz vor. Die Kandidaten-Verifikation wählt den Offset,
    der über den GANZEN Clip passt - nicht den Schein-Treffer."""
    rng = np.random.default_rng(11)
    sr = sync_audio.SYNC_SR
    ref = rng.standard_normal(60 * sr).astype(np.float32) * 0.3
    # 6-s-"Take" bei 10 s wird bei 40 s wiederholt
    ref[40 * sr:46 * sr] = ref[10 * sr:16 * sr]

    # Clip = 20 s ab Sekunde 8 (enthält den Take) -> wahre Position 8.0
    clip = ref[8 * sr:28 * sr] * 0.7
    offset, konf, alternative = sync_audio.find_offset(
        ref, clip, mit_alternative=True)
    assert offset == pytest.approx(8.0, abs=0.02)
    assert konf > 0.5
    # Der Schein-Kandidat (Take-Kopie -> Offset 38) fällt durch
    assert alternative is None
    assert sync_audio._offset_score(ref, clip, 8.0) > \
        2 * sync_audio._offset_score(ref, clip, 38.0)


def test_offset_reports_genuine_ambiguity():
    """Clip existiert IDENTISCH an zwei Stellen der Referenz -> beide
    Offsets gültig, die Alternative wird gemeldet (Hinweis im Dashboard)."""
    rng = np.random.default_rng(12)
    sr = sync_audio.SYNC_SR
    ref = rng.standard_normal(60 * sr).astype(np.float32) * 0.3
    ref[40 * sr:48 * sr] = ref[10 * sr:18 * sr]
    clip = ref[10 * sr:18 * sr] * 0.8

    offset, _, alternative = sync_audio.find_offset(
        ref, clip, mit_alternative=True)
    assert alternative is not None
    gefunden = sorted([offset, alternative])
    assert gefunden[0] == pytest.approx(10.0, abs=0.05)
    assert gefunden[1] == pytest.approx(40.0, abs=0.05)
