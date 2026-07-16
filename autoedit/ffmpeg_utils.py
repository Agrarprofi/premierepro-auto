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
            info["fps"] = parse_fps(
                stream.get("avg_frame_rate") or stream.get("r_frame_rate")
            ) or parse_fps(stream.get("r_frame_rate"))
            info["video_codec"] = stream.get("codec_name")
            video_start = _start_time(stream)
            if not info["dauer"] and stream.get("duration"):
                info["dauer"] = float(stream["duration"])
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
