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
# Unterhalb dieser Korrelation gilt ein Clip als "ohne Referenz-Überlappung"
# und wird sequenziell hinter seinen Vorgänger gelegt statt auf einen
# Zufallspeak. Gleiche Aufnahme über verschiedene Mikros liegt real >> 0.1,
# nicht überlappendes Material erfahrungsgemäß < 0.05.
MIN_KONFIDENZ = 0.10
MIN_OVERLAP_SEC = 5.0    # Mindest-Überlappung für einen gültigen Grob-Lag
DRIFT_MIN_CLIP_SEC = 150.0  # Uhren-Drift erst bei längeren Takes messen


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


def _ncc_over_lags(env_ref: np.ndarray, env_clip: np.ndarray,
                   min_overlap: int) -> tuple[np.ndarray, np.ndarray]:
    """Normalisierte Kreuzkorrelation je Lag.

    Die unnormierte Korrelation bevorzugt Lags mit großer Überlappung –
    ein nur teilweise überlappender Clip landet dann auf einem Scheinpeak
    in der Mitte. Hier wird jeder Lag durch die Energie der tatsächlich
    überlappenden Fenster geteilt (per Kumulativsummen, O(n)).
    """
    corr = signal.correlate(env_ref, env_clip, mode="full", method="fft")
    lags = np.arange(-(len(env_clip) - 1), len(env_ref))
    cs_r = np.concatenate(([0.0], np.cumsum(env_ref.astype(np.float64) ** 2)))
    cs_c = np.concatenate(([0.0], np.cumsum(env_clip.astype(np.float64) ** 2)))
    r0 = np.clip(lags, 0, len(env_ref))
    r1 = np.clip(lags + len(env_clip), 0, len(env_ref))
    c0 = np.clip(-lags, 0, len(env_clip))
    c1 = np.clip(len(env_ref) - lags, 0, len(env_clip))
    energie = np.sqrt((cs_r[r1] - cs_r[r0]) * (cs_c[c1] - cs_c[c0]))
    gueltig = ((r1 - r0) >= min_overlap) & (energie > 1e-9)
    ncc = np.where(gueltig, corr / np.maximum(energie, 1e-9), -np.inf)
    return ncc, lags


def _top_lags(ncc: np.ndarray, lags: np.ndarray, k: int = 5,
              min_abstand: int = 2 * ENV_SR) -> list[int]:
    """Die k stärksten, mindestens 2 s auseinanderliegenden Grob-Peaks."""
    order = np.argsort(ncc)[::-1]
    picked: list[int] = []
    for i in order:
        if not np.isfinite(ncc[i]):
            break
        lag = int(lags[i])
        if all(abs(lag - p) >= min_abstand for p in picked):
            picked.append(lag)
        if len(picked) >= k:
            break
    return picked


def _local_ncc(ref: np.ndarray, clip: np.ndarray, offset: float,
               frac: float, sr: int) -> float | None:
    """Normalisierte Fein-Korrelation eines ~8-s-Ausschnitts an Position
    frac des Überlapps (Suche ±0.35 s um den Kandidaten-Offset)."""
    ov0 = max(0.0, offset)
    ov1 = min(len(ref) / sr, offset + len(clip) / sr)
    if ov1 - ov0 < MIN_OVERLAP_SEC:
        return None
    mitte = ov0 + frac * (ov1 - ov0)
    r0 = int(max(ov0, mitte - 4.0) * sr)
    r1 = int(min(ov1, mitte + 4.0) * sr)
    c0 = int(round(r0 - offset * sr))
    c1 = c0 + (r1 - r0)
    if c0 < 0 or c1 > len(clip) or r1 - r0 < 2 * sr:
        return None
    chunk = clip[c0:c1]
    pad = int(0.35 * sr)
    w0, w1 = max(0, r0 - pad), min(len(ref), r1 + pad)
    fine = signal.correlate(ref[w0:w1], chunk, mode="valid", method="fft")
    k = int(np.argmax(fine))
    seg_ref = ref[w0 + k : w0 + k + len(chunk)]
    denom = float(np.linalg.norm(seg_ref) * np.linalg.norm(chunk))
    if denom <= 0:
        return None
    return float(fine[k] / denom)


