import pytest

from autoedit import broll, ingest, paths
from tests.conftest import prepared_project


def test_analyze_broll(projekt, fake_claude):
    prepared_project(projekt, fake_claude, with_broll=False)
    result = broll.analyze_broll(projekt, client=fake_claude)
    clips = result["clips"]
    assert len(clips) == 2
    for c in clips:
        assert c["beschreibung"]
        assert len(c["schlagwoerter"]) == 5
        assert 0.0 <= c["beste_einstiegszeit"] <= c["dauer"]
        thumb = paths.output_dir(projekt) / c["thumbnail"]
        assert thumb.is_file()
    # alle 2 s ein Frame bei 8 s Clip -> 4 Frames extrahiert
    frames = list((paths.output_dir(projekt) / "tmp" / "broll_frames"
                   / "broll_traktor").glob("*.jpg"))
    assert 3 <= len(frames) <= 5


def test_match_broll_enforces_rules(projekt, fake_claude):
    prepared_project(projekt, fake_claude, with_broll=False)
    broll.analyze_broll(projekt, client=fake_claude)
    result = broll.match_broll(projekt, client=fake_claude)
    matches = result["matches"]
    # Fake liefert 3 Kandidaten: t=1.0 (Hook-Sperre), t=4.0 (ok),
    # t=5.0 (zu nah am vorigen) -> genau 1 überlebt
    assert len(matches) == 1
    m = matches[0]
    assert m["transkript_zeit"] == 4.0
    assert m["broll_datei"] == "input/broll/broll_traktor.mp4"
    assert m["dauer"] == 2.0


def test_enforce_rules_budget():
    cands = [
        {"id": str(i), "transkript_zeit": float(t), "broll_einstieg": 0.0,
         "dauer": 2.0, "broll_datei": "x", "begruendung": ""}
        for i, t in enumerate([4.0, 8.0, 12.0, 16.0, 20.0])
    ]
    # Reel 30 s, 40 % Budget = 12 s -> max. 6 Einblendungen; Abstand ok
    ok = broll._enforce_rules([dict(c) for c in cands], 2.0, 30.0)
    assert len(ok) == 5
    # Reel 12 s: Budget 4.8 s -> nur 2 Einblendungen à 2 s; Rest fällt weg
    ok = broll._enforce_rules([dict(c) for c in cands], 2.0, 12.0)
    assert [m["transkript_zeit"] for m in ok] == [4.0, 8.0]


def test_delete_match(projekt, fake_claude):
    prepared_project(projekt, fake_claude)
    data = broll.load_matches(projekt)
    mid = data["matches"][0]["id"]
    result = broll.delete_match(projekt, mid)
    assert result["matches"] == []
    with pytest.raises(KeyError):
        broll.delete_match(projekt, "gibtsnicht")


def test_enforce_rules_target_count_60s_reel():
    """60s-Reel, Ziel 15 Schnittbilder à 2 s mit 1 s Abstand -> genau 15."""
    cands = [
        {"id": str(i), "transkript_zeit": 3.5 + 3.0 * i, "broll_einstieg": 0.0,
         "dauer": 2.0, "broll_datei": "x", "begruendung": ""}
        for i in range(19)
    ]
    ok = broll._enforce_rules([dict(c) for c in cands], 2.0, 60.0,
                              min_abstand=1.0, budget=15 * 2.0)
    assert len(ok) == 15


def test_match_broll_with_target_count(projekt):
    """broll_ziel_anzahl ersetzt die 40%-Regel; physikalisch Unmögliches
    wird auf das Machbare begrenzt."""
    from autoedit import config
    from tests.conftest import FakeClaude, prepared_project

    # Reel ist ~9.9 s lang: bei 2 s Dauer + 0.5 s Abstand passen max. 2
    # Einblendungen nach der Hook-Sperre -> Ziel 15 wird auf 2 begrenzt
    config.save_config(projekt, {"broll_ziel_anzahl": 15,
                                 "broll_min_abstand_sek": 0.5})
    viele = [
        {"transkript_zeit": 3.5 + 3.0 * i,
         "broll_datei": "input/broll/broll_traktor.mp4",
         "broll_einstieg": 0.5 * i, "begruendung": "passt"}
        for i in range(10)
    ]
    fake = FakeClaude(broll_matches=viele)
    prepared_project(projekt, fake, with_broll=False)
    broll.analyze_broll(projekt, client=fake)
    result = broll.match_broll(projekt, client=fake)
    assert len(result["matches"]) == 2
    # Wiederverwendung desselben Clips ist erlaubt
    assert all(m["broll_datei"] == "input/broll/broll_traktor.mp4"
               for m in result["matches"])


