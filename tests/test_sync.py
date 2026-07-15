import numpy as np

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
