"""
desktop.py — Desktop launcher (Layer 8).

Runs the FastAPI backend locally in a background thread and opens the web frontend
in a native window via pywebview. This is the "desktop today" path from CLAUDE.md:
backend runs locally, the universal web UI is served into a native window. The same
frontend is later wrapped (e.g. Capacitor) for phones against the same API.

Run:  python -m backend.desktop
"""

from __future__ import annotations

import threading
import time
from contextlib import closing

HOST = "127.0.0.1"
DEFAULT_PORT = 8756
APP_PATH = "/app/"


def _find_free_port(preferred: int = DEFAULT_PORT) -> int:
    import socket

    for port in (preferred, 0):
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            try:
                s.bind((HOST, port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("No free port available")


def _serve(port: int) -> None:
    import uvicorn

    from backend.api import create_app

    uvicorn.run(create_app(), host=HOST, port=port, log_level="warning")


def _wait_until_up(url: str, timeout: float = 10.0) -> None:
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=0.5)
            return
        except Exception:
            time.sleep(0.1)
    # Fall through; pywebview will still try to load and show any error.


def main() -> None:  # pragma: no cover - requires a display + pywebview
    import webview

    port = _find_free_port()
    threading.Thread(target=_serve, args=(port,), daemon=True).start()

    base = f"http://{HOST}:{port}"
    _wait_until_up(base + "/")
    webview.create_window("BokYup", base + APP_PATH, width=1100, height=760, min_size=(820, 560))
    webview.start()


if __name__ == "__main__":  # pragma: no cover
    main()
