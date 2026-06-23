# Bokföring — Swedish bookkeeping application

A legal-grade bookkeeping (bokföring) application for Swedish small businesses.
Built to support **multiple separate legal entities** (e.g. an enskild firma and an
aktiebolag), each as its own **encrypted database**, switched between like browser tabs.

> This file is the source of truth for design decisions. Read it fully before
> making changes. The decisions here were made deliberately with the user; do not
> silently override them.

---

## Architecture (decided)

- **Backend-first, single source of truth in Python.** All legal/crypto logic lives
  in the Python backend so bookkeeping rules are written and verified **once**.
- **API boundary via FastAPI.** UI talks to the backend over HTTP. This makes the
  same logic reusable by future phone clients.
- **UI is a web frontend** (HTML/CSS/JS) — the only UI layer that is truly universal
  across Windows / macOS / Linux / Android / iOS.
- **Desktop today:** backend runs locally, UI served into a native window
  (pywebview) or the browser.
- **Phone later:** same web frontend wrapped (e.g. Capacitor) against the same API.
  Receipt capture via the phone camera through web media APIs.
- **OS-agnostic, pure-pip stack.** No native build steps. (This is why we do NOT use
  SQLCipher — see Encryption.)

## Multi-database / "tabs" model (decided)

- One database = one legal entity's complete books = one tab.
- Entities must never be commingled (Swedish law). Physical DB separation enforces this.
- Each database has its **own** encryption (own DEK, own salt, own passphrase).
- **Per-database passphrase only.** There is NO app-level password. The app opens to a
  list of known databases; each is unlocked individually with its passphrase.
- The app keeps a **registry** of known databases: display name + file path + last opened.
  The registry stores NO passphrases and NO keys.
- New book / open existing / import all behave like new-tab / open-file.

## Encryption (decided + implemented — `core/crypto.py`)

Envelope encryption, pure-Python (`argon2-cffi` + `cryptography`):

    passphrase --Argon2id(salt)--> KEK --wraps--> DEK --AES-256-GCM--> data

- **DEK** generated once per DB, NEVER changes. Encrypts DB fields + photo blobs.
- **KEK** derived from passphrase via Argon2id; only wraps/unwraps the DEK.
- **Change passphrase = re-wrap the same DEK.** No bulk re-encryption (fast, crash-safe).
  PROVEN by tests: same DEK survives passphrase change, old data still readable.
- **Recovery key**: optional second wrapping slot so a forgotten passphrase does not
  destroy a 7-year legal archive. Store offline.
- AES-256-GCM everywhere (authenticated; detects tampering).
- We deliberately do **application-level field/blob encryption** instead of SQLCipher
  to keep the stack pip-installable on every OS including phone wrappers.

## Auto-lock (decided)

- Setting. Default **15 min** inactivity. Editable up to **60 min**. Can be turned off.
- On lock: DEK wiped from memory. On return: re-enter that database's passphrase.

## Legal requirements (this IS a real legal book — build strict)

- **Verifikationsnummer**: unbroken, sequential, per database. Never reused or skipped.
  Auto-assigned. This is the single most important integrity rule.
- **Immutability**: a posted verifikation may NOT be altered or deleted. Corrections are
  done via a **rättelse** (correcting entry that references the original); both remain
  visible. The user's "edit history" UX (approve/decline/cancel) must, on approve,
  create a rättelse — NOT mutate the original row.
- **Reference data** (categories, customer details, company/supplier defaults) MAY be
  edited freely. But edits must NOT rewrite already-issued invoices/verifikationer.
- **Snapshot-on-invoice**: when income/invoice is created, snapshot the customer details
  ONTO the record, independent of the live customer row (an invoice is frozen at issue).
- **Period locking**: once a momsdeklaration for a period is filed, lock the period so
  nothing back-dates into it.
- **Retention 7 years.** Receipts: store a flag for original format (paper vs digital);
  a photo of a digital receipt is not a valid substitute for the digital original.
- **Method**: kontantmetod (book when money moves) fits the pending→paid flow; at
  year-end even kontantmetod must book unpaid invoices.

## Moms (VAT) model

- Per transaction store THREE figures: ex-moms (beskattningsunderlag), moms amount,
  inc-moms total. Split by rate because the momsdeklaration has separate boxes.
- Purchases = ingående moms (deductible). Sales = utgående moms (owed). Model the sign.
- Moms states (not just on/off): **25% / 12% / 6% / 0% / momsfri / ej avdragsgill**.
- Per company/supplier: editable default moms rate (defaults to 25% before editing).

## RUT (private customers only)

- Lifecycle state machine: **pending → customer paid → Skatteverket paid**.
  RUT fields/buttons only appear for income flagged with a RUT amount.
- Track the date Skatteverket pays separately from the customer payment date
  (they land in different months in the books).
- RUT cap is per customer per year and shared with ROT — make the cap a CONFIG value,
  not hardcoded (rules change). Warn as a customer approaches the cap.
- RUT requires the customer's personnummer (so storing it is necessary & legitimate).

## Customers

- Type flag: **private** or **business**.
  - Private: first name, last name, personnummer, address, email, phone.
  - Business: company name, organisationsnummer, optional contact person, address,
    email, phone, VAT-nummer (if abroad/EU).
- Stable internal **kundnummer** (survives name/address edits).
- Personnummer: validate format + Luhn checksum on entry. Sensitive data (GDPR) —
  another reason the DB is encrypted at rest and backups are encrypted.
- RUT only applies to private customers.

## Suppliers/companies (expense counterparties)

- e.g. Inet, Webhallen, Kraüta, Electrokit. Minimum: name + default moms rate.
- OPEN QUESTION: whether to also carry org.nr/address for suppliers (user to decide).

