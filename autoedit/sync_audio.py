"""Phase 2: Synchronisation von Multicam + DJI-Audio per Kreuzkorrelation.

Offset-Semantik: `offset_sekunden` ist die Position des Clip-Anfangs auf der
Referenz-Zeitachse (Referenz = DJI-Audio). Also: ref_zeit = clip_zeit + offset.

Zweistufig für Geschwindigkeit UND Subframe-Genauigkeit:
  1. Grob: Hüllkurven mit 100 Hz kreuzkorrelieren (Auflösung 10 ms).
  2. Fein: Rohsignal bei 16 kHz in einem ±1s-Fenster um das Grobergebnis
     korrelieren, Peak parabolisch interpolieren (Auflösung << 1 Frame).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from scipy import signal

from . import ffmpeg_utils, ingest, paths

SYNC_FILE = "sync.json"
SYNC_SR = 16000
ENV_SR = 100  # Hüllkurven-Samplerate für die Grobsuche
FINE_CHUNK_SEC = 30.0  # max. Clip-Ausschnitt für die Feinsuche
FINE_PAD_SEC = 1.0


def _load_wav_mono(path: Path) -> np.ndarray:
    from scipy.io import wavfile

    sr, data = wavfile.read(path)
    if sr != SYNC_SR:
        raise ValueError(f"Erwarte {SYNC_SR} Hz, bekam {sr} Hz: {path}")
    if data.ndim > 1:
        data = data.mean(axis=1)
    x = data.astype(np.float32)
    if np.issubdtype(data.dtype, np.integer):
        x /= float(np.iinfo(data.dtype).max)
    return x


def _envelope(x: np.ndarray, sr: int = SYNC_SR, env_sr: int = ENV_SR) -> np.ndarray:
    hop = sr // env_sr
    n = len(x) // hop
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    env = np.abs(x[: n * hop]).reshape(n, hop).mean(axis=1)
    return env - env.mean()


def _parabolic(y: np.ndarray, i: int) -> float:
    """Peak-Feinjustierung: Scheitel der Parabel durch y[i-1..i+1]."""
    if i <= 0 or i >= len(y) - 1:
        return float(i)
    denom = y[i - 1] - 2.0 * y[i] + y[i + 1]
    if abs(denom) < 1e-12:
        return float(i)
    return i + 0.5 * (y[i - 1] - y[i + 1]) / denom


def find_offset(ref: np.ndarray, clip: np.ndarray, sr: int = SYNC_SR) -> tuple[float, float]:
    """Offset (Sekunden) des Clips relativ zur Referenz + Konfidenz [0..1]."""
    if len(ref) < sr or len(clip) < sr:
        raise ValueError("Signale zu kurz für Sync (< 1 s)")

    # --- Stufe 1: grob über Hüllkurven
    env_ref = _envelope(ref)
    env_clip = _envelope(clip)
    corr = signal.correlate(env_ref, env_clip, mode="full", method="fft")
    lag_env = int(np.argmax(corr)) - (len(env_clip) - 1)
    coarse = lag_env / ENV_SR  # ref_zeit = clip_zeit + coarse

    # --- Stufe 2: fein auf dem Rohsignal in einem Fenster um das Grobergebnis
    chunk_len = min(len(clip), int(FINE_CHUNK_SEC * sr))
    # energiereichsten Ausschnitt des Clips wählen (robuster als der Anfang)
    if len(clip) > chunk_len:
        cumsum = np.cumsum(np.abs(clip))
        sums = cumsum[chunk_len:] - cumsum[:-chunk_len]
        c0 = int(np.argmax(sums))
    else:
        c0 = 0
    chunk = clip[c0 : c0 + chunk_len]

    pad = int(FINE_PAD_SEC * sr)
    r_expect = int(round(c0 + coarse * sr))
    w0 = max(0, r_expect - pad)
    w1 = min(len(ref), r_expect + chunk_len + pad)
    ref_win = ref[w0:w1]
    if len(ref_win) < len(chunk):
        # Clip ragt über die Referenz hinaus -> nur Grobergebnis nutzbar
        return coarse, _ncc_at(ref, clip, coarse, sr)

    fine = signal.correlate(ref_win, chunk, mode="valid", method="fft")
    k = int(np.argmax(fine))
    k_interp = _parabolic(fine, k)
    offset_samples = (w0 + k_interp) - c0
    offset = offset_samples / sr

    ref_seg = ref_win[k : k + len(chunk)]
    denom = float(np.linalg.norm(ref_seg) * np.linalg.norm(chunk))
    konfidenz = float(fine[k] / denom) if denom > 0 else 0.0
    return float(offset), max(0.0, min(1.0, konfidenz))


def _ncc_at(ref: np.ndarray, clip: np.ndarray, offset: float, sr: int) -> float:
    start = int(round(offset * sr))
    a0, a1 = max(0, start), min(len(ref), start + len(clip))
    if a1 - a0 < sr:
        return 0.0
    seg_ref = ref[a0:a1]
    seg_clip = clip[a0 - start : a1 - start]
    denom = float(np.linalg.norm(seg_ref) * np.linalg.norm(seg_clip))
    if denom == 0:
        return 0.0
    return max(0.0, min(1.0, float(np.dot(seg_ref, seg_clip) / denom)))


# ------------------------------------------------------------ Projektebene

def _tmp_wav(project: str, src: Path) -> Path:
    out = paths.output_dir(project) / "tmp" / "sync"
    rel = src.name + ".16k.wav"
    dst = out / rel
    if not dst.is_file() or dst.stat().st_mtime < src.stat().st_mtime:
        ffmpeg_utils.extract_audio_wav(src, dst, sample_rate=SYNC_SR, mono=True)
    return dst


def reference_source(project: str) -> tuple[Path, str]:
    """Referenz für den Sync: erste DJI-Datei, sonst erster Kamera-A-Clip."""
    dji = ingest.clips_for_role(project, "audio_dji")
    if dji:
        return dji[0], "audio_dji"
    cam_a = ingest.clips_for_role(project, "cam_a")
    if cam_a:
        return cam_a[0], "cam_a"
    raise FileNotFoundError("Keine Referenz-Audioquelle (audio_dji oder cam_a) gefunden.")


def compute_offsets(project: str, progress=None) -> dict:
    """Offsets aller Quellen relativ zur Referenz berechnen -> sync.json."""
    base = paths.project_dir(project)
    ref_file, ref_role = reference_source(project)
    ref = _load_wav_mono(_tmp_wav(project, ref_file))

    targets: list[tuple[str, Path]] = []
    for role in ("cam_a", "cam_b", "audio_dji"):
        for f in ingest.clips_for_role(project, role):
            targets.append((role, f))

    offsets: dict[str, dict] = {}
    seq_end: dict[str, float] = {}  # Fallback: sequenzielle Platzierung je Rolle
    for i, (role, f) in enumerate(targets):
        rel = str(f.relative_to(base))
        if progress:
            progress(i / max(1, len(targets)), f"Sync: {f.name}")
        if f == ref_file:
            info = ffmpeg_utils.media_info(f)
            offsets[rel] = {"offset_sekunden": 0.0, "konfidenz": 1.0,
                            "hinweis": "Referenz"}
            seq_end[role] = info["dauer"]
            continue
        try:
            clip = _load_wav_mono(_tmp_wav(project, f))
            offset, konf = find_offset(ref, clip)
            offsets[rel] = {"offset_sekunden": round(offset, 6),
                            "konfidenz": round(konf, 3)}
            seq_end[role] = offset + len(clip) / SYNC_SR
        except (ValueError, ffmpeg_utils.FfmpegError) as exc:
            # Kein gemeinsames Audio (z.B. Folge-Clip derselben Kamera ohne
            # Referenz-Überlappung): sequenziell hinter den Vorgänger legen.
            fallback = seq_end.get(role, 0.0)
            info = ffmpeg_utils.media_info(f)
            offsets[rel] = {"offset_sekunden": round(fallback, 6),
                            "konfidenz": 0.0,
                            "hinweis": f"Fallback sequenziell: {exc}"}
            seq_end[role] = fallback + info["dauer"]

    result = {
        "projekt": project,
        "referenz": str(ref_file.relative_to(base)),
        "referenz_rolle": ref_role,
        "offsets": offsets,
    }
    out = paths.output_dir(project)
    out.mkdir(parents=True, exist_ok=True)
    (out / SYNC_FILE).write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if progress:
        progress(1.0, f"{len(offsets)} Offsets berechnet")
    return result


def load_sync(project: str) -> dict | None:
    f = paths.output_dir(project) / SYNC_FILE
    if not f.is_file():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


def offset_for(sync: dict, relpfad: str) -> float:
    entry = sync["offsets"].get(relpfad)
    if entry is None:
        raise KeyError(f"Kein Sync-Offset für {relpfad}")
    return float(entry["offset_sekunden"])


def clip_for_ref_time(project: str, media: dict, sync: dict, role: str,
                      ref_time: float) -> tuple[dict, float] | None:
    """Clip einer Rolle finden, der eine Referenzzeit abdeckt.

    Rückgabe: (clip_info, quellzeit_im_clip) oder None.
    """
    for clip in ingest.clips_by_role(media).get(role, []):
        entry = sync["offsets"].get(clip["relpfad"])
        if entry is None:
            continue
        off = float(entry["offset_sekunden"])
        if off <= ref_time < off + float(clip["dauer"]):
            return clip, ref_time - off
    return None


def render_sync_preview(project: str, dauer: float = 10.0) -> Path:
    """10s-Vorschau: Bild von Kamera A + Referenz-Ton mit angewendetem Offset."""
    media = ingest.load_media_info(project)
    sync = load_sync(project)
    if media is None or sync is None:
        raise RuntimeError("Erst Ingest und Sync ausführen.")

    base = paths.project_dir(project)
    ref_path = base / sync["referenz"]
    ref_dauer = ffmpeg_utils.media_info(ref_path)["dauer"]

    cams = ingest.clips_by_role(media).get("cam_a", [])
    if not cams:
        raise RuntimeError("Kein Kamera-A-Clip vorhanden.")

    # Clip mit größter Überlappung zur Referenz wählen
    best, best_overlap = None, -1.0
    for clip in cams:
        entry = sync["offsets"].get(clip["relpfad"])
        if entry is None:
            continue
        off = float(entry["offset_sekunden"])
        overlap = min(off + clip["dauer"], ref_dauer) - max(off, 0.0)
        if overlap > best_overlap:
            best, best_overlap = (clip, off), overlap
    if best is None or best_overlap < dauer:
        raise RuntimeError("Keine ausreichende Überlappung zwischen Kamera A "
                           "und Referenz-Audio gefunden.")

    clip, off = best
    ov_start = max(off, 0.0)
    ref_start = ov_start + max(0.0, (best_overlap - dauer) / 2.0)
    clip_start = ref_start - off

    out = paths.output_dir(project) / "preview_sync.mp4"
    ffmpeg_utils.run([
        "ffmpeg", "-y", "-v", "error",
        "-ss", f"{clip_start:.3f}", "-t", f"{dauer:.3f}", "-i", str(base / clip["relpfad"]),
        "-ss", f"{ref_start:.3f}", "-t", f"{dauer:.3f}", "-i", str(ref_path),
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf", "scale=-2:720",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-shortest", str(out),
    ])
    return out
