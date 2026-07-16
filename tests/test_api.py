import time

import pytest
from fastapi.testclient import TestClient

from autoedit import config, paths
from autoedit.web.app import app
from tests.conftest import FakeClaude, fake_transcriber, prepared_project


@pytest.fixture
def client(env):
    return TestClient(app)


def test_health(client):
    data = client.get("/api/health").json()
    assert "ffmpeg" in data["ffmpeg"] or "gefunden" in data["ffmpeg"]
    assert "projekte_ordner" in data


def test_dashboard_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "autoedit" in res.text


def test_create_and_list_projects(client):
    res = client.post("/api/projects", json={"name": "api-test"})
    assert res.status_code == 200
    assert paths.project_exists("api-test")

    res = client.post("/api/projects", json={"name": "api-test"})
    assert res.status_code == 409
    res = client.post("/api/projects", json={"name": "../böse"})
    assert res.status_code == 400

    names = [p["name"] for p in client.get("/api/projects").json()]
    assert "api-test" in names


def test_config_endpoints(client):
    client.post("/api/projects", json={"name": "cfg"})
    cfg = client.get("/api/projects/cfg/config").json()
    assert cfg["reel_laenge_sek"] == 60

    res = client.put("/api/projects/cfg/config", json={"reel_laenge_sek": 30})
    assert res.status_code == 200
    assert res.json()["reel_laenge_sek"] == 30

    res = client.put("/api/projects/cfg/config", json={"reel_laenge_sek": 1})
    assert res.status_code == 400
    assert client.get("/api/projects/fehlt/config").status_code == 404


def test_overview_and_segments_flow(client, projekt, fake_claude):
    prepared_project(projekt, fake_claude, with_broll=False)

    data = client.get(f"/api/projects/{projekt}/overview").json()
    assert data["transcript_vorhanden"] is True
    assert data["sync"]["referenz"] == "input/audio_dji/dji.wav"
    assert len(data["segments"]["segmente"]) == 2

    ids = [s["id"] for s in data["segments"]["segmente"]]
    res = client.put(f"/api/projects/{projekt}/segments",
                     json={"aktiv": {ids[0]: False}})
    assert res.status_code == 200
    segs = res.json()["segments"]["segmente"]
    assert segs[0]["aktiv"] is False


def test_step_job_via_api(client, projekt):
    res = client.post(f"/api/projects/{projekt}/steps/ingest", json={})
    assert res.status_code == 200
    job_id = res.json()["id"]
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "laeuft":
            break
        time.sleep(0.1)
    assert job["status"] == "fertig", job
    assert (paths.output_dir(projekt) / "media_info.json").is_file()


def test_output_file_serving_and_traversal(client, projekt):
    out = paths.output_dir(projekt)
    out.mkdir(parents=True, exist_ok=True)
    (out / "test.txt").write_text("hallo")
    res = client.get(f"/api/projects/{projekt}/files/test.txt")
    assert res.status_code == 200 and res.text == "hallo"
    res = client.get(f"/api/projects/{projekt}/files/../config.yaml")
    assert res.status_code in (403, 404)


def test_presets_api(client):
    client.post("/api/projects", json={"name": "pra"})
    client.put("/api/projects/pra/config", json={"reel_laenge_sek": 42})
    res = client.post("/api/presets", json={"name": "kurz", "projekt": "pra"})
    assert "kurz" in res.json()

    client.post("/api/projects", json={"name": "prb"})
    res = client.post("/api/projects/prb/apply_preset", json={"preset": "kurz"})
    assert res.json()["reel_laenge_sek"] == 42


def test_music_endpoints(client, projekt, fake_claude, music_lib):
    prepared_project(projekt, fake_claude, with_broll=False)
    tracks = client.get("/api/music/library").json()
    assert len(tracks) == 2
    res = client.put(f"/api/projects/{projekt}/music",
                     json={"name": "treibend_rock.wav"})
    assert res.json()["musik"]["name"] == "treibend_rock.wav"