def _offset_score(ref: np.ndarray, clip: np.ndarray, offset: float,
                  sr: int = SYNC_SR) -> float:
    """Kandidaten-Offset über den GANZEN Überlapp verifizieren.

    Ein echter Offset korreliert an Anfang, Mitte UND Ende; ein
    Schein-Peak (z.B. wiederholter Take im Interview) passt nur an
    einer Stelle und fällt im Mittel durch.
    """
    scores = [s for frac in (0.12, 0.5, 0.88)
              if (s := _local_ncc(ref, clip, offset, frac, sr)) is not None]
    if not scores:
        return 0.0
    return float(np.mean([max(0.0, s) for s in scores]))


def find_offset(ref: np.ndarray, clip: np.ndarray, sr: int = SYNC_SR,
                mit_alternative: bool = False):
    """Offset (Sekunden) des Clips relativ zur Referenz + Konfidenz [0..1].

    mit_alternative=True liefert zusätzlich den zweitbesten Kandidaten
    (oder None), wenn er fast genauso gut passt - bei Interviews mit
    wiederholten Takes ist die Zuordnung sonst stumm mehrdeutig.
    """
    if len(ref) < sr or len(clip) < sr:
        raise ValueError("Signale zu kurz für Sync (< 1 s)")

    # --- Stufe 1: grob über Hüllkurven (normalisiert, s. _ncc_over_lags)
    env_ref = _envelope(ref)
    env_clip = _envelope(clip)
    min_overlap = min(int(MIN_OVERLAP_SEC * ENV_SR), len(env_clip), len(env_ref))
    ncc, lags = _ncc_over_lags(env_ref, env_clip, min_overlap)
    if not np.isfinite(ncc).any():
        raise ValueError("Keine ausreichende Überlappung mit der Referenz")

    # --- Stufe 1b: Top-Kandidaten über die GANZE Datei verifizieren
    kandidaten = _top_lags(ncc, lags)
    alternative: float | None = None
    if len(kandidaten) > 1:
        bewertet = sorted(
            ((_offset_score(ref, clip, lag / ENV_SR, sr), lag)
             for lag in kandidaten),
            reverse=True,
        )
        if bewertet[0][0] > 0:
            lag_env = bewertet[0][1]
            if bewertet[1][0] >= 0.8 * bewertet[0][0]:
                alternative = bewertet[1][1] / ENV_SR
        else:
            lag_env = kandidaten[0]  # Verifikation nicht möglich -> argmax
    else:
        lag_env = kandidaten[0]
    coarse = lag_env / ENV_SR  # ref_zeit = clip_zeit + coarse

    offset, konf = _fine_offset(ref, clip, coarse, sr)
    if mit_alternative:
        return offset, konf, alternative
    return offset, konf


def _fine_offset(ref: np.ndarray, clip: np.ndarray, coarse: float,
                 sr: int) -> tuple[float, float]:
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
    if r_expect < 0 or r_expect + chunk_len > len(ref):
        # Erwartete Position ragt über die Referenz hinaus: die Feinsuche
        # würde auf einen Zufallspeak im Fenster laufen -> Grobergebnis nutzen.
        return coarse, _ncc_at(ref, clip, coarse, sr)
    w0 = max(0, r_expect - pad)
    w1 = min(len(ref), r_expect + chunk_len + pad)
    ref_win = ref[w0:w1]
    if len(ref_win) < len(chunk):
        return coarse, _ncc_at(ref, clip, coarse, sr)

    fine = signal.correlate(ref_win, chunk, mode="valid", method="fft")
    k = int(np.argmax(fine))
    k_interp = _parabolic(fine, k)
    offset_samples = (w0 + k_interp) - c0
    offset = offset_samples / sr
    if abs(offset - coarse) > FINE_PAD_SEC:
        # Feinsuche widerspricht der Grobsuche deutlich -> Grobergebnis behalten
        return coarse, _ncc_at(ref, clip, coarse, sr)

    ref_seg = ref_win[k : k + len(chunk)]
    denom = float(np.linalg.norm(ref_seg) * np.linalg.norm(chunk))
    konfidenz = float(fine[k] / denom) if denom > 0 else 0.0
    return float(offset), max(0.0, min(1.0, konfidenz))


