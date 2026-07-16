"""FastAPI-Backend + Web-Dashboard."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import (broll, claude_client, config, cutting, ffmpeg_utils, ingest,
                jobs, music, paths, pipeline, subtitles, sync_audio,
                transcribe)

# .env schon beim Serverstart laden, damit die API-Key-Anzeige im
# Dashboard-Kopf stimmt (nicht erst beim ersten Claude-Aufruf).
load_dotenv(paths.REPO_ROOT / ".env")

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="autoedit", version="0.1.0")


def _project_or_404(name: str) -> str:
    if not paths.project_exists(name):
        raise HTTPException(404, f"Projekt nicht gefunden: {name}")
    return name


def _err(exc: Exception) -> HTTPException:
    return HTTPException(400, f"{type(exc).__name__}: {exc}")


# ------------------------------------------------------------ Basis

@app.get("/api/health")
def health() -> dict:
    ok, msg = ffmpeg_utils.check_ffmpeg()
    import os
    return {
        "status": "ok" if ok else "warnung",
        "ffmpeg": msg,
        "anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "projekte_ordner": str(paths.projects_dir()),
        "musik_ordner": str(paths.music_dir()),
    }


@app.get("/api/projects")
def list_projects() -> list[dict]:
    result = []
    for name in paths.list_projects():
        status = pipeline.load_status(name)
        result.append({
            "name": name,
            "status": {s: status[s]["status"] for s in pipeline.STEPS},
        })
    return result


class ProjectCreate(BaseModel):
    name: str


@app.post("/api/projects")
def create_project(body: ProjectCreate) -> dict:
    if not paths.valid_project_name(body.name):
        raise HTTPException(400, "Ungültiger Projektname (erlaubt: A-Z a-z 0-9 . _ -)")
    if paths.project_exists(body.name):
        raise HTTPException(409, "Projekt existiert bereits")
    paths.create_project(body.name)
    config.save_config(body.name, {})
    pipeline.log(body.name, "Projekt angelegt")
    return {"name": body.name, "pfad": str(paths.project_dir(body.name))}


@app.get("/api/projects/{name}/config")
def get_config(name: str) -> dict:
    return config.load_config(_project_or_404(name))


@app.put("/api/projects/{name}/config")
def put_config(name: str, updates: dict[str, Any]) -> dict:
    _project_or_404(name)
    try:
        return config.save_config(name, updates)
    except ValueError as exc:
        raise _err(exc)


@app.get("/api/projects/{name}/overview")
def overview(name: str) -> dict:
    _project_or_404(name)
    import json as _json

    media = ingest.load_media_info(name)
    warnungen = []
    timeline_file = paths.output_dir(name) / "timeline.json"
    if timeline_file.is_file():
        warnungen = _json.loads(
            timeline_file.read_text(encoding="utf-8")).get("warnungen", [])
    dateien: dict[str, list[dict]] = {}
    for role in paths.ROLES:
        dateien[role] = [
            {"name": f.name} for f in ingest.clips_for_role(name, role)
        ]
    segs = cutting.load_segments(name)
    return {
        "name": name,
        "config": config.load_config(name),
        "dateien": dateien,
        "media": media,
        "status": pipeline.load_status(name),
        "sync": sync_audio.load_sync(name),
        "segments": segs,
        "statements": cutting.load_statements(name),
        "reel_dauer": cutting.reel_dauer(name),
        "broll_index": broll.load_broll_index(name),
        "broll_matches": broll.load_matches(name),
        "musik": music.load_selection(name),
        "export_warnungen": warnungen,
        "kosten": claude_client.load_costs(name),
        "transcript_vorhanden": transcribe.load_transcript(name) is not None,
    }


# ------------------------------------------------------------ Jobs

def _start_job(name: str, jobname: str, fn) -> dict:
    running = jobs.MANAGER.running_for(f"{name}:")
    if running:
        raise HTTPException(409, f"Es läuft bereits ein Job: {running.name}")
    job = jobs.MANAGER.start(f"{name}:{jobname}", fn)
    return job.as_dict()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = jobs.MANAGER.get(job_id)
    if job is None:
        raise HTTPException(404, "Job nicht gefunden")
    return job.as_dict()


# ------------------------------------------------------------ Schritte

class StepOptions(BaseModel):
    modus: str = "auto"          # Schnitt: "auto" | "skript"
    skript: str | None = None
    fortsetzen: bool = True      # run_all: fertige Schritte überspringen
    ab_schritt: str | None = None  # run_all: ab diesem Schritt bis zum Ende


@app.post("/api/projects/{name}/steps/{step}")
def run_step(name: str, step: str, body: StepOptions | None = None) -> dict:
    _project_or_404(name)
    if step not in pipeline.STEPS:
        raise HTTPException(404, f"Unbekannter Schritt: {step}")
    opts = body or StepOptions()

    def runner(progress):
        return pipeline.run_step(name, step, progress=progress,
                                 modus=opts.modus, skript=opts.skript)

    return _start_job(name, step, runner)


@app.post("/api/projects/{name}/run_all")
def run_all(name: str, body: StepOptions | None = None) -> dict:
    _project_or_404(name)
    opts = body or StepOptions()

    if opts.ab_schritt is not None and opts.ab_schritt not in pipeline.STEPS:
        raise HTTPException(400, f"Unbekannter Schritt: {opts.ab_schritt}")

    def runner(progress):
        return pipeline.run_all(name, progress=progress,
                                fortsetzen=opts.fortsetzen,
                                ab_schritt=opts.ab_schritt,
                                modus=opts.modus, skript=opts.skript)

    return _start_job(name, "run_all", runner)


# ------------------------------------------------------------ Transkript

@app.get("/api/projects/{name}/transcript")
def get_transcript(name: str) -> dict:
    _project_or_404(name)
    t = transcribe.load_transcript(name)
    if t is None:
        raise HTTPException(404, "Noch kein Transkript vorhanden")
    return {
        "sprache": t["sprache"],
        "quelle": t["quelle"],
        "woerter_anzahl": len(t["woerter"]),
        "segmente": t["segmente"],
    }


# ------------------------------------------------------------ Sync

class SyncPreviewOptions(BaseModel):
    rolle: str = "cam_a"


@app.post("/api/projects/{name}/sync/preview")
def sync_preview(name: str, body: SyncPreviewOptions | None = None) -> dict:
    _project_or_404(name)
    rolle = (body or SyncPreviewOptions()).rolle

    def runner(progress):
        progress(0.2, f"Rendere Sync-Vorschau ({rolle})")
        out = sync_audio.render_sync_preview(name, rolle=rolle)
        return {"datei": out.name}

    return _start_job(name, f"sync_preview_{rolle}", runner)


class OffsetUpdate(BaseModel):
    relpfad: str
    offset_sekunden: float


@app.put("/api/projects/{name}/sync/offsets")
def put_sync_offset(name: str, body: OffsetUpdate) -> dict:
    """Manuelle Offset-Korrektur aus der Sync-Tabelle."""
    _project_or_404(name)
    try:
        return sync_audio.set_manual_offset(name, body.relpfad,
                                            body.offset_sekunden)
    except (KeyError, RuntimeError) as exc:
        raise _err(exc)


# ------------------------------------------------------------ Schnitt

class SegmentUpdate(BaseModel):
    order: list[str] | None = None
    aktiv: dict[str, bool] | None = None


@app.put("/api/projects/{name}/segments")
def put_segments(name: str, body: SegmentUpdate) -> dict:
    _project_or_404(name)
    try:
        data = cutting.update_segments(name, order=body.order, aktiv=body.aktiv)
    except RuntimeError as exc:
        raise _err(exc)
    return {"segments": data, "reel_dauer": cutting.reel_dauer(name)}


@app.post("/api/projects/{name}/cut/preview")
def cut_preview(name: str) -> dict:
    _project_or_404(name)

    def runner(progress):
        out = cutting.render_cut_preview(name, progress=progress)
        return {"datei": out.name}

    return _start_job(name, "cut_preview", runner)


# ------------------------------------------------------------ B-Roll

@app.delete("/api/projects/{name}/broll/matches/{match_id}")
def delete_broll_match(name: str, match_id: str) -> dict:
    _project_or_404(name)
    try:
        return broll.delete_match(name, match_id)
    except (KeyError, RuntimeError) as exc:
        raise _err(exc)


# ------------------------------------------------------------ Untertitel

@app.get("/api/projects/{name}/subtitles")
def get_subtitles(name: str) -> dict:
    _project_or_404(name)
    return subtitles.load_subtitles(name)


# ------------------------------------------------------------ Musik

@app.get("/api/music/library")
def music_library() -> list[dict]:
    return [
        {k: v for k, v in t.items() if k != "datei"} | {"name": t["name"]}
        for t in music.scan_library()
    ]


class MusicSet(BaseModel):
    name: str | None = None


@app.put("/api/projects/{name}/music")
def put_music(name: str, body: MusicSet) -> dict:
    _project_or_404(name)
    try:
        result = music.set_track(name, body.name)
    except KeyError as exc:
        raise _err(exc)
    return {"musik": result}


# ------------------------------------------------------------ Presets

@app.get("/api/presets")
def get_presets() -> dict:
    return config.list_presets()


class PresetSave(BaseModel):
    name: str
    projekt: str


@app.post("/api/presets")
def post_preset(body: PresetSave) -> dict:
    _project_or_404(body.projekt)
    try:
        config.save_preset(body.name, config.load_config(body.projekt))
    except ValueError as exc:
        raise _err(exc)
    return config.list_presets()


class PresetApply(BaseModel):
    preset: str


@app.post("/api/projects/{name}/apply_preset")
def apply_preset(name: str, body: PresetApply) -> dict:
    _project_or_404(name)
    try:
        return config.apply_preset(name, body.preset)
    except ValueError as exc:
        raise _err(exc)


# ------------------------------------------------------------ Log / Kosten / Dateien

@app.get("/api/projects/{name}/log", response_class=PlainTextResponse)
def get_log(name: str) -> str:
    _project_or_404(name)
    return pipeline.read_log(name)


@app.get("/api/projects/{name}/cost_estimate")
def cost_estimate(name: str) -> dict:
    _project_or_404(name)
    return pipeline.estimate_costs(name)


@app.get("/api/projects/{name}/files/{relpath:path}")
def get_output_file(name: str, relpath: str):
    """Output-Dateien ausliefern (Vorschau-Videos, Thumbnails, XML, SRT)."""
    _project_or_404(name)
    base = paths.output_dir(name).resolve()
    target = (base / relpath).resolve()
    if not str(target).startswith(str(base) + "/") and target != base:
        raise HTTPException(403, "Pfad außerhalb des Output-Ordners")
    if not target.is_file():
        raise HTTPException(404, f"Datei nicht gefunden: {relpath}")
    return FileResponse(target)


# Statisches Frontend (muss nach den API-Routen gemountet werden)
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
