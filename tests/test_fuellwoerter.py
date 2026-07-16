"""Füllwort-Entfernung, Phase 1: Erkennung gegen ein Beispiel-Transkript."""

import pytest

from autoedit import fuellwoerter, paths


def _woerter(saetze, start=10.0, wortdauer=0.28, luecke=0.05):
    """[(satz, pause_danach_sek), ...] -> Word-Level-Transkript."""
    words = []
    t = start
    for text, pause in saetze:
        for w in text.split():
            words.append({"word": w, "start": round(t, 3),
                          "end": round(t + wortdauer, 3)})
            t += wortdauer + luecke
        t += pause
    return words


# Beispiel-Transkript mit allen Fällen: Füllwort, Doppelung, bewusste
# Verstärkung, lange Pause, Versprecher (Abbruch + Neustart), Optional-Wort
BEISPIEL = _woerter([
    ("Die Kartoffelernte war ähm richtig gut heuer.", 0.3),
    ("Wir setzen auf die die regenerative Landwirtschaft.", 0.3),
    ("Der Boden ist ganz ganz wichtig für uns.", 2.5),
    ("Wir haben heuer", 0.4),
    ("Wir haben heuer richtig gute Erträge eingefahren.", 0.3),
    ("Das ist halt so bei uns am Hof.", 0.0),
])


def _typen(stellen):
    return [s["typ"] for s in stellen]


def test_beispiel_findet_alle_faelle():
    stellen = fuellwoerter.finde_schnittstellen(BEISPIEL)

    # Konservativ: genau 3 Stellen - ähm, die-Doppelung, Pause+Versprecher
    # (Pause und erster Anlauf grenzen aneinander -> zu EINEM Schnitt
    # verschmolzen, Punkt 2 der Spezifikation)
    assert len(stellen) == 3
    assert _typen(stellen) == ["fuellwort", "doppelung", "stille+versprecher"]

    aehm, doppel, kombi = stellen
    assert aehm["text"] == "ähm"
    assert "war **ähm** richtig" in aehm["kontext"]
    assert doppel["text"] == "die"
    assert "auf **die** die" in doppel["kontext"]
    assert "Wir haben heuer" in kombi["text"]

    # bewusste Verstärkung "ganz ganz" bleibt drin
    assert not any("ganz" in s["text"] for s in stellen)
    # Optional-Wort "halt" ohne Freischaltung NICHT erkannt
    assert not any("halt" in s["text"] for s in stellen)
    # alle Stellen standardmäßig aktiv, mit Dauer und Id
    for s in stellen:
        assert s["aktiv"] is True
        assert s["dauer"] > 0
        assert s["id"]


def test_optionale_liste_zuschaltbar():
    stellen = fuellwoerter.finde_schnittstellen(BEISPIEL, optionale=True)
    assert any(s["text"] == "halt" for s in stellen)


def test_sicherheits_padding_schuetzt_nachbarwoerter():
    """Kein Schnitt näher als ~100 ms an einem behaltenen Wort."""
    stellen = fuellwoerter.finde_schnittstellen(BEISPIEL)
    geschnitten_text = " ".join(s["text"] for s in stellen)
    for s in stellen:
        for w in BEISPIEL:
            if w["word"].rstrip(".") in geschnitten_text:
                continue  # Wort wird selbst entfernt
            drin = w["start"] >= s["start"] - 1e-6 and \
                w["end"] <= s["ende"] + 1e-6
            assert not drin, f"{w['word']} läge im Schnitt {s['text']}"
            # behaltene Wörter: Abstand zur Schnittgrenze >= Puffer
            if w["end"] <= s["start"]:
                assert s["start"] - w["end"] >= fuellwoerter.PAD_SEC - 1e-6 \
                    or s["typ"] == "stille"
            if w["start"] >= s["ende"]:
                assert w["start"] - s["ende"] >= fuellwoerter.PAD_SEC - 1e-6 \
                    or s["typ"] == "stille"


