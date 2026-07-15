#!/usr/bin/env python3
"""autoedit starten: python run.py  (Dashboard auf http://127.0.0.1:8765)"""

import sys

import uvicorn

from autoedit import ffmpeg_utils


def main() -> None:
    ok, msg = ffmpeg_utils.check_ffmpeg()
    if not ok:
        print(f"FEHLER: {msg}", file=sys.stderr)
        sys.exit(1)
    print(f"✓ {msg}")
    print("Dashboard: http://127.0.0.1:8765")
    uvicorn.run("autoedit.web.app:app", host="127.0.0.1", port=8765, reload=False)


if __name__ == "__main__":
    main()
