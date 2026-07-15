#!/usr/bin/env python3
"""autoedit starten: python run.py  (Dashboard auf http://127.0.0.1:8765)"""

import sys

if sys.version_info < (3, 11):
    sys.exit(
        f"FEHLER: autoedit braucht Python 3.11+, gefunden: "
        f"{sys.version.split()[0]}.\n"
        "Auf macOS:  brew install python@3.12\n"
        "Dann:       rm -rf .venv && python3.12 -m venv .venv && "
        "source .venv/bin/activate && pip install -r requirements.txt"
    )


def main() -> None:
    import uvicorn

    from autoedit import ffmpeg_utils

    ok, msg = ffmpeg_utils.check_ffmpeg()
    if not ok:
        print(f"FEHLER: {msg}", file=sys.stderr)
        sys.exit(1)
    print(f"✓ {msg}")
    print("Dashboard: http://127.0.0.1:8765")
    uvicorn.run("autoedit.web.app:app", host="127.0.0.1", port=8765, reload=False)


if __name__ == "__main__":
    main()
