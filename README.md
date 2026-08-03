# BokYup

Legal-grade Swedish bookkeeping for multiple separate entities, each an encrypted
database you switch between like browser tabs. Pure-Python, OS-agnostic, built so the
same backend serves a desktop app today and phone apps later.

See **CLAUDE.md** for the full architecture and decision record.

## Status
All eight planned layers are implemented and tested (crypto → database manager →
schema → operations → export/import → reports → FastAPI → web frontend). See the
build-status checklist in CLAUDE.md.

## Setup
    python -m pip install -r requirements.txt
    python -m pytest

## Run

**Desktop (native window via pywebview):**

    python -m backend.desktop

**In a browser (run the API + UI server yourself):**

    python -m backend.api.app          # serves on http://127.0.0.1:8000
    # then open http://127.0.0.1:8000/app/

By default the registry of books lives in `~/.buyn` (override with the
`BUYN_DATA_DIR` environment variable). Each book is its own encrypted `.db` +
`.db.key`; there is no app-level password — every book is unlocked individually
with its own passphrase, and idle books auto-lock (default 15 min).

## Server (self-hosted, NixOS)

BokYup can run as a self-hosted **authority server** that holds the encrypted books, so
several devices share one set of books while the server stays the sole writer (this is what
keeps the verifikationsnummer sequence unbroken). It is the **same backend** as the app —
never a forked copy — packaged as a flake output and run **isolated in a Docker container**
built by Nix. **NixOS with flakes is the supported install path** (the only one we currently
focus on); a plain `docker compose` variant is noted in `docs/server-nixos.md`.

Point your system flake at this repo and import the module:

```nix
# flake.nix inputs
bokyup.url = "github:gurglamesh/bokyup";
bokyup.inputs.nixpkgs.follows = "nixpkgs";

# in your host's modules
imports = [ inputs.bokyup.nixosModules.bokyup-server ];
services.bokyup-server.enable = true;      # Docker + a 0600 API token are set up for you
```

Then bring Tailscale to reach it (the server binds to `127.0.0.1` only):

```bash
sudo nixos-rebuild switch --flake .#<yourhost>
sudo cat /var/lib/bokyup-token        # the API token the app logs in with
sudo tailscale serve --bg 8756        # publish over your tailnet (HTTPS, no open ports)
```

In the app: **Anslut till server** → the `https://<host>.<tailnet>.ts.net` URL + that token.

BokYup stays **runtime-agnostic** — `services.bokyup-server.runtime` picks how it runs:
`"docker"` (default; a Nix-built OCI image, and Docker is installed/enabled for you),
`"podman"`, or `"native"` (the server directly as a systemd service, no container). Other
niceties: a declaratively generated API token kept **out of the Nix store** (`generateToken`,
or point `tokenFile` at an agenix/sops secret), books under `/var/lib/bokyup`, and an optional
nightly consistent backup (`backup.enable`). **Full walkthrough:
[`docs/server-flake.md`](docs/server-flake.md).**

## What works now
- **Encryption** — per-database envelope encryption (Argon2id KEK wrapping a stable
  DEK), passphrase change with no data re-encryption, optional offline recovery key,
  authenticated (tamper-detecting) field/blob encryption.
- **Books** — multi-database registry ("tabs"), per-database unlock/lock, DEK in
  memory only.
- **Bookkeeping** — kontantmetod pending→paid booking with balanced double-entry and
  an unbroken verifikationsnummer sequence, DB-enforced immutability of posted
  entries, rättelse corrections, snapshot-on-invoice, RUT state machine, period
  locking, and year-end accrual of unpaid invoices (bokslut).
- **Reports** — momsdeklaration helper, result/NE building block, and SIE (type 4)
  export.
- **Interfaces** — a FastAPI HTTP layer and a tabbed web UI (served at `/app`).

## Layout
    backend/core/      crypto core
    backend/db/        database manager, operations, .buyn export/import bundle
    backend/models/    SQLite schema + validation/money helpers
    backend/reports/   momsdeklaration, result, SIE
    backend/api/       FastAPI app + Pydantic schemas + static web UI
    backend/desktop.py pywebview desktop launcher
    tests/             pytest suite (+ tests/test_crypto.py runs standalone)