## Categories ↔ BAS-konto

- Each expense/income categorized; can create new categories at entry time.
- Each category carries a BAS-konto number (user-entered).
- BAS-konto numbers feed the reports (momsdeklaration, then simplified deklaration).
- Categories editable after the fact; editing old transactions' category follows the
  approve(→rättelse)/decline/cancel rule above.

## Export / import (decided)

- Per-database. Export = a single bundle (`.buyn`, really a zip) containing:
  encrypted DB + encrypted photos + the wrapped-DEK envelope + a manifest
  (app version, schema version, export timestamp, checksum).
- **Option A** (default): the wrapped DEK travels in the bundle; unlock on the new
  device with the SAME passphrase. (Option B — separate export passphrase — later.)
- The export bundle doubles as the **encrypted backup** mechanism.
- Import semantics: **full restore / replace**, never merge (merging breaks the
  verifikationsnummer sequence — explicitly out of scope). Single authoritative device.
- Import must: verify checksum, check schema version (migrate or refuse), and
  auto-backup current state before overwriting.

## Reports (build order)

1. **Momsdeklaration helper** (most frequent — quarterly/monthly). Falls almost
   directly out of the BAS-konto + moms-rate data.
2. **Simplified deklaration** building block (NE-blankett direction).
3. **SIE export** (Swedish standard; every accounting program + revisor accepts it).
   Design data to be SIE-compatible from the start even before the exporter exists.

---

## Build status

- [x] **Layer 1 — Crypto core** (`core/crypto.py`) + tests (`tests/test_crypto.py`). DONE, all tests pass.
- [x] **Layer 2 — Database manager** (`db/manager.py`) + tests (`tests/test_manager.py`).
      Registry of books, create/open/lock sessions, recovery-key unlock, per-DB DEK in
      memory only. DONE, all tests pass. (Import/export bundle deferred to Layer 5.)
- [x] **Layer 3 — Schema** (`models/schema.py`) + tests (`tests/test_schema.py`).
      All tables (verifikation/posting, transaktion/moms_line, customer, supplier,
      category↔BAS, rut_claim, period_lock, rättelse via `verifikation.rattelse_of`),
      DB-level immutability triggers, personnummer Luhn + money (öre) helpers.
      DONE, all tests pass. NOTE: money stored as integer ören everywhere; the
      RUT/ROT cap is editable config (default 75 000 kr — VERIFY vs Skatteverket).
- [x] **Layer 4 — Core operations** (`db/operations.py`) + tests (`tests/test_operations.py`).
      Reference CRUD, kontantmetod pending→paid booking with balanced double-entry +
      unbroken verifikationsnummer, RUT state machine (2 verifikationer), rättelse via
      mirror postings, snapshot-on-invoice, period-lock enforcement. DONE, all tests pass.
      NOTE: all system BAS-konton are config (account_bank/ingaende_moms/utgaende_moms_*
      /rut_fordran) — verify `account_rut_fordran` (default 1513) with a revisor.
- [x] **Layer 5 — Export/import** (`db/bundle.py`) + tests (`tests/test_bundle.py`).
      `.buyn` zip bundle (manifest + db snapshot via backup API + wrapped-DEK envelope
      + optional `<db>.photos/`), SHA-256 per-file checksums, schema-version gate,
      full-restore/replace with auto-backup-before-overwrite. Doubles as encrypted
      backup. Wired into DatabaseManager.export_book/import_book. DONE, all tests pass.
- [x] **Layer 6 — Reports** (`reports/vat.py`, `reports/result.py`, `reports/sie.py`)
      + tests (`tests/test_reports.py`). Momsdeklaration (boxes 05/10/11/12/48/49),
      result/NE building block (income/expense/result by category), SIE type-4 export
      (#KONTO + #VER/#TRANS, cp437/PC8). All filter on verifikation date of posted
      entries (kontantmetod). DONE, all tests pass. NOTE: rättelse not yet netted in
      the moms_line aggregation (period locking guards the filed-then-correct flow);
      SIE omits #IB/#UB/#RES until year-end closing exists.
- [x] **Layer 7 — FastAPI API layer** (`api/app.py`, `api/schemas.py`) + tests
      (`tests/test_api.py`). create_app factory over DatabaseManager; per-DB unlock/
      lock endpoints, auto-lock (on-access check + background sweeper, default 15 min),
      reference + bookkeeping + reports + export/import routes, domain→HTTP error
      mapping (401/404/409/423). DONE, all tests pass. NOTE: localhost single-user
      backend — db/bundle paths in requests are trusted; do not expose on a network.
- [x] **Layer 8 — Web frontend** (`api/static/{index.html,styles.css,app.js}`,
      `desktop.py`) + tests (`tests/test_web.py`). Vanilla-JS tabbed SPA (multi-book
      tabs, unlock/lock, record income/expense, pending→paid, customers/suppliers/
      categories, momsdeklaration + SIE download), served at /app by the same FastAPI
      server; pywebview desktop launcher (`python -m backend.desktop`). DONE; JS
      `node --check`'d, server wiring tested, full stack live-smoke-tested.
- [ ] Later — phone wrappers (Android/iOS) against the same API; camera receipt capture.

## Working agreement

- Deliver in **working increments**. Each legal-critical layer must be testable and
  verified before the next sits on top of it.
- Keep all legal rules in the backend. Never reimplement them per platform.
- When in doubt about a Swedish tax/bookkeeping rule, verify it — do not guess on a
  legal book.

## Run / test

    python -m pip install -r requirements.txt
    python -m pytest            # or: python tests/test_crypto.py