def test_sync_offset_and_video_korrektur_api(client, projekt, fake_claude):
    prepared_project(projekt, fake_claude, with_broll=False)
    url = f"/api/projects/{projekt}/sync/offsets"
    rel = "input/cam_b/cam_b_001.mp4"

    # weder Offset noch Bild-Korrektur angegeben -> 400
    res = client.put(url, json={"relpfad": rel})
    assert res.status_code == 400

    res = client.put(url, json={"relpfad": rel,
                                "video_korrektur_sekunden": 0.2})
    assert res.status_code == 200
    entry = res.json()["offsets"][rel]
    assert entry["video_korrektur_sekunden"] == 0.2
    assert "offset_sekunden" in entry  # berechneter Offset bleibt erhalten

    res = client.put(url, json={"relpfad": "input/cam_b/gibtsnicht.mp4",
                                "video_korrektur_sekunden": 0.2})
    assert res.status_code == 400


def test_running_job_endpoint(client, projekt):
    import threading

    from autoedit import jobs

    url = f"/api/projects/{projekt}/jobs/running"
    assert client.get(url).json()["job"] is None

    ev = threading.Event()
    job = jobs.MANAGER.start(f"{projekt}:warte", lambda p: ev.wait(5))
    r = client.get(url).json()["job"]
    assert r is not None
    assert r["id"] == job.id
    assert "laufzeit_sekunden" in r

    ev.set()
    for _ in range(100):
        if jobs.MANAGER.get(job.id).status != "laeuft":
            break
        time.sleep(0.05)
    assert client.get(url).json()["job"] is None


def test_overview_includes_script_file(client, projekt):
    from autoedit import paths as p

    ov = client.get(f"/api/projects/{projekt}/overview").json()
    assert ov["skript_datei"] is None

    (p.project_dir(projekt) / "skript.txt").write_text(
        "Mein Reel-Skript", encoding="utf-8")
    ov = client.get(f"/api/projects/{projekt}/overview").json()
    assert ov["skript_datei"]["datei"] == "skript.txt"
    assert ov["skript_datei"]["text"] == "Mein Reel-Skript"


def test_batch_api_validation(client, projekt):
    res = client.post("/api/batch", json={"projekte": []})
    assert res.status_code == 400
    res = client.post("/api/batch", json={"projekte": ["gibtsnicht"]})
    assert res.status_code == 404
    assert client.get("/api/batch/running").json()["job"] is None


def test_batch_blocks_parallel_jobs(client, projekt):
    import threading

    from autoedit import jobs

    ev = threading.Event()
    jobs.MANAGER.start("batch:run_all", lambda p: ev.wait(5))
    try:
        # Einzelschritt während der Warteschlange -> 409
        res = client.post(f"/api/projects/{projekt}/steps/ingest", json={})
        assert res.status_code == 409
        # zweite Warteschlange -> 409
        res = client.post("/api/batch", json={"projekte": [projekt]})
        assert res.status_code == 409
        assert client.get("/api/batch/running").json()["job"] is not None
    finally:
        ev.set()
        for _ in range(100):
            if jobs.MANAGER.running_any() is None:
                break
            time.sleep(0.05)


def test_cancel_job(client, projekt):
    from autoedit import jobs

    def langsam(progress):
        for i in range(200):
            progress(i / 200, f"Schritt {i}")  # wirft nach dem Abbruch
            time.sleep(0.02)

    job = jobs.MANAGER.start(f"{projekt}:langsam", langsam)
    time.sleep(0.1)
    res = client.post(f"/api/jobs/{job.id}/cancel")
    assert res.status_code == 200

    for _ in range(100):
        j = client.get(f"/api/jobs/{job.id}").json()
        if j["status"] != "laeuft":
            break
        time.sleep(0.05)
    assert j["status"] == "abgebrochen"

    res = client.post("/api/jobs/gibtsnicht/cancel")
    assert res.status_code == 404


def test_cancel_not_swallowed_by_optional_steps(projekt, fake_claude):
    """Ein Abbruch während eines optionalen Schritts (B-Roll) darf die
    Kette NICHT weiterlaufen lassen."""
    import pytest as _pytest

    from autoedit import jobs, pipeline
    from tests.conftest import fake_transcriber, prepared_project

    prepared_project(projekt, fake_claude, with_broll=False)

    class AbbruchClaude:
        def complete_json(self, zweck, *a, **kw):
            raise jobs.JobAbgebrochen()
        complete_text = complete_json
        describe_images_json = complete_json

    with _pytest.raises(jobs.JobAbgebrochen):
        pipeline.run_all(projekt, fortsetzen=True, client=AbbruchClaude(),
                         transcriber=fake_transcriber)


