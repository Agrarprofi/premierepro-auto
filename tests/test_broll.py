import pytest

from autoedit import broll, paths
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