def test_pause_wird_gekuerzt_nicht_entfernt():
    words = _woerter([("Erster Satz endet hier.", 2.5),
                      ("Zweiter Satz beginnt neu.", 0.0)])
    stellen = fuellwoerter.finde_schnittstellen(words, pause_ab=1.5,
                                                pause_ziel=0.4)
    assert _typen(stellen) == ["stille"]
    s = stellen[0]
    # Lücke = 2.5 s Pause + 0.05 s Wortabstand -> 2.15 s Schnitt,
    # es bleiben 0.2 s Luft vor und nach dem Schnitt
    assert s["dauer"] == pytest.approx(2.15, abs=0.01)
    ende_wort = max(w["end"] for w in words if w["end"] <= s["start"])
    start_wort = min(w["start"] for w in words if w["start"] >= s["ende"])
    assert s["start"] - ende_wort == pytest.approx(0.2, abs=0.01)
    assert start_wort - s["ende"] == pytest.approx(0.2, abs=0.01)
    # kürzere Pausen unter dem Schwellwert: nichts zu tun
    assert fuellwoerter.finde_schnittstellen(words, pause_ab=3.0) == []


def test_versprecher_konservativ():
    # Fertiger Satz mit Punkt + bewusste Wiederholung -> KEIN Schnitt
    rhetorik = _woerter([("Der Boden lebt.", 0.5), ("Der Boden lebt.", 0.0)])
    assert fuellwoerter.finde_schnittstellen(rhetorik) == []

    # Neustart erst nach 4 s -> außerhalb des 3-s-Fensters, kein Schnitt
    spaet = _woerter([("Wir haben heuer", 4.0),
                      ("Wir haben heuer gute Erträge.", 0.0)])
    stellen = fuellwoerter.finde_schnittstellen(spaet)
    assert not any(s["typ"].startswith("versprecher") for s in stellen)

    # klassischer Abbruch binnen 3 s -> erster Versuch fliegt
    abbruch = _woerter([("Wir haben heuer", 0.5),
                        ("Wir haben heuer gute Erträge.", 0.0)])
    stellen = fuellwoerter.finde_schnittstellen(abbruch)
    treffer = [s for s in stellen if "versprecher" in s["typ"]]
    assert len(treffer) == 1
    assert treffer[0]["text"] == "Wir haben heuer"


def test_merge_benachbarter_fuellwoerter():
    words = _woerter([("Das war ähm äh wirklich gut.", 0.0)])
    stellen = fuellwoerter.finde_schnittstellen(words)
    assert len(stellen) == 1
    assert stellen[0]["typ"] == "fuellwort"
    assert stellen[0]["text"] == "ähm äh"


def test_eigene_wortliste_wird_gelesen(env):
    f = fuellwoerter.liste_datei()
    f.write_text("immer: [servas]\n", encoding="utf-8")
    liste = fuellwoerter.lade_liste()
    assert liste["immer"] == ["servas"]
    assert "halt" in liste["optional"]  # fehlende Schlüssel: Defaults

    words = _woerter([("Ja servas das ist neu.", 0.0)])
    stellen = fuellwoerter.finde_schnittstellen(words, liste=liste)
    assert [s["text"] for s in stellen] == ["servas"]


def test_default_wortliste_wird_angelegt(env):
    assert not fuellwoerter.liste_datei().is_file()
    liste = fuellwoerter.lade_liste()
    assert fuellwoerter.liste_datei().is_file()   # editierbar für den Nutzer
    assert "ähm" in liste["immer"]


def test_analysiere_projekt(env, media, projekt):
    from autoedit import ingest, transcribe

    def transkript(wav_path, language, progress=None):
        return BEISPIEL

    ingest.scan_project(projekt)
    transcribe.transcribe_project(projekt, transcriber=transkript)
    result = fuellwoerter.analysiere(projekt)

    assert result["anzahl"] == 3
    assert result["entfernt_sek"] > 0
    assert result["dauer_nachher_sek"] == pytest.approx(
        result["dauer_vorher_sek"] - result["entfernt_sek"], abs=0.01)
    gespeichert = fuellwoerter.load_stellen(projekt)
    assert gespeichert["stellen"] == result["stellen"]
    assert (paths.output_dir(projekt) / "fuellwoerter.json").is_file()
