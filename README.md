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
