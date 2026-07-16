#!/bin/bash
# autoedit per Doppelklick starten (macOS öffnet .command-Dateien im Terminal)
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Noch kein Setup. Bitte einmalig im Terminal ausführen:"
  echo "  python3.12 -m venv .venv"
  echo "  source .venv/bin/activate"
  echo "  pip install -r requirements.txt"
  read -r -p "Enter zum Schließen …"
  exit 1
fi

source .venv/bin/activate
python run.py
