"""Phase 4: B-Roll-Analyse (Claude Vision) und Matching auf die Reel-Timeline."""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

from . import claude_client, config, cutting, ffmpeg_utils, ingest, paths

BROLL_INDEX_FILE = "broll_index.json"
BROLL_MATCHES_FILE = "broll_matches.json"
FRAME_INTERVAL_SEC = 2.0
FRAME_MAX_PX = 768
MAX_FRAMES_PER_CLIP = 8

HOOK_SPERRE_SEC = 3.0   # nie über die ersten 3 Sekunden (Hook bleibt Talking Head)
MAX_ABDECKUNG = 0.40    # max. 40 % des Reels mit B-Roll
MIN_ABSTAND_SEC = 1.0   # nie zwei B-Rolls direkt hintereinander


# ------------------------------------------------------------ Analyse

def extract_frames(project: str, clip: Path) -> list[Path]:
    """Alle 2 s ein Frame, auf max. 768 px Breite verkleinert."""
    out_dir = paths.output_dir(project) / "tmp" / "broll_frames" / clip.stem
    if out_dir.is_dir():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # out_range=full + format=yuv420p: Kamera-Material ist meist Limited-Range
    # (und teils 10-Bit-HEVC); der JPEG-Encoder in neueren ffmpeg-Versionen
    # bricht sonst ab ("Non full-range YUV is non-standard").
    ffmpeg_utils.run([
        "ffmpeg", "-y", "-v", "error", "-i", str(clip),
        "-vf", (f"fps=1/{FRAME_INTERVAL_SEC},"
                f"scale='min({FRAME_MAX_PX},iw)':-2:out_range=full,"
                "format=yuv420p"),
        "-strict", "unofficial",
        "-q:v", "4", str(out_dir / "frame_%04d.jpg"),
    ])
    return sorted(out_dir.glob("frame_*.jpg"))


def _sample(frames: list[Path], limit: int) -> list[Path]:
    if len(frames) <= limit:
        return frames
    step = (len(frames) - 1) / (limit - 1)
    return [frames[round(i * step)] for i in range(limit)]


def analyze_broll(project: str, progress=None, client=None) -> dict:
    cfg = config.load_config(project)
    clips = ingest.clips_for_role(project, "broll")
    if not clips:
        raise RuntimeError("Keine B-Roll-Clips in input/broll gefunden.")
    if client is None:
        client = claude_client.ClaudeClient(model=cfg["claude_modell"], project=project)

    thumbs_dir = paths.output_dir(project) / "broll_thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    base = paths.project_dir(project)

    entries = []
    for i, clip in enumerate(clips):
        if progress:
            progress(i / len(clips), f"Analysiere B-Roll {clip.name}")
        info = ffmpeg_utils.media_info(clip)
        frames = extract_frames(project, clip)
        if not frames:
            continue
        thumb = thumbs_dir / f"{clip.stem}.jpg"
        shutil.copyfile(frames[0], thumb)

        picked = _sample(frames, MAX_FRAMES_PER_CLIP)
        stamps = [f"{(frames.index(f)) * FRAME_INTERVAL_SEC:.0f}s" for f in picked]
        prompt = (
            f"Das sind {len(picked)} Frames eines B-Roll-Clips "
            f"(Dauer {info['dauer']:.1f} s), aufgenommen bei "
            f"{', '.join(stamps)} (in dieser Reihenfolge).\n"
            "Beschreibe in 1-2 Sätzen, was zu sehen ist, nenne 5 Schlagwörter "
            "und die beste Einstiegszeit in Sekunden (ab wann der Clip am "
            "stärksten wirkt).\n"
            'Antworte NUR als JSON: {"beschreibung": "...", '
            '"schlagwoerter": ["...", "...", "...", "...", "..."], '
            '"beste_einstiegszeit": <sek>}'
        )
        data = client.describe_images_json("broll_analyse", picked, prompt)
        einstieg = 0.0
        try:
            einstieg = max(0.0, min(float(data.get("beste_einstiegszeit", 0.0)),
                                    max(0.0, info["dauer"] - 1.0)))
        except (TypeError, ValueError):
            pass
        entries.append({
            "datei": str(clip.relative_to(base)),
            "name": clip.name,
            "dauer": info["dauer"],
            "beschreibung": str(data.get("beschreibung", "")),
            "schlagwoerter": [str(s) for s in data.get("schlagwoerter", [])][:8],
            "beste_einstiegszeit": round(einstieg, 2),
            "thumbnail": f"broll_thumbs/{thumb.name}",
        })

    result = {"projekt": project, "clips": entries}
    (paths.output_dir(project) / BROLL_INDEX_FILE).write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if progress:
        progress(1.0, f"{len(entries)} B-Roll-Clips analysiert")
    return result


def load_broll_index(project: str) -> dict | None:
    f = paths.output_dir(project) / BROLL_INDEX_FILE
    if not f.is_file():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


# ------------------------------------------------------------ Matching

def _reel_text(segs: list[dict]) -> str:
    return "\n".join(
        f"[{s['timeline_start']:.2f} – {s['timeline_ende']:.2f}] {s['text']}"
        for s in segs
    )


