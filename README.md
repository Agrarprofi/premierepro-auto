# autoedit – Auto-Edit-Pipeline für Premiere Pro

Lokales Python-Tool mit Web-Dashboard: Rohmaterial in einen Ordner werfen,
im Dashboard konfigurieren (Reel-Länge, B-Roll-Dauer, Musik ja/nein), das
Tool erzeugt eine **FCPXML-Datei** für Premiere Pro → fertige, editierbare
Sequenz mit allen Schnitten, Multicam-Sync, B-Rolls und Untertiteln.

## Pipeline

```
Ingest → Transkript (WhisperX) → Sync (Kreuzkorrelation) → Schnitt (Claude)
       → B-Roll (Claude Vision) → Untertitel (SRT) → Musik → FCPXML-Export
```

| Spur | Inhalt |
|------|--------|
| V1   | Kamera A, nach Segmenten geschnitten |
| V2   | Kamera B, synchron (pro Segment Kamera wählbar) |
| V3   | B-Rolls an den zugeordneten Stellen |
| A1   | DJI-Audio, synchron geschnitten |
| A2   | Kamera-Ton als Backup (Spur deaktiviert) |
| A3   | Musik, −18 dB Startpegel |

## Setup (macOS, Apple Silicon)

```bash
# 1. ffmpeg
brew install ffmpeg

# 2. Python-Umgebung (3.11+)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. API-Key
cp .env.example .env   # und ANTHROPIC_API_KEY eintragen

# 4. Starten
python run.py          # → http://127.0.0.1:8765
```

Beim ersten Transkriptionslauf lädt WhisperX das `large-v3`-Modell
(~3 GB). Auf Apple Silicon läuft es mit `compute_type="int8"` auf der CPU.

## Projektstruktur

```
projects/<projektname>/
├── config.yaml            # Reel-Länge, B-Roll-Dauer, Sprache, Musik, …
├── skript.txt             # optional: Skript/Stichworte für den Schnitt
├── input/                 #   (auch hier möglich; .txt oder .md)
│   ├── cam_a/             # Hauptkamera (MP4/MOV)
│   ├── cam_b/             # Zweite Kamera
│   ├── audio_dji/         # DJI-Mic-WAV (beste Tonqualität, Sync-Referenz)
│   └── broll/             # B-Roll-Clips
└── output/
    ├── transcript.json/.txt
    ├── sync.json          # Offsets aller Quellen zur DJI-Referenz
    ├── segments.json      # gewählte Reel-Segmente (Dashboard: an/aus, Reihenfolge)
    ├── broll_matches.json
    ├── reel.srt / reel_korrigiert.srt
    ├── <projekt>_premiere.xml   # → Premiere: Datei > Importieren
    │                            #   (landet in Ablage "<projekt> – autoedit")
    ├── costs.json / log.txt
    └── preview_*.mp4      # Sync- und Schnitt-Vorschauen
```

**Dateinamen-Reihenfolge = Aufnahmereihenfolge** (bei mehreren Clips pro
Kamera). Die Sync-Referenz ist die erste DJI-Datei; fehlt DJI-Audio, wird
die Tonspur von Kamera A verwendet.

**Variable Framerate (VFR):** Dateien mit variabler Framerate (Handys,
manche Kameras) erkennt der Ingest automatisch und wandelt sie einmalig
nach konstanter Framerate (`output/cfr/…`, Video visuell verlustfrei neu
kodiert, Ton 1:1 kopiert). Vorschau und Premiere-Export verwenden dann die
CFR-Kopie – Bild und Ton bleiben so fest verbunden, in Premiere driftet
nichts. Die Wandlung kann bei langen Clips einige Minuten dauern und läuft
nur beim ersten Ingest (danach gecacht).

## config.yaml

```yaml
reel_laenge_sek: 60        # Ziellänge des Reels
broll_dauer_sek: 2.0       # Länge jeder B-Roll-Einblendung
broll_ziel_anzahl: 0       # 0 = automatisch (max. 40%); z.B. 15 Schnittbilder
broll_min_abstand_sek: 1.0 # Mindestabstand zwischen zwei B-Rolls
sprache: de
pausen_schnitt_sek: 1.0    # Innen-Pausen ab dieser Länge rausschneiden (0 = aus)
schnitt_hinweise: ""       # Freitext-Vorgaben für die Reel-Auswahl
musik_aktiv: false
untertitel_aktiv: true
export_format: quelle      # oder "9:16" (1080×1920, Clips zentriert skaliert)
claude_modell: claude-sonnet-4-6
whisper_modell: large-v3   # "large-v3-turbo" = ~4x schnellere Transkription
```

