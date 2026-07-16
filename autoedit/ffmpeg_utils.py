"""ffmpeg/ffprobe-Hilfen."""

from __future__ import annotations

import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path


class FfmpegError(RuntimeError):
    pass


def check_ffmpeg() -> tuple[bool, str]:
    """Prüft, ob ffmpeg und ffprobe installiert sind."""
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        return False, (
            f"{' und '.join(missing)} nicht gefunden. "
            "Bitte installieren: brew install ffmpeg (macOS) "
            "bzw. apt install ffmpeg (Linux)."
        )
    return True, "ffmpeg gefunden"


def require_ffmpeg() -> None:
    ok, msg = check_ffmpeg()
    if not ok:
        raise FfmpegError(msg)


def run(cmd: list[str], timeout: int = 1800) -> str:
    """Kommando ausführen, bei Fehler stderr in die Exception packen."""
    proc = subprocess.run(
        [str(c) for c in cmd], capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-2000:]
        raise FfmpegError(f"Kommando fehlgeschlagen ({cmd[0]}):\n{tail}")
    return proc.stdout


def run_ffmpeg_progress(args: list[str], dauer: float, cb=None,
                        timeout: int = 7200) -> None:
    """ffmpeg mit Fortschritts-Callback ausführen.

    args = alles NACH 'ffmpeg'. cb(frac) wird während des Laufs mit dem
    Anteil 0..1 aufgerufen (aus out_time relativ zu `dauer` in Sekunden).
    ffmpeg meldet auf pipe:1 ca. 2x pro Sekunde u.a. 'out_time_ms='
    (Mikrosekunden, trotz des Namens).
    """
    cmd = ["ffmpeg", "-nostats", "-progress", "pipe:1"] + [str(a) for a in args]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if cb and dauer > 0 and line.startswith("out_time_ms="):
                raw = line.split("=", 1)[1].strip()
                if raw.lstrip("-").isdigit():
                    cb(max(0.0, min(1.0, int(raw) / 1e6 / dauer)))
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise FfmpegError(f"ffmpeg-Timeout nach {timeout} s")
    except BaseException:
        # z.B. Job-Abbruch aus dem Callback: ffmpeg nicht weiterlaufen lassen
        proc.kill()
        raise
    if proc.returncode != 0:
        tail = (proc.stderr.read() if proc.stderr else "").strip()[-2000:]
        raise FfmpegError(f"Kommando fehlgeschlagen (ffmpeg):\n{tail}")


def ffprobe_json(path: Path | str) -> dict:
    require_ffmpeg()
    out = run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ]
    )
    return json.loads(out)


def parse_fps(rate: str | None) -> float | None:
    """'25/1' oder '30000/1001' -> float."""
    if not rate or rate in ("0/0", "N/A"):
        return None
    try:
        frac = Fraction(rate)
    except (ValueError, ZeroDivisionError):
        return None
    if frac == 0:
        return None
    return float(frac)


# Übliche Aufnahme-Framerates; VFR-Material wird auf die nächstliegende
# gewandelt. NTSC-Raten als exakte Brüche, damit der fps-Filter und
# Premiere dieselbe Rate sehen.
STANDARD_FPS: list[tuple[float, str]] = [
    (23.976, "24000/1001"), (24.0, "24"), (25.0, "25"),
    (29.97, "30000/1001"), (30.0, "30"), (48.0, "48"), (50.0, "50"),
    (59.94, "60000/1001"), (60.0, "60"), (100.0, "100"),
    (119.88, "120000/1001"), (120.0, "120"),
]


def nearest_standard_fps(fps: float) -> tuple[float, str]:
    """(float, ffmpeg-Bruch) der nächstliegenden Standard-Framerate."""
    return min(STANDARD_FPS, key=lambda s: abs(s[0] - fps))


_ENCODER_CACHE: str | None = None


def _video_encoder() -> str:
    """Schnellsten verfügbaren H.264-Encoder wählen: auf dem Mac die
    Hardware (VideoToolbox, ~10x schneller bei 4K), sonst libx264."""
    global _ENCODER_CACHE
    if _ENCODER_CACHE is None:
        _ENCODER_CACHE = "libx264"
        import platform
        if platform.system() == "Darwin":
            try:
                if "h264_videotoolbox" in run(["ffmpeg", "-hide_banner",
                                               "-encoders"]):
                    _ENCODER_CACHE = "h264_videotoolbox"
            except FfmpegError:
                pass
    return _ENCODER_CACHE


def _encoder_args(breite: int | None, hoehe: int | None,
                  fps: float | None) -> list[str]:
    # Schnittfreundlich kodieren: KEINE B-Frames und kurze GOPs (2 s) -
    # Premieres Frame-Abruf stolpert sonst über einzelne Frames
    # ("Fehler beim Abruf von Frame ..., wird ersetzt").
    gop = str(int(round((fps or 25.0) * 2)))
    if _video_encoder() == "h264_videotoolbox":
        # Hardware-Encoder kennt kein CRF: Bitrate großzügig nach
        # Auflösung/Framerate (~0.09 bit/Pixel), 8-80 MBit/s
        pixel_rate = (breite or 1920) * (hoehe or 1080) * (fps or 25.0)
        bitrate = int(max(8e6, min(80e6, pixel_rate * 0.09)))
        return ["-c:v", "h264_videotoolbox", "-b:v", str(bitrate),
                "-bf", "0", "-g", gop, "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "fast", "-crf", "16",
            "-bf", "0", "-g", gop]