def measure_drift(ref: np.ndarray, clip: np.ndarray, offset: float,
                  sr: int = SYNC_SR) -> float | None:
    """Uhren-Drift: Offset-Differenz zwischen Clip-Ende und Clip-Anfang.

    Kamera- und Rekorder-Uhren laufen leicht auseinander; bei langen Takes
    stimmt ein einzelner globaler Offset dann nicht mehr über den ganzen
    Clip. Rückgabe: Drift in Sekunden über die Cliplänge (None wenn nicht
    messbar oder Clip zu kurz).
    """
    if len(clip) < DRIFT_MIN_CLIP_SEC * sr:
        return None

    def _local_offset(frac0: float, frac1: float) -> float | None:
        a, b = int(frac0 * len(clip)), int(frac1 * len(clip))
        seg = clip[a:b]
        chunk_len = min(len(seg), int(15 * sr))
        if chunk_len < 5 * sr:
            return None
        if len(seg) > chunk_len:
            cs = np.cumsum(np.abs(seg))
            sums = cs[chunk_len:] - cs[:-chunk_len]
            c0 = a + int(np.argmax(sums))
        else:
            c0 = a
        chunk = clip[c0:c0 + chunk_len]
        pad = int(0.5 * sr)
        r_exp = int(round(c0 + offset * sr))
        w0, w1 = r_exp - pad, r_exp + chunk_len + pad
        if w0 < 0 or w1 > len(ref):
            return None
        fine = signal.correlate(ref[w0:w1], chunk, mode="valid", method="fft")
        k = int(np.argmax(fine))
        return ((w0 + _parabolic(fine, k)) - c0) / sr

    anfang = _local_offset(0.0, 0.25)
    ende = _local_offset(0.75, 1.0)
    if anfang is None or ende is None:
        return None
    return float(ende - anfang)


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
    # Cache-Schlüssel aus dem relativen Pfad, nicht nur dem Dateinamen:
    # zwei baugleiche Kameras liefern identische Namen (C0001.MP4 in cam_a
    # UND cam_b) und dürfen sich nicht denselben Cache-Eintrag teilen.
    try:
        rel = str(src.resolve().relative_to(paths.project_dir(project).resolve()))
    except ValueError:
        import hashlib

        rel = hashlib.md5(str(src.resolve()).encode()).hexdigest()[:12] + "_" + src.name
    dst = out / (rel.replace("/", "__") + ".16k.wav")
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
            offset, konf, alternative = find_offset(ref, clip,
                                                    mit_alternative=True)
            if konf < MIN_KONFIDENZ:
                raise ValueError(
                    f"Korrelation zu schwach (Konfidenz {konf:.3f}) – "
                    "vermutlich keine Überlappung mit der Referenz"
                )
            entry = {"offset_sekunden": round(offset, 6),
                     "konfidenz": round(konf, 3)}
            hinweise = []
            if alternative is not None:
                entry["offset_alternative"] = round(alternative, 3)
                hinweise.append(
                    f"⚠ mehrdeutig (wiederholte Takes?): zweitbeste "
                    f"Übereinstimmung bei {alternative:+.2f} s – bei Versatz "
                    "diesen Wert in die Offset-Spalte eintragen und mit der "
                    "Sync-Vorschau prüfen"
                )
            drift = measure_drift(ref, clip, offset)
            if drift is not None:
                entry["drift_sekunden"] = round(drift, 4)
                if abs(drift) > 0.04:
                    hinweise.append(
                        f"Achtung: Uhren-Drift {drift * 1000:+.0f} ms über den "
                        "Clip – bei langen Takes Kamera-Wahl in Premiere "
                        "segmentweise prüfen"
                    )
            if hinweise:
                entry["hinweis"] = " · ".join(hinweise)
            offsets[rel] = entry
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


