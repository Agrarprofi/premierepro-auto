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
