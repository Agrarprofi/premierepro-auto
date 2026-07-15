"""Phase 1b: Transkription mit WhisperX (Wort-Level-Timestamps).

WhisperX wird lazy importiert, damit Server/Tests auch ohne installiertes
WhisperX laufen. In Tests wird `run_whisperx` durch einen Fake ersetzt.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import config, ffmpeg_utils, ingest, paths

TRANSCRIPT_JSON = "transcript.json"
TRANSCRIPT_TXT = "transcript.txt"

WHISPER_MODEL = "large-v3"


def pick_audio_source(project: str) -> tuple[Path, str]:
    """DJI-Audio bevorzugen (beste Tonqualität), sonst Tonspur von Kamera A."""
    dji = ingest.clips_for_role(project, "audio_dji")
    if dji:
        return dji[0], "audio_dji"
    cam_a = ingest.clips_for_role(project, "cam_a")
    if cam_a:
        return cam_a[0], "cam_a"
    raise FileNotFoundError(
        "Keine Audioquelle gefunden: weder input/audio_dji noch input/cam_a "
        "enthalten Dateien."
    )


def run_whisperx(wav_path: Path, language: str, progress=None) -> list[dict]:
    """WhisperX ausführen; gibt Wortliste [{word, start, end}] zurück."""
    import whisperx  # lazy: großes Paket, nur bei echter Transkription nötig

    device = "cpu"
    compute_type = "int8"
    try:
        import torch

        if torch.cuda.is_available():
            device = "cuda"
            compute_type = "float16"
    except ImportError:
        pass

    if progress:
        progress(0.05, f"Lade WhisperX-Modell {WHISPER_MODEL} ({device}/{compute_type})")
    model = whisperx.load_model(WHISPER_MODEL, device, compute_type=compute_type)
    audio = whisperx.load_audio(str(wav_path))

    if progress:
        progress(0.2, "Transkribiere …")
    result = model.transcribe(audio, language=language, batch_size=8)

    if progress:
        progress(0.7, "Wort-Alignment …")
    align_model, metadata = whisperx.load_align_model(
        language_code=result.get("language", language), device=device
    )
    aligned = whisperx.align(
        result["segments"], align_model, metadata, audio, device,
        return_char_alignments=False,
    )

    words: list[dict] = []
    for seg in aligned.get("segments", []):
        for w in seg.get("words", []):
            if "start" in w and "end" in w:
                words.append(
                    {"word": str(w["word"]).strip(),
                     "start": float(w["start"]),
                     "end": float(w["end"])}
                )
    return words


def _words_to_segments(words: list[dict], max_gap: float = 1.0,
                       max_len: int = 30) -> list[dict]:
    """Wörter zu lesbaren Sätzen/Segmenten gruppieren (für TXT + Prompts)."""
    segments: list[dict] = []
    current: list[dict] = []

    def flush() -> None:
        if current:
            segments.append({
                "start": current[0]["start"],
                "end": current[-1]["end"],
                "text": " ".join(w["word"] for w in current),
            })
            current.clear()

    for w in words:
        if current:
            gap = w["start"] - current[-1]["end"]
            prev = current[-1]["word"]
            if gap > max_gap or len(current) >= max_len or prev.endswith((".", "!", "?")):
                flush()
        current.append(w)
    flush()
    return segments


def transcribe_project(project: str, progress=None, transcriber=None) -> dict:
    """Audioquelle wählen, transkribieren, transcript.json + .txt schreiben."""
    cfg = config.load_config(project)
    source, source_role = pick_audio_source(project)

    out = paths.output_dir(project)
    out.mkdir(parents=True, exist_ok=True)
    tmp_wav = out / "tmp" / "transcribe_16k.wav"
    if progress:
        progress(0.02, f"Extrahiere Audio aus {source.name}")
    ffmpeg_utils.extract_audio_wav(source, tmp_wav)

    runner = transcriber or run_whisperx
    words = runner(tmp_wav, cfg["sprache"], progress=progress)
    words = [w for w in words if w["word"]]
    segments = _words_to_segments(words)

    result = {
        "projekt": project,
        "sprache": cfg["sprache"],
        "quelle": str(source),
        "quelle_rolle": source_role,
        "modell": WHISPER_MODEL,
        "woerter": words,
        "segmente": segments,
    }
    (out / TRANSCRIPT_JSON).write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    txt_lines = [
        f"[{s['start']:8.2f} – {s['end']:8.2f}]  {s['text']}" for s in segments
    ]
    (out / TRANSCRIPT_TXT).write_text("\n".join(txt_lines) + "\n", encoding="utf-8")

    if progress:
        progress(1.0, f"{len(words)} Wörter transkribiert")
    return result


def load_transcript(project: str) -> dict | None:
    f = paths.output_dir(project) / TRANSCRIPT_JSON
    if not f.is_file():
        return None
    return json.loads(f.read_text(encoding="utf-8"))