def set_video_korrektur(project: str, relpfad: str, sekunden: float) -> dict:
    """Bild-Korrektur pro Datei: verschiebt NUR das Video dieser Kamera im
    Export (positiv = Bild später aus der Quelle nehmen), der Ton bleibt.
    Rettungsanker, wenn eine Datei intern Bild/Ton-versetzt ist (z.B. VFR)."""
    sync = load_sync(project)
    if sync is None:
        raise RuntimeError("Erst Sync ausführen.")
    if relpfad not in sync["offsets"]:
        raise KeyError(f"Unbekannte Datei: {relpfad}")
    sync["offsets"][relpfad]["video_korrektur_sekunden"] = round(float(sekunden), 6)
    (paths.output_dir(project) / SYNC_FILE).write_text(
        json.dumps(sync, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return sync


def set_manual_offset(project: str, relpfad: str, offset_sekunden: float) -> dict:
    """Offset aus dem Dashboard von Hand korrigieren (überschreibt den
    berechneten Wert in sync.json)."""
    sync = load_sync(project)
    if sync is None:
        raise RuntimeError("Erst Sync ausführen.")
    if relpfad not in sync["offsets"]:
        raise KeyError(f"Unbekannte Datei: {relpfad}")
    sync["offsets"][relpfad] = {
        "offset_sekunden": round(float(offset_sekunden), 6),
        "konfidenz": None,
        "hinweis": "manuell gesetzt",
    }
    (paths.output_dir(project) / SYNC_FILE).write_text(
        json.dumps(sync, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return sync


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


def cover_range(media: dict, sync: dict, role: str, ref_start: float,
                ref_end: float) -> tuple[list[dict], float]:
    """Referenzbereich [ref_start, ref_end) mit Clips einer Rolle abdecken.

    Ein Segment kann über eine Clip-Grenze laufen (Kameras splitten bei 4 GB);
    dann wird es aus mehreren Stücken zusammengesetzt. Rückgabe:
    (stücke, unabgedeckte_sekunden); jedes Stück hat clip, src_in,
    rel_start (Position im Segment) und dauer.
    """
    spans = []
    for clip in ingest.clips_by_role(media).get(role, []):
        entry = sync["offsets"].get(clip["relpfad"])
        if entry is None:
            continue
        off = float(entry["offset_sekunden"])
        spans.append((off, off + float(clip["dauer"]), clip))
    spans.sort(key=lambda s: s[0])

    pieces: list[dict] = []
    t = ref_start
    for off, end, clip in spans:
        if end <= t + 1e-6 or off >= ref_end - 1e-6:
            continue
        piece_start = max(t, off)
        piece_end = min(ref_end, end)
        if piece_end - piece_start < 1e-3:
            continue
        pieces.append({
            "clip": clip,
            "src_in": piece_start - off,
            "rel_start": piece_start - ref_start,
            "dauer": piece_end - piece_start,
        })
        t = piece_end
    uncovered = (ref_end - ref_start) - sum(p["dauer"] for p in pieces)
    return pieces, max(0.0, uncovered)


def render_sync_preview(project: str, dauer: float = 10.0,
                        rolle: str = "cam_a") -> Path:
    """10s-Vorschau: Kamerabild + Referenz-Ton mit angewendetem Offset –
    zum Hören, ob der Sync passt (für Kamera A und Kamera B)."""
    media = ingest.load_media_info(project)
    sync = load_sync(project)
    if media is None or sync is None:
        raise RuntimeError("Erst Ingest und Sync ausführen.")
    if rolle not in ("cam_a", "cam_b"):
        raise ValueError(f"Ungültige Rolle für Sync-Vorschau: {rolle!r}")

    base = paths.project_dir(project)
    ref_path = base / sync["referenz"]
    ref_dauer = ffmpeg_utils.media_info(ref_path)["dauer"]

    cams = ingest.clips_by_role(media).get(rolle, [])
    if not cams:
        raise RuntimeError(f"Kein {rolle}-Clip vorhanden.")

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
        raise RuntimeError(f"Keine ausreichende Überlappung zwischen {rolle} "
                           "und Referenz-Audio gefunden.")

    clip, off = best
    ov_start = max(off, 0.0)
    ref_start = ov_start + max(0.0, (best_overlap - dauer) / 2.0)
    clip_start = ref_start - off

    out = paths.output_dir(project) / f"preview_sync_{rolle}.mp4"
    ffmpeg_utils.run([
        "ffmpeg", "-y", "-v", "error",
        "-ss", f"{clip_start:.3f}", "-t", f"{dauer:.3f}",
        "-i", str(ingest.clip_datei(project, clip)),
        "-ss", f"{ref_start:.3f}", "-t", f"{dauer:.3f}", "-i", str(ref_path),
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf", "scale=-2:720",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-shortest", str(out),
    ])
    return out
