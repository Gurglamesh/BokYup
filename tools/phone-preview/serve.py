#!/usr/bin/env python3
"""
serve.py — assemble and serve the PHONE web bundle so you can try the phone app in a
browser (including your phone's browser on the same network) BEFORE wrapping it as an
APK/IPA. It is the exact bundle Capacitor packages: the BokYup web UI + the Python
backend running as WebAssembly (Pyodide) in-page, fully local, no server backend.

  python tools/phone-preview/serve.py            # http://<this-host>:8794
  python tools/phone-preview/serve.py --port 9000 --host 0.0.0.0

First run assembles phone/www (UI + backend_src.zip). You must also drop the Pyodide
runtime + crypto wasm wheels into phone/www/vendor/ once (this script lists what's
missing; see tools/build_phone_assets.py / phone/README.md). Open the printed URL on a
phone browser to try the app — the same engine the Android WebView uses.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import socket
import socketserver
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WWW = ROOT / "phone/www"
NEEDED = ["pyodide.js", "pyodide.asm.wasm", "python_stdlib.zip", "pyodide-lock.json"]


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".wasm": "application/wasm", ".mjs": "text/javascript", ".js": "text/javascript",
        ".whl": "application/zip", ".zip": "application/zip", ".json": "application/json",
    }

    def end_headers(self):
        # No cross-origin isolation needed (Argon2 parallelism=1, no WASM threads).
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8794)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()

    subprocess.run([sys.executable, str(ROOT / "phone/build_www.py")], check=True)

    missing = [f for f in NEEDED if not (WWW / "vendor" / f).exists()]
    wheels = list((WWW / "vendor").glob("*.whl"))
    if missing or not wheels:
        print("\n⚠  phone/www/vendor is missing the Pyodide runtime / wheels:")
        for f in missing:
            print("   -", f)
        if not wheels:
            print("   - the crypto wasm wheels (cryptography, argon2-cffi, ...)")
        print("Add them once (see phone/README.md), then re-run. Not serving yet.")
        sys.exit(1)

    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer((args.host, args.port),
                                   functools.partial(Handler, directory=str(WWW)))
    ip = _lan_ip()
    print(f"\nServing the phone app bundle from {WWW}")
    print(f"  local:   http://127.0.0.1:{args.port}/")
    print(f"  network: http://{ip}:{args.port}/   (open this on your phone's browser)")
    print("Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