def match_broll(project: str, progress=None, client=None) -> dict:
    cfg = config.load_config(project)
    index = load_broll_index(project)
    segs = cutting.enabled_segments(project)
    if index is None or not index["clips"]:
        raise RuntimeError("Erst B-Roll analysieren.")
    if not segs:
        raise RuntimeError("Keine aktiven Reel-Segmente (Phase 3).")
    if client is None:
        client = claude_client.ClaudeClient(model=cfg["claude_modell"], project=project)

    broll_dauer = float(cfg["broll_dauer_sek"])
    reel_dauer = segs[-1]["timeline_ende"]

    beschreibungen = "\n".join(
        f"- {c['datei']}: {c['beschreibung']} "
        f"(Schlagwörter: {', '.join(c['schlagwoerter'])}; Dauer {c['dauer']:.1f} s; "
        f"bester Einstieg {c['beste_einstiegszeit']:.1f} s)"
        for c in index["clips"]
    )
    prompt = (
        "Ordne passenden Stellen im Reel-Transkript B-Roll-Clips zu.\n"
        "Regeln:\n"
        f"- nie über die ersten {HOOK_SPERRE_SEC:.0f} Sekunden des Reels "
        "(der Hook bleibt Talking Head)\n"
        f"- jede Einblendung dauert {broll_dauer:.1f} Sekunden\n"
        f"- maximal {MAX_ABDECKUNG * 100:.0f} % des Reels "
        f"({reel_dauer * MAX_ABDECKUNG:.1f} s) mit B-Roll bedeckt\n"
        "- nie zwei B-Rolls direkt hintereinander\n"
        "- 'transkript_zeit' ist der Startzeitpunkt der Einblendung auf der "
        "Reel-Zeitachse in Sekunden\n\n"
        f"Reel-Transkript (Reel-Zeitachse, Gesamtlänge {reel_dauer:.1f} s):\n"
        f"{_reel_text(segs)}\n\n"
        f"Verfügbare B-Roll-Clips:\n{beschreibungen}\n\n"
        'Antworte NUR als JSON-Array: [{"transkript_zeit": <sek>, '
        '"broll_datei": "...", "broll_einstieg": <sek>, "begruendung": "..."}]'
    )
    if progress:
        progress(0.2, "Frage Claude nach B-Roll-Zuordnungen …")
    raw = client.complete_json("broll_matching", prompt, max_tokens=2048)
    if not isinstance(raw, list):
        raise ValueError(f"Unerwartete Claude-Antwort (kein Array): {raw!r}")

    by_file = {c["datei"]: c for c in index["clips"]}
    by_name = {c["name"]: c for c in index["clips"]}
    candidates = []
    for item in raw:
        try:
            t = float(item["transkript_zeit"])
            datei = str(item["broll_datei"])
            einstieg = float(item.get("broll_einstieg", 0.0))
        except (KeyError, TypeError, ValueError):
            continue
        clip = by_file.get(datei) or by_name.get(Path(datei).name)
        if clip is None:
            continue
        einstieg = max(0.0, min(einstieg, max(0.0, clip["dauer"] - broll_dauer)))
        candidates.append({
            "id": uuid.uuid4().hex[:8],
            "transkript_zeit": round(t, 3),
            "broll_datei": clip["datei"],
            "broll_einstieg": round(einstieg, 3),
            "dauer": broll_dauer,
            "begruendung": str(item.get("begruendung", "")),
            "thumbnail": clip.get("thumbnail"),
        })

    matches = _enforce_rules(candidates, broll_dauer, reel_dauer)
    result = {"projekt": project, "matches": matches}
    save_matches(project, result)
    if progress:
        progress(1.0, f"{len(matches)} B-Roll-Zuordnungen")
    return result


def _enforce_rules(candidates: list[dict], broll_dauer: float,
                   reel_dauer: float) -> list[dict]:
    """Regeln hart durchsetzen, egal was Claude liefert."""
    ok: list[dict] = []
    budget = reel_dauer * MAX_ABDECKUNG
    for cand in sorted(candidates, key=lambda c: c["transkript_zeit"]):
        t = cand["transkript_zeit"]
        if t < HOOK_SPERRE_SEC:
            continue
        if t + broll_dauer > reel_dauer:
            continue
        if ok and t < ok[-1]["transkript_zeit"] + broll_dauer + MIN_ABSTAND_SEC:
            continue
        if (len(ok) + 1) * broll_dauer > budget:
            break
        ok.append(cand)
    return ok


def save_matches(project: str, data: dict) -> None:
    (paths.output_dir(project) / BROLL_MATCHES_FILE).write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def load_matches(project: str) -> dict | None:
    f = paths.output_dir(project) / BROLL_MATCHES_FILE
    if not f.is_file():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


def delete_match(project: str, match_id: str) -> dict:
    data = load_matches(project)
    if data is None:
        raise RuntimeError("Keine B-Roll-Zuordnungen vorhanden.")
    before = len(data["matches"])
    data["matches"] = [m for m in data["matches"] if m["id"] != match_id]
    if len(data["matches"]) == before:
        raise KeyError(f"Zuordnung {match_id} nicht gefunden.")
    save_matches(project, data)
    return data