def convert_to_cfr(src: Path | str, dst: Path | str, fps_bruch: str,
                   dauer: float = 0.0, progress=None,
                   breite: int | None = None, hoehe: int | None = None,
                   fps: float | None = None) -> Path:
    """VFR-Datei nach konstanter Framerate wandeln.

    Video wird neu kodiert (visuell verlustfrei; auf dem Mac per
    Hardware-Encoder), der Ton wird 1:1 KOPIERT - Bild und Ton der Datei
    bleiben dadurch fest verbunden und die Audio-Sync-Offsets gelten
    unverändert.

    dauer + progress: Quelldauer in Sekunden und Callback cb(frac 0..1)
    für die Fortschrittsanzeige während der (langen) Kodierung.
    """
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.stem + ".tmp" + dst.suffix)
    # Auf dem Mac auch das DEKODIEREN in Hardware - bei 4K-HEVC ist sonst
    # der Decoder der Flaschenhals, nicht der Encoder.
    hwaccel = (["-hwaccel", "videotoolbox"]
               if _video_encoder() == "h264_videotoolbox" else [])
    try:
        run_ffmpeg_progress([
            "-y", "-v", "error", *hwaccel, "-i", str(src),
            "-vf", f"fps={fps_bruch}",
            *_encoder_args(breite, hoehe, fps),
            "-c:a", "copy",
            "-movflags", "+faststart", str(tmp),
        ], dauer, progress)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(dst)
    return dst


def media_info(path: Path | str) -> dict:
    """Kompakte Medieninfo für eine Datei (Dauer, Auflösung, fps, Audio)."""
    data = ffprobe_json(path)
    fmt = data.get("format", {})
    info: dict = {
        "datei": str(path),
        "dauer": float(fmt.get("duration", 0.0) or 0.0),
        "container": fmt.get("format_name"),
        "breite": None,
        "hoehe": None,
        "fps": None,
        "video_codec": None,
        "audio_kanaele": None,
        "audio_samplerate": None,
        "audio_codec": None,
        # Startversatz Video- vs. Audiospur im Container (video_start -
        # audio_start). Kameras schreiben oft Edit-Lists/start_times;
        # Premiere zählt Frames ab dem ersten VIDEObild, unsere Offsets ab
        # dem ersten AUDIOsample - der Export kompensiert diese Differenz.
        "av_versatz": 0.0,
    }

    def _start_time(stream) -> float | None:
        raw = stream.get("start_time")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    video_start = audio_start = None
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and info["breite"] is None:
            info["breite"] = stream.get("width")
            info["hoehe"] = stream.get("height")
            fps_avg = parse_fps(stream.get("avg_frame_rate"))
            fps_r = parse_fps(stream.get("r_frame_rate"))
            info["fps"] = fps_avg or fps_r
            info["video_codec"] = stream.get("codec_name")
            video_start = _start_time(stream)
            if not info["dauer"] and stream.get("duration"):
                info["dauer"] = float(stream["duration"])
            # Variable Framerate: entscheidend ist der AUFSUMMIERTE Versatz
            # über die Dateilänge, nicht die relative Abweichung. Beispiel:
            # 99.97 statt 100 fps klingt harmlos (0.03%), sind bei 830 s
            # aber ~25 fehlende Frames = 0.25 s Bild-Drift am Dateiende -
            # Premiere nummeriert stur durch, der Ton bleibt richtig.
            if fps_avg:
                nominal = nearest_standard_fps(fps_avg)[0]
                drift = (info["dauer"] or 0.0) * abs(fps_avg - nominal) / nominal
                rel = (abs(fps_avg - fps_r) / max(fps_avg, fps_r)
                       if fps_r else 0.0)
                info["vfr_drift_sek"] = round(drift, 3)
                info["vfr_verdacht"] = rel > 0.005 or drift > 0.05
            else:
                info["vfr_verdacht"] = False
        elif stream.get("codec_type") == "audio" and info["audio_kanaele"] is None:
            info["audio_kanaele"] = stream.get("channels")
            info["audio_samplerate"] = int(stream.get("sample_rate", 0) or 0) or None
            info["audio_codec"] = stream.get("codec_name")
            audio_start = _start_time(stream)
    if video_start is not None and audio_start is not None:
        info["av_versatz"] = round(video_start - audio_start, 6)
    return info


def extract_audio_wav(
    src: Path | str, dst: Path | str, sample_rate: int = 16000, mono: bool = True
) -> Path:
    """Tonspur als PCM-WAV extrahieren (für Sync/Transkription)."""
    require_ffmpeg()
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn",
           "-acodec", "pcm_s16le", "-ar", str(sample_rate)]
    if mono:
        cmd += ["-ac", "1"]
    cmd.append(str(dst))
    run(cmd)
    return dst
