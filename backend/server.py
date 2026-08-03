"""
server.py — BokYup self-hosted server (Phase 1).

Runs the SAME FastAPI backend as the desktop, but headless (no pywebview) and bound to a
network interface, gated by an API token. Books live under one server data directory;
clients supply a name, never a server path. Intended to sit behind Tailscale first
(no ports exposed to the internet), TLS/public exposure later.

    BOKYUP_API_TOKEN=<secret>  BOKYUP_DATA_DIR=/data  python -m backend.server

Environment:
    BOKYUP_API_TOKEN   (required) bearer token every client must send. Refuses to start
                       without one, so the server is never accidentally open.
    BOKYUP_DATA_DIR    where the registry + books live (default: ./bokyup-data)
    BOKYUP_HOST        bind address (default 0.0.0.0)
    BOKYUP_PORT        port (default 8756)
    BOKYUP_AUTOLOCK    per-book auto-lock seconds (default 900)
    BOKYUP_CORS        comma-separated allowed origins for app clients (default "*")
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    token = os.environ.get("BOKYUP_API_TOKEN", "").strip()
    if not token:
        sys.stderr.write(
            "ERROR: BOKYUP_API_TOKEN is required — the server refuses to run without a token\n"
            "so it is never accidentally open. Generate one with:\n"
            "  python -c \"import secrets; print(secrets.token_urlsafe(32))\"\n")
        raise SystemExit(2)

    data_dir = Path(os.environ.get("BOKYUP_DATA_DIR", "bokyup-data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    host = os.environ.get("BOKYUP_HOST", "0.0.0.0")
    port = int(os.environ.get("BOKYUP_PORT", "8756"))
    autolock = int(os.environ.get("BOKYUP_AUTOLOCK", "900"))
    cors = [o.strip() for o in os.environ.get("BOKYUP_CORS", "*").split(",") if o.strip()]

    import uvicorn
    from backend.api import create_app

    app = create_app(app_dir=data_dir / "app", books_dir=data_dir, api_token=token,
                     cors_origins=cors, autolock_seconds=autolock)
    sys.stderr.write(f"BokYup server: http://{host}:{port}  (data: {data_dir})\n")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