def test_script_save_api(client, projekt):
    from autoedit import paths as p

    url = f"/api/projects/{projekt}/script"
    res = client.put(url, json={"text": "Mein Reel-Skript"})
    assert res.status_code == 200
    assert res.json()["skript_datei"]["datei"] == "skript.txt"
    assert (p.project_dir(projekt) / "skript.txt").is_file()

    # Overview liefert das gespeicherte Skript (fürs Vorbefüllen)
    ov = client.get(f"/api/projects/{projekt}/overview").json()
    assert ov["skript_datei"]["text"] == "Mein Reel-Skript"

    # leerer Text löscht
    res = client.put(url, json={"text": ""})
    assert res.json()["skript_datei"] is None
    assert not (p.project_dir(projekt) / "skript.txt").exists()


def test_delete_project_api(client, projekt):
    from pathlib import Path

    from autoedit import paths as p

    assert p.project_exists(projekt)
    res = client.delete(f"/api/projects/{projekt}")
    assert res.status_code == 200
    data = res.json()
    assert data["geloescht"] == projekt
    assert not p.project_exists(projekt)

    # Projekt liegt vollständig im Papierkorb (nichts zerstört)
    ziel = Path(data["papierkorb"])
    assert ziel.parent == p.trash_dir()
    assert (ziel / "input/cam_a/cam_a_001.mov").is_file()

    # zweites Löschen: Projekt existiert nicht mehr
    assert client.delete(f"/api/projects/{projekt}").status_code == 404

    # Namenskollision im Papierkorb: neues Projekt mit gleichem Namen
    # löschen -> eigener Eintrag mit Zeitstempel
    p.create_project(projekt)
    res = client.delete(f"/api/projects/{projekt}")
    ziel2 = Path(res.json()["papierkorb"])
    assert ziel2 != ziel and ziel2.is_dir()


def test_delete_project_blocked_while_job_running(client, projekt):
    import threading

    from autoedit import jobs
    from autoedit import paths as p

    ev = threading.Event()
    job = jobs.MANAGER.start(f"{projekt}:warte", lambda pr: ev.wait(5))
    try:
        res = client.delete(f"/api/projects/{projekt}")
        assert res.status_code == 409
        assert p.project_exists(projekt)
    finally:
        ev.set()
        for _ in range(100):
            if jobs.MANAGER.get(job.id).status != "laeuft":
                break
            time.sleep(0.05)


def test_preset_delete_api(client, projekt):
    client.post("/api/presets", json={"name": "test-preset",
                                      "projekt": projekt})
    assert "test-preset" in client.get("/api/presets").json()

    res = client.delete("/api/presets/test-preset")
    assert res.status_code == 200
    assert "test-preset" not in res.json()
    assert client.delete("/api/presets/test-preset").status_code == 404


def test_input_files_watch_endpoint(client, projekt):
    """Datei-Watcher: Verzeichnis-Liste + Fingerprint ändern sich, sobald
    im input-Ordner etwas dazukommt (Dashboard pollt und aktualisiert)."""
    from autoedit import paths as p

    r1 = client.get(f"/api/projects/{projekt}/input_files").json()
    assert "cam_a_001.mov" in r1["dateien"]["cam_a"]
    assert r1["fingerprint"]

    # unverändert -> gleicher Fingerprint
    r1b = client.get(f"/api/projects/{projekt}/input_files").json()
    assert r1b["fingerprint"] == r1["fingerprint"]

    # neue Datei im Finder abgelegt -> neuer Fingerprint, Datei gelistet
    (p.input_dir(projekt, "broll") / "frisch.mp4").write_bytes(b"x" * 128)
    r2 = client.get(f"/api/projects/{projekt}/input_files").json()
    assert r2["fingerprint"] != r1["fingerprint"]
    assert "frisch.mp4" in r2["dateien"]["broll"]