def test_match_prompt_word_level_timeline(projekt, fake_claude):
    """B-Roll-Matching bekommt die wortgenaue Reel-Zeitachse, damit die
    Einblendung exakt beim passenden Wort liegt."""
    from tests.conftest import prepared_project
    prepared_project(projekt, fake_claude, with_broll=True)
    prompt = fake_claude.prompts["broll_matching"]
    assert "Wortgenaue Reel-Zeitachse" in prompt
    assert "EXAKT" in prompt
    # Wortzeilen mit Zeitmarken vorhanden
    import re
    assert re.search(r"\n\[\d+\.\d\] \w+", prompt)
    # Schnitt-Stand wird gespeichert
    data = broll.load_matches(projekt)
    assert data["reel_stand"]


def test_analyze_broll_cached_per_file(projekt, fake_claude):
    """Vision-Analyse läuft pro Datei nur einmal: unveränderte Dateien
    kommen aus dem Index-Cache, geänderte werden neu analysiert."""
    import os
    import time as _time

    from tests.conftest import FakeClaude

    ingest.scan_project(projekt)
    broll.analyze_broll(projekt, client=fake_claude)
    assert fake_claude.calls.count("broll_analyse") == 2

    # Zweiter Lauf, nichts geändert: keine Vision-Aufrufe, Index vollständig.
    # client=None beweist: es wird nicht mal ein Claude-Client gebraucht.
    result = broll.analyze_broll(projekt)
    assert len(result["clips"]) == 2
    assert all(e.get("fingerprint") for e in result["clips"])

    # Eine Datei "ändern" (mtime) -> nur diese wird neu analysiert
    f = paths.input_dir(projekt, "broll") / "broll_feld.mp4"
    zukunft = _time.time() + 30
    os.utime(f, (zukunft, zukunft))
    fake2 = FakeClaude()
    broll.analyze_broll(projekt, client=fake2)
    assert fake2.calls.count("broll_analyse") == 1

    # force=True analysiert alles neu
    fake3 = FakeClaude()
    broll.analyze_broll(projekt, client=fake3, force=True)
    assert fake3.calls.count("broll_analyse") == 2

    # Gelöschte Dateien fliegen aus dem Index
    f.unlink()
    result = broll.analyze_broll(projekt)
    assert [e["name"] for e in result["clips"]] == ["broll_traktor.mp4"]


def test_ensure_list_tolerates_claude_formats():
    from autoedit import claude_client

    assert claude_client.ensure_list([1, 2]) == [1, 2]
    # einzelnes Objekt statt Array (der Fehler aus dem LKOE-Log)
    einzel = {"transkript_zeit": 3.0, "broll_datei": "x.mov"}
    assert claude_client.ensure_list(einzel) == [einzel]
    # Liste in ein Objekt verpackt
    assert claude_client.ensure_list({"matches": [einzel]}) == [einzel]
    with pytest.raises(ValueError):
        claude_client.ensure_list("nur Text")


def test_match_broll_accepts_single_object(projekt, fake_claude):
    """Claude liefert bei nur einem Treffer manchmal ein Objekt statt
    einer Ein-Element-Liste - das darf den Schritt nicht abbrechen."""
    from tests.conftest import FakeClaude, prepared_project

    fake = FakeClaude(broll_matches={
        "transkript_zeit": 4.0,
        "broll_datei": "input/broll/broll_traktor.mp4",
        "broll_einstieg": 1.0, "begruendung": "einzelner Treffer",
    })
    prepared_project(projekt, fake, with_broll=True)
    matches = broll.load_matches(projekt)["matches"]
    assert len(matches) == 1
    assert matches[0]["broll_datei"].endswith("broll_traktor.mp4")