## Performance (Apple Silicon)

Alles außer den Claude-API-Aufrufen läuft lokal. Die zwei schweren
lokalen Schritte nutzen die Hardware voll aus:

- **Transkription**: läuft auf allen CPU-Kernen (CTranslate2/int8).
  `whisper_modell: large-v3-turbo` transkribiert ~4x schneller bei fast
  gleicher Qualität – fürs Interview-Deutsch meist völlig ausreichend.
- **CFR-Wandlung**: kodiert und dekodiert über die Video-Hardware
  (VideoToolbox) und wandelt mehrere Dateien parallel. Läuft pro Datei
  nur einmal (Cache).

## Warteschlange (Nacht-Modus)

Mehrere Projekte vorbereiten (Dateien in die input-Ordner, optional
`skript.txt` daneben), in der Seitenleiste ankreuzen und **„🌙 Nacheinander
abarbeiten"** klicken: Jedes Projekt läuft komplett durch (bereits grüne
Schritte werden übersprungen), ein Fehler in einem Projekt stoppt die
anderen nicht – Details stehen im Log des jeweiligen Projekts. Auf macOS
hält `caffeinate` den Rechner wach, solange die Warteschlange läuft
(Deckel trotzdem offen lassen bzw. Netzteil anstecken). Morgens liegen
die fertigen XMLs in den Output-Ordnern.

## Reel-Auswahl (zweistufig)

Der Schnitt-Schritt arbeitet auf annotiertem Rohmaterial: Sprechpausen werden
markiert und **wiederholte Anläufe erkannt** (Falschstart → Pause → der Satz
kommt nochmal, meist sauber). Regel: immer der letzte Take.

**Skript/Leitfaden (optional):** Text ins Feld im Schnitt-Bereich tippen
oder eine `skript.txt` in den Projektordner legen. Das Skript dient als
inhaltlicher Leitfaden, nicht als sture Vorgabe: Die KI gleicht das
Material damit ab und wählt die stärksten Aussagen zur gewünschten
Botschaft – Hook-Dramaturgie und Take-Regeln gelten weiter, Wortlaut und
Reihenfolge des Skripts sind nicht bindend.

1. **Aussagen-Analyse**: Claude bewertet alle Statements (Punkte 0–10,
   Kategorie Hook/Kern/Abschluss, Qualität sauber/Versprecher/abgebrochen)
   → `output/statements.json`, im Dashboard einsehbar.
2. **Auswahl**: Auf Basis der Bewertung wird das Reel gebaut
   (Hook → Kernaussage → Abschluss), unter Berücksichtigung der
   `schnitt_hinweise` aus der Config.

Presets lassen sich im Dashboard speichern/anwenden (z. B.
„Reel 60s / B-Roll 2s / mit Musik“).

## Musik

Lizenzfreie Tracks in `music_library/` legen. Optional `music_library/tags.yaml`:

```yaml
ruhig_akustik.mp3: {genre: Akustik, stimmung: ruhig}
```

Ohne tags.yaml werden die Dateinamen-Tokens als Tags genutzt
(z. B. `ruhig_akustik_feld.mp3`).

## Untertitel in Premiere (der eine manuelle Schritt)

1. `reel_korrigiert.srt` importieren (Datei > Importieren)
2. Auf die Untertitel-Spur ziehen
3. Gespeicherten Track Style anwenden (~10 Sekunden)

Details: `output/README_UNTERTITEL.txt`

## Kosten

Grob 0,50–2 € pro Video (größter Posten: Vision-Analyse der B-Rolls).
Das Dashboard zeigt eine Schätzung pro Lauf und die tatsächlichen Kosten
(`output/costs.json`).

## Entwicklung / Tests

Die Tests laufen ohne echtes Material, WhisperX oder API-Key – synthetische
Medien werden mit ffmpeg erzeugt, Claude/WhisperX werden gefakt:

```bash
pytest
```
