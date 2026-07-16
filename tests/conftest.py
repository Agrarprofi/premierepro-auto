"""Testfixtures: synthetische Medien (ffmpeg), Fake-Claude, Fake-WhisperX.

Szenario "Interview": 30 s Referenz-Audio (DJI, Rausch-Bursts),
Kamera A ab Referenzzeit 3.0 s (24 s, MOV/PCM), Kamera B ab 5.0 s (22 s,
MP4/AAC), zwei B-Roll-Clips à 8 s.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from scipy.io import wavfile

SR = 16000
REF_DUR = 30.0
CAM_A_START, CAM_A_DUR = 3.0, 24.0
CAM_B_START, CAM_B_DUR = 5.0, 22.0

SAMPLE_WORDS = (
    "die kartoffel ernte war heuer richtig gut weil der boden im "
    "frühjahr genug wasser hatte . wir setzen seit drei jahren auf "
    "regenerative landwirtschaft und sehen deutliche verbesserungen . "
    "der traktor fährt jetzt mit gps gesteuerter präzision über das feld "
    "und spart diesel . am ende zählt für uns die qualität der knollen "
    "und die gesundheit vom boden ."
).split()


def _run(cmd: list[str]) -> None:
    proc = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-2000:]


def make_reference(path: Path, seed: int = 7) -> np.ndarray:
    """Rausch-Burst-Signal, gut korrelierbar."""
    rng = np.random.default_rng(seed)
    n = int(REF_DUR * SR)
    x = rng.standard_normal(n).astype(np.float32) * 0.05
    for start in rng.uniform(0.0, REF_DUR - 0.5, size=60):
        s = int(start * SR)
        burst = rng.standard_normal(int(0.2 * SR)).astype(np.float32) * 0.5
        x[s : s + len(burst)] += burst
    x = np.clip(x, -0.99, 0.99)
    wavfile.write(path, SR, (x * 32767).astype(np.int16))
    return x


def make_camera(path: Path, ref: np.ndarray, start: float, dur: float,
                audio_codec: str, gain: float = 0.8,
                audio_delay: float = 0.0) -> None:
    """Video (testsrc) + Audio = Ausschnitt der Referenz ab `start`.

    audio_delay > 0 verschiebt die Audiospur im Container nach hinten
    (start_time/Edit-List) – wie es echte Kameras tun.
    """
    seg = ref[int(start * SR) : int((start + dur) * SR)] * gain
    tmp_wav = path.with_suffix(".tmp.wav")
    wavfile.write(tmp_wav, SR, (seg * 32767).astype(np.int16))
    _run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc=duration={dur}:size=640x360:rate=25",
        "-itsoffset", str(audio_delay), "-i", str(tmp_wav),
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", audio_codec, "-shortest", str(path),
    ])
    tmp_wav.unlink()


def make_broll(path: Path, dur: float = 8.0, pattern: str = "testsrc2",
               rate: int = 25, size: str = "640x360") -> None:
    _run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"{pattern}=duration={dur}:size={size}:rate={rate}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        str(path),
    ])


def make_vfr(src: Path, dst: Path) -> None:
    """VFR-Variante einer Datei: Frames unregelmäßig verwerfen (Muster
    2 behalten / 3 verwerfen), Timestamps beibehalten, Audio 1:1 kopieren –
    ffprobe meldet dann avg_frame_rate << r_frame_rate wie bei echtem
    VFR-Material."""
    _run([
        "ffmpeg", "-y", "-v", "error", "-i", str(src),
        "-vf", "select='lt(mod(n,5),2)'", "-fps_mode", "vfr",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "copy", str(dst),
    ])


def make_music(path: Path, freq: int, dur: float = 20.0) -> None:
    _run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={dur}",
        "-c:a", "pcm_s16le", str(path),
    ])


@pytest.fixture(scope="session")
def media(tmp_path_factory) -> dict:
    """Alle synthetischen Medien einmal pro Testlauf erzeugen."""
    root = tmp_path_factory.mktemp("media")
    ref = make_reference(root / "dji.wav")
    make_camera(root / "cam_a_001.mov", ref, CAM_A_START, CAM_A_DUR, "pcm_s16le")
    make_camera(root / "cam_b_001.mp4", ref, CAM_B_START, CAM_B_DUR, "aac", gain=0.6)
    make_broll(root / "broll_traktor.mp4", pattern="testsrc2")
    make_broll(root / "broll_feld.mp4", pattern="smptebars")
    make_music(root / "ruhig_akustik.wav", 220)
    make_music(root / "treibend_rock.wav", 440)
    return {"root": root, "ref": ref}


@pytest.fixture
def env(tmp_path, monkeypatch) -> Path:
    """Isolierte projects/-, music_library/- und presets/-Ordner pro Test."""
    monkeypatch.setenv("AUTOEDIT_PROJECTS", str(tmp_path / "projects"))
    monkeypatch.setenv("AUTOEDIT_MUSIC", str(tmp_path / "music_library"))
    monkeypatch.setenv("AUTOEDIT_PRESETS", str(tmp_path / "presets"))
    monkeypatch.setenv("AUTOEDIT_FUELLWOERTER",
                       str(tmp_path / "fuellwoerter.yaml"))
    monkeypatch.delenv("AUTOEDIT_CLAUDE_MODEL", raising=False)
    return tmp_path


@pytest.fixture
def projekt(env, media) -> str:
    """Komplettes Testprojekt mit allen Medien."""
    from autoedit import paths

    name = "testprojekt"
    paths.create_project(name)
    root = media["root"]
    base = paths.project_dir(name)
    shutil.copy(root / "dji.wav", base / "input/audio_dji/dji.wav")
    shutil.copy(root / "cam_a_001.mov", base / "input/cam_a/cam_a_001.mov")
    shutil.copy(root / "cam_b_001.mp4", base / "input/cam_b/cam_b_001.mp4")
    shutil.copy(root / "broll_traktor.mp4", base / "input/broll/broll_traktor.mp4")
    shutil.copy(root / "broll_feld.mp4", base / "input/broll/broll_feld.mp4")
    return name


@pytest.fixture
def music_lib(env, media) -> Path:
    lib = env / "music_library"
    lib.mkdir(exist_ok=True)
    shutil.copy(media["root"] / "ruhig_akustik.wav", lib / "ruhig_akustik.wav")
    shutil.copy(media["root"] / "treibend_rock.wav", lib / "treibend_rock.wav")
    (lib / "tags.yaml").write_text(
        "ruhig_akustik.wav: {genre: Akustik, stimmung: ruhig}\n"
        "treibend_rock.wav: {genre: Rock, stimmung: energisch}\n",
        encoding="utf-8",
    )
    return lib


# ------------------------------------------------------------ Fakes

def fake_transcriber(wav_path, language, progress=None) -> list[dict]:
    """Gleichmäßig verteilte Wörter von Referenzzeit 4.0 bis ~25.5 s."""
    words = []
    t = 4.0
    i = 0
    while t < 25.5:
        w = SAMPLE_WORDS[i % len(SAMPLE_WORDS)]
        if w == ".":
            if words:
                words[-1]["word"] += "."
        else:
            words.append({"word": w, "start": round(t, 3),
                          "end": round(t + 0.28, 3)})
            t += 0.33
        i += 1
    return words


class FakeClaude:
    """Deterministischer Ersatz für ClaudeClient in Tests."""

    def __init__(self, reel_segments=None, broll_matches=None,
                 music_choice="ruhig_akustik.wav",
                 subtitle_transform=None):
        self.reel_segments = reel_segments or [
            {"start": 5.5, "ende": 10.0, "text": "Hook",
             "begruendung": "starker Einstieg"},
            {"start": 15.0, "ende": 20.0, "text": "Kern",
             "begruendung": "Kernaussage"},
        ]
        self.broll_matches = broll_matches
        self.music_choice = music_choice
        self.subtitle_transform = subtitle_transform or (lambda s: s)
        self.calls: list[str] = []
        self.prompts: dict[str, str] = {}

    def complete_json(self, zweck, prompt, system=None, max_tokens=4096):
        self.calls.append(zweck)
        self.prompts[zweck] = prompt
        if zweck.startswith("aussagen_analyse"):
            return [
                {"start": s["start"], "ende": s["ende"], "text": s["text"],
                 "punkte": 9 - i, "kategorie": "hook" if i == 0 else "kern",
                 "qualitaet": "sauber", "kommentar": "stark"}
                for i, s in enumerate(self.reel_segments)
            ]
        if zweck.startswith("reel_auswahl"):
            return self.reel_segments
        if zweck.startswith("broll_matching"):
            if self.broll_matches is not None:
                return self.broll_matches
            return [
                {"transkript_zeit": 1.0, "broll_datei": "input/broll/broll_feld.mp4",
                 "broll_einstieg": 0.0, "begruendung": "verstößt gegen Hook-Regel"},
                {"transkript_zeit": 4.0,
                 "broll_datei": "input/broll/broll_traktor.mp4",
                 "broll_einstieg": 1.0, "begruendung": "Traktor passt"},
                {"transkript_zeit": 5.0, "broll_datei": "input/broll/broll_feld.mp4",
                 "broll_einstieg": 0.5, "begruendung": "zu nah am vorigen"},
            ]
        if zweck.startswith("musik_auswahl"):
            return {"datei": self.music_choice, "begruendung": "ruhige Stimmung passt"}
        raise AssertionError(f"Unerwarteter JSON-Aufruf: {zweck}")

    def complete_text(self, zweck, prompt, system=None, max_tokens=4096):
        self.calls.append(zweck)
        self.prompts[zweck] = prompt
        if zweck.startswith("untertitel_korrektur"):
            srt = prompt.split("ohne Erklärungen.\n\n", 1)[1]
            return self.subtitle_transform(srt)
        raise AssertionError(f"Unerwarteter Text-Aufruf: {zweck}")

    def describe_images_json(self, zweck, image_paths, prompt, max_tokens=1024):
        self.calls.append(zweck)
        assert image_paths, "Vision-Aufruf ohne Frames"
        return {
            "beschreibung": "Ein Traktor fährt über ein Kartoffelfeld.",
            "schlagwoerter": ["Traktor", "Feld", "Kartoffel", "Ernte", "Landwirtschaft"],
            "beste_einstiegszeit": 1.0,
        }


@pytest.fixture
def fake_claude() -> FakeClaude:
    return FakeClaude()


def prepared_project(projekt: str, fake: FakeClaude, *, with_broll=True,
                     with_subtitles=False, with_music=False) -> str:
    """Projekt bis inkl. Schnitt (und optional weiter) durchlaufen lassen."""
    from autoedit import broll as broll_mod
    from autoedit import cutting, ingest, subtitles, sync_audio, transcribe

    ingest.scan_project(projekt)
    transcribe.transcribe_project(projekt, transcriber=fake_transcriber)
    sync_audio.compute_offsets(projekt)
    cutting.select_segments(projekt, client=fake)
    if with_broll:
        broll_mod.analyze_broll(projekt, client=fake)
        broll_mod.match_broll(projekt, client=fake)
    if with_subtitles:
        subtitles.generate_subtitles(projekt, client=fake)
    return projekt
