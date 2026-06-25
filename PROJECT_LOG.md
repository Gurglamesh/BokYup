# BokYup — project log & handoff

A running, human-readable log so this project can be continued from any machine with
a fresh Claude Code session. **`CLAUDE.md` is the authoritative design/decision record
and is loaded automatically by Claude Code — read it first.** This file is the
"where are we / how do I continue" companion.

---

## What this project is

**BokYup** — a legal-grade Swedish bookkeeping (bokföring) app for small businesses.
Supports **multiple separate legal entities**, each as its own **encrypted database**,
switched between like browser tabs. Pure-Python, OS-agnostic (no native build steps),
built so the same backend serves a desktop app today and phone apps later.

- Backend-first: all legal/crypto rules live in Python, written once.
- API boundary via FastAPI; UI is a vanilla-JS web frontend served by the same server.
- Desktop today via pywebview; phone later via the same API.
- Per-database passphrase only (no app-level password); DEK in memory only; auto-lock.

See `CLAUDE.md` for the full architecture and the deliberate decisions behind it.

---

## Status — all 8 planned layers complete

| Layer | Module | What |
|------|--------|------|
| 1 Crypto | `backend/core/crypto.py` | Argon2id KEK → wrapped DEK → AES-256-GCM; recovery key |
| 2 Manager | `backend/db/manager.py` | encrypted multi-book registry ("tabs"), unlock/lock |
| 3 Schema | `backend/models/schema.py` | legal tables, immutability triggers, öre money, Luhn |
| 4 Operations | `backend/db/operations.py` | kontantmetod booking, verifikationsnr, RUT, rättelse, year-end accrual |
| 5 Export/import | `backend/db/bundle.py` | `.buyn` bundle + encrypted backup |
| 6 Reports | `backend/reports/{vat,result,sie}.py` | momsdeklaration, result/NE, SIE type-4 |
| 7 API | `backend/api/{app,schemas}.py` | FastAPI over the backend, auto-lock, error mapping |
| 8 Web UI | `backend/api/static/*`, `backend/desktop.py` | tabbed SPA + pywebview launcher |

**Tests:** `python -m pytest` → 163 passing (plus `python tests/test_crypto.py` runs
standalone). The full stack has also been live-smoke-tested through the browser UI.

Not started: phone wrappers (Android/iOS) against the same API; camera receipt capture.

---

## Run it

    python -m pip install -r requirements.txt
    python -m pytest                     # verify
    python -m backend.desktop            # desktop (native window via pywebview)
    # or browser:
    python -m backend.api.app            # http://127.0.0.1:8000  → open /app/

Books/registry live in `~/.buyn` by default (override with `BUYN_DATA_DIR`).

---

## Continue from another machine

1. `git clone https://github.com/Gurglamesh/BokYup.git`  (folder will be `BokYup`)
2. `cd BokYup && python -m pip install -r requirements.txt && python -m pytest`
3. Open the folder in Claude Code and start a new session. It will read `CLAUDE.md`
   automatically; point it at this file (`PROJECT_LOG.md`) for the progress summary.
4. The previous chat history does NOT transfer (sessions are local, not in git) — the
   docs + tests are the context a fresh session needs.

**Git workflow:** all work is local; GitHub updates only on `git push`. Before working
on a machine, `git pull`; after a chunk of work, commit and push. This is currently a
single-clone setup — keep one machine as the source of truth at a time to avoid
divergence. (As of this log: auto-push after each commit is the agreed default.)

---

## Key conventions (so a fresh session doesn't relearn them the hard way)

- **Money is integer ören everywhere** (`*_ore` columns/args). Never floats. Convert at
  the edges with `schema.kronor_to_ore` / `ore_to_kronor`.
- **kontantmetod**: book when money moves. All reports filter on the *verifikation date*
  (payment date) of *posted* entries.
- **Immutability is enforced in the DB** (triggers): a posted verifikation/posting can't
  be altered or deleted — corrections go through `reverse_verifikation` (rättelse).
- **System BAS-konton are config** (in the `config` table, see `schema._DEFAULT_CONFIG`),
  not hardcoded — a revisor can remap them.
- Verified 2026-06 against Skatteverket/BAS: combined ROT+RUT cap 75 000 kr/person/year;
  BAS 1513 for the RUT/ROT receivable. Still config.

---

## Suggested next steps (nothing is blocking)

- **Revisor sign-off** on the config account mappings before real filing.
- **Phone wrapper** (Capacitor) against the same API + camera receipt capture.
- Optional: richer transaction views/filtering; hide synthetic accrual/rättelse
  transaktioner from the default list (they carry `note` = periodisering/återföring/rättelse).

### Done since the 8-layer build (UI surfacing, 2026-06)

The previously API-only features now have web-UI front ends:

- **RUT** section: lists claims with their lifecycle state; "Bokför SKV-utbetalning"
  button on `customer_paid` claims (new `GET /books/{id}/rut-claims` endpoint backs it).
  The record-income flow also surfaces the RUT/ROT cap `near_cap`/`over_cap` warning.
- **Verifikat** section: lists the legal ledger; "Rätta" button posts a rättelse
  (reverse) on posted, non-rättelse verifikationer.
- **Bokslut** section: lock a period, and book year-end accruals (vändning).
- **Reports**: SIE export now takes company name, org.nr and an optional fiscal year
  (so #IB/#UB/#RES balances are emitted).

### Receipt capture + encrypted storage (2026-06)

- **Multi-rate expense entry**: the Bokför form now has a lines editor (add/remove rows),
  so one receipt can carry 6 % + 12 % + 25 % moms. Backend `record_expense` already took
  a `lines` list — this is the manual counterpart to what OCR will later prefill.
- **Receipt photos**: import a file or take a photo (file `capture` opens the phone
  camera; a live `getUserMedia` "Ta foto" button covers desktop webcams). Stored
  AES-256-GCM-encrypted (book DEK) as files in `<db>.photos/`, indexed by a new `receipt`
  table; deletable only while the purchase is still pending. View via 📎 in Transaktioner.
- **Schema migration**: `SCHEMA_VERSION` 1→2; `schema.migrate()` runs on book open so
  existing books gain the `receipt` table automatically.
- **OCR is deferred** (decision: clashes with pure-pip/offline/privacy). A clean seam is
  planned — `backend/ocr/` + `POST …/receipts/ocr-suggest` returning the same
  `{lines,total}` the lines editor already edits. Engine choice (Claude vision vs.
  Tesseract vs. both) still open; revisit when ready.
