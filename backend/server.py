"""
server.py — BokYup self-hosted server (Phase 1).

Runs the SAME FastAPI backend as the desktop, but headless (no pywebview) and bound to a
network interface, gated by an API token. Books live under one server data directory;
clients supply a name, never a server path. Intended to sit behind Tailscale first
(no ports exposed to the internet), TLS/public exposure later.

    BOKYUP_API_TOKEN=<secret>  BOKYUP_DATA_DIR=/data  python -m backend.server

Environment:
    BOKYUP_API_TOKEN   bearer token every client must send. Refuses to start without a
                       token (from here or the file below), so it is never accidentally open.
    BOKYUP_API_TOKEN_FILE  (alternative) path to a file containing the token — for secret
                       managers (agenix/sops, systemd LoadCredential) so it stays off the env.
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


def read_token(env=os.environ) -> str:
    """The API token, from BOKYUP_API_TOKEN or (secret-manager friendly) the file named
    by BOKYUP_API_TOKEN_FILE — so the secret never has to live in the environment or the
    Nix store. Returns "" if neither is set (the server then refuses to start)."""
    token = env.get("BOKYUP_API_TOKEN", "").strip()
    if token:
        return token
    token_file = env.get("BOKYUP_API_TOKEN_FILE", "").strip()
    if token_file and Path(token_file).exists():
        return Path(token_file).read_text(encoding="utf-8").strip()
    return ""


def main() -> None:
    token = read_token()
    if not token:
        sys.stderr.write(
            "ERROR: an API token is required — the server refuses to run without one\n"
            "so it is never accidentally open. Set BOKYUP_API_TOKEN, or point\n"
            "BOKYUP_API_TOKEN_FILE at a file containing the token. Generate one with:\n"
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
