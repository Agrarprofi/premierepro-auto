import pytest

from autoedit import music
from tests.conftest import prepared_project


def test_scan_library_with_tags(music_lib):
    tracks = music.scan_library()
    assert len(tracks) == 2
    ruhig = next(t for t in tracks if t["name"] == "ruhig_akustik.wav")
    assert ruhig["tags"] == {"genre": "Akustik", "stimmung": "ruhig"}
    assert ruhig["dauer"] == pytest.approx(20.0, abs=0.5)


def test_scan_library_filename_tags(music_lib):
    (music_lib / "tags.yaml").unlink()
    tracks = music.scan_library()
    ruhig = next(t for t in tracks if t["name"] == "ruhig_akustik.wav")
    assert "ruhig" in ruhig["tags"]["stichworte"]


def test_scan_library_empty_without_dir(env):
    assert music.scan_library() == []


def test_select_track_via_claude(projekt, fake_claude, music_lib):
    prepared_project(projekt, fake_claude, with_broll=False)
    result = music.select_track(projekt, client=fake_claude)
    assert result["name"] == "ruhig_akustik.wav"
    assert result["gain_db"] == -18.0
    assert music.load_selection(projekt)["quelle"] == "claude"


def test_set_track_manual_override(projekt, fake_claude, music_lib):
    prepared_project(projekt, fake_claude, with_broll=False)
    result = music.set_track(projekt, "treibend_rock.wav")
    assert result["quelle"] == "manuell"
    with pytest.raises(KeyError):
        music.set_track(projekt, "gibtsnicht.mp3")
    assert music.set_track(projekt, None) is None
    assert music.load_selection(projekt) is None
