#!/usr/bin/env python3
"""
build_www.py — assemble the phone WebView bundle (phone/www) for the Capacitor app.

The phone app is the SAME web frontend as the desktop, plus the Pyodide runtime that
runs the SAME Python backend in-process (see backend/api/static/pyodide-boot.js). This
script gathers everything the WebView loads:

  1. copies backend/api/static/* into phone/www/
  2. builds phone/www/vendor/backend_src.zip (the backend package) and reports the
     prebuilt Pyodide runtime + wasm wheels still to drop into phone/www/vendor/
  3. writes phone/www/index.html = the desktop index with two extra <script> tags
     (vendor/pyodide.js + pyodide-boot.js) injected before app.js, so the phone build
     boots the in-process backend while the desktop build keeps using fetch.

After this, add the Pyodide runtime + wheels to phone/www/vendor/ (see the printed
list / tools/build_phone_assets.py), then run Capacitor (see phone/README.md).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "backend/api/static"
WWW = ROOT / "phone/www"
INJECT = ('  <script src="vendor/pyodide.js"></script>\n'
          '  <script src="pyodide-boot.js"></script>\n'
          '  <script src="native-bridge.js"></script>\n')


def main() -> None:
    WWW.mkdir(parents=True, exist_ok=True)
    for item in STATIC.iterdir():
        if item.is_file():
            shutil.copy2(item, WWW / item.name)

    # Inject the Pyodide bootstrap before app.js in the phone's index.html.
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    needle = '  <script src="app.js"></script>'
    if needle not in html:
        raise SystemExit("index.html layout changed: could not find app.js script tag")
    (WWW / "index.html").write_text(html.replace(needle, INJECT + needle), encoding="utf-8")

    # backend_src.zip (+ report the remaining prebuilt assets).
    subprocess.run([sys.executable, str(ROOT / "tools/build_phone_assets.py"),
                    "--out", str(WWW / "vendor")], check=True)

    print(f"\nphone/www assembled at {WWW}")
    print("Next: add the Pyodide runtime + wasm wheels to phone/www/vendor/, "
          "then `cd phone && npx cap sync` (see phone/README.md).")


if __name__ == "__main__":
    main()
